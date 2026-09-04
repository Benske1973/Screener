from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from local_high.config import Config
from local_high.indicators import Candle


@dataclass(frozen=True, slots=True)
class Target:
    label: str        # "TP1 (structure)", "TP2 (3R)", ...
    price: float
    rr: float          # (price - entry) / initial risk
    gain_pct: float     # (price / entry - 1) * 100


@dataclass(frozen=True, slots=True)
class TradePlan:
    entry: float
    stop: float
    risk: float             # entry - stop (absolute, > 0)
    risk_pct: float          # risk / entry * 100
    extended_pct: float      # how far entry sits above the broken level
    targets: tuple[Target, ...]
    notes: tuple[str, ...]

    @property
    def rr_last(self) -> float:
        return self.targets[-1].rr if self.targets else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["targets"] = [asdict(t) for t in self.targets]
        return data


def pivot_highs(candles: list[Candle], window: int) -> list[float]:
    """Swing-high prices: a candle whose high tops every high within ``window`` bars each side."""
    out: list[float] = []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i].high
        if all(h >= candles[j].high for j in range(i - window, i)) and all(
            h > candles[j].high for j in range(i + 1, i + 1 + window)
        ):
            out.append(h)
    return out


def _dedupe_ascending(targets: list[Target], tol: float) -> list[Target]:
    """Merge targets closer than ``tol``, preferring a named method over a bare R multiple."""
    merged: list[Target] = []
    for t in sorted(targets, key=lambda x: x.price):
        if merged and t.price - merged[-1].price <= tol:
            prev = merged[-1]
            prev_is_r = prev.label.endswith("R)")
            cur_is_r = t.label.endswith("R)")
            if prev_is_r and not cur_is_r:
                merged[-1] = t
            continue
        merged.append(t)
    return merged


def build_trade_plan(
    *,
    entry: float,
    stop: float,
    projection_level: float,
    base_low: float,
    reference_level: float,
    candles: list[Candle],
    atr_value: float,
    cfg: Config,
) -> TradePlan | None:
    risk = entry - stop
    if risk <= 0 or entry <= 0:
        return None

    risk_pct = risk / entry * 100.0
    extended_pct = (entry / reference_level - 1.0) * 100.0 if reference_level > 0 else 0.0
    tol = max(atr_value * 0.4, entry * 0.004)

    raw: list[Target] = []

    for m in cfg.target_r_multiples:
        price = entry + m * risk
        raw.append(Target(f"({m:g}R)", price, m, (price / entry - 1.0) * 100.0))

    if cfg.target_measured_move and 0 < base_low < projection_level:
        price = projection_level + (projection_level - base_low)
        if price > entry + tol:
            gain = (price / entry - 1.0) * 100.0
            raw.append(Target("(measured move)", price, (price - entry) / risk, gain))

    if cfg.target_structure and cfg.structure_max_targets > 0:
        above = sorted(
            h for h in pivot_highs(candles, cfg.structure_swing_window) if h > entry + tol
        )
        picked: list[float] = []
        for h in above:
            if picked and h - picked[-1] <= tol:
                continue
            picked.append(h)
            if len(picked) >= cfg.structure_max_targets:
                break
        for h in picked:
            raw.append(Target("(resistance)", h, (h - entry) / risk, (h / entry - 1.0) * 100.0))

    merged = _dedupe_ascending(raw, tol)[: cfg.max_targets]
    targets = tuple(
        Target(
            f"TP{i + 1} {t.label}",
            round(t.price, 10),
            round(t.rr, 2),
            round(t.gain_pct, 2),
        )
        for i, t in enumerate(merged)
    )

    notes: list[str] = []
    if extended_pct >= cfg.extended_entry_warn_pct:
        notes.append(
            f"entry {extended_pct:.0f}% boven uitbraakniveau — een pullback-entry geeft betere R:R"
        )
    if cfg.breakeven_after_target and len(targets) >= cfg.breakeven_after_target:
        n = cfg.breakeven_after_target
        notes.append(
            f"na TP{n}: stop naar entry (breakeven), daarna trail onder elke hogere pullback-low"
        )

    return TradePlan(
        entry=round(entry, 10),
        stop=round(stop, 10),
        risk=round(risk, 10),
        risk_pct=round(risk_pct, 2),
        extended_pct=round(extended_pct, 2),
        targets=targets,
        notes=tuple(notes),
    )
