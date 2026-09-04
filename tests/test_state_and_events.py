from __future__ import annotations

import csv

from local_high.events import EventLog
from local_high.state import StateStore
from local_high.strategy import BREAKOUT, IDLE, Event, SymbolState


def test_state_store_round_trip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.load()
    st = SymbolState(
        symbol="AAA-USDT", state=BREAKOUT, breakout_level=1.23, alerts={"BREAKOUT": 42}
    )
    store.put(st)
    store.save()

    reopened = StateStore(path)
    reopened.load()
    got = reopened.get("AAA-USDT")
    assert got is not None
    assert got.state == BREAKOUT
    assert got.breakout_level == 1.23
    assert got.alerts == {"BREAKOUT": 42}


def test_state_store_prune_keeps_active(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.load()
    store.put(SymbolState(symbol="OLD-USDT", state=IDLE))
    store.put(SymbolState(symbol="GONE-USDT", state=BREAKOUT))
    store.prune(keep={"STILL-USDT"})
    assert store.get("OLD-USDT") is None       # idle + not in universe -> dropped
    assert store.get("GONE-USDT") is not None   # active -> kept even if delisted


def test_state_store_meta_round_trip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.load()
    assert store.get_meta("last_heartbeat_ts", 0) == 0  # default when unset
    store.set_meta("last_heartbeat_ts", 1_700_000_000)
    store.save()

    reopened = StateStore(path)
    reopened.load()
    assert reopened.get_meta("last_heartbeat_ts", 0) == 1_700_000_000


def test_state_store_survives_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    store = StateStore(path)
    store.load()  # must not raise
    assert store.get("ANY") is None


def _event(kind: str, plan=None) -> Event:
    return Event(
        symbol="AAA-USDT",
        kind=kind,
        candle_ms=1_600_000_000_000,
        price=1.2345,
        level=1.2,
        stop=1.19,
        matched_lookbacks=(30, 90),
        rvol=2.4,
        change_24h_pct=8.1,
        distance_pct=1.5,
        atr=0.03,
        score=72.0,
        seeded=False,
        note="hi",
        plan=plan,
    )


def test_event_log_writes_header_once(tmp_path):
    path = tmp_path / "events.csv"
    log = EventLog(path, "4hour")
    log.append(_event(BREAKOUT), now_ms=1_600_000_100_000)
    log.append(_event("REBREAK"), now_ms=1_600_000_200_000)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAA-USDT"
    assert rows[0]["matched_lookbacks"] == "30|90"
    assert rows[0]["timeframe"] == "4hour"
    assert rows[0]["tp1"] == ""            # no plan on this event
    assert rows[1]["kind"] == "REBREAK"


def test_event_log_serialises_trade_plan(tmp_path):
    from local_high.targets import Target, TradePlan

    plan = TradePlan(
        entry=1.2345,
        stop=1.19,
        risk=0.0445,
        risk_pct=3.6,
        extended_pct=1.2,
        targets=(
            Target("TP1 (2R)", 1.323, 2.0, 7.2),
            Target("TP2 (3R)", 1.368, 3.0, 10.8),
        ),
        notes=(),
    )
    path = tmp_path / "events.csv"
    EventLog(path, "4hour").append(_event(BREAKOUT, plan=plan), now_ms=1_600_000_100_000)
    row = next(csv.DictReader(path.open(encoding="utf-8")))
    assert row["risk_pct"] == "3.60"
    assert row["tp1"] == "1.323"
    assert row["tp2"] == "1.368"
    assert row["tp3"] == ""
    assert row["rr_last"] == "3.00"
