from __future__ import annotations

from dataclasses import dataclass

from local_high.config import Config

STABLE_BASES = frozenset(
    {
        "USDT", "USDC", "USDD", "USDP", "TUSD", "DAI", "FDUSD", "PYUSD",
        "EURC", "EURT", "USDE", "GUSD", "USDG", "USD1", "FRAX", "LUSD",
        "USDJ", "SUSD", "GHO", "USDX", "CUSD", "EURS", "EURI", "XUSD",
    }
)
LEVERAGED_SUFFIXES = ("3L", "3S", "2L", "2S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    base_currency: str
    quote_currency: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class UniverseResult:
    eligible: list[str]
    rejected: dict[str, str]


def filter_universe(
    symbols: list[SymbolInfo],
    quote_volume: dict[str, float],
    last_price: dict[str, float],
    cfg: Config,
) -> UniverseResult:
    """Apply static (listing) and liquidity gates. Returns eligible symbols sorted by name."""
    allow = set(cfg.symbol_allowlist)
    deny = set(cfg.symbol_denylist)
    eligible: list[str] = []
    rejected: dict[str, str] = {}

    for info in sorted(symbols, key=lambda s: s.symbol):
        sym = info.symbol.upper()
        base = info.base_currency.upper()

        if allow and sym not in allow:
            continue
        if sym in deny:
            rejected[sym] = "DENYLIST"
        elif not info.enabled:
            rejected[sym] = "SUSPENDED"
        elif info.quote_currency.upper() != "USDT" or sym != f"{base}-USDT":
            rejected[sym] = "NOT_USDT_SPOT"
        elif cfg.exclude_stablecoins and base in STABLE_BASES:
            rejected[sym] = "STABLE_BASE"
        elif cfg.exclude_leveraged and base.endswith(LEVERAGED_SUFFIXES):
            rejected[sym] = "LEVERAGED_TOKEN"
        elif last_price.get(sym, 0.0) < cfg.min_price:
            rejected[sym] = "PRICE_TOO_LOW"
        elif quote_volume.get(sym, 0.0) < cfg.min_24h_quote_volume:
            rejected[sym] = "THIN_LIQUIDITY"
        else:
            eligible.append(sym)

    return UniverseResult(eligible=eligible, rejected=rejected)
