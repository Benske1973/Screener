from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float          # base-asset volume
    quote_volume: float     # quote-asset turnover
    closed: bool


class CandleParseError(ValueError):
    pass


def parse_kucoin_candles(
    rows: Iterable[Sequence[str]], interval_seconds: int, now_ms: int
) -> list[Candle]:
    """Parse KuCoin ``/api/v1/market/candles`` rows into ascending, de-duplicated candles.

    KuCoin returns newest-first rows shaped ``[time, open, close, high, low, volume, turnover]``
    with ``time`` in seconds. The most recent row is usually the in-progress candle; it is kept
    but flagged ``closed=False`` so callers can drop it.
    """
    if interval_seconds <= 0:
        raise CandleParseError("interval_seconds must be positive")
    duration_ms = interval_seconds * 1000
    by_time: dict[int, Candle] = {}
    for row in rows:
        if len(row) < 7:
            raise CandleParseError(f"invalid KuCoin candle row: {row!r}")
        try:
            open_time_ms = int(float(row[0])) * 1000
            candle = Candle(
                open_time_ms=open_time_ms,
                open=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                volume=float(row[5]),
                quote_volume=float(row[6]),
                closed=open_time_ms + duration_ms <= now_ms,
            )
        except (TypeError, ValueError) as exc:
            raise CandleParseError(f"malformed KuCoin candle row {row!r}: {exc}") from exc
        by_time[open_time_ms] = candle
    return [by_time[key] for key in sorted(by_time)]


def closed_only(candles: Iterable[Candle]) -> list[Candle]:
    return [c for c in candles if c.closed]


def window_extreme(candles: Sequence[Candle], end_idx: int, lookback: int, source: str) -> float:
    """Highest ``source`` value over the ``lookback`` candles ending just before ``end_idx``."""
    start = end_idx - lookback
    if start < 0:
        raise ValueError("not enough candles for the requested lookback")
    window = candles[start:end_idx]
    if source == "close":
        return max(c.close for c in window)
    return max(c.high for c in window)


def ema(values: Sequence[float], length: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (length + 1.0)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1.0 - k)
    return out


def atr(candles: Sequence[Candle], length: int) -> float:
    """Wilder's ATR. Falls back to a simple mean of available true ranges when history is short."""
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = candles[0].close
    for c in candles[1:]:
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
        prev_close = c.close
    if len(trs) < length:
        return sum(trs) / len(trs)
    value = sum(trs[:length]) / length
    for tr in trs[length:]:
        value = (value * (length - 1) + tr) / length
    return value


def relative_volume(candles: Sequence[Candle], lookback: int) -> float:
    """Last closed candle volume divided by the mean of the ``lookback`` candles before it."""
    if len(candles) < 2:
        return 0.0
    baseline = candles[-lookback - 1 : -1] if len(candles) > lookback else candles[:-1]
    volumes = [c.volume for c in baseline]
    avg = sum(volumes) / len(volumes) if volumes else 0.0
    if avg <= 0:
        return 0.0
    return candles[-1].volume / avg


def sma(values: Sequence[float], length: int) -> float:
    if length <= 0 or len(values) < length:
        return 0.0
    window = values[-length:]
    return sum(window) / length


def stdev(values: Sequence[float], length: int) -> float:
    """Population standard deviation of the last ``length`` values."""
    if length <= 1 or len(values) < length:
        return 0.0
    window = values[-length:]
    mean = sum(window) / length
    variance = sum((v - mean) ** 2 for v in window) / length
    return variance**0.5


def bollinger_bands(
    closes: Sequence[float], length: int, num_std: float
) -> tuple[float, float, float]:
    """(mid, upper, lower) bands: SMA and SMA +/- ``num_std`` population standard deviations."""
    mid = sma(closes, length)
    sd = stdev(closes, length)
    return mid, mid + num_std * sd, mid - num_std * sd


def rsi(closes: Sequence[float], length: int) -> float:
    """Wilder's RSI. Returns 50.0 (neutral) when there isn't enough history."""
    if len(closes) < length + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for gain, loss in zip(gains[length:], losses[length:], strict=True):
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ema_series(values: Sequence[float], length: int) -> list[float]:
    """Full EMA series aligned with ``values`` (same recurrence as :func:`ema`)."""
    if not values:
        return []
    k = 2.0 / (length + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def macd(
    closes: Sequence[float], fast: int, slow: int, signal: int
) -> tuple[list[float], list[float], list[float]]:
    """(macd_line, signal_line, histogram) series, each aligned with ``closes``."""
    if not closes:
        return [], [], []
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_e, slow_e, strict=True)]
    signal_line = ema_series(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line, strict=True)]
    return macd_line, signal_line, histogram


def swing_highs(candles: Sequence[Candle], window: int) -> list[tuple[int, float]]:
    """(index, price) of each candle whose high tops every high ``window`` bars either side."""
    out: list[tuple[int, float]] = []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i].high
        if all(h >= candles[j].high for j in range(i - window, i)) and all(
            h > candles[j].high for j in range(i + 1, i + 1 + window)
        ):
            out.append((i, h))
    return out


def swing_lows(candles: Sequence[Candle], window: int) -> list[tuple[int, float]]:
    """(index, price) of each candle whose low sits under every low ``window`` bars either side."""
    out: list[tuple[int, float]] = []
    n = len(candles)
    for i in range(window, n - window):
        low = candles[i].low
        if all(low <= candles[j].low for j in range(i - window, i)) and all(
            low < candles[j].low for j in range(i + 1, i + 1 + window)
        ):
            out.append((i, low))
    return out


def linear_regression(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """Least-squares line through ``(x, y)`` points. Returns ``(slope, intercept, r2)``."""
    n = len(points)
    if n == 0:
        return 0.0, 0.0, 0.0
    if n < 2:
        return 0.0, points[0][1], 0.0
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    mean_y = sum_y / n
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0, mean_y, 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    ss_tot = sum((p[1] - mean_y) ** 2 for p in points)
    # Guard against catastrophic cancellation when the y-values are nearly
    # identical relative to their scale (a genuinely flat line): below this
    # floor, ss_tot/ss_res are dominated by float rounding noise rather than
    # real variance, so the ratio is meaningless - treat it as a perfect fit.
    noise_floor = max(mean_y * mean_y, 1.0) * 1e-10 * n
    if ss_tot <= noise_floor:
        return slope, intercept, 1.0
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r2 = 1.0 - ss_res / ss_tot
    return slope, intercept, r2
