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
    format_heartbeat,
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

        nlh_items = await self._emit(evaluations, now_ms)
        pattern_items = await self._emit_patterns()
        if self.cfg.alert_digest_mode:
            await self._send_digest(nlh_items + pattern_items)
        await self._maybe_heartbeat(now_ms)
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

    async def _emit_patterns(self) -> list[tuple[float, str]]:
        digest_items: list[tuple[float, str]] = []
        for match, score in self._pending_pattern_alerts:
            message = format_pattern_alert(match, score, self.cfg.timeframe)
            LOGGER.info("pattern alert %s %s score=%.0f", match.pattern, match.symbol, score)
            if self.cfg.alert_digest_mode:
                digest_items.append((score, message))
            else:
                await self._send_single(
                    message, log_label=f"pattern {match.pattern} {match.symbol}"
                )
        return digest_items

    async def _send_digest(self, items: list[tuple[float, str]]) -> None:
        """Combine one cycle's alerts into a single Telegram message, best-scored
        first and capped at ``alert_digest_max_items`` - so a busy market sends one
        message, not one ping per setup."""
        if not items:
            return
        items.sort(key=lambda item: item[0], reverse=True)
        cap = self.cfg.alert_digest_max_items
        shown, extra = items[:cap], items[cap:]
        blocks = [text for _, text in shown]
        if extra:
            blocks.append(f"… en {len(extra)} andere melding(en) deze cyclus (zie dashboard).")
        message = "\n\n———\n\n".join(blocks)
        if len(message) > 3900:
            message = message[:3900] + "\n… (afgekapt, zie dashboard/console voor het geheel)"
        print(f"\n*** ALERT DIGEST ({len(items)}) ***\n{message}\n", flush=True)
        LOGGER.info("alert digest sent: %d item(s), %d shown", len(items), len(shown))
        if self.dry_run or self.telegram is None:
            return
        await self.telegram.send(message)

    async def _maybe_heartbeat(self, now_ms: int) -> None:
        """Send a periodic 'still alive' ping so silence never means 'did it crash?'.

        Fires on the first cycle after a fresh state.json (nothing to compare
        against yet), then every ``heartbeat_interval_seconds`` after that -
        the timestamp is persisted in state.json so it survives a restart.
        """
        if not self.cfg.heartbeat_enabled:
            return
        now_s = now_ms // 1000
        last = self.state.get_meta("last_heartbeat_ts", 0)
        if now_s - last < self.cfg.heartbeat_interval_seconds:
            return
        self.state.set_meta("last_heartbeat_ts", now_s)
        active = self.state.active_count()
        message = format_heartbeat(self._cycle, len(self._universe), active, self.cfg.timeframe)
        print(f"\n*** HEARTBEAT ***\n{message}\n", flush=True)
        LOGGER.info(
            "heartbeat sent (cycle=%d universe=%d active=%d)",
            self._cycle,
            len(self._universe),
            active,
        )
        if self.dry_run or self.telegram is None:
            return
        await self.telegram.send(message)

    # ------------------------------------------------------------------ #
    async def _emit(self, evaluations: list[Evaluation], now_ms: int) -> list[tuple[float, str]]:
        now_s = now_ms // 1000
        digest_items: list[tuple[float, str]] = []
        for ev in evaluations:
            for event in ev.events:
                self.events.append(event, now_ms=now_ms)
                if self._should_alert(ev, event, now_s):
                    ev.state.alerts[event.kind] = now_s
                    message = format_message(event, self.cfg.timeframe, self.cfg.atr_len)
                    LOGGER.info("alert %s %s score=%.0f", event.kind, event.symbol, event.score)
                    if self.cfg.alert_digest_mode:
                        digest_items.append((event.score, message))
                    else:
                        await self._send_single(
                            message, log_label=f"{event.kind} {event.symbol}"
                        )
        return digest_items

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

    async def _send_single(self, message: str, *, log_label: str) -> None:
        print(f"\n*** ALERT ***\n{message}\n", flush=True)
        LOGGER.info("alert sent: %s", log_label)
        if self.dry_run or self.telegram is None:
            return
        await self.telegram.send(message)
        await asyncio.sleep(0.4)  # stay well under Telegram's per-chat rate limit
