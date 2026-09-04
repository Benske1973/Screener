from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from local_high.strategy import IDLE, SymbolState


class StateStore:
    """Per-symbol state machine snapshots persisted as a single JSON file.

    Also holds a small ``meta`` dict for scanner-wide bookkeeping that isn't
    per-symbol (e.g. the heartbeat's last-sent timestamp), so it survives a
    restart the same way the per-symbol state does.

    Writes are atomic (temp file + ``os.replace``) so an interrupted run cannot
    leave a truncated store behind.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._states: dict[str, SymbolState] = {}
        self._meta: dict[str, Any] = {}

    def load(self) -> None:
        self._states = {}
        self._meta = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for symbol, payload in (raw.get("symbols") or {}).items():
            if isinstance(payload, dict):
                payload.setdefault("symbol", symbol)
                self._states[symbol] = SymbolState.from_dict(payload)
        meta = raw.get("meta")
        if isinstance(meta, dict):
            self._meta = meta

    def get(self, symbol: str) -> SymbolState | None:
        return self._states.get(symbol)

    def put(self, state: SymbolState) -> None:
        self._states[state.symbol] = state

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: Any) -> None:
        self._meta[key] = value

    def prune(self, keep: set[str]) -> None:
        """Drop IDLE states for symbols no longer in the universe."""
        for symbol in list(self._states):
            if symbol not in keep and self._states[symbol].state == IDLE:
                del self._states[symbol]

    def active_count(self) -> int:
        return sum(1 for s in self._states.values() if s.state != IDLE)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "meta": self._meta,
            "symbols": {sym: st.to_dict() for sym, st in sorted(self._states.items())},
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
