from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

VALID_EVENT_KINDS = ("WATCH", "BREAKOUT", "PULLBACK", "REBREAK", "FAKEOUT")

# KuCoin candle `type` -> seconds
INTERVAL_SECONDS: dict[str, int] = {
    "1min": 60,
    "3min": 180,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1hour": 3600,
    "2hour": 7200,
    "4hour": 14400,
    "6hour": 21600,
    "8hour": 28800,
    "12hour": 43200,
    "1day": 86400,
    "1week": 604800,
}


class ConfigError(ValueError):
    """Raised when config.yaml is missing required values or is inconsistent."""


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    rvol: float = 1.0
    change_24h: float = 0.5
    breakout_strength: float = 1.0
    trend: float = 0.5

    def total(self) -> float:
        return self.rvol + self.change_24h + self.breakout_strength + self.trend


@dataclass(frozen=True, slots=True)
class PatternScoreWeights:
    """Weights for pattern_confidence() - how 'solid' a pattern breakout looks."""

    fit: float = 1.0        # goodness of fit (r2) of the two trendlines
    rvol: float = 1.0        # volume confirming the breakout
    trend: float = 0.5        # breakout in the direction of the broader trend
    strength: float = 1.0      # how far price cleared the line, relative to pattern height

    def total(self) -> float:
        return self.fit + self.rvol + self.trend + self.strength


@dataclass(frozen=True, slots=True)
class Config:
    # data source
    kucoin_base_url: str = "https://api.kucoin.com"
    timeframe: str = "4hour"
    candle_history: int = 720
    request_timeout_seconds: float = 15.0
    max_concurrent_requests: int = 6
    cycle_seconds: int = 300
    universe_refresh_cycles: int = 12

    # universe gates
    min_24h_quote_volume: float = 500_000.0
    min_price: float = 1e-8
    exclude_stablecoins: bool = True
    exclude_leveraged: bool = True
    symbol_allowlist: tuple[str, ...] = ()
    symbol_denylist: tuple[str, ...] = ()

    # new local high
    lookbacks: tuple[int, ...] = (30, 90)
    breakout_source: str = "close"
    prior_high_source: str = "high"
    min_breakout_pct: float = 0.05
    require_fresh: bool = True
    near_high_pct: float = 1.5
    max_breakout_distance_pct: float = 5.0

    # score
    rvol_lookback: int = 20
    trend_ema_len: int = 50
    atr_len: int = 14
    min_rvol: float = 1.0
    score_weights: ScoreWeights = field(default_factory=ScoreWeights)

    # state machine
    track_rebreak: bool = True
    pullback_retest_pct: float = 0.5
    pullback_max_candles: int = 12
    invalidate_pct: float = 1.0
    rebreak_reset_candles: int = 6

    # risk hints
    stop_buffer_pct: float = 0.5

    # trade plan / targets
    target_r_multiples: tuple[float, ...] = (2.0, 3.0, 5.0)
    target_measured_move: bool = True
    target_structure: bool = True
    structure_swing_window: int = 3
    structure_max_targets: int = 3
    extended_entry_warn_pct: float = 8.0
    breakeven_after_target: int = 1
    max_targets: int = 5

    # alerts
    alert_on: tuple[str, ...] = ("BREAKOUT", "REBREAK", "FAKEOUT")
    alert_on_seed: bool = False
    alert_cooldown_seconds: int = 21_600
    telegram_enabled: bool = True

    # preset screeners (--screen / --list-screens)
    screen_lookback: int = 20
    screen_trend_fast_len: int = 12
    screen_trend_slow_len: int = 50
    screen_rsi_len: int = 14
    screen_rsi_oversold: float = 30.0
    screen_rsi_oversold_mild: float = 45.0
    screen_pullback_lookback: int = 5
    screen_pullback_min_pct: float = 1.0
    screen_bb_len: int = 20
    screen_bb_stdev: float = 2.0
    screen_macd_fast: int = 12
    screen_macd_slow: int = 26
    screen_macd_signal: int = 9
    screen_min_rvol: float = 1.5

    # chart-pattern detection (--patterns)
    pattern_lookback_candles: int = 80
    pattern_swing_window: int = 3
    pattern_min_swings: int = 2
    pattern_min_r2: float = 0.5
    pattern_flat_slope_pct: float = 0.05
    pattern_parallel_tol_pct: float = 0.08
    pattern_breakout_pct: float = 0.5
    hs_shoulder_tolerance_pct: float = 12.0
    hs_min_head_prominence_pct: float = 3.0
    # head-and-shoulders bases can take weeks to form - a separate, much
    # longer window than pattern_lookback_candles, with a coarser swing
    # filter so only prominent shoulders/heads register, not minor noise.
    hs_lookback_candles: int = 600
    hs_swing_window: int = 8

    # chart-pattern breakout alerts (Telegram, via local-high-scanner)
    pattern_alerts_enabled: bool = True
    pattern_alert_directions: tuple[str, ...] = ("breakout_up",)
    pattern_alert_min_score: float = 70.0
    pattern_alert_cooldown_seconds: int = 21_600
    pattern_confidence_weights: PatternScoreWeights = field(default_factory=PatternScoreWeights)

    # heartbeat (Telegram, local-high-scanner only)
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: int = 86_400

    # alert noise control (Telegram, local-high-scanner only)
    # Bundles every alert a cycle produces (NLH + pattern breakouts) into one
    # Telegram message instead of one message per alert, capped and sorted by
    # score - so a busy market sends you one digest, not a wall of pings.
    alert_digest_mode: bool = True
    alert_digest_max_items: int = 8

    # storage
    state_path: str = "data/state.json"
    events_csv: str = "data/events.csv"
    log_path: str = "logs/scanner.jsonl"

    # web dashboard (local-high-web)
    web_refresh_seconds: int = 60

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS[self.timeframe]

    @property
    def max_lookback(self) -> int:
        return max(self.lookbacks)

    def validate(self) -> None:
        if self.timeframe not in INTERVAL_SECONDS:
            raise ConfigError(
                f"timeframe {self.timeframe!r} is not a KuCoin candle type "
                f"({', '.join(INTERVAL_SECONDS)})"
            )
        if not self.lookbacks or any(lb < 2 for lb in self.lookbacks):
            raise ConfigError("lookbacks must be a non-empty list of integers >= 2")
        if self.candle_history < self.max_lookback + 3:
            raise ConfigError(
                f"candle_history ({self.candle_history}) must be at least "
                f"max(lookbacks) + 3 = {self.max_lookback + 3}"
            )
        if self.breakout_source not in ("close", "high"):
            raise ConfigError("breakout_source must be 'close' or 'high'")
        if self.prior_high_source not in ("close", "high"):
            raise ConfigError("prior_high_source must be 'close' or 'high'")
        if self.rvol_lookback < 2:
            raise ConfigError("rvol_lookback must be >= 2")
        if self.max_breakout_distance_pct < 0:
            raise ConfigError("max_breakout_distance_pct must be >= 0")
        if self.cycle_seconds < 5:
            raise ConfigError("cycle_seconds must be >= 5")
        if self.max_concurrent_requests < 1:
            raise ConfigError("max_concurrent_requests must be >= 1")
        bad = [k for k in self.alert_on if k not in VALID_EVENT_KINDS]
        if bad:
            raise ConfigError(
                f"alert_on contains unknown event kinds: {bad} "
                f"(valid: {', '.join(VALID_EVENT_KINDS)})"
            )
        if self.score_weights.total() <= 0:
            raise ConfigError("score_weights must sum to a positive number")
        if not self.target_r_multiples or any(m <= 0 for m in self.target_r_multiples):
            raise ConfigError("target_r_multiples must be a non-empty list of positive numbers")
        if self.structure_swing_window < 1:
            raise ConfigError("structure_swing_window must be >= 1")
        if self.structure_max_targets < 0:
            raise ConfigError("structure_max_targets must be >= 0")
        if self.max_targets < 1:
            raise ConfigError("max_targets must be >= 1")
        if self.screen_lookback < 2:
            raise ConfigError("screen_lookback must be >= 2")
        if self.screen_trend_fast_len < 1 or self.screen_trend_slow_len < 1:
            raise ConfigError("screen_trend_fast_len / screen_trend_slow_len must be >= 1")
        if self.screen_trend_fast_len >= self.screen_trend_slow_len:
            raise ConfigError("screen_trend_fast_len must be < screen_trend_slow_len")
        if self.screen_rsi_len < 2:
            raise ConfigError("screen_rsi_len must be >= 2")
        if not (0 < self.screen_rsi_oversold < self.screen_rsi_oversold_mild < 100):
            raise ConfigError(
                "screen_rsi_oversold must be < screen_rsi_oversold_mild, both between 0 and 100"
            )
        if self.screen_bb_len < 2 or self.screen_bb_stdev <= 0:
            raise ConfigError("screen_bb_len must be >= 2 and screen_bb_stdev > 0")
        if self.screen_macd_fast >= self.screen_macd_slow:
            raise ConfigError("screen_macd_fast must be < screen_macd_slow")
        if self.screen_macd_signal < 1:
            raise ConfigError("screen_macd_signal must be >= 1")
        if self.screen_min_rvol < 0:
            raise ConfigError("screen_min_rvol must be >= 0")
        if self.pattern_lookback_candles < 8:
            raise ConfigError("pattern_lookback_candles must be >= 8")
        if self.pattern_swing_window < 1:
            raise ConfigError("pattern_swing_window must be >= 1")
        if self.pattern_min_swings < 2:
            raise ConfigError("pattern_min_swings must be >= 2")
        if not (0 <= self.pattern_min_r2 <= 1):
            raise ConfigError("pattern_min_r2 must be between 0 and 1")
        if self.pattern_flat_slope_pct < 0 or self.pattern_parallel_tol_pct < 0:
            raise ConfigError("pattern_flat_slope_pct / pattern_parallel_tol_pct must be >= 0")
        if self.pattern_breakout_pct < 0:
            raise ConfigError("pattern_breakout_pct must be >= 0")
        if self.hs_shoulder_tolerance_pct <= 0:
            raise ConfigError("hs_shoulder_tolerance_pct must be > 0")
        if self.hs_min_head_prominence_pct < 0:
            raise ConfigError("hs_min_head_prominence_pct must be >= 0")
        if self.hs_swing_window < 1:
            raise ConfigError("hs_swing_window must be >= 1")
        if self.hs_lookback_candles < self.hs_swing_window * 4:
            raise ConfigError("hs_lookback_candles must be >= hs_swing_window * 4")
        if self.hs_lookback_candles > self.candle_history:
            raise ConfigError(
                f"hs_lookback_candles ({self.hs_lookback_candles}) must be <= "
                f"candle_history ({self.candle_history}) or there won't be enough "
                "fetched history to fill the window"
            )
        if self.web_refresh_seconds < 5:
            raise ConfigError("web_refresh_seconds must be >= 5")
        bad_dirs = [
            d for d in self.pattern_alert_directions if d not in ("breakout_up", "breakout_down")
        ]
        if bad_dirs:
            raise ConfigError(f"pattern_alert_directions contains invalid values: {bad_dirs}")
        if not (0 <= self.pattern_alert_min_score <= 100):
            raise ConfigError("pattern_alert_min_score must be between 0 and 100")
        if self.pattern_alert_cooldown_seconds < 0:
            raise ConfigError("pattern_alert_cooldown_seconds must be >= 0")
        if self.pattern_confidence_weights.total() <= 0:
            raise ConfigError("pattern_confidence_weights must sum to a positive number")
        if self.heartbeat_interval_seconds < 60:
            raise ConfigError("heartbeat_interval_seconds must be >= 60")
        if self.alert_digest_max_items < 1:
            raise ConfigError("alert_digest_max_items must be >= 1")


def _coerce(raw: dict[str, Any]) -> Config:
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    data = dict(raw)
    if "score_weights" in data:
        sw = data["score_weights"]
        if not isinstance(sw, dict):
            raise ConfigError("score_weights must be a mapping")
        sw_known = {f.name for f in fields(ScoreWeights)}
        sw_unknown = set(sw) - sw_known
        if sw_unknown:
            raise ConfigError(f"unknown score_weights keys: {', '.join(sorted(sw_unknown))}")
        data["score_weights"] = ScoreWeights(**sw)

    if "pattern_confidence_weights" in data:
        pw = data["pattern_confidence_weights"]
        if not isinstance(pw, dict):
            raise ConfigError("pattern_confidence_weights must be a mapping")
        pw_known = {f.name for f in fields(PatternScoreWeights)}
        pw_unknown = set(pw) - pw_known
        if pw_unknown:
            raise ConfigError(
                f"unknown pattern_confidence_weights keys: {', '.join(sorted(pw_unknown))}"
            )
        data["pattern_confidence_weights"] = PatternScoreWeights(**pw)

    for key in (
        "lookbacks",
        "symbol_allowlist",
        "symbol_denylist",
        "alert_on",
        "target_r_multiples",
        "pattern_alert_directions",
    ):
        if key in data and data[key] is not None:
            data[key] = tuple(data[key])
    if "target_r_multiples" in data and data["target_r_multiples"] is not None:
        data["target_r_multiples"] = tuple(float(m) for m in data["target_r_multiples"])
    if "symbol_allowlist" in data:
        data["symbol_allowlist"] = tuple(s.upper() for s in data["symbol_allowlist"])
    if "symbol_denylist" in data:
        data["symbol_denylist"] = tuple(s.upper() for s in data["symbol_denylist"])
    if "alert_on" in data:
        data["alert_on"] = tuple(s.upper() for s in data["alert_on"])

    try:
        cfg = Config(**data)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ConfigError(str(exc)) from exc
    cfg.validate()
    return cfg


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a YAML mapping")
    return _coerce(raw)
