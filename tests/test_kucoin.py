from __future__ import annotations

import pytest

from local_high.kucoin import KuCoinApiError, KuCoinRestClient


async def test_candles_sends_time_range_and_slices():
    client = KuCoinRestClient("https://example.test")
    seen: dict[str, object] = {}

    async def fake_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return {"code": "200000", "data": [[str(i)] * 7 for i in range(10)]}

    client._get = fake_get  # type: ignore[assignment]
    rows = await client.candles("BTC-USDT", "1hour", 5, start_at=1_000, end_at=2_000)

    assert seen["path"] == "/api/v1/market/candles"
    assert seen["params"] == {
        "symbol": "BTC-USDT",
        "type": "1hour",
        "startAt": "1000",
        "endAt": "2000",
    }
    assert len(rows) == 5


async def test_candles_without_range_omits_time_params():
    client = KuCoinRestClient("https://example.test")
    seen: dict[str, object] = {}

    async def fake_get(path, params=None):
        seen["params"] = params
        return {"code": "200000", "data": []}

    client._get = fake_get  # type: ignore[assignment]
    await client.candles("BTC-USDT", "1hour", 5)
    assert "startAt" not in seen["params"]
    assert "endAt" not in seen["params"]


async def test_candles_rejects_non_list_payload():
    client = KuCoinRestClient("https://example.test")

    async def fake_get(path, params=None):
        return {"code": "200000", "data": {"oops": True}}

    client._get = fake_get  # type: ignore[assignment]
    with pytest.raises(KuCoinApiError):
        await client.candles("BTC-USDT", "1hour", 5)
