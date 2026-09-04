from __future__ import annotations

from local_high.config import Config, ScoreWeights
from local_high.indicators import Candle, ema, relative_volume
from local_high.patterns import PatternMatch


def _clamp01(value: float, ceiling: float) -> float:
    if ceiling <= 0:
        return 0.0
    return min(max(value, 0.0), ceiling) / ceiling


def confluence_score(
    distance_pct: float,
    rvol: float,
    change_24h_pct: float,
    trend_ok: bool,
    weights: ScoreWeights,
) -> float:
    """0-100 confluence score for a breakout candidate.

    Components (each normalised to 0-1 before weighting):
      * rvol               - breakout-candle volume vs baseline, saturating at 5x
      * change_24h         - 24h move, saturating at +20%
      * breakout_strength  - close above the cleared resistance, saturating at +5%
      * trend              - 1 when the last close is above the trend EMA, else 0
    """
    rvol_c = _clamp01(rvol, 5.0)
    change_c = _clamp01(change_24h_pct, 20.0)
    strength_c = _clamp01(distance_pct, 5.0)
    trend_c = 1.0 if trend_ok else 0.0

    numerator = (
        weights.rvol * rvol_c
        + weights.change_24h * change_c
        + weights.breakout_strength * strength_c
        + weights.trend * trend_c
    )
    return round(100.0 * numerator / weights.total(), 1)


def pattern_confidence(candles: list[Candle], match: PatternMatch, cfg: Config) -> float:
    """0-100 confidence score for a chart-pattern breakout - how 'solid' it looks.

    Components (each normalised to 0-1 before weighting):
      * fit       - average r2 of the two fitted trendlines (already 0-1)
      * rvol      - breakout-candle volume vs baseline, saturating at 5x
      * trend     - 1 when the last close sits on the breakout's side of the trend EMA
      * strength  - how far price cleared the line, as % of pattern height, saturating at 20%
    """
    weights = cfg.pattern_confidence_weights
    fit_c = _clamp01((match.resistance.r2 + match.support.r2) / 2.0, 1.0)

    rvol_c = _clamp01(relative_volume(candles, cfg.rvol_lookback), 5.0)

    closes = [c.close for c in candles]
    trend_ema = ema(closes, cfg.trend_ema_len)
    last_close = candles[-1].close if candles else 0.0
    if trend_ema > 0 and match.status == "breakout_up":
        trend_c = 1.0 if last_close > trend_ema else 0.0
    elif trend_ema > 0 and match.status == "breakout_down":
        trend_c = 1.0 if last_close < trend_ema else 0.0
    else:
        trend_c = 0.0

    height = match.resistance.value_now - match.support.value_now
    if height > 0 and match.status == "breakout_up":
        strength_pct = (match.price - match.resistance.value_now) / height * 100.0
    elif height > 0 and match.status == "breakout_down":
        strength_pct = (match.support.value_now - match.price) / height * 100.0
    else:
        strength_pct = 0.0
    strength_c = _clamp01(strength_pct, 20.0)

    numerator = (
        weights.fit * fit_c
        + weights.rvol * rvol_c
        + weights.trend * trend_c
        + weights.strength * strength_c
    )
    return round(100.0 * numerator / weights.total(), 1)
