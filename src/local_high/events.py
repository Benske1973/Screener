from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from local_high.strategy import Event

_FIELDS = [
    "timestamp_utc",
    "symbol",
    "kind",
    "timeframe",
    "candle_utc",
    "price",
    "level",
    "stop",
    "matched_lookbacks",
    "rvol",
    "change_24h_pct",
    "distance_pct",
    "atr",
    "score",
    "seeded",
    "risk_pct",
    "tp1",
    "tp2",
    "tp3",
    "rr_last",
    "note",
]


def _iso(ms: int) -> str:
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    def __init__(self, path: str | Path, timeframe: str) -> None:
        self.path = Path(path)
        self.timeframe = timeframe

    def append(self, event: Event, *, now_ms: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        plan = event.plan
        tps = list(plan.targets) if plan else []
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp_utc": _iso(now_ms),
                    "symbol": event.symbol,
                    "kind": event.kind,
                    "timeframe": self.timeframe,
                    "candle_utc": _iso(event.candle_ms),
                    "price": f"{event.price:.10g}",
                    "level": f"{event.level:.10g}",
                    "stop": f"{event.stop:.10g}" if event.stop else "",
                    "matched_lookbacks": "|".join(str(x) for x in event.matched_lookbacks),
                    "rvol": f"{event.rvol:.3f}",
                    "change_24h_pct": f"{event.change_24h_pct:.2f}",
                    "distance_pct": f"{event.distance_pct:.3f}",
                    "atr": f"{event.atr:.10g}",
                    "score": f"{event.score:.1f}",
                    "seeded": "1" if event.seeded else "0",
                    "risk_pct": f"{plan.risk_pct:.2f}" if plan else "",
                    "tp1": f"{tps[0].price:.10g}" if len(tps) > 0 else "",
                    "tp2": f"{tps[1].price:.10g}" if len(tps) > 1 else "",
                    "tp3": f"{tps[2].price:.10g}" if len(tps) > 2 else "",
                    "rr_last": f"{plan.rr_last:.2f}" if plan else "",
                    "note": event.note,
                }
            )
