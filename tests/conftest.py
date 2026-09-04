from __future__ import annotations

from dataclasses import replace

import pytest

from local_high.config import Config, ScoreWeights
from local_high.indicators import Candle
from local_high.kucoin import Ticker

INTERVAL_S = 14_400
START_MS = 1_600_000_000_000


@pytest.fixture
def cfg() -> Config:
    return Config(
        timeframe="4hour",
        candle_history=200,
        lookbacks=(10,),
        breakout_source="close",
        prior_high_source="high",
        min_breakout_pct=0.0,
        require_fresh=True,
        near_high_pct=1.0,
        rvol_lookback=5,
        trend_ema_len=5,
        atr_len=5,
        min_rvol=0.0,
        score_weights=ScoreWeights(),
        track_rebreak=True,
        pullback_retest_pct=0.5,
        pullback_max_candles=6,
        invalidate_pct=1.0,
        rebreak_reset_candles=3,
        stop_buffer_pct=0.5,
    )


def make_candles(
    specs: list[tuple[float, float, float]], *, start_ms: int = START_MS
) -> list[Candle]:
    """specs: list of (high, low, close). open == previous close, volume flat."""
    out: list[Candle] = []
    t = start_ms
    prev_close = specs[0][2]
    for high, low, close in specs:
        out.append(
            Candle(
                open_time_ms=t,
                open=prev_close,
                high=high,
                low=low,
                close=close,
                volume=100.0,
                quote_volume=100.0 * close,
                closed=True,
            )
        )
        prev_close = close
        t += INTERVAL_S * 1000
    return out


def flat(n: int, price: float = 100.0) -> list[tuple[float, float, float]]:
    return [(price, price - 1.0, price) for _ in range(n)]


def ticker(symbol: str = "AAA-USDT", *, change: float = 0.05) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=100.0,
        quote_volume_24h=5_000_000.0,
        change_rate_24h=change,
        high_24h=110.0,
        low_24h=90.0,
        timestamp_ms=START_MS,
    )


@pytest.fixture
def with_min_breakout(cfg: Config) -> Config:
    return replace(cfg, min_breakout_pct=0.1)
