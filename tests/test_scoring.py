from __future__ import annotations

from conftest import flat, make_candles

from local_high.config import Config, PatternScoreWeights, ScoreWeights
from local_high.patterns import PatternLine, PatternMatch
from local_high.scoring import confluence_score, pattern_confidence


def test_score_is_zero_for_a_flat_no_volume_candidate():
    assert confluence_score(0.0, 0.0, 0.0, False, ScoreWeights()) == 0.0


def test_score_is_100_when_every_component_saturates():
    score = confluence_score(10.0, 8.0, 40.0, True, ScoreWeights())
    assert score == 100.0


def test_score_is_bounded_and_monotone_in_rvol():
    w = ScoreWeights()
    low = confluence_score(1.0, 1.0, 5.0, False, w)
    high = confluence_score(1.0, 4.0, 5.0, False, w)
    assert 0.0 <= low < high <= 100.0


def test_negative_distance_does_not_go_below_zero():
    score = confluence_score(-3.0, 0.0, 0.0, False, ScoreWeights())
    assert score == 0.0


def test_weights_change_the_mix():
    only_trend = ScoreWeights(rvol=0.0, change_24h=0.0, breakout_strength=0.0, trend=1.0)
    assert confluence_score(0.0, 0.0, 0.0, True, only_trend) == 100.0
    assert confluence_score(5.0, 5.0, 5.0, False, only_trend) == 0.0


def _pattern_match(**over):
    defaults = dict(
        symbol="AAA-USDT",
        pattern="ascending_triangle",
        status="breakout_up",
        price=110.0,
        resistance=PatternLine(0.1, 0.9, 100.0, 95.0),
        support=PatternLine(0.5, 0.9, 90.0, 80.0),
        target=120.0,
        note="",
    )
    defaults.update(over)
    return PatternMatch(**defaults)


def test_pattern_confidence_high_for_a_well_confirmed_breakout():
    from dataclasses import replace as dc_replace

    cfg = Config()
    # strong uptrend, big volume spike on the last candle, well clear of the line
    specs = [(100.0 + i, 99.0 + i, 100.0 + i) for i in range(60)]
    candles = make_candles(specs)
    candles[-1] = dc_replace(candles[-1], volume=1000.0)
    last_close = candles[-1].close
    match = _pattern_match(
        price=last_close + 20,
        resistance=PatternLine(0.1, 0.95, last_close, last_close - 5),
        support=PatternLine(0.5, 0.95, last_close - 15, last_close - 20),
    )
    score = pattern_confidence(candles, match, cfg)
    assert score > 70.0


def test_pattern_confidence_low_for_a_barely_there_breakout():
    cfg = Config()
    # flat/choppy series, no volume spike, price just barely over the line, poor fit
    candles = make_candles(flat(60, 100.0))
    match = _pattern_match(
        price=100.5,
        resistance=PatternLine(0.0, 0.4, 100.0, 100.0),
        support=PatternLine(0.0, 0.4, 90.0, 90.0),
    )
    score = pattern_confidence(candles, match, cfg)
    assert score < 40.0


def test_pattern_confidence_weights_change_the_mix():
    cfg = Config(
        pattern_confidence_weights=PatternScoreWeights(fit=1.0, rvol=0.0, trend=0.0, strength=0.0)
    )
    candles = make_candles(flat(30, 100.0))
    perfect_fit = _pattern_match(
        resistance=PatternLine(0.0, 1.0, 100.0, 100.0), support=PatternLine(0.0, 1.0, 90.0, 90.0)
    )
    poor_fit = _pattern_match(
        resistance=PatternLine(0.0, 0.0, 100.0, 100.0), support=PatternLine(0.0, 0.0, 90.0, 90.0)
    )
    assert pattern_confidence(candles, perfect_fit, cfg) == 100.0
    assert pattern_confidence(candles, poor_fit, cfg) == 0.0
