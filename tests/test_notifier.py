from __future__ import annotations

from local_high.notifier import format_message, render_table
from local_high.strategy import BREAKOUT, REBREAK, Evaluation, Event, SymbolState


def _event(kind: str, **over) -> Event:
    base = dict(
        symbol="AAA-USDT",
        kind=kind,
        candle_ms=1_600_000_000_000,
        price=0.0044389,
        level=0.0036561,
        stop=0.0036378,
        matched_lookbacks=(30, 90),
        rvol=33.5,
        change_24h_pct=36.4,
        distance_pct=21.41,
        atr=0.00016,
        score=100.0,
        seeded=False,
        note="",
    )
    base.update(over)
    return Event(**base)


def _evaluation(state: str) -> Evaluation:
    return Evaluation(
        symbol="AAA-USDT",
        state=SymbolState(symbol="AAA-USDT", state=state),
        events=[],
        active=True,
        price=0.0044389,
        distance_pct=21.41,
        rvol=33.5,
        change_24h_pct=36.4,
        trend_ok=True,
        atr=0.00016,
        score=100.0,
        matched_lookbacks=(30, 90),
    )


def test_render_table_lists_active_rows():
    out = render_table(3, 123, [_evaluation(BREAKOUT)], 1_600_000_000_000)
    assert "cycle 3" in out
    assert "AAA-USDT" in out
    assert "BREAKOUT" in out


def test_render_table_handles_no_active():
    out = render_table(1, 50, [], 1_600_000_000_000)
    assert "no active breakout setups" in out


def test_format_message_breakout_has_entry_and_stop():
    msg = format_message(_event(BREAKOUT), "4hour", 14)
    assert "BREAKOUT — AAA-USDT" in msg
    assert "Instap 1 (50%)" in msg
    assert "Stop:" in msg
    assert "geen order" in msg


def test_format_message_rebreak_points_stop_to_pullback_low():
    msg = format_message(_event(REBREAK, stop=0.0039), "4hour", 14)
    assert "resterende 50%" in msg
    assert "0.0039" in msg
