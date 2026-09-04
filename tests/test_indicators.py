from __future__ import annotations

import pytest
from conftest import flat, make_candles

from local_high.indicators import (
    CandleParseError,
    atr,
    bollinger_bands,
    closed_only,
    ema,
    ema_series,
    linear_regression,
    macd,
    parse_kucoin_candles,
    relative_volume,
    rsi,
    sma,
    stdev,
    swing_highs,
    swing_lows,
    window_extreme,
)

INTERVAL = 3600


def test_parse_kucoin_candles_orders_and_flags_closed():
    # KuCoin returns newest-first: [time, open, close, high, low, volume, turnover]
    now_ms = 10_000 * 1000
    rows = [
        ["9000", "1", "2", "3", "0.5", "10", "20"],   # closed (9000+3600 <= 10000)
        ["7200", "1", "1.5", "2", "0.8", "8", "12"],
        ["3600", "1", "1.2", "1.5", "0.9", "5", "6"],
    ]
    candles = parse_kucoin_candles(rows, INTERVAL, now_ms)
    assert [c.open_time_ms for c in candles] == [3_600_000, 7_200_000, 9_000_000]
    assert candles[-1].closed is False        # 9_000_000 + 3_600_000 > 10_000_000
    assert candles[0].closed is True
    assert candles[0].high == 1.5


def test_parse_kucoin_candles_rejects_short_rows():
    with pytest.raises(CandleParseError):
        parse_kucoin_candles([["1", "2", "3"]], INTERVAL, 0)


def test_closed_only_filters():
    from dataclasses import replace

    candles = make_candles(flat(3))
    candles[-1] = replace(candles[-1], closed=False)
    assert len(closed_only(candles)) == 2


def test_window_extreme_high_and_close():
    candles = make_candles([(10, 5, 8), (12, 6, 11), (9, 4, 7), (20, 1, 2)])
    assert window_extreme(candles, 3, 3, "high") == 12
    assert window_extreme(candles, 3, 3, "close") == 11


def test_window_extreme_needs_history():
    candles = make_candles(flat(3))
    with pytest.raises(ValueError):
        window_extreme(candles, 1, 5, "high")


def test_ema_tracks_upward_series():
    assert ema([1.0], 10) == 1.0
    value = ema([float(i) for i in range(1, 21)], 5)
    assert 17.0 < value < 20.0


def test_atr_positive_for_ranging_candles():
    candles = make_candles([(i + 2, i - 2, i) for i in range(5, 25)])
    assert atr(candles, 14) > 0


def test_relative_volume_ratio():
    specs = flat(10, 100.0)
    candles = make_candles(specs)
    # bump the last candle's volume via a rebuilt Candle
    from dataclasses import replace

    candles[-1] = replace(candles[-1], volume=300.0)
    assert relative_volume(candles, 5) == pytest.approx(3.0)


def test_sma_averages_the_window():
    assert sma([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(3.5)
    assert sma([1.0, 2.0], 5) == 0.0  # not enough history


def test_stdev_zero_for_flat_series():
    assert stdev([5.0, 5.0, 5.0], 3) == 0.0
    assert stdev([1.0, 2.0, 3.0], 3) > 0.0


def test_bollinger_bands_widen_with_volatility():
    flat_series = [100.0] * 20
    mid, upper, lower = bollinger_bands(flat_series, 20, 2.0)
    assert mid == upper == lower == pytest.approx(100.0)
    volatile = [100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(20)]
    _, upper2, lower2 = bollinger_bands(volatile, 20, 2.0)
    assert upper2 > 100.0 > lower2


def test_rsi_extremes():
    rising = [float(i) for i in range(1, 30)]
    falling = list(reversed(rising))
    assert rsi(rising, 14) > 70
    assert rsi(falling, 14) < 30
    assert rsi([1.0, 2.0], 14) == 50.0  # not enough history -> neutral


def test_ema_series_last_value_matches_ema():
    values = [float(i) for i in range(1, 21)]
    series = ema_series(values, 5)
    assert len(series) == len(values)
    assert series[-1] == pytest.approx(ema(values, 5))


def test_macd_crosses_above_signal_on_a_reversal():
    down = [100.0 - i for i in range(40)]
    up = [down[-1] + i for i in range(1, 20)]
    closes = down + up
    macd_line, signal_line, hist = macd(closes, 12, 26, 9)
    assert len(macd_line) == len(signal_line) == len(hist) == len(closes)
    # after a sustained rally the MACD line should be back above its signal line
    assert macd_line[-1] > signal_line[-1]


def test_swing_highs_and_lows_find_local_extremes():
    specs = flat(3, 10.0) + [(20.0, 5.0, 6.0)] + flat(3, 10.0) + [(1.0, 0.5, 0.6)] + flat(3, 10.0)
    candles = make_candles(specs)
    highs = swing_highs(candles, 2)
    lows = swing_lows(candles, 2)
    assert 20.0 in [p for _, p in highs]
    assert 0.5 in [p for _, p in lows]


def test_linear_regression_recovers_a_perfect_line():
    points = [(0.0, 10.0), (1.0, 12.0), (2.0, 14.0), (3.0, 16.0)]
    slope, intercept, r2 = linear_regression(points)
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(10.0)
    assert r2 == pytest.approx(1.0)


def test_linear_regression_flat_series_has_zero_slope():
    slope, intercept, r2 = linear_regression([(0.0, 5.0), (1.0, 5.0), (2.0, 5.0)])
    assert slope == pytest.approx(0.0)
    assert intercept == pytest.approx(5.0)
    assert r2 == pytest.approx(1.0)  # zero variance -> defined as a perfect fit
