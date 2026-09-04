from __future__ import annotations

import asyncio
import logging
import time

from local_high.config import Config
from local_high.events import EventLog
from local_high.indicators import Candle, closed_only, parse_kucoin_candles
from local_high.kucoin import KuCoinApiError, KuCoinRestClient, Ticker
from local_high.notifier import (
    TelegramNotifier,
    format_message,
    format_pattern_alert,
    render_table,
)
from local_high.patterns import PatternMatch, detect_pattern
from local_high.scoring import pattern_confidence
from local_high.state import StateStore
from local_high.strategy import Evaluation, Event, SymbolState, evaluate
from local_high.universe import SymbolInfo, filter_universe

LOGGER = logging.getLogger("local_high.scanner")


class Scanner:
    def __init__(
        self,
        cfg: Config,
        *,
        telegram: TelegramNotifier | None = None,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.telegram = telegram
        self.dry_run = dry_run
        self.state = StateStore(cfg.state_path)
        self.events = EventLog(cfg.events_csv, cfg.timeframe)
        self._symbols: list[SymbolInfo] = []
        self._universe: list[str] = []
        self._cycle = 0
        self._pending_pattern_alerts: list[tuple[PatternMatch, float]] = []

    async def run_forever(self) -> None:
        self.state.load()
        while True:
            started = time.monotonic()
            try:
                await self.run_cycle()
            except KuCoinApiError as exc:
                LOGGER.warning("cycle aborted: %s", exc)
            except Exception:  # noqa: BLE001 - keep the loop alive, log the trace
                LOGGER.exception("unexpected error during cycle")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.cfg.cycle_seconds - elapsed))

    async def run_once(self) -> list[Evaluation]:
        self.state.load()
        return await self.run_cycle()

    async def run_cycle(self) -> list[Evaluation]:
        self._cycle += 1
        now_ms = int(time.time() * 1000)
        async with KuCoinRestClient(
            self.cfg.kucoin_base_url, timeout_seconds=self.cfg.request_timeout_seconds
        ) as client:
            tickers = await client.all_tickers()
            if self._needs_universe_refresh():
                await self._refresh_universe(client, tickers)
            evaluations = await self._scan(client, tickers, now_ms)

        await self._emit(evaluations, now_ms)
        await self._emit_patterns()
        self.state.prune(set(self._universe))
        self.state.save()
        print(render_table(self._cycle, len(self._universe), evaluations, now_ms), flush=True)
        return evaluations

    # ------------------------------------------------------------------ #
    def _needs_universe_refresh(self) -> bool:
        if not self._symbols or not self._universe:
            return True
        return self._cycle % max(1, self.cfg.universe_refresh_cycles) == 1

    async def _refresh_universe(
        self, client: KuCoinRestClient, tickers: dict[str, Ticker]
    ) -> None:
        self._symbols = await client.list_symbols()
        quote_volume = {s: t.quote_volume_24h for s, t in tickers.items()}
        last_price = {s: t.last for s, t in tickers.items()}
        result = filter_universe(self._symbols, quote_volume, last_price, self.cfg)
        self._universe = result.eligible
        LOGGER.info(
            "universe refreshed: %d eligible / %d listed", len(result.eligible), len(self._symbols)
        )

    async def _scan(
        self, client: KuCoinRestClient, tickers: dict[str, Ticker], now_ms: int
    ) -> list[Evaluation]:
        sem = asyncio.Semaphore(self.cfg.max_concurrent_requests)
        end_at = now_ms // 1000
        now_s = end_at
        start_at = end_at - self.cfg.candle_history * self.cfg.interval_seconds
        need = self.cfg.max_lookback + 3
        short_history = 0
        pattern_alerts: list[tuple[PatternMatch, float]] = []

        async def worker(symbol: str) -> Evaluation | None:
            nonlocal short_history
            async with sem:
                try:
                    rows = await client.candles(
                        symbol,
                        self.cfg.timeframe,
                        self.cfg.candle_history,
                        start_at=start_at,
                        end_at=end_at,
                    )
                except KuCoinApiError as exc:
                    LOGGER.debug("candles failed for %s: %s", symbol, exc)
                    return None
            candles = closed_only(parse_kucoin_candles(rows, self.cfg.interval_seconds, now_ms))
            if len(candles) < need:
                short_history += 1
            prior = self.state.get(symbol)
            result = evaluate(symbol, candles, tickers.get(symbol), self.cfg, prior)
            self.state.put(result.state)
            self._check_pattern_alert(symbol, candles, result.state, now_s, pattern_alerts)
            return result

        gathered = await asyncio.gather(*(worker(s) for s in self._universe))
        self._pending_pattern_alerts = pattern_alerts
        if short_history:
            LOGGER.warning(
                "%d/%d symbols had < %d candles (need %d for max lookback %d) - "
                "listing too new, or KuCoin returned a short window",
                short_history,
                len(self._universe),
                need,
                need,
                self.cfg.max_lookback,
            )
        return [e for e in gathered if e is not None]

    def _check_pattern_alert(
        self,
        symbol: str,
        candles: list[Candle],
        state: SymbolState,
        now_s: int,
        out: list[tuple[PatternMatch, float]],
    ) -> None:
        """Detect a chart pattern for ``symbol`` and queue a Telegram alert when it's
        a fresh, high-confidence breakout in a configured direction (cooldown per
        symbol + pattern, tracked in ``state.pattern_alerts``)."""
        if not self.cfg.pattern_alerts_enabled:
            return
        match = detect_pattern(symbol, candles, self.cfg)
        if match is None or match.status not in self.cfg.pattern_alert_directions:
            return
        score = pattern_confidence(candles, match, self.cfg)
        if score < self.cfg.pattern_alert_min_score:
            return
        key = f"{match.pattern}:{match.status}"
        last = state.pattern_alerts.get(key, 0)
        if now_s - last < self.cfg.pattern_alert_cooldown_seconds:
            return
        state.pattern_alerts[key] = now_s
        out.append((match, score))

    async def _emit_patterns(self) -> None:
        for match, score in self._pending_pattern_alerts:
            message = format_pattern_alert(match, score, self.cfg.timeframe)
            print(f"\n*** PATTERN ALERT ***\n{message}\n", flush=True)
            LOGGER.info(
                "pattern alert %s %s score=%.0f", match.pattern, match.symbol, score
            )
            if self.dry_run or self.telegram is None:
                continue
            await self.telegram.send(message)
            await asyncio.sleep(0.4)  # stay well under Telegram's per-chat rate limit

    # ------------------------------------------------------------------ #
    async def _emit(self, evaluations: list[Evaluation], now_ms: int) -> None:
        now_s = now_ms // 1000
        for ev in evaluations:
            for event in ev.events:
                self.events.append(event, now_ms=now_ms)
                if self._should_alert(ev, event, now_s):
                    ev.state.alerts[event.kind] = now_s
                    await self._send_alert(event)

    _RVOL_GATED = frozenset({"BREAKOUT", "REBREAK"})

    def _should_alert(self, ev: Evaluation, event: Event, now_s: int) -> bool:
        if event.kind not in self.cfg.alert_on:
            return False
        if event.seeded and not self.cfg.alert_on_seed:
            return False
        if (
            self.cfg.min_rvol > 0
            and event.kind in self._RVOL_GATED
            and ev.rvol < self.cfg.min_rvol
        ):
            LOGGER.info(
                "suppressed %s %s: rvol %.2f < min_rvol %.2f",
                event.kind,
                event.symbol,
                ev.rvol,
                self.cfg.min_rvol,
            )
            return False
        if (
            event.kind == "BREAKOUT"
            and self.cfg.max_breakout_distance_pct > 0
            and event.distance_pct > self.cfg.max_breakout_distance_pct
        ):
            LOGGER.info(
                "suppressed extended BREAKOUT %s: +%.1f%% above level > %.1f%% (still tracked)",
                event.symbol,
                event.distance_pct,
                self.cfg.max_breakout_distance_pct,
            )
            return False
        last = ev.state.alerts.get(event.kind, 0)
        return now_s - last >= self.cfg.alert_cooldown_seconds

    async def _send_alert(self, event: Event) -> None:
        message = format_message(event, self.cfg.timeframe, self.cfg.atr_len)
        print(f"\n*** ALERT ***\n{message}\n", flush=True)
        LOGGER.info("alert %s %s score=%.0f", event.kind, event.symbol, event.score)
        if self.dry_run or self.telegram is None:
            return
        await self.telegram.send(message)
        await asyncio.sleep(0.4)  # stay well under Telegram's per-chat rate limit
