from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from local_high.config import Config
from local_high.indicators import (
    Candle,
    bollinger_bands,
    closed_only,
    ema,
    ema_series,
    macd,
    parse_kucoin_candles,
    relative_volume,
    rsi,
    window_extreme,
)
from local_high.kucoin import KuCoinApiError, KuCoinRestClient
from local_high.universe import filter_universe

# ----------------------------------------------------------------------------
# altFINS-style "preset filter" screens: each is a pure function over one
# symbol's closed candles, independent of the New Local High state machine in
# strategy.py. No persistent state, no alerts - just "does this symbol match
# right now".
# ----------------------------------------------------------------------------


class ScreenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenMatch:
    symbol: str
    screen: str
    price: float
    detail: str
    metrics: dict[str, float]


ScreenFn = Callable[[str, list[Candle], Config], "ScreenMatch | None"]


@dataclass(frozen=True, slots=True)
class ScreenDef:
    name: str
    label: str
    description: str
    fn: ScreenFn


# --------------------------------------------------------------------------- #
# individual screens
# --------------------------------------------------------------------------- #
def _screen_new_local_high(symbol: str, candles: list[Candle], cfg: Config) -> ScreenMatch | None:
    lb = cfg.screen_lookback
    if len(candles) < lb + 1:
        return None
    last = candles[-1]
    prior_high = window_extreme(candles, len(candles) - 1, lb, "high")
    if prior_high <= 0 or last.close <= prior_high:
        return None
    pct = (last.close / prior_high - 1.0) * 100.0
    return ScreenMatch(
        symbol,
        "new_local_high",
        last.close,
        f"close {pct:+.2f}% above the {lb}-candle high ({prior_high:.8g})",
        {"distance_pct": pct, "level": prior_high},
    )


def _screen_new_local_low(symbol: str, candles: list[Candle], cfg: Config) -> ScreenMatch | None:
    lb = cfg.screen_lookback
    if len(candles) < lb + 1:
        return None
    last = candles[-1]
    start = len(candles) - 1 - lb
    prior_low = min(c.low for c in candles[start : len(candles) - 1])
    if last.close >= prior_low:
        return None
    pct = (last.close / prior_low - 1.0) * 100.0
    return ScreenMatch(
        symbol,
        "new_local_low",
        last.close,
        f"close {pct:.2f}% below the {lb}-candle low ({prior_low:.8g})",
        {"distance_pct": pct, "level": prior_low},
    )


def _trend_emas(candles: list[Candle], cfg: Config) -> tuple[float, float]:
    closes = [c.close for c in candles]
    return ema(closes, cfg.screen_trend_fast_len), ema(closes, cfg.screen_trend_slow_len)


def _screen_strong_uptrend(symbol: str, candles: list[Candle], cfg: Config) -> ScreenMatch | None:
    need = max(cfg.screen_trend_fast_len, cfg.screen_trend_slow_len) + 1
    if len(candles) < need:
        return None
    fast, slow = _trend_emas(candles, cfg)
    last = candles[-1]
    if not (last.close > fast > slow > 0):
        return None
    spread_pct = (fast / slow - 1.0) * 100.0
    return ScreenMatch(
        symbol,
        "strong_uptrend",
        last.close,
        f"close above EMA{cfg.screen_trend_fast_len} above EMA{cfg.screen_trend_slow_len} "
        f"(+{spread_pct:.2f}%)",
        {"ema_fast": fast, "ema_slow": slow, "spread_pct": spread_pct},
    )


def _screen_pullback_in_uptrend(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    need = max(cfg.screen_trend_slow_len, cfg.screen_pullback_lookback) + 1
    if len(candles) < need:
        return None
    fast, slow = _trend_emas(candles, cfg)
    last = candles[-1]
    if not (fast > slow > 0 and last.close > slow):
        return None
    recent_high = max(c.high for c in candles[-cfg.screen_pullback_lookback :])
    pullback_pct = (recent_high / last.close - 1.0) * 100.0
    if pullback_pct < cfg.screen_pullback_min_pct:
        return None
    return ScreenMatch(
        symbol,
        "pullback_in_uptrend",
        last.close,
        f"-{pullback_pct:.2f}% off the {cfg.screen_pullback_lookback}-candle high, trend stays up",
        {"pullback_pct": pullback_pct},
    )


def _screen_very_oversold(symbol: str, candles: list[Candle], cfg: Config) -> ScreenMatch | None:
    if len(candles) < cfg.screen_rsi_len + 1:
        return None
    closes = [c.close for c in candles]
    value = rsi(closes, cfg.screen_rsi_len)
    if value > cfg.screen_rsi_oversold:
        return None
    return ScreenMatch(
        symbol,
        "very_oversold",
        candles[-1].close,
        f"RSI({cfg.screen_rsi_len}) {value:.1f} <= {cfg.screen_rsi_oversold:.0f}",
        {"rsi": value},
    )


def _screen_oversold_in_uptrend(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    need = max(cfg.screen_trend_slow_len, cfg.screen_rsi_len) + 1
    if len(candles) < need:
        return None
    closes = [c.close for c in candles]
    slow = ema(closes, cfg.screen_trend_slow_len)
    last = candles[-1]
    if slow <= 0 or last.close <= slow:
        return None
    value = rsi(closes, cfg.screen_rsi_len)
    if value > cfg.screen_rsi_oversold_mild:
        return None
    return ScreenMatch(
        symbol,
        "oversold_in_uptrend",
        last.close,
        f"RSI({cfg.screen_rsi_len}) {value:.1f} while close stays above "
        f"EMA{cfg.screen_trend_slow_len}",
        {"rsi": value},
    )


def _screen_bullish_ema_crossover(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    need = max(cfg.screen_trend_fast_len, cfg.screen_trend_slow_len) + 2
    if len(candles) < need:
        return None
    closes = [c.close for c in candles]
    fast_series = ema_series(closes, cfg.screen_trend_fast_len)
    slow_series = ema_series(closes, cfg.screen_trend_slow_len)
    if fast_series[-2] > slow_series[-2] or fast_series[-1] <= slow_series[-1]:
        return None
    return ScreenMatch(
        symbol,
        "bullish_ema_crossover",
        candles[-1].close,
        f"EMA{cfg.screen_trend_fast_len} crossed above EMA{cfg.screen_trend_slow_len} this candle",
        {"ema_fast": fast_series[-1], "ema_slow": slow_series[-1]},
    )


def _screen_bullish_macd_crossover(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    need = cfg.screen_macd_slow + cfg.screen_macd_signal + 2
    if len(candles) < need:
        return None
    closes = [c.close for c in candles]
    macd_line, signal_line, _ = macd(
        closes, cfg.screen_macd_fast, cfg.screen_macd_slow, cfg.screen_macd_signal
    )
    if macd_line[-2] > signal_line[-2] or macd_line[-1] <= signal_line[-1]:
        return None
    return ScreenMatch(
        symbol,
        "bullish_macd_crossover",
        candles[-1].close,
        "MACD line crossed above the signal line this candle",
        {"macd": macd_line[-1], "signal": signal_line[-1]},
    )


def _screen_bollinger_breakout(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    if len(candles) < cfg.screen_bb_len + 1:
        return None
    closes = [c.close for c in candles]
    _, upper, _ = bollinger_bands(closes, cfg.screen_bb_len, cfg.screen_bb_stdev)
    last = candles[-1]
    if upper <= 0 or last.close <= upper:
        return None
    pct = (last.close / upper - 1.0) * 100.0
    return ScreenMatch(
        symbol,
        "bollinger_breakout",
        last.close,
        f"close {pct:+.2f}% above the upper Bollinger Band "
        f"({cfg.screen_bb_len}, {cfg.screen_bb_stdev:g}σ)",
        {"upper_band": upper, "distance_pct": pct},
    )


def _screen_rvol_spike_in_uptrend(
    symbol: str, candles: list[Candle], cfg: Config
) -> ScreenMatch | None:
    need = max(cfg.rvol_lookback, cfg.screen_trend_slow_len) + 1
    if len(candles) < need:
        return None
    closes = [c.close for c in candles]
    slow = ema(closes, cfg.screen_trend_slow_len)
    last = candles[-1]
    if slow <= 0 or last.close <= slow:
        return None
    rv = relative_volume(candles, cfg.rvol_lookback)
    if rv < cfg.screen_min_rvol:
        return None
    return ScreenMatch(
        symbol,
        "rvol_spike_in_uptrend",
        last.close,
        f"RVOL {rv:.2f}x while in an uptrend (close above EMA{cfg.screen_trend_slow_len})",
        {"rvol": rv},
    )


SCREENS: dict[str, ScreenDef] = {
    d.name: d
    for d in (
        ScreenDef(
            "new_local_high",
            "New Local High",
            "Close breaks the highest high of the prior `screen_lookback` candles.",
            _screen_new_local_high,
        ),
        ScreenDef(
            "new_local_low",
            "New Local Low",
            "Close breaks the lowest low of the prior `screen_lookback` candles.",
            _screen_new_local_low,
        ),
        ScreenDef(
            "strong_uptrend",
            "Strong Uptrend",
            "Close above the fast EMA, fast EMA above the slow EMA.",
            _screen_strong_uptrend,
        ),
        ScreenDef(
            "pullback_in_uptrend",
            "Pullback in Uptrend",
            "Uptrend intact, but price has pulled back from its recent high.",
            _screen_pullback_in_uptrend,
        ),
        ScreenDef(
            "very_oversold",
            "Very Oversold",
            "RSI at or below the oversold threshold.",
            _screen_very_oversold,
        ),
        ScreenDef(
            "oversold_in_uptrend",
            "Oversold in Uptrend",
            "Mildly oversold RSI while price still trades above the slow EMA.",
            _screen_oversold_in_uptrend,
        ),
        ScreenDef(
            "bullish_ema_crossover",
            "Bullish EMA Crossover",
            "Fast EMA just crossed above the slow EMA.",
            _screen_bullish_ema_crossover,
        ),
        ScreenDef(
            "bullish_macd_crossover",
            "Bullish MACD Crossover",
            "MACD line just crossed above its signal line.",
            _screen_bullish_macd_crossover,
        ),
        ScreenDef(
            "bollinger_breakout",
            "Bollinger Band Breakout",
            "Close broke above the upper Bollinger Band.",
            _screen_bollinger_breakout,
        ),
        ScreenDef(
            "rvol_spike_in_uptrend",
            "RVOL Spike in Uptrend",
            "Relative volume spike while price trades above the slow EMA.",
            _screen_rvol_spike_in_uptrend,
        ),
    )
}


# --------------------------------------------------------------------------- #
# one-shot orchestration: fetch the universe, run one screen, return matches
# --------------------------------------------------------------------------- #
async def run_screen(cfg: Config, name: str) -> list[ScreenMatch]:
    screen = SCREENS.get(name)
    if screen is None:
        raise ScreenError(
            f"unknown screen {name!r} (available: {', '.join(sorted(SCREENS))})"
        )

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

        async def worker(symbol: str) -> ScreenMatch | None:
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
            return screen.fn(symbol, candles, cfg)

        results: list[Any] = await asyncio.gather(*(worker(s) for s in universe))

    matches = [r for r in results if r is not None]
    matches.sort(key=lambda m: m.symbol)
    return matches
