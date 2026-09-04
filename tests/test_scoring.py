from __future__ import annotations

from local_high.config import ScoreWeights
from local_high.scoring import confluence_score


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
