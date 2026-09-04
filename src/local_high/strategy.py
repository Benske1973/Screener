from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from local_high.config import Config
from local_high.indicators import Candle, atr, ema, relative_volume, window_extreme
from local_high.kucoin import Ticker
from local_high.scoring import confluence_score
from local_high.targets import TradePlan, build_trade_plan

# state machine vertices
IDLE = "IDLE"
WATCH = "WATCH"
BREAKOUT = "BREAKOUT"
PULLBACK = "PULLBACK"
REBREAK = "REBREAK"
FAKEOUT = "FAKEOUT"

_ACTIVE_STATES = frozenset({WATCH, BREAKOUT, PULLBACK, REBREAK})
_TERMINAL_STATES = frozenset({REBREAK, FAKEOUT})


@dataclass(frozen=True, slots=True)
class NlhResult:
    is_high: bool
    fresh: bool
    matched_lookbacks: tuple[int, ...]
    nearest_level: float        # resistance just cleared (smallest matched lookback)
    structural_level: float     # resistance of the largest matched lookback (0.0 if none)
    near_high: bool
    distance_pct: float          # trigger vs nearest reference level, signed


@dataclass(slots=True)
class SymbolState:
    symbol: str
    state: str = IDLE
    last_candle_ms: int = 0
    entered_candle_ms: int = 0
    breakout_candle_ms: int = 0
    breakout_level: float = 0.0
    breakout_high: float = 0.0
    highest_since_breakout: float = 0.0
    pullback_low: float = 0.0
    seeded: bool = False
    alerts: dict[str, int] = field(default_factory=dict)
    # cooldown tracker for chart-pattern breakout alerts, keyed "<pattern>:<status>"
    # - separate from `alerts` above, which is only for NLH state-machine events.
    pattern_alerts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SymbolState:
        known = cls.__dataclass_fields__
        data = {k: v for k, v in raw.items() if k in known}
        data.setdefault("symbol", raw.get("symbol", "?"))
        state = cls(**data)
        state.alerts = {str(k): int(v) for k, v in (state.alerts or {}).items()}
        state.pattern_alerts = {str(k): int(v) for k, v in (state.pattern_alerts or {}).items()}
        return state


@dataclass(frozen=True, slots=True)
class Event:
    symbol: str
    kind: str
    candle_ms: int
    price: float
    level: float
    stop: float
    matched_lookbacks: tuple[int, ...]
    rvol: float
    change_24h_pct: float
    distance_pct: float
    atr: float
    score: float
    seeded: bool
    note: str
    plan: TradePlan | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    symbol: str
    state: SymbolState
    events: list[Event]
    active: bool
    price: float
    distance_pct: float
    rvol: float
    change_24h_pct: float
    trend_ok: bool
    atr: float
    score: float
    matched_lookbacks: tuple[int, ...]
    plan: TradePlan | None = None


# --------------------------------------------------------------------------- #
# New Local High detection
# --------------------------------------------------------------------------- #
def detect_nlh(candles: list[Candle], cfg: Config) -> NlhResult:
    n = len(candles)
    last = candles[-1]
    trigger = last.close if cfg.breakout_source == "close" else last.high
    eps = cfg.min_breakout_pct / 100.0

    prior_by: dict[int, float] = {}
    prev_prior_by: dict[int, float] = {}
    for lb in sorted(set(cfg.lookbacks)):
        if n >= lb + 1:
            prior_by[lb] = window_extreme(candles, n - 1, lb, cfg.prior_high_source)
        if n >= lb + 2:
            prev_prior_by[lb] = window_extreme(candles, n - 2, lb, cfg.prior_high_source)

    matched = tuple(lb for lb, ph in prior_by.items() if trigger > ph * (1.0 + eps))

    fresh = False
    if matched:
        prev_trigger = (
            candles[-2].close if cfg.breakout_source == "close" else candles[-2].high
        )
        for lb in matched:
            prev_ph = prev_prior_by.get(lb)
            if prev_ph is None or not (prev_trigger > prev_ph * (1.0 + eps)):
                fresh = True
                break

    if matched:
        nearest_level = prior_by[min(matched)]
        structural_level = prior_by[max(matched)]
        distance_pct = (trigger / nearest_level - 1.0) * 100.0
        near_high = False
    else:
        structural_level = 0.0
        ref = prior_by[min(prior_by)] if prior_by else trigger
        nearest_level = ref
        distance_pct = (trigger / ref - 1.0) * 100.0 if ref > 0 else 0.0
        near_high = ref > 0 and trigger >= ref * (1.0 - cfg.near_high_pct / 100.0)

    return NlhResult(
        is_high=bool(matched),
        fresh=fresh,
        matched_lookbacks=matched,
        nearest_level=nearest_level,
        structural_level=structural_level,
        near_high=near_high,
        distance_pct=distance_pct,
    )


# --------------------------------------------------------------------------- #
# per-symbol evaluation
# --------------------------------------------------------------------------- #
def _candle_age(from_ms: int, to_ms: int, interval_ms: int) -> int:
    if interval_ms <= 0 or from_ms <= 0:
        return 0
    return max(0, round((to_ms - from_ms) / interval_ms))


def _stop_from_level(level: float, cfg: Config) -> float:
    return level * (1.0 - cfg.stop_buffer_pct / 100.0)


def evaluate(
    symbol: str,
    candles: list[Candle],
    ticker: Ticker | None,
    cfg: Config,
    prior: SymbolState | None,
    *,
    min_candles: int | None = None,
) -> Evaluation:
    """Advance the per-symbol state machine by the newest closed candle.

    ``candles`` must be closed candles in ascending time order.
    """
    need = min_candles if min_candles is not None else cfg.max_lookback + 3
    interval_ms = cfg.interval_seconds * 1000
    change_24h_pct = (ticker.change_rate_24h * 100.0) if ticker else 0.0

    base = SymbolState(symbol=symbol) if prior is None else _copy_state(prior)

    if len(candles) < need:
        base.state = IDLE
        return Evaluation(
            symbol, base, [], False, 0.0, 0.0, 0.0, change_24h_pct, False, 0.0, 0.0, ()
        )

    last = candles[-1]
    new_candle = prior is None or last.open_time_ms != prior.last_candle_ms
    base.last_candle_ms = last.open_time_ms

    nlh = detect_nlh(candles, cfg)
    closes = [c.close for c in candles]
    rvol = relative_volume(candles, cfg.rvol_lookback)
    trend_ema = ema(closes, cfg.trend_ema_len)
    trend_ok = last.close > trend_ema if trend_ema > 0 else False
    atr_value = atr(candles, cfg.atr_len)
    score = confluence_score(
        nlh.distance_pct, rvol, change_24h_pct, trend_ok, cfg.score_weights
    )

    metrics = dict(
        price=last.close,
        distance_pct=nlh.distance_pct,
        rvol=rvol,
        change_24h_pct=change_24h_pct,
        trend_ok=trend_ok,
        atr=atr_value,
        score=score,
        matched_lookbacks=nlh.matched_lookbacks,
    )

    if not new_candle:
        plan = _plan_for_state(base, candles, nlh, atr_value, last, cfg)
        return Evaluation(
            symbol, base, [], base.state in _ACTIVE_STATES, **metrics, plan=plan
        )

    events: list[Event] = []
    current = base.state
    first_sight = prior is None

    def mk_event(kind: str, *, level: float, stop: float, note: str = "") -> Event:
        return Event(
            symbol=symbol,
            kind=kind,
            candle_ms=last.open_time_ms,
            price=last.close,
            level=level,
            stop=stop,
            matched_lookbacks=nlh.matched_lookbacks,
            rvol=rvol,
            change_24h_pct=change_24h_pct,
            distance_pct=nlh.distance_pct,
            atr=atr_value,
            score=score,
            seeded=base.seeded,
            note=note,
        )

    # terminal states linger a few candles, then fall through to a fresh evaluation
    if current in _TERMINAL_STATES:
        age = _candle_age(base.entered_candle_ms, last.open_time_ms, interval_ms)
        if age < cfg.rebreak_reset_candles:
            plan = _plan_for_state(base, candles, nlh, atr_value, last, cfg)
            return Evaluation(
                symbol, base, [], current == REBREAK, **metrics, plan=plan
            )
        current = IDLE
        base.state = IDLE

    if current in (IDLE, WATCH):
        can_break = nlh.is_high and (
            nlh.fresh or not cfg.require_fresh or first_sight
        )
        if can_break:
            seeded = first_sight and cfg.require_fresh and not nlh.fresh
            base.state = BREAKOUT
            base.entered_candle_ms = last.open_time_ms
            base.breakout_candle_ms = last.open_time_ms
            base.breakout_level = nlh.nearest_level
            base.breakout_high = last.high
            base.highest_since_breakout = last.high
            base.pullback_low = last.low
            base.seeded = seeded
            note = "setup already in progress at first sight" if seeded else ""
            events.append(
                mk_event(
                    BREAKOUT,
                    level=base.breakout_level,
                    stop=_stop_from_level(base.breakout_level, cfg),
                    note=note,
                )
            )
        elif nlh.near_high:
            if base.state != WATCH:
                base.entered_candle_ms = last.open_time_ms
            base.state = WATCH
            events.append(
                mk_event(WATCH, level=nlh.nearest_level, stop=0.0)
            )
        else:
            base.state = IDLE
            base.seeded = False

    elif current == BREAKOUT:
        high_water = base.highest_since_breakout
        base.highest_since_breakout = max(high_water, last.high)
        age = _candle_age(base.breakout_candle_ms, last.open_time_ms, interval_ms)
        invalid_level = base.breakout_level * (1.0 - cfg.invalidate_pct / 100.0)
        retest_ceiling = base.breakout_level * (1.0 + cfg.pullback_retest_pct / 100.0)

        if last.close < invalid_level:
            base.state = FAKEOUT
            base.entered_candle_ms = last.open_time_ms
            events.append(
                mk_event(
                    FAKEOUT,
                    level=base.breakout_level,
                    stop=0.0,
                    note="close below breakout level",
                )
            )
        elif not cfg.track_rebreak:
            if age >= cfg.pullback_max_candles:
                base.state = IDLE
        elif (
            last.low <= retest_ceiling
            and last.close >= base.breakout_level
            and last.close < high_water
        ):
            base.state = PULLBACK
            base.entered_candle_ms = last.open_time_ms
            base.pullback_low = last.low
            events.append(
                mk_event(
                    PULLBACK,
                    level=base.breakout_level,
                    stop=_stop_from_level(base.breakout_level, cfg),
                    note="retesting broken resistance as support",
                )
            )
        elif age >= cfg.pullback_max_candles:
            base.state = IDLE

    elif current == PULLBACK:
        high_water = base.highest_since_breakout
        base.pullback_low = min(base.pullback_low, last.low)
        base.highest_since_breakout = max(high_water, last.high)
        age = _candle_age(base.breakout_candle_ms, last.open_time_ms, interval_ms)
        invalid_level = base.breakout_level * (1.0 - cfg.invalidate_pct / 100.0)

        if last.close < invalid_level:
            base.state = FAKEOUT
            base.entered_candle_ms = last.open_time_ms
            events.append(
                mk_event(FAKEOUT, level=base.breakout_level, stop=0.0, note="pullback failed")
            )
        elif last.close > high_water:
            base.state = REBREAK
            base.entered_candle_ms = last.open_time_ms
            events.append(
                mk_event(
                    REBREAK,
                    level=high_water,
                    stop=base.pullback_low,
                    note="new high after pullback - add remaining size, stop to pullback low",
                )
            )
        elif age >= cfg.pullback_max_candles + cfg.rebreak_reset_candles:
            base.state = IDLE

    plan = _plan_for_state(base, candles, nlh, atr_value, last, cfg)
    if plan is not None:
        events = [
            replace(e, plan=plan) if e.kind in (BREAKOUT, REBREAK) else e for e in events
        ]

    return Evaluation(
        symbol, base, events, base.state in _ACTIVE_STATES, **metrics, plan=plan
    )


def _plan_for_state(
    base: SymbolState,
    candles: list[Candle],
    nlh: NlhResult,
    atr_value: float,
    last: Candle,
    cfg: Config,
) -> TradePlan | None:
    """Deterministic entry / stop / target plan for a live BREAKOUT or REBREAK setup."""
    if base.state == BREAKOUT:
        lookbacks = nlh.matched_lookbacks or tuple(sorted(cfg.lookbacks))
        window = candles[-(min(lookbacks) + 1) : -1] or candles[:-1]
        return build_trade_plan(
            entry=last.close,
            stop=base.breakout_level * (1.0 - cfg.stop_buffer_pct / 100.0),
            projection_level=base.breakout_level,
            base_low=min(c.low for c in window),
            reference_level=base.breakout_level,
            candles=candles,
            atr_value=atr_value,
            cfg=cfg,
        )
    if base.state == REBREAK:
        return build_trade_plan(
            entry=last.close,
            stop=base.pullback_low,
            projection_level=base.highest_since_breakout,
            base_low=base.pullback_low,
            reference_level=base.breakout_level,
            candles=candles,
            atr_value=atr_value,
            cfg=cfg,
        )
    return None


def _copy_state(state: SymbolState) -> SymbolState:
    clone = SymbolState(**{k: getattr(state, k) for k in SymbolState.__dataclass_fields__})
    clone.alerts = dict(state.alerts)
    clone.pattern_alerts = dict(state.pattern_alerts)
    return clone
