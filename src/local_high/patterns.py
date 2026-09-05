from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from local_high.config import Config
from local_high.indicators import (
    Candle,
    closed_only,
    linear_regression,
    parse_kucoin_candles,
    swing_highs,
    swing_lows,
)
from local_high.kucoin import KuCoinApiError, KuCoinRestClient
from local_high.universe import filter_universe

# ----------------------------------------------------------------------------
# Rule-based chart-pattern detection, altFINS-style.
#
# Two families, both reported through the same PatternMatch shape (a
# "resistance" line, a "support" line, a status, a measured-move target):
#
# 1. Channel patterns - fit a trendline through recent swing highs
#    (resistance) and swing lows (support), classify the pair by slope, and
#    report whether price is still trading between the lines ("emerging")
#    or has just cleared one of them ("breakout_up"/"breakout_down"). Covers
#    triangles, wedges and channels.
# 2. Head-and-shoulders - three alternating swing extremes (shoulder, head,
#    shoulder) with the head clearing both shoulders, plus a neckline fit
#    through the two swings between them. "resistance"/"support" become the
#    neckline and a flat reference line at the head - whichever sits higher
#    is "resistance" - so the rest of the pipeline (confidence scoring,
#    alerting, the dashboard's chart drawer) needs no pattern-specific code.
#
# Double top/bottom would need a third detector (two comparable extremes, no
# head) and isn't implemented.
# ----------------------------------------------------------------------------

PATTERNS = (
    "ascending_triangle",
    "descending_triangle",
    "rising_wedge",
    "falling_wedge",
    "ascending_channel",
    "descending_channel",
    "inverse_head_and_shoulders",
    "head_and_shoulders",
)

# head-and-shoulders matches are fit over `hs_lookback_candles`, not
# `pattern_lookback_candles` - callers that cache a candle window for
# charting (see webapp.py) need to know which window a given match used.
HS_PATTERNS = frozenset({"inverse_head_and_shoulders", "head_and_shoulders"})


@dataclass(frozen=True, slots=True)
class PatternLine:
    slope_pct: float   # % price change per candle, relative to the window's first close
    r2: float           # goodness of fit, 0-1
    value_now: float     # the line's value at the last candle in the fit window
    value_start: float    # the line's value at the first candle in the fit window


@dataclass(frozen=True, slots=True)
class PatternMatch:
    symbol: str
    pattern: str          # one of PATTERNS
    status: str            # "emerging" | "breakout_up" | "breakout_down"
    price: float
    resistance: PatternLine
    support: PatternLine
    target: float | None     # measured-move projection, only set on a breakout
    note: str


def _classify(res_slope_pct: float, sup_slope_pct: float, cfg: Config) -> str | None:
    flat = cfg.pattern_flat_slope_pct
    tol = cfg.pattern_parallel_tol_pct

    if abs(res_slope_pct) <= flat and sup_slope_pct > flat:
        return "ascending_triangle"
    if abs(sup_slope_pct) <= flat and res_slope_pct < -flat:
        return "descending_triangle"
    if res_slope_pct > flat and sup_slope_pct > flat:
        if abs(res_slope_pct - sup_slope_pct) <= tol:
            return "ascending_channel"
        if sup_slope_pct > res_slope_pct:
            return "rising_wedge"
        return None
    if res_slope_pct < -flat and sup_slope_pct < -flat:
        if abs(res_slope_pct - sup_slope_pct) <= tol:
            return "descending_channel"
        if res_slope_pct < sup_slope_pct:
            return "falling_wedge"
        return None
    return None


def detect_pattern(symbol: str, candles: list[Candle], cfg: Config) -> PatternMatch | None:
    """Detect a chart pattern, trying head-and-shoulders first (a more specific,
    three-extreme shape), then falling back to the channel-pattern (triangle/
    wedge/channel) fit. ``candles`` must be closed candles in ascending order.

    The two detectors use **different** windows on purpose: a multi-week
    head-and-shoulders base needs far more history than a triangle/wedge,
    which typically forms in days - one shared window would either miss slow
    reversals (too short) or blur fast patterns into noise (too long). See
    `hs_lookback_candles`/`hs_swing_window` vs. `pattern_lookback_candles`/
    `pattern_swing_window` in config.yaml.
    """
    hs = _detect_head_and_shoulders_windowed(symbol, candles, cfg)
    if hs is not None:
        return hs
    return _detect_channel_pattern_windowed(symbol, candles, cfg)


def _detect_channel_pattern_windowed(
    symbol: str, candles: list[Candle], cfg: Config
) -> PatternMatch | None:
    n = len(candles)
    win = min(cfg.pattern_lookback_candles, n)
    if win < cfg.pattern_swing_window * 4:
        return None
    window = candles[-win:]
    highs = swing_highs(window, cfg.pattern_swing_window)
    lows = swing_lows(window, cfg.pattern_swing_window)
    return _detect_channel_pattern(symbol, window, highs, lows, cfg)


def _detect_head_and_shoulders_windowed(
    symbol: str, candles: list[Candle], cfg: Config
) -> PatternMatch | None:
    n = len(candles)
    win = min(cfg.hs_lookback_candles, n)
    if win < cfg.hs_swing_window * 4:
        return None
    window = candles[-win:]
    highs = swing_highs(window, cfg.hs_swing_window)
    lows = swing_lows(window, cfg.hs_swing_window)
    return _detect_head_and_shoulders(symbol, window, highs, lows, cfg)


def _detect_channel_pattern(
    symbol: str,
    window: list[Candle],
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    cfg: Config,
) -> PatternMatch | None:
    if len(highs) < cfg.pattern_min_swings or len(lows) < cfg.pattern_min_swings:
        return None

    res_slope, res_intercept, res_r2 = linear_regression(highs)
    sup_slope, sup_intercept, sup_r2 = linear_regression(lows)
    if res_r2 < cfg.pattern_min_r2 or sup_r2 < cfg.pattern_min_r2:
        return None

    last_idx = len(window) - 1
    resistance_now = res_intercept + res_slope * last_idx
    support_now = sup_intercept + sup_slope * last_idx
    if resistance_now <= support_now:
        return None  # lines have already crossed - not a valid channel/triangle

    ref_price = window[0].close or 1.0
    res_slope_pct = res_slope / ref_price * 100.0
    sup_slope_pct = sup_slope / ref_price * 100.0

    pattern = _classify(res_slope_pct, sup_slope_pct, cfg)
    if pattern is None:
        return None

    last = window[-1]
    eps = cfg.pattern_breakout_pct / 100.0
    if last.close > resistance_now * (1.0 + eps):
        status = "breakout_up"
    elif last.close < support_now * (1.0 - eps):
        status = "breakout_down"
    else:
        status = "emerging"

    height = resistance_now - support_now
    target: float | None = None
    if status == "breakout_up":
        target = resistance_now + height
    elif status == "breakout_down":
        target = support_now - height

    note = (
        f"support {sup_slope_pct:+.2f}%/candle (r2 {sup_r2:.2f}), "
        f"resistance {res_slope_pct:+.2f}%/candle (r2 {res_r2:.2f})"
    )

    return PatternMatch(
        symbol=symbol,
        pattern=pattern,
        status=status,
        price=last.close,
        resistance=PatternLine(
            round(res_slope_pct, 4), round(res_r2, 3), resistance_now, res_intercept
        ),
        support=PatternLine(
            round(sup_slope_pct, 4), round(sup_r2, 3), support_now, sup_intercept
        ),
        target=target,
        note=note,
    )


# --------------------------------------------------------------------------- #
# head-and-shoulders: three alternating swing extremes (shoulder/head/
# shoulder) with a neckline fit through the two swings between them.
# --------------------------------------------------------------------------- #
def _neckline_point(
    points: list[tuple[int, float]], lo: int, hi: int, *, pick_max: bool
) -> tuple[int, float] | None:
    """The most prominent point strictly between index ``lo`` and ``hi``."""
    candidates = [p for p in points if lo < p[0] < hi]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p[1]) if pick_max else min(candidates, key=lambda p: p[1])


def _detect_head_and_shoulders(
    symbol: str,
    window: list[Candle],
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    cfg: Config,
) -> PatternMatch | None:
    last_idx = len(window) - 1
    last_close = window[-1].close
    inverse = _try_inverse_hs(symbol, window, highs, lows, cfg, last_idx, last_close)
    if inverse is not None:
        return inverse
    return _try_classic_hs(symbol, window, highs, lows, cfg, last_idx, last_close)


def _try_inverse_hs(
    symbol: str,
    window: list[Candle],
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    cfg: Config,
    last_idx: int,
    last_close: float,
) -> PatternMatch | None:
    """Bullish reversal: shoulder-head-shoulder troughs, neckline through the
    two peaks between them, confirms on a close above the (extended) neckline."""
    if len(lows) < 3:
        return None
    (i0, l0), (i1, l1), (i2, l2) = lows[-3], lows[-2], lows[-1]
    if not (l1 < l0 and l1 < l2):
        return None
    if l1 > min(l0, l2) * (1.0 - cfg.hs_min_head_prominence_pct / 100.0):
        return None  # head isn't clearly deeper than both shoulders
    if abs(l0 - l2) / min(l0, l2) * 100.0 > cfg.hs_shoulder_tolerance_pct:
        return None  # shoulders too uneven to call symmetric

    n0 = _neckline_point(highs, i0, i1, pick_max=True)
    n1 = _neckline_point(highs, i1, i2, pick_max=True)
    if n0 is None or n1 is None:
        return None
    slope, intercept, r2 = linear_regression([n0, n1])
    neckline_now = intercept + slope * last_idx
    if neckline_now <= l1:
        return None  # neckline has to sit above the head to make sense

    ref_price = window[0].close or 1.0
    slope_pct = slope / ref_price * 100.0
    eps = cfg.pattern_breakout_pct / 100.0
    status = "breakout_up" if last_close > neckline_now * (1.0 + eps) else "emerging"
    height = neckline_now - l1
    target = neckline_now + height if status == "breakout_up" else None
    note = f"shoulders {l0:.6g}/{l2:.6g}, head {l1:.6g}, neckline {slope_pct:+.2f}%/candle"

    return PatternMatch(
        symbol=symbol,
        pattern="inverse_head_and_shoulders",
        status=status,
        price=last_close,
        resistance=PatternLine(round(slope_pct, 4), round(r2, 3), neckline_now, intercept),
        support=PatternLine(0.0, 1.0, l1, l1),
        target=target,
        note=note,
    )


def _try_classic_hs(
    symbol: str,
    window: list[Candle],
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    cfg: Config,
    last_idx: int,
    last_close: float,
) -> PatternMatch | None:
    """Bearish reversal: shoulder-head-shoulder peaks, neckline through the
    two troughs between them, confirms on a close below the (extended) neckline."""
    if len(highs) < 3:
        return None
    (i0, h0), (i1, h1), (i2, h2) = highs[-3], highs[-2], highs[-1]
    if not (h1 > h0 and h1 > h2):
        return None
    if h1 < max(h0, h2) * (1.0 + cfg.hs_min_head_prominence_pct / 100.0):
        return None  # head isn't clearly higher than both shoulders
    if abs(h0 - h2) / min(h0, h2) * 100.0 > cfg.hs_shoulder_tolerance_pct:
        return None  # shoulders too uneven to call symmetric

    n0 = _neckline_point(lows, i0, i1, pick_max=False)
    n1 = _neckline_point(lows, i1, i2, pick_max=False)
    if n0 is None or n1 is None:
        return None
    slope, intercept, r2 = linear_regression([n0, n1])
    neckline_now = intercept + slope * last_idx
    if neckline_now >= h1:
        return None  # neckline has to sit below the head to make sense

    ref_price = window[0].close or 1.0
    slope_pct = slope / ref_price * 100.0
    eps = cfg.pattern_breakout_pct / 100.0
    status = "breakout_down" if last_close < neckline_now * (1.0 - eps) else "emerging"
    height = h1 - neckline_now
    target = neckline_now - height if status == "breakout_down" else None
    note = f"shoulders {h0:.6g}/{h2:.6g}, head {h1:.6g}, neckline {slope_pct:+.2f}%/candle"

    return PatternMatch(
        symbol=symbol,
        pattern="head_and_shoulders",
        status=status,
        price=last_close,
        resistance=PatternLine(0.0, 1.0, h1, h1),
        support=PatternLine(round(slope_pct, 4), round(r2, 3), neckline_now, intercept),
        target=target,
        note=note,
    )


# --------------------------------------------------------------------------- #
# one-shot orchestration: fetch the universe, detect patterns, return matches
# --------------------------------------------------------------------------- #
async def run_patterns(cfg: Config, only: str | None = None) -> list[PatternMatch]:
    if only is not None and only not in PATTERNS:
        raise ValueError(f"unknown pattern {only!r} (available: {', '.join(PATTERNS)})")

    now_ms = int(time.time() * 1000)
    end_at = now_ms // 1000
    start_at = end_at - cfg.candle_history * cfg.interval_seconds

    async with KuCoinRestClient(
        cfg.kucoin_base_url, timeout_seconds=cfg.request_timeout_seconds
    ) as client:
        symbols = await client.list_symbols()
        tickers = await client.all_tickers()
        universe = filter_universe(
            symbols,
            {s: t.quote_volume_24h for s, t in tickers.items()},
            {s: t.last for s, t in tickers.items()},
            cfg,
        ).eligible

        sem = asyncio.Semaphore(cfg.max_concurrent_requests)

        async def worker(symbol: str) -> PatternMatch | None:
            async with sem:
                try:
                    rows = await client.candles(
                        symbol,
                        cfg.timeframe,
                        cfg.candle_history,
                        start_at=start_at,
                        end_at=end_at,
                    )
                except KuCoinApiError:
                    return None
            candles = closed_only(parse_kucoin_candles(rows, cfg.interval_seconds, now_ms))
            return detect_pattern(symbol, candles, cfg)

        results: list[Any] = await asyncio.gather(*(worker(s) for s in universe))

    matches = [m for m in results if m is not None and (only is None or m.pattern == only)]
    matches.sort(key=lambda m: m.symbol)
    return matches
