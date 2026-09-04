from __future__ import annotations

from local_high.config import ScoreWeights


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
