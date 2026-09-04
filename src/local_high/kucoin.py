from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

import aiohttp

from local_high import __version__
from local_high.universe import SymbolInfo

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_RETRIES = 4


class KuCoinApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last: float
    quote_volume_24h: float
    change_rate_24h: float   # fraction, e.g. 0.083 == +8.3%
    high_24h: float
    low_24h: float
    timestamp_ms: int


class KuCoinRestClient:
    def __init__(
        self,
        base_url: str = "https://api.kucoin.com",
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout_seconds = timeout_seconds

    async def __aenter__(self) -> KuCoinRestClient:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                headers={"User-Agent": f"local-high-scanner/{__version__} research-only"},
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("KuCoinRestClient must be used as an async context manager")
        last_error: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                async with self._session.get(f"{self._base_url}{path}", params=params) as resp:
                    if resp.status in _TRANSIENT_STATUS:
                        raise KuCoinApiError(f"transient HTTP {resp.status} for {path}")
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        raise KuCoinApiError(f"HTTP {resp.status} for {path}: {body}")
                    payload = await resp.json(content_type=None)
                if not isinstance(payload, dict) or payload.get("code") != "200000":
                    msg = (
                        payload.get("msg", payload.get("code"))
                        if isinstance(payload, dict)
                        else "?"
                    )
                    raise KuCoinApiError(f"KuCoin error for {path}: {msg}")
                return payload
            except (aiohttp.ClientError, TimeoutError, KuCoinApiError) as exc:
                last_error = exc
                if isinstance(exc, KuCoinApiError) and "transient" not in str(exc):
                    raise
                if attempt < _RETRIES - 1:
                    await asyncio.sleep(0.5 * 2**attempt + random.random() * 0.25)
        raise KuCoinApiError(f"request failed for {path}: {last_error}") from last_error

    async def list_symbols(self) -> list[SymbolInfo]:
        payload = await self._get("/api/v2/symbols")
        data = payload.get("data")
        if not isinstance(data, list):
            raise KuCoinApiError("invalid symbols response")
        out: list[SymbolInfo] = []
        for row in data:
            try:
                out.append(
                    SymbolInfo(
                        symbol=str(row["symbol"]).upper(),
                        base_currency=str(row["baseCurrency"]).upper(),
                        quote_currency=str(row["quoteCurrency"]).upper(),
                        enabled=bool(row.get("enableTrading", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def all_tickers(self) -> dict[str, Ticker]:
        payload = await self._get("/api/v1/market/allTickers")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KuCoinApiError("invalid tickers response")
        timestamp = int(data.get("time", 0))
        out: dict[str, Ticker] = {}
        for row in data.get("ticker", []):
            try:
                out[str(row["symbol"]).upper()] = Ticker(
                    symbol=str(row["symbol"]).upper(),
                    last=float(row["last"]),
                    quote_volume_24h=float(row["volValue"]),
                    change_rate_24h=float(row.get("changeRate") or 0.0),
                    high_24h=float(row.get("high") or 0.0),
                    low_24h=float(row.get("low") or 0.0),
                    timestamp_ms=timestamp,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_at: int | None = None,
        end_at: int | None = None,
    ) -> list[list[str]]:
        """Fetch candles. Without ``start_at``/``end_at`` KuCoin returns only a short
        default window (~100 rows), so callers that need history must pass the range
        (epoch seconds)."""
        params = {"symbol": symbol, "type": interval}
        if start_at is not None:
            params["startAt"] = str(int(start_at))
        if end_at is not None:
            params["endAt"] = str(int(end_at))
        payload = await self._get("/api/v1/market/candles", params)
        data = payload.get("data")
        if not isinstance(data, list):
            raise KuCoinApiError(f"invalid candles response for {symbol}")
        return data[:limit]
