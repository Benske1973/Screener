from __future__ import annotations

from dataclasses import replace

from conftest import make_candles

from local_high.config import Config
from local_high.scanner import Scanner
from local_high.strategy import BREAKOUT, WATCH, Evaluation, Event, SymbolState


def _zigzag_candles(vertices):
    """Candles whose highs/lows trace straight lines between (index, price)
    vertices, alternating swing highs and swing lows - see test_patterns.py."""
    n = vertices[-1][0] + 1
    prices = [0.0] * n
    for (i0, p0), (i1, p1) in zip(vertices, vertices[1:], strict=False):
        for i in range(i0, i1 + 1):
            t = (i - i0) / (i1 - i0)
            prices[i] = p0 + (p1 - p0) * t
    specs = [(p + max(abs(p) * 0.001, 0.001), p - max(abs(p) * 0.001, 0.001), p) for p in prices]
    return make_candles(specs)


# a clean ascending-triangle breakout_up: flat resistance ~100, rising support, close breaks out
_BREAKOUT_CANDLES = _zigzag_candles(
    [(0, 90.0), (5, 100.0), (10, 93.0), (15, 100.0), (20, 96.0), (25, 100.0), (30, 105.0)]
)


def _evaluation(rvol: float, alerts: dict[str, int] | None = None) -> Evaluation:
    state = SymbolState(symbol="AAA-USDT", state=BREAKOUT, alerts=alerts or {})
    return Evaluation(
        symbol="AAA-USDT",
        state=state,
        events=[],
        active=True,
        price=1.0,
        distance_pct=2.0,
        rvol=rvol,
        change_24h_pct=5.0,
        trend_ok=True,
        atr=0.1,
        score=60.0,
        matched_lookbacks=(30,),
    )


def _event(kind: str, *, seeded: bool = False, distance_pct: float = 2.0) -> Event:
    return Event(
        symbol="AAA-USDT",
        kind=kind,
        candle_ms=1_600_000_000_000,
        price=1.0,
        level=0.98,
        stop=0.97,
        matched_lookbacks=(30,),
        rvol=0.0,
        change_24h_pct=5.0,
        distance_pct=distance_pct,
        atr=0.1,
        score=60.0,
        seeded=seeded,
        note="",
    )


def test_should_alert_honours_alert_on(cfg):
    scanner = Scanner(replace(cfg, alert_on=("REBREAK",)))
    assert scanner._should_alert(_evaluation(3.0), _event(BREAKOUT), now_s=10_000_000) is False


def test_should_alert_suppresses_seeded(cfg):
    scanner = Scanner(replace(cfg, alert_on=("BREAKOUT",), alert_on_seed=False, min_rvol=0.0))
    assert scanner._should_alert(_evaluation(3.0), _event(BREAKOUT, seeded=True), now_s=1) is False


def test_should_alert_rvol_gate(cfg):
    scanner = Scanner(replace(cfg, alert_on=("BREAKOUT",), min_rvol=1.5))
    assert scanner._should_alert(_evaluation(0.9), _event(BREAKOUT), now_s=10_000_000) is False
    assert scanner._should_alert(_evaluation(2.0), _event(BREAKOUT), now_s=10_000_000) is True


def test_should_alert_extended_breakout_gate(cfg):
    scanner = Scanner(
        replace(cfg, alert_on=("BREAKOUT", "REBREAK"), min_rvol=0.0, max_breakout_distance_pct=5.0)
    )
    near = _event(BREAKOUT, distance_pct=3.0)
    far = _event(BREAKOUT, distance_pct=21.0)
    assert scanner._should_alert(_evaluation(3.0), near, now_s=10_000_000) is True
    assert scanner._should_alert(_evaluation(3.0), far, now_s=10_000_000) is False
    # the gate is BREAKOUT-only; an extended re-break still alerts
    assert scanner._should_alert(_evaluation(3.0), _event("REBREAK", distance_pct=21.0),
                                 now_s=10_000_000) is True


def test_extended_breakout_gate_disabled_with_zero(cfg):
    scanner = Scanner(
        replace(cfg, alert_on=("BREAKOUT",), min_rvol=0.0, max_breakout_distance_pct=0.0)
    )
    assert scanner._should_alert(_evaluation(3.0), _event(BREAKOUT, distance_pct=50.0),
                                 now_s=10_000_000) is True


def test_should_alert_cooldown(cfg):
    scanner = Scanner(
        replace(cfg, alert_on=("BREAKOUT",), min_rvol=0.0, alert_cooldown_seconds=3600)
    )
    recent = _evaluation(3.0, alerts={"BREAKOUT": 10_000_000 - 100})
    assert scanner._should_alert(recent, _event(BREAKOUT), now_s=10_000_000) is False
    stale = _evaluation(3.0, alerts={"BREAKOUT": 10_000_000 - 5000})
    assert scanner._should_alert(stale, _event(BREAKOUT), now_s=10_000_000) is True


def test_watch_event_not_rvol_gated(cfg):
    scanner = Scanner(replace(cfg, alert_on=("WATCH",), min_rvol=5.0))
    assert scanner._should_alert(_evaluation(0.1), _event(WATCH), now_s=10_000_000) is True


def _pattern_scanner_cfg(**over):
    base = dict(pattern_swing_window=1, pattern_min_swings=2, pattern_min_r2=0.5)
    base.update(over)
    return replace(Config(), **base)


def test_check_pattern_alert_queues_a_high_confidence_breakout():
    scanner = Scanner(_pattern_scanner_cfg())
    state = SymbolState(symbol="AAA-USDT")
    out: list = []
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_000, out)
    assert len(out) == 1
    match, score = out[0]
    assert match.status == "breakout_up"
    assert score >= scanner.cfg.pattern_alert_min_score
    assert state.pattern_alerts  # cooldown timestamp recorded


def test_check_pattern_alert_respects_cooldown():
    scanner = Scanner(_pattern_scanner_cfg(pattern_alert_cooldown_seconds=3600))
    state = SymbolState(symbol="AAA-USDT")
    out: list = []
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_000, out)
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_100, out)
    assert len(out) == 1  # second call falls inside the cooldown window


def test_check_pattern_alert_disabled_via_config():
    scanner = Scanner(_pattern_scanner_cfg(pattern_alerts_enabled=False))
    state = SymbolState(symbol="AAA-USDT")
    out: list = []
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_000, out)
    assert out == []


def test_check_pattern_alert_direction_filter():
    scanner = Scanner(_pattern_scanner_cfg(pattern_alert_directions=("breakout_down",)))
    state = SymbolState(symbol="AAA-USDT")
    out: list = []
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_000, out)
    assert out == []  # this pattern is a breakout_up, not breakout_down


def test_check_pattern_alert_score_gate():
    scanner = Scanner(_pattern_scanner_cfg(pattern_alert_min_score=99.9))
    state = SymbolState(symbol="AAA-USDT")
    out: list = []
    scanner._check_pattern_alert("AAA-USDT", _BREAKOUT_CANDLES, state, 1_000_000, out)
    assert out == []


async def test_heartbeat_fires_on_a_fresh_state(cfg, tmp_path):
    scanner = Scanner(replace(cfg, state_path=str(tmp_path / "state.json")))
    scanner.state.load()
    scanner._universe = ["AAA-USDT"]
    await scanner._maybe_heartbeat(now_ms=1_700_000_000_000)
    assert scanner.state.get_meta("last_heartbeat_ts") == 1_700_000_000


async def test_heartbeat_respects_the_interval(cfg, tmp_path):
    scanner = Scanner(
        replace(cfg, state_path=str(tmp_path / "state.json"), heartbeat_interval_seconds=3600)
    )
    scanner.state.load()
    scanner._universe = ["AAA-USDT"]
    await scanner._maybe_heartbeat(now_ms=1_700_000_000_000)
    await scanner._maybe_heartbeat(now_ms=1_700_000_100_000)  # 100s later, inside the hour
    assert scanner.state.get_meta("last_heartbeat_ts") == 1_700_000_000  # unchanged


async def test_heartbeat_disabled_via_config(cfg, tmp_path):
    scanner = Scanner(
        replace(cfg, state_path=str(tmp_path / "state.json"), heartbeat_enabled=False)
    )
    scanner.state.load()
    await scanner._maybe_heartbeat(now_ms=1_700_000_000_000)
    assert scanner.state.get_meta("last_heartbeat_ts") is None
