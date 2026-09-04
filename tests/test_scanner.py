from __future__ import annotations

from dataclasses import replace

from local_high.scanner import Scanner
from local_high.strategy import BREAKOUT, WATCH, Evaluation, Event, SymbolState


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
