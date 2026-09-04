from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from local_high import __version__
from local_high.config import ConfigError, load_config
from local_high.kucoin import KuCoinRestClient
from local_high.logsetup import configure_logging
from local_high.notifier import TelegramNotifier, render_pattern_table, render_screen_table
from local_high.patterns import PATTERNS, run_patterns
from local_high.scanner import Scanner
from local_high.screens import SCREENS, ScreenError, run_screen
from local_high.universe import filter_universe

LOGGER = logging.getLogger("local_high")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="local-high-scanner",
        description="Research-only 'New Local High' breakout scanner for KuCoin USDT spot.",
    )
    p.add_argument(
        "--config", default="config.yaml", help="path to config.yaml (default: %(default)s)"
    )
    p.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="never send Telegram messages")
    p.add_argument("--no-telegram", action="store_true", help="disable Telegram for this run")
    p.add_argument(
        "--list-universe", action="store_true", help="print the eligible symbol list and exit"
    )
    p.add_argument(
        "--list-screens", action="store_true", help="print available preset screeners and exit"
    )
    p.add_argument(
        "--screen",
        metavar="NAME",
        help="run one preset screener once over the universe, print matches, and exit "
        "(see --list-screens)",
    )
    p.add_argument(
        "--patterns",
        action="store_true",
        help="run chart-pattern detection once over the universe, print matches, and exit",
    )
    p.add_argument(
        "--pattern",
        metavar="TYPE",
        help=f"with --patterns, only show this pattern type ({', '.join(PATTERNS)})",
    )
    p.add_argument("--verbose", action="store_true", help="debug-level logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


async def _list_universe(cfg) -> int:
    async with KuCoinRestClient(
        cfg.kucoin_base_url, timeout_seconds=cfg.request_timeout_seconds
    ) as client:
        symbols = await client.list_symbols()
        tickers = await client.all_tickers()
    result = filter_universe(
        symbols,
        {s: t.quote_volume_24h for s, t in tickers.items()},
        {s: t.last for s, t in tickers.items()},
        cfg,
    )
    for sym in result.eligible:
        vol = tickers[sym].quote_volume_24h if sym in tickers else 0.0
        print(f"{sym:<18} 24h quote vol: {vol:,.0f} USDT")
    print(f"\n{len(result.eligible)} eligible / {len(symbols)} listed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    configure_logging(cfg.log_path, verbose=args.verbose)

    if args.list_universe:
        return asyncio.run(_list_universe(cfg))

    if args.list_screens:
        for s in sorted(SCREENS.values(), key=lambda s: s.name):
            print(f"{s.name:<26} {s.label} — {s.description}")
        return 0

    if args.screen:
        try:
            matches = asyncio.run(run_screen(cfg, args.screen))
        except ScreenError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(render_screen_table(SCREENS[args.screen].label, matches))
        return 0

    if args.patterns:
        try:
            matches = asyncio.run(run_patterns(cfg, args.pattern))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(render_pattern_table(matches))
        return 0

    telegram: TelegramNotifier | None = None
    if cfg.telegram_enabled and not args.no_telegram and not args.dry_run:
        telegram = TelegramNotifier.from_environment()
        if telegram is None:
            LOGGER.warning(
                "telegram enabled but LOCAL_HIGH_TELEGRAM_BOT_TOKEN / "
                "LOCAL_HIGH_TELEGRAM_CHAT_ID are not set - alerts print to console only"
            )

    scanner = Scanner(cfg, telegram=telegram, dry_run=args.dry_run)
    LOGGER.info(
        "starting local-high-scanner %s (timeframe=%s lookbacks=%s once=%s telegram=%s)",
        __version__,
        cfg.timeframe,
        list(cfg.lookbacks),
        args.once,
        telegram is not None,
    )

    try:
        if args.once:
            asyncio.run(scanner.run_once())
        else:
            asyncio.run(scanner.run_forever())
    except KeyboardInterrupt:
        LOGGER.info("interrupted - shutting down")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
