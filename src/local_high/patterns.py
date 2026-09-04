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
# Rule-based chart-pattern detection, altFINS-style: fit a trendline through
# recent swing highs (resistance) and swing lows (support), classify the pair
# by slope, and report whether price is still trading between the lines
# ("emerging") or has just cleared one of them ("breakout_up"/"breakout_down").
#
# Covers triangles, wedges and channels - the geometrically well-defined
# patterns. Head-and-shoulders / double top-bottom need a separate detector
# (peak/trough counting, not a two-line fit) and are intentionally out of
# scope here.
# ----------------------------------------------------------------------------

PATTERNS = (
    "ascending_triangle",
    "descending_triangle",
    "rising_wedge",
    "falling_wedge",
    "ascending_channel",
    "descending_channel",
)


@dataclass(frozen=True, slots=True)
class PatternLine:
    slope_pct: float   # % price change per candle, relative to the window's first close
    r2: float           # goodness of fit, 0-1
    value_now: float     # the line's value at the last candle


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
    """Fit support/resistance over the trailing `pattern_lookback_candles` and classify.

    ``candles`` must be closed candles in ascending time order.
    """
    n = len(candles)
    win = min(cfg.pattern_lookback_candles, n)
    if win < cfg.pattern_swing_window * 4:
        return None
    window = candles[-win:]

    highs = swing_highs(window, cfg.pattern_swing_window)
    lows = swing_lows(window, cfg.pattern_swing_window)
    if len(highs) < cfg.pattern_min_swings or len(lows) < cfg.pattern_min_swings:
        return None

    res_slope, res_intercept, res_r2 = linear_regression(highs)
    sup_slope, sup_intercept, sup_r2 = linear_regression(lows)
    if res_r2 < cfg.pattern_min_r2 or sup_r2 < cfg.pattern_min_r2:
        return None

    last_idx = win - 1
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

    last = candles[-1]
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
        resistance=PatternLine(round(res_slope_pct, 4), round(res_r2, 3), resistance_now),
        support=PatternLine(round(sup_slope_pct, 4), round(sup_r2, 3), support_now),
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
