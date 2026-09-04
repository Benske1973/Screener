from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import aiohttp

from local_high.patterns import PatternMatch
from local_high.screens import ScreenMatch
from local_high.strategy import BREAKOUT, FAKEOUT, PULLBACK, REBREAK, WATCH, Evaluation, Event
from local_high.targets import TradePlan

LOGGER = logging.getLogger(__name__)

BOT_TOKEN_ENV = "LOCAL_HIGH_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "LOCAL_HIGH_TELEGRAM_CHAT_ID"

_EMOJI = {
    WATCH: "\U0001f440",       # eyes
    BREAKOUT: "\U0001f680",    # rocket
    PULLBACK: "\U0001f504",    # arrows
    REBREAK: "✅",         # check mark
    FAKEOUT: "⚠️",   # warning
}


def _fmt(value: float) -> str:
    return f"{value:.8g}"


def _lookbacks(event: Event) -> str:
    return ", ".join(str(x) for x in event.matched_lookbacks) or "-"


def _plan_lines(plan: TradePlan) -> list[str]:
    out = [f"1R = {_fmt(plan.risk)}  ({plan.risk_pct:.1f}% risk)"]
    for t in plan.targets:
        out.append(f"{t.label}: {_fmt(t.price)}   +{t.rr:.1f}R   (+{t.gain_pct:.1f}%)")
    out.extend(plan.notes)
    return out


def format_message(event: Event, timeframe: str, atr_len: int) -> str:
    head = f"{_EMOJI.get(event.kind, '')} {event.kind} — {event.symbol}".strip()
    lines = [head]

    if event.kind == BREAKOUT:
        lines += [
            f"TF {timeframe} · nieuwe {_lookbacks(event)}-candle high",
            f"Prijs: {_fmt(event.price)}  "
            f"(+{event.distance_pct:.2f}% boven weerstand {_fmt(event.level)})",
            f"RVOL: {event.rvol:.2f}x   24h: {event.change_24h_pct:+.1f}%   "
            f"Score: {event.score:.0f}/100",
            f"Instap 1 (50%): ~{_fmt(event.price)}   Stop: {_fmt(event.stop)} "
            f"(net onder {_fmt(event.level)})",
        ]
        if event.plan:
            lines += _plan_lines(event.plan)
        lines.append(f"ATR({atr_len}): {_fmt(event.atr)}")
        if event.seeded:
            lines.append("(seeded — setup liep al bij eerste waarneming)")
    elif event.kind == PULLBACK:
        lines += [
            f"Retest van {_fmt(event.level)} als steun. Prijs: {_fmt(event.price)}",
            "Wacht op re-break van het recente hoogtepunt voor de tweede helft.",
        ]
    elif event.kind == REBREAK:
        lines += [
            f"Nieuw hoogtepunt gebroken na pullback. Prijs: {_fmt(event.price)}",
            f"Voeg resterende 50% toe. Stop → {_fmt(event.stop)} (pullback-low).",
            f"Score: {event.score:.0f}/100",
        ]
        if event.plan:
            lines += _plan_lines(event.plan)
    elif event.kind == FAKEOUT:
        lines += [
            f"Prijs terug onder uitbraakniveau {_fmt(event.level)} (nu {_fmt(event.price)}).",
            "Breakout ongeldig — setup gesloten.",
        ]
    elif event.kind == WATCH:
        lines += [
            f"{event.distance_pct:+.2f}% t.o.v. weerstand {_fmt(event.level)} "
            f"({_lookbacks(event)}-candle high). Prijs {_fmt(event.price)}.",
        ]

    lines.append("— research-signaal, geen order geplaatst.")
    return "\n".join(lines)


_PATTERN_LABELS = {
    "ascending_triangle": "Ascending Triangle",
    "descending_triangle": "Descending Triangle",
    "rising_wedge": "Rising Wedge",
    "falling_wedge": "Falling Wedge",
    "ascending_channel": "Ascending Channel",
    "descending_channel": "Descending Channel",
}
_PATTERN_EMOJI = {"breakout_up": "\U0001f680", "breakout_down": "\U0001f53b"}  # rocket / red tri


def format_heartbeat(cycle: int, universe_size: int, active_count: int, timeframe: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"\U0001f49a Scanner draait — {stamp}\n"
        f"TF {timeframe} · cycle {cycle} · universe {universe_size} · "
        f"actieve setups {active_count}"
    )


def format_pattern_alert(match: PatternMatch, score: float, timeframe: str) -> str:
    label = _PATTERN_LABELS.get(match.pattern, match.pattern)
    direction = "Bullish" if match.status == "breakout_up" else "Bearish"
    lines = [
        f"{_PATTERN_EMOJI.get(match.status, '')} PATROON-UITBRAAK — {match.symbol}",
        f"TF {timeframe} · {label} · {direction} breakout",
        f"Prijs: {_fmt(match.price)}",
        f"Weerstand: {_fmt(match.resistance.value_now)}   Steun: {_fmt(match.support.value_now)}",
    ]
    if match.target is not None:
        gain_pct = (match.target / match.price - 1.0) * 100.0
        lines.append(f"Koersdoel (measured move): {_fmt(match.target)}  ({gain_pct:+.1f}%)")
    lines += [
        f"Betrouwbaarheid: {score:.0f}/100  "
        f"(fit r2 {match.resistance.r2:.2f}/{match.support.r2:.2f})",
        match.note,
        "— research-signaal, geen order geplaatst.",
    ]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        self._token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._api_base_url = api_base_url.rstrip("/")
        if not self._token or not self._chat_id:
            raise ValueError("Telegram bot token and chat id are both required")

    @classmethod
    def from_environment(cls) -> TelegramNotifier | None:
        token = os.getenv(BOT_TOKEN_ENV, "").strip()
        chat_id = os.getenv(CHAT_ID_ENV, "").strip()
        if not token or not chat_id:
            return None
        return cls(token, chat_id)

    async def send(self, text: str) -> bool:
        url = f"{self._api_base_url}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200 or not (isinstance(body, dict) and body.get("ok")):
                        LOGGER.warning("telegram rejected message (status %s)", resp.status)
                        return False
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            LOGGER.warning("telegram send failed: %s", type(exc).__name__)
            return False
        return True


def render_table(cycle: int, universe_size: int, evaluations: list[Evaluation], now_ms: int) -> str:
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    active = sorted(
        (e for e in evaluations if e.active),
        key=lambda e: e.score,
        reverse=True,
    )
    header = f"{stamp} | cycle {cycle} | universe {universe_size} | active {len(active)}"
    if not active:
        return header + "\n  (no active breakout setups)"

    cols = (
        f"  {'SYMBOL':<16}{'STATE':<10}{'PRICE':>14}{'ΔRES%':>9}{'RVOL':>7}"
        f"{'24H%':>8}{'SCORE':>7}{'ENTRY':>14}{'STOP':>14}{'TP1':>14}{'R:R':>7}"
    )
    rows = [header, cols]
    for e in active:
        if e.plan and e.plan.targets:
            entry = f"{e.plan.entry:>14.8g}"
            stop = f"{e.plan.stop:>14.8g}"
            tp1 = f"{e.plan.targets[0].price:>14.8g}"
            rr = f"{e.plan.rr_last:>7.1f}"
        else:
            entry = stop = tp1 = f"{'-':>14}"
            rr = f"{'-':>7}"
        rows.append(
            f"  {e.symbol:<16}{e.state.state:<10}{e.price:>14.8g}{e.distance_pct:>9.2f}"
            f"{e.rvol:>7.2f}{e.change_24h_pct:>8.1f}{e.score:>7.0f}{entry}{stop}{tp1}{rr}"
        )
    return "\n".join(rows)


def render_screen_table(label: str, matches: list[ScreenMatch]) -> str:
    header = f"{label} | {len(matches)} match(es)"
    if not matches:
        return header
    rows = [header, f"  {'SYMBOL':<16}{'PRICE':>14}  DETAIL"]
    for m in matches:
        rows.append(f"  {m.symbol:<16}{m.price:>14.8g}  {m.detail}")
    return "\n".join(rows)


def render_pattern_table(matches: list[PatternMatch]) -> str:
    header = f"chart patterns | {len(matches)} match(es)"
    if not matches:
        return header
    rows = [header, f"  {'SYMBOL':<16}{'PATTERN':<20}{'STATUS':<14}{'PRICE':>14}{'TARGET':>14}"]
    for m in matches:
        target = f"{m.target:>14.8g}" if m.target is not None else f"{'-':>14}"
        rows.append(
            f"  {m.symbol:<16}{m.pattern:<20}{m.status:<14}{m.price:>14.8g}{target}"
        )
    return "\n".join(rows)
