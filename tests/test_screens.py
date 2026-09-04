from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import flat, make_candles

from local_high.screens import SCREENS, ScreenError, run_screen


def _small(cfg):
    return replace(
        cfg,
        screen_lookback=5,
        screen_trend_fast_len=3,
        screen_trend_slow_len=6,
        screen_rsi_len=5,
        screen_pullback_lookback=3,
        screen_bb_len=5,
        screen_macd_fast=3,
        screen_macd_slow=6,
        screen_macd_signal=2,
        rvol_lookback=5,
    )


def _rising(n: int, start: float = 100.0) -> list[tuple[float, float, float]]:
    return [(start + i, start + i - 1.0, start + i) for i in range(n)]


def _falling(n: int, start: float = 100.0) -> list[tuple[float, float, float]]:
    return [(start - i + 1.0, start - i - 1.0, start - i) for i in range(n)]


def test_registry_has_the_expected_presets():
    expected = {
        "new_local_high",
        "new_local_low",
        "strong_uptrend",
        "pullback_in_uptrend",
        "very_oversold",
        "oversold_in_uptrend",
        "bullish_ema_crossover",
        "bullish_macd_crossover",
        "bollinger_breakout",
        "rvol_spike_in_uptrend",
    }
    assert expected == set(SCREENS)
    for name, screen in SCREENS.items():
        assert screen.name == name
        assert screen.label
        assert screen.description


async def test_run_screen_rejects_unknown_name(cfg):
    with pytest.raises(ScreenError):
        await run_screen(cfg, "does_not_exist")


def test_new_local_high_matches_a_fresh_break(cfg):
    cfg = _small(cfg)
    specs = flat(6, 100.0) + [(110.0, 100.0, 109.0)]
    match = SCREENS["new_local_high"].fn("AAA-USDT", make_candles(specs), cfg)
    assert match is not None
    assert match.screen == "new_local_high"


def test_new_local_high_no_match_inside_range(cfg):
    cfg = _small(cfg)
    match = SCREENS["new_local_high"].fn("AAA-USDT", make_candles(flat(7, 100.0)), cfg)
    assert match is None


def test_new_local_low_matches_a_fresh_break(cfg):
    cfg = _small(cfg)
    specs = flat(6, 100.0) + [(100.0, 85.0, 86.0)]
    match = SCREENS["new_local_low"].fn("AAA-USDT", make_candles(specs), cfg)
    assert match is not None
    assert match.screen == "new_local_low"


def test_strong_uptrend_matches_a_rising_series(cfg):
    cfg = _small(cfg)
    match = SCREENS["strong_uptrend"].fn("AAA-USDT", make_candles(_rising(10)), cfg)
    assert match is not None


def test_strong_uptrend_no_match_on_a_flat_series(cfg):
    cfg = _small(cfg)
    match = SCREENS["strong_uptrend"].fn("AAA-USDT", make_candles(flat(10, 100.0)), cfg)
    assert match is None


def test_pullback_in_uptrend_matches_a_dip(cfg):
    cfg = replace(_small(cfg), screen_pullback_min_pct=1.0)
    specs = _rising(10) + [(108.0, 105.0, 107.0)]
    match = SCREENS["pullback_in_uptrend"].fn("AAA-USDT", make_candles(specs), cfg)
    assert match is not None


def test_very_oversold_matches_a_falling_series(cfg):
    cfg = _small(cfg)
    match = SCREENS["very_oversold"].fn("AAA-USDT", make_candles(_falling(10)), cfg)
    assert match is not None


def test_very_oversold_no_match_on_a_rising_series(cfg):
    cfg = _small(cfg)
    match = SCREENS["very_oversold"].fn("AAA-USDT", make_candles(_rising(10)), cfg)
    assert match is None


def test_bollinger_breakout_matches_a_sharp_spike(cfg):
    cfg = replace(_small(cfg), screen_bb_len=5, screen_bb_stdev=1.0)
    specs = flat(5, 100.0) + [(130.0, 100.0, 130.0)]
    match = SCREENS["bollinger_breakout"].fn("AAA-USDT", make_candles(specs), cfg)
    assert match is not None


def test_bollinger_breakout_no_match_on_flat_series(cfg):
    cfg = replace(_small(cfg), screen_bb_len=5, screen_bb_stdev=1.0)
    match = SCREENS["bollinger_breakout"].fn("AAA-USDT", make_candles(flat(8, 100.0)), cfg)
    assert match is None


def test_rvol_spike_in_uptrend_matches_a_volume_spike(cfg):
    from dataclasses import replace as dc_replace

    cfg = replace(_small(cfg), screen_min_rvol=1.5)
    candles = make_candles(_rising(10))
    candles[-1] = dc_replace(candles[-1], volume=1000.0)
    match = SCREENS["rvol_spike_in_uptrend"].fn("AAA-USDT", candles, cfg)
    assert match is not None


def test_rvol_spike_in_uptrend_no_match_without_a_spike(cfg):
    cfg = replace(_small(cfg), screen_min_rvol=1.5)
    match = SCREENS["rvol_spike_in_uptrend"].fn("AAA-USDT", make_candles(_rising(10)), cfg)
    assert match is None
