from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from local_high.config import Config
from local_high.webapp import WebState, build_app


def _cfg() -> Config:
    return Config(candle_history=200)


async def test_index_serves_the_dashboard_shell():
    app = build_app(_cfg(), web_state=WebState(), start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "local-high-scanner" in text
        assert "New Local High" in text
        assert "__REFRESH_MS__" not in text  # template placeholders got substituted
        assert "__SCREENS_JSON__" not in text


async def test_state_endpoint_serves_the_injected_snapshot():
    state = WebState()
    state.updated_utc = "2026-01-01T00:00:00Z"
    state.universe_size = 3
    state.nlh_active = [
        {
            "symbol": "AAA-USDT",
            "state": "BREAKOUT",
            "price": 1.0,
            "distance_pct": 1.0,
            "rvol": 2.0,
            "change_24h_pct": 3.0,
            "score": 80.0,
        }
    ]
    state.screens["new_local_high"] = [
        {"symbol": "AAA-USDT", "price": 1.0, "detail": "fresh high", "metrics": {}}
    ]
    app = build_app(_cfg(), web_state=state, start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["universe_size"] == 3
        assert data["updated_utc"] == "2026-01-01T00:00:00Z"
        assert data["nlh_active"][0]["symbol"] == "AAA-USDT"
        assert data["screens"]["new_local_high"][0]["detail"] == "fresh high"
        assert data["error"] is None


async def test_state_endpoint_surfaces_the_last_error_without_dropping_data():
    state = WebState()
    state.nlh_active = [{"symbol": "AAA-USDT", "state": "BREAKOUT", "price": 1.0,
                          "distance_pct": 1.0, "rvol": 2.0, "change_24h_pct": 3.0, "score": 80.0}]
    state.error = "transient HTTP 503 for /api/v1/market/candles"
    app = build_app(_cfg(), web_state=state, start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get("/api/state")).json()
        assert data["error"] == "transient HTTP 503 for /api/v1/market/candles"
        assert data["nlh_active"]  # stale-but-good data is still served


async def test_auth_middleware_blocks_without_a_token(monkeypatch):
    monkeypatch.setenv("LOCAL_HIGH_WEB_TOKEN", "secret123")
    app = build_app(_cfg(), web_state=WebState(), start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/state")
        assert resp.status == 401

        resp2 = await client.get("/api/state", params={"token": "secret123"})
        assert resp2.status == 200

        resp3 = await client.get("/api/state", params={"token": "wrong"})
        assert resp3.status == 401


async def test_auth_middleware_accepts_a_bearer_header(monkeypatch):
    monkeypatch.setenv("LOCAL_HIGH_WEB_TOKEN", "secret123")
    app = build_app(_cfg(), web_state=WebState(), start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/state", headers={"Authorization": "Bearer secret123"}
        )
        assert resp.status == 200


async def test_candles_endpoint_serves_the_cached_window():
    state = WebState()
    state.candles["AAA-USDT"] = [
        {"t": 1000, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1},
        {"t": 2000, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2},
    ]
    app = build_app(_cfg(), web_state=state, start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/candles/AAA-USDT")
        assert resp.status == 200
        data = await resp.json()
        assert data["symbol"] == "AAA-USDT"
        assert len(data["candles"]) == 2
        assert data["candles"][0]["c"] == 1.1


async def test_candles_endpoint_404s_for_an_unmatched_symbol():
    app = build_app(_cfg(), web_state=WebState(), start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/candles/ZZZ-USDT")
        assert resp.status == 404


async def test_no_token_configured_means_open_access(monkeypatch):
    monkeypatch.delenv("LOCAL_HIGH_WEB_TOKEN", raising=False)
    app = build_app(_cfg(), web_state=WebState(), start_refresh=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/state")
        assert resp.status == 200
