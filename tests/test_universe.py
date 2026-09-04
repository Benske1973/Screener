from __future__ import annotations

from dataclasses import replace

from local_high.universe import SymbolInfo, filter_universe


def _sym(symbol: str, *, enabled: bool = True) -> SymbolInfo:
    base, quote = symbol.split("-")
    return SymbolInfo(symbol=symbol, base_currency=base, quote_currency=quote, enabled=enabled)


def test_filter_universe_applies_all_gates(cfg):
    symbols = [
        _sym("BTC-USDT"),
        _sym("ETH-USDT"),
        _sym("DEAD-USDT", enabled=False),
        _sym("USDC-USDT"),          # stable base
        _sym("ETH3L-USDT"),          # leveraged token
        _sym("XMR-BTC"),             # non-USDT quote
        _sym("THIN-USDT"),           # fails liquidity
        _sym("CHEAP-USDT"),          # fails min price
    ]
    quote_volume = {
        "BTC-USDT": 9e9,
        "ETH-USDT": 5e9,
        "USDC-USDT": 9e9,
        "ETH3L-USDT": 9e9,
        "THIN-USDT": 1_000.0,
        "CHEAP-USDT": 9e9,
    }
    last_price = {s: 10.0 for s in quote_volume}
    last_price["CHEAP-USDT"] = 1e-12

    result = filter_universe(symbols, quote_volume, last_price, cfg)

    assert result.eligible == ["BTC-USDT", "ETH-USDT"]
    assert result.rejected["DEAD-USDT"] == "SUSPENDED"
    assert result.rejected["USDC-USDT"] == "STABLE_BASE"
    assert result.rejected["ETH3L-USDT"] == "LEVERAGED_TOKEN"
    assert result.rejected["XMR-BTC"] == "NOT_USDT_SPOT"
    assert result.rejected["THIN-USDT"] == "THIN_LIQUIDITY"
    assert result.rejected["CHEAP-USDT"] == "PRICE_TOO_LOW"


def test_allowlist_restricts_scope(cfg):
    cfg = replace(cfg, symbol_allowlist=("ETH-USDT",))
    symbols = [_sym("BTC-USDT"), _sym("ETH-USDT")]
    vol = {"BTC-USDT": 9e9, "ETH-USDT": 9e9}
    price = {"BTC-USDT": 10.0, "ETH-USDT": 10.0}
    result = filter_universe(symbols, vol, price, cfg)
    assert result.eligible == ["ETH-USDT"]


def test_denylist_wins(cfg):
    cfg = replace(cfg, symbol_denylist=("BTC-USDT",))
    symbols = [_sym("BTC-USDT")]
    result = filter_universe(symbols, {"BTC-USDT": 9e9}, {"BTC-USDT": 10.0}, cfg)
    assert result.eligible == []
    assert result.rejected["BTC-USDT"] == "DENYLIST"
