from __future__ import annotations

from dataclasses import replace

from conftest import INTERVAL_S, flat, make_candles, ticker

from local_high.strategy import (
    BREAKOUT,
    FAKEOUT,
    IDLE,
    PULLBACK,
    REBREAK,
    WATCH,
    SymbolState,
    detect_nlh,
    evaluate,
)

INTERVAL_MS = INTERVAL_S * 1000


# --------------------------------------------------------------------------- #
# detect_nlh
# --------------------------------------------------------------------------- #
def test_detect_nlh_fresh_breakout(cfg):
    candles = make_candles(flat(12) + [(105.0, 100.0, 105.0)])
    result = detect_nlh(candles, cfg)
    assert result.is_high
    assert result.fresh
    assert result.matched_lookbacks == (10,)
    assert result.nearest_level == 100.0
    assert round(result.distance_pct, 2) == 5.0


def test_detect_nlh_not_fresh_when_prior_candle_already_broke(cfg):
    candles = make_candles(flat(11) + [(105.0, 104.0, 105.0), (106.0, 105.0, 106.0)])
    result = detect_nlh(candles, cfg)
    assert result.is_high
    assert not result.fresh


def test_detect_nlh_near_high(cfg):
    candles = make_candles(flat(12, 100.0) + [(99.6, 99.0, 99.5)])
    result = detect_nlh(candles, cfg)
    assert not result.is_high
    assert result.near_high
    assert round(result.distance_pct, 2) == -0.5


def test_detect_nlh_respects_min_breakout_pct(cfg):
    cfg = replace(cfg, min_breakout_pct=1.0)  # require +1% over resistance
    candles = make_candles(flat(12) + [(100.5, 100.0, 100.5)])
    assert not detect_nlh(candles, cfg).is_high
    candles = make_candles(flat(12) + [(101.5, 100.0, 101.5)])
    assert detect_nlh(candles, cfg).is_high


def test_detect_nlh_multiple_lookbacks(cfg):
    cfg = replace(cfg, lookbacks=(5, 10), candle_history=200)
    # 10-window resistance 106 (older), 5-window resistance 102 (recent); close breaks both
    specs = flat(7, 106.0) + flat(5, 102.0) + [(107.0, 105.0, 107.0)]
    result = detect_nlh(make_candles(specs), cfg)
    assert set(result.matched_lookbacks) == {5, 10}
    assert result.nearest_level == 102.0        # smallest matched lookback
    assert result.structural_level == 106.0     # largest matched lookback


# --------------------------------------------------------------------------- #
# evaluate - state machine
# --------------------------------------------------------------------------- #
def test_evaluate_fresh_breakout_emits_event(cfg):
    candles = make_candles(flat(12) + [(105.0, 100.0, 105.0)])
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.state.state == BREAKOUT
    assert [e.kind for e in result.events] == [BREAKOUT]
    event = result.events[0]
    assert event.level == 100.0
    assert round(event.stop, 3) == round(100.0 * (1 - cfg.stop_buffer_pct / 100), 3)
    assert not event.seeded


def test_evaluate_non_fresh_breakout_is_ignored_after_first_sight(cfg):
    candles = make_candles(flat(11) + [(105.0, 104.0, 105.0), (106.0, 105.0, 106.0)])
    prior = SymbolState(symbol="AAA-USDT", state=IDLE, last_candle_ms=candles[-2].open_time_ms)
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    assert result.state.state == IDLE
    assert result.events == []


def test_evaluate_seeds_in_progress_setup_on_first_sight(cfg):
    candles = make_candles(flat(11) + [(105.0, 104.0, 105.0), (106.0, 105.0, 106.0)])
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.state.state == BREAKOUT
    assert result.events[0].seeded
    assert "in progress" in result.events[0].note


def test_evaluate_watch_when_near_high(cfg):
    candles = make_candles(flat(12, 100.0) + [(99.6, 99.0, 99.5)])
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.state.state == WATCH
    assert [e.kind for e in result.events] == [WATCH]


def test_evaluate_breakout_to_pullback(cfg):
    candles = make_candles(flat(12, 100.0) + [(101.0, 100.3, 101.0)])
    prior = SymbolState(
        symbol="AAA-USDT",
        state=BREAKOUT,
        last_candle_ms=candles[-2].open_time_ms,
        breakout_candle_ms=candles[-1].open_time_ms - INTERVAL_MS,
        breakout_level=100.0,
        breakout_high=105.0,
        highest_since_breakout=105.0,
        pullback_low=104.0,
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    assert result.state.state == PULLBACK
    assert result.state.pullback_low == 100.3
    assert [e.kind for e in result.events] == [PULLBACK]


def test_evaluate_pullback_to_rebreak_sets_stop_to_pullback_low(cfg):
    candles = make_candles(flat(12, 100.0) + [(106.0, 105.0, 106.0)])
    prior = SymbolState(
        symbol="AAA-USDT",
        state=PULLBACK,
        last_candle_ms=candles[-2].open_time_ms,
        breakout_candle_ms=candles[-1].open_time_ms - 2 * INTERVAL_MS,
        breakout_level=100.0,
        breakout_high=105.0,
        highest_since_breakout=105.0,
        pullback_low=100.3,
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    assert result.state.state == REBREAK
    event = result.events[0]
    assert event.kind == REBREAK
    assert event.stop == 100.3
    assert event.level == 105.0


def test_evaluate_breakout_to_fakeout(cfg):
    candles = make_candles(flat(12, 100.0) + [(99.0, 97.5, 98.0)])
    prior = SymbolState(
        symbol="AAA-USDT",
        state=BREAKOUT,
        last_candle_ms=candles[-2].open_time_ms,
        breakout_candle_ms=candles[-1].open_time_ms - INTERVAL_MS,
        breakout_level=100.0,
        breakout_high=105.0,
        highest_since_breakout=105.0,
        pullback_low=104.0,
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    assert result.state.state == FAKEOUT
    assert result.events[0].kind == FAKEOUT


def test_evaluate_no_new_candle_is_a_no_op(cfg):
    candles = make_candles(flat(13, 100.0))
    prior = SymbolState(
        symbol="AAA-USDT", state=BREAKOUT, last_candle_ms=candles[-1].open_time_ms
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    assert result.events == []
    assert result.state.state == BREAKOUT
    assert result.active is True


def test_evaluate_terminal_state_lingers_then_resets(cfg):
    candles = make_candles(flat(12, 100.0) + [(95.0, 94.0, 95.0)])
    lingering = SymbolState(
        symbol="AAA-USDT",
        state=REBREAK,
        last_candle_ms=candles[-2].open_time_ms,
        entered_candle_ms=candles[-1].open_time_ms - INTERVAL_MS,  # age 1 < 3
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, lingering)
    assert result.state.state == REBREAK
    assert result.events == []

    reset = replace(
        lingering, entered_candle_ms=candles[-1].open_time_ms - 5 * INTERVAL_MS  # age 5 >= 3
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, reset)
    assert result.state.state == IDLE


def test_evaluate_insufficient_history_is_idle(cfg):
    candles = make_candles(flat(5, 100.0))
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.state.state == IDLE
    assert result.events == []
    assert result.active is False


def test_breakout_event_carries_a_trade_plan(cfg):
    candles = make_candles(flat(12, 100.0) + [(103.0, 100.5, 102.0)])
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.state.state == BREAKOUT
    plan = result.events[0].plan
    assert plan is not None
    assert plan.entry == 102.0
    assert plan.stop < 100.0                       # just below the broken resistance
    assert plan.risk > 0
    assert len(plan.targets) >= 1
    assert plan.targets[0].price > plan.entry
    assert result.plan is plan


def test_rebreak_event_plan_uses_pullback_low_as_stop(cfg):
    candles = make_candles(flat(12, 100.0) + [(106.0, 105.0, 106.0)])
    prior = SymbolState(
        symbol="AAA-USDT",
        state=PULLBACK,
        last_candle_ms=candles[-2].open_time_ms,
        breakout_candle_ms=candles[-1].open_time_ms - 2 * INTERVAL_MS,
        breakout_level=100.0,
        breakout_high=105.0,
        highest_since_breakout=105.0,
        pullback_low=100.3,
    )
    result = evaluate("AAA-USDT", candles, ticker(), cfg, prior)
    plan = result.events[0].plan
    assert plan is not None
    assert plan.stop == 100.3
    assert plan.entry == 106.0


def test_watch_event_has_no_plan(cfg):
    candles = make_candles(flat(12, 100.0) + [(99.6, 99.0, 99.5)])
    result = evaluate("AAA-USDT", candles, ticker(), cfg, None)
    assert result.events[0].kind == WATCH
    assert result.events[0].plan is None
    assert result.plan is None
