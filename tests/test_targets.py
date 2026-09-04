from __future__ import annotations

from dataclasses import replace

from conftest import flat, make_candles

from local_high.targets import build_trade_plan, pivot_highs


def _plan(cfg, **over):
    candles = over.pop("candles", make_candles(flat(30, 100.0)))
    kwargs = dict(
        entry=100.0,
        stop=95.0,
        projection_level=98.0,
        base_low=90.0,
        reference_level=98.0,
        candles=candles,
        atr_value=1.0,
        cfg=cfg,
    )
    kwargs.update(over)
    return build_trade_plan(**kwargs)


def test_none_when_stop_above_entry(cfg):
    assert _plan(cfg, entry=100.0, stop=101.0) is None


def test_r_multiple_targets(cfg):
    cfg = replace(
        cfg, target_r_multiples=(2.0, 3.0), target_measured_move=False, target_structure=False
    )
    plan = _plan(cfg, entry=100.0, stop=95.0)  # 1R = 5
    assert plan is not None
    assert plan.risk == 5.0
    assert plan.risk_pct == 5.0
    prices = [t.price for t in plan.targets]
    assert prices == [110.0, 115.0]           # entry + 2R, entry + 3R
    assert plan.targets[0].label == "TP1 (2R)"
    assert plan.rr_last == 3.0


def test_measured_move_target(cfg):
    cfg = replace(cfg, target_r_multiples=(1.0,), target_measured_move=True, target_structure=False)
    # projection_level 120, base_low 100 -> height 20 -> target 140
    plan = _plan(cfg, entry=121.0, stop=118.0, projection_level=120.0, base_low=100.0,
                 reference_level=120.0)
    labels = [t.label for t in plan.targets]
    assert any("measured move" in label for label in labels)
    mm = next(t for t in plan.targets if "measured move" in t.label)
    assert mm.price == 140.0


def test_structure_targets_from_prior_swing_highs(cfg):
    cfg = replace(
        cfg,
        target_r_multiples=(1.0,),
        target_measured_move=False,
        target_structure=True,
        structure_swing_window=2,
        structure_max_targets=2,
    )
    # a clear swing high of 130 in the middle of an otherwise ~100 series
    specs = flat(8, 100.0) + [(130.0, 120.0, 121.0)] + flat(8, 100.0)
    plan = _plan(cfg, entry=105.0, stop=100.0, candles=make_candles(specs))
    prices = [round(t.price, 1) for t in plan.targets]
    assert 130.0 in prices


def test_extended_entry_warning(cfg):
    cfg = replace(
        cfg, extended_entry_warn_pct=8.0, target_structure=False, target_measured_move=False
    )
    plan = _plan(cfg, entry=120.0, stop=113.0, reference_level=100.0)  # 20% above the level
    assert any("boven uitbraakniveau" in n for n in plan.notes)


def test_targets_are_capped_and_sorted(cfg):
    cfg = replace(cfg, target_r_multiples=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0), max_targets=3)
    plan = _plan(cfg, entry=100.0, stop=99.0)
    assert len(plan.targets) == 3
    assert [t.price for t in plan.targets] == sorted(t.price for t in plan.targets)


def test_pivot_highs_finds_local_maxima():
    specs = flat(3, 10.0) + [(20.0, 5.0, 6.0)] + flat(3, 10.0) + [(15.0, 5.0, 6.0)] + flat(3, 10.0)
    highs = pivot_highs(make_candles(specs), 2)
    assert 20.0 in highs
    assert 15.0 in highs
