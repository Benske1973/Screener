from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import make_candles

from local_high.patterns import detect_pattern


def _zigzag_candles(vertices: list[tuple[int, float]]) -> list:
    """Candles whose highs/lows trace straight lines between (index, price)
    vertices, alternating swing highs and swing lows every `window` bars."""
    n = vertices[-1][0] + 1
    prices = [0.0] * n
    for (i0, p0), (i1, p1) in zip(vertices, vertices[1:], strict=False):
        for i in range(i0, i1 + 1):
            t = (i - i0) / (i1 - i0)
            prices[i] = p0 + (p1 - p0) * t
    specs = []
    for p in prices:
        pad = max(abs(p) * 0.001, 0.001)
        specs.append((p + pad, p - pad, p))
    return make_candles(specs)


def _pattern_cfg(cfg):
    return replace(
        cfg,
        pattern_lookback_candles=100,
        pattern_swing_window=1,
        pattern_min_swings=2,
        pattern_min_r2=0.9,
        pattern_flat_slope_pct=0.05,
        pattern_parallel_tol_pct=0.08,
        pattern_breakout_pct=0.5,
        hs_lookback_candles=100,
        hs_swing_window=1,
    )


def test_detect_pattern_none_with_too_few_candles():
    cfg = _pattern_cfg(_base_cfg())
    candles = _zigzag_candles([(0, 90.0), (5, 100.0)])
    assert detect_pattern("AAA-USDT", candles, cfg) is None


def test_ascending_triangle_emerging():
    cfg = _pattern_cfg(_base_cfg())
    # flat resistance ~100, rising support 93 -> 96, still trading inside at the end
    vertices = [(0, 90.0), (5, 100.0), (10, 93.0), (15, 100.0), (20, 96.0), (25, 100.0), (30, 99.5)]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "ascending_triangle"
    assert match.status == "emerging"
    # resistance is flat: value_start and value_now should sit close together
    assert match.resistance.value_start == pytest.approx(match.resistance.value_now, rel=0.01)
    # support rises: value_now should be well above value_start
    assert match.support.value_now > match.support.value_start


def test_ascending_triangle_breakout_up_has_a_target():
    cfg = _pattern_cfg(_base_cfg())
    vertices = [
        (0, 90.0), (5, 100.0), (10, 93.0), (15, 100.0), (20, 96.0), (25, 100.0), (30, 103.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "ascending_triangle"
    assert match.status == "breakout_up"
    assert match.target is not None
    assert match.target > match.resistance.value_now


def test_descending_triangle():
    cfg = _pattern_cfg(_base_cfg())
    # flat support ~100, falling resistance 110 -> 107, emerging inside
    vertices = [
        (0, 120.0), (5, 100.0), (10, 110.0), (15, 100.0), (20, 107.0), (25, 100.0), (30, 102.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "descending_triangle"


def test_ascending_channel_roughly_parallel_rising_lines():
    cfg = _pattern_cfg(_base_cfg())
    # resistance 100 -> 106 -> 112, support 90 -> 96 -> 102: both rise ~ the same rate
    vertices = [
        (0, 90.0), (5, 100.0), (10, 96.0), (15, 106.0), (20, 102.0), (25, 112.0), (30, 108.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "ascending_channel"


def test_descending_channel_roughly_parallel_falling_lines():
    cfg = _pattern_cfg(_base_cfg())
    vertices = [
        (0, 112.0), (5, 102.0), (10, 106.0), (15, 96.0), (20, 100.0), (25, 90.0), (30, 94.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "descending_channel"


def test_falling_wedge_resistance_falls_faster_than_support():
    cfg = _pattern_cfg(_base_cfg())
    # resistance falls fast: 100 -> 90 -> 80; support falls slowly: 70 -> 68 -> 66 (converging)
    vertices = [
        (0, 70.0), (5, 100.0), (10, 68.0), (15, 90.0), (20, 66.0), (25, 80.0), (30, 74.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "falling_wedge"


def test_rising_wedge_support_rises_faster_than_resistance():
    cfg = _pattern_cfg(_base_cfg())
    # resistance rises slowly: 100 -> 102 -> 104 ; support rises faster: 90 -> 95 (converging up)
    vertices = [
        (0, 80.0), (5, 100.0), (10, 90.0), (15, 102.0), (20, 95.0), (25, 104.0), (30, 102.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "rising_wedge"


def test_inverse_head_and_shoulders_emerging():
    cfg = _pattern_cfg(_base_cfg())
    # shoulders ~80/82, head 70 (clearly deeper), neckline ~95->96, still below it
    vertices = [(0, 100.0), (5, 80.0), (10, 95.0), (15, 70.0), (20, 96.0), (25, 82.0), (30, 90.0)]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "inverse_head_and_shoulders"
    assert match.status == "emerging"
    assert match.target is None


def test_inverse_head_and_shoulders_breakout_up_has_a_target():
    cfg = _pattern_cfg(_base_cfg())
    vertices = [(0, 100.0), (5, 80.0), (10, 95.0), (15, 70.0), (20, 96.0), (25, 82.0), (30, 100.0)]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "inverse_head_and_shoulders"
    assert match.status == "breakout_up"
    assert match.target is not None
    assert match.target > match.resistance.value_now  # neckline


def test_head_and_shoulders_breakout_down_has_a_target():
    cfg = _pattern_cfg(_base_cfg())
    # shoulders ~80/78, head 90 (clearly higher), neckline ~65->64, closes well below it
    vertices = [(0, 60.0), (5, 80.0), (10, 65.0), (15, 90.0), (20, 64.0), (25, 78.0), (30, 55.0)]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is not None
    assert match.pattern == "head_and_shoulders"
    assert match.status == "breakout_down"
    assert match.target is not None
    assert match.target < match.support.value_now  # neckline


def test_head_and_shoulders_rejects_asymmetric_shoulders():
    cfg = _pattern_cfg(_base_cfg())
    # right shoulder (110) is nowhere near the left shoulder's depth (80) - not symmetric
    vertices = [
        (0, 100.0), (5, 80.0), (10, 95.0), (15, 70.0), (20, 96.0), (25, 110.0), (30, 90.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is None or "head_and_shoulders" not in match.pattern


def test_head_and_shoulders_rejects_a_shallow_head():
    cfg = _pattern_cfg(_base_cfg())
    # head (79) barely clears the shoulders (80) - not a real head-and-shoulders
    vertices = [
        (0, 100.0), (5, 80.0), (10, 95.0), (15, 79.0), (20, 96.0), (25, 82.0), (30, 90.0),
    ]
    candles = _zigzag_candles(vertices)
    match = detect_pattern("AAA-USDT", candles, cfg)
    assert match is None or "head_and_shoulders" not in match.pattern


def _base_cfg():
    from local_high.config import Config

    return Config(candle_history=200)
