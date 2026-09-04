from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from local_high import __version__
from local_high.config import Config, ConfigError, load_config
from local_high.indicators import closed_only, parse_kucoin_candles
from local_high.kucoin import KuCoinApiError, KuCoinRestClient
from local_high.logsetup import configure_logging
from local_high.patterns import PatternMatch, detect_pattern
from local_high.screens import SCREENS, ScreenMatch
from local_high.state import StateStore
from local_high.strategy import Evaluation, evaluate
from local_high.universe import filter_universe

LOGGER = logging.getLogger("local_high.webapp")

WEB_TOKEN_ENV = "LOCAL_HIGH_WEB_TOKEN"
_COOKIE_NAME = "lh_token"

_CFG_KEY = web.AppKey("cfg", Config)
_WEB_TOKEN_KEY = web.AppKey("web_token", str)
_REFRESH_TASK_KEY: web.AppKey[asyncio.Task[None]] = web.AppKey("refresh_task")

# ----------------------------------------------------------------------------
# In-memory snapshot, refreshed on a timer. One candle fetch per cycle powers
# the New Local High board, every preset screener, and pattern detection -
# the web dashboard is read-only and never re-fetches per request.
# ----------------------------------------------------------------------------


class WebState:
    def __init__(self) -> None:
        self.updated_utc: str = ""
        self.universe_size: int = 0
        self.nlh_active: list[dict[str, Any]] = []
        self.screens: dict[str, list[dict[str, Any]]] = {name: [] for name in SCREENS}
        self.patterns: list[dict[str, Any]] = []
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "updated_utc": self.updated_utc,
                "universe_size": self.universe_size,
                "nlh_active": list(self.nlh_active),
                "screens": {k: list(v) for k, v in self.screens.items()},
                "patterns": list(self.patterns),
                "error": self.error,
            }

    async def update(self, **fields: Any) -> None:
        async with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)


_WEB_STATE_KEY = web.AppKey("web_state", WebState)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nlh_row(ev: Evaluation) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": ev.symbol,
        "state": ev.state.state,
        "price": ev.price,
        "distance_pct": round(ev.distance_pct, 3),
        "rvol": round(ev.rvol, 3),
        "change_24h_pct": round(ev.change_24h_pct, 2),
        "score": ev.score,
    }
    if ev.plan and ev.plan.targets:
        row["entry"] = ev.plan.entry
        row["stop"] = ev.plan.stop
        row["tp1"] = ev.plan.targets[0].price
        row["rr"] = ev.plan.rr_last
    return row


def _screen_row(m: ScreenMatch) -> dict[str, Any]:
    return {"symbol": m.symbol, "price": m.price, "detail": m.detail, "metrics": m.metrics}


def _pattern_row(m: PatternMatch) -> dict[str, Any]:
    return asdict(m)


# --------------------------------------------------------------------------- #
# one refresh cycle: fetch the universe + candles once, run every analysis
# --------------------------------------------------------------------------- #
async def _refresh_once(cfg: Config, web_state: WebState, symbol_states: StateStore) -> None:
    now_ms = int(time.time() * 1000)
    end_at = now_ms // 1000
    start_at = end_at - cfg.candle_history * cfg.interval_seconds
    need_nlh = cfg.max_lookback + 3

    async with KuCoinRestClient(
        cfg.kucoin_base_url, timeout_seconds=cfg.request_timeout_seconds
    ) as client:
        symbols = await client.list_symbols()
        tickers = await client.all_tickers()
        universe = filter_universe(
            symbols,
            {s: t.quote_volume_24h for s, t in tickers.items()},
            {s: t.last for s, t in tickers.items()},
            cfg,
        ).eligible

        sem = asyncio.Semaphore(cfg.max_concurrent_requests)
        nlh_acc: list[dict[str, Any]] = []
        screens_acc: dict[str, list[dict[str, Any]]] = {name: [] for name in SCREENS}
        patterns_acc: list[dict[str, Any]] = []

        async def worker(symbol: str) -> None:
            async with sem:
                try:
                    rows = await client.candles(
                        symbol,
                        cfg.timeframe,
                        cfg.candle_history,
                        start_at=start_at,
                        end_at=end_at,
                    )
                except KuCoinApiError:
                    return
            candles = closed_only(parse_kucoin_candles(rows, cfg.interval_seconds, now_ms))

            if len(candles) >= need_nlh:
                prior = symbol_states.get(symbol)
                ev = evaluate(symbol, candles, tickers.get(symbol), cfg, prior)
                symbol_states.put(ev.state)
                if ev.active:
                    nlh_acc.append(_nlh_row(ev))

            for name, screen in SCREENS.items():
                match = screen.fn(symbol, candles, cfg)
                if match is not None:
                    screens_acc[name].append(_screen_row(match))

            pattern = detect_pattern(symbol, candles, cfg)
            if pattern is not None:
                patterns_acc.append(_pattern_row(pattern))

        await asyncio.gather(*(worker(s) for s in universe))
        symbol_states.prune(set(universe))
        symbol_states.save()

    nlh_acc.sort(key=lambda r: r["score"], reverse=True)
    for rows in screens_acc.values():
        rows.sort(key=lambda r: r["symbol"])
    patterns_acc.sort(key=lambda r: r["symbol"])

    await web_state.update(
        updated_utc=_iso_now(),
        universe_size=len(universe),
        nlh_active=nlh_acc,
        screens=screens_acc,
        patterns=patterns_acc,
        error=None,
    )


async def _refresh_loop(cfg: Config, web_state: WebState, symbol_states: StateStore) -> None:
    symbol_states.load()
    while True:
        started = time.monotonic()
        try:
            await _refresh_once(cfg, web_state, symbol_states)
        except KuCoinApiError as exc:
            LOGGER.warning("web refresh aborted: %s", exc)
            await web_state.update(error=str(exc))
        except Exception:  # noqa: BLE001 - keep the loop alive, log the trace
            LOGGER.exception("unexpected error during web refresh")
            await web_state.update(error="internal error - see logs")
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, cfg.web_refresh_seconds - elapsed))


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
@web.middleware
async def _auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    token = request.app.get(_WEB_TOKEN_KEY, "")
    if not token:
        return await handler(request)
    supplied = request.query.get("token") or request.cookies.get(_COOKIE_NAME)
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer ") :].strip()
    if supplied != token:
        raise web.HTTPUnauthorized(text="missing or invalid token (?token=... once, then cookied)")
    response = await handler(request)
    if request.query.get("token") == token:
        response.set_cookie(
            _COOKIE_NAME, token, httponly=True, samesite="Strict", max_age=30 * 24 * 3600
        )
    return response


async def _handle_index(request: web.Request) -> web.Response:
    cfg = request.app[_CFG_KEY]
    screens_meta = [{"name": s.name, "label": s.label} for s in SCREENS.values()]
    html = _INDEX_HTML.replace("__REFRESH_MS__", str(cfg.web_refresh_seconds * 1000)).replace(
        "__SCREENS_JSON__", _json_dumps(screens_meta)
    )
    return web.Response(text=html, content_type="text/html")


async def _handle_state(request: web.Request) -> web.Response:
    web_state = request.app[_WEB_STATE_KEY]
    return web.json_response(await web_state.snapshot())


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def build_app(
    cfg: Config, *, web_state: WebState | None = None, start_refresh: bool = True
) -> web.Application:
    """Build the aiohttp app. Tests pass a pre-populated ``web_state`` and
    ``start_refresh=False`` to exercise the HTTP layer without any network I/O."""
    app = web.Application(middlewares=[_auth_middleware])
    app[_CFG_KEY] = cfg
    app[_WEB_STATE_KEY] = web_state if web_state is not None else WebState()
    app[_WEB_TOKEN_KEY] = os.getenv(WEB_TOKEN_ENV, "").strip()

    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/state", _handle_state)

    if start_refresh:

        async def on_startup(app: web.Application) -> None:
            symbol_states = StateStore(cfg.state_path)
            app[_REFRESH_TASK_KEY] = asyncio.create_task(
                _refresh_loop(cfg, app[_WEB_STATE_KEY], symbol_states)
            )

        async def on_cleanup(app: web.Application) -> None:
            task = app.get(_REFRESH_TASK_KEY)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)

    return app


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>local-high-scanner</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0b0e14; color: #d7dce3;
    font: 14px/1.4 "Cascadia Code", "Consolas", monospace;
  }
  header {
    padding: 14px 20px; border-bottom: 1px solid #1f2733;
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  }
  header h1 { font-size: 16px; margin: 0; color: #7ee787; }
  header .meta { color: #8892a0; font-size: 12px; }
  .banner { padding: 8px 20px; background: #3a2020; color: #ffb4b4; display: none; }
  main { padding: 16px 20px; display: flex; flex-direction: column; gap: 22px; }
  section h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
    color: #8892a0; margin: 0 0 8px;
  }
  .screen-picker { margin-bottom: 8px; }
  select {
    background: #131722; color: #d7dce3; border: 1px solid #2a3441;
    padding: 4px 8px; font: inherit;
  }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: right; padding: 4px 10px; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { color: #8892a0; font-weight: 500; border-bottom: 1px solid #1f2733; }
  tbody tr:nth-child(odd) { background: #10141d; }
  td.detail { text-align: left; white-space: normal; color: #b7c0cc; }
  .up { color: #7ee787; } .down { color: #ff7b72; }
  .empty { color: #565f6c; font-style: italic; padding: 6px 0; }
  a { color: #7ee787; }
</style>
</head>
<body>
<header>
  <h1>local-high-scanner</h1>
  <span class="meta" id="meta">loading…</span>
</header>
<div class="banner" id="banner"></div>
<main>
  <section>
    <h2>New Local High — active setups</h2>
    <div id="nlh"></div>
  </section>
  <section>
    <h2>Preset screeners</h2>
    <div class="screen-picker">
      <select id="screen-select"></select>
    </div>
    <div id="screen-table"></div>
  </section>
  <section>
    <h2>Chart patterns</h2>
    <div id="patterns"></div>
  </section>
</main>
<p style="padding:0 20px 20px;color:#565f6c;font-size:11.5px;">
  Research-only. Not financial advice — nothing here places an order.
</p>
<script>
const REFRESH_MS = __REFRESH_MS__;
const SCREENS = __SCREENS_JSON__;

const screenSelect = document.getElementById('screen-select');
for (const s of SCREENS) {
  const opt = document.createElement('option');
  opt.value = s.name; opt.textContent = s.label;
  screenSelect.appendChild(opt);
}

function fmt(n) {
  if (n === null || n === undefined) return '-';
  if (typeof n === 'string') return n;
  const v = Number(n);
  if (!isFinite(v)) return '-';
  return v.toLocaleString('en-US', { maximumFractionDigits: 8 });
}

function table(rows, cols) {
  if (!rows.length) return '<div class="empty">no matches</div>';
  const head = cols.map(c => `<th>${c.label}</th>`).join('');
  const body = rows.map(r => '<tr>' + cols.map(c => {
    const cls = c.cls ? c.cls(r) : '';
    const val = c.fmt ? c.fmt(r) : fmt(r[c.key]);
    return `<td class="${cls}">${val}</td>`;
  }).join('') + '</tr>').join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderNlh(rows) {
  document.getElementById('nlh').innerHTML = table(rows, [
    { key: 'symbol', label: 'Symbol' },
    { key: 'state', label: 'State' },
    { key: 'price', label: 'Price' },
    { key: 'distance_pct', label: 'Δres%',
      fmt: r => (r.distance_pct >= 0 ? '+' : '') + r.distance_pct.toFixed(2),
      cls: r => r.distance_pct >= 0 ? 'up' : 'down' },
    { key: 'rvol', label: 'RVOL' },
    { key: 'change_24h_pct', label: '24h%' },
    { key: 'score', label: 'Score' },
    { key: 'entry', label: 'Entry' },
    { key: 'stop', label: 'Stop' },
    { key: 'tp1', label: 'TP1' },
    { key: 'rr', label: 'R:R' },
  ]);
}

function renderScreen(rows) {
  document.getElementById('screen-table').innerHTML = table(rows, [
    { key: 'symbol', label: 'Symbol' },
    { key: 'price', label: 'Price' },
    { key: 'detail', label: 'Detail', fmt: r => r.detail, cls: () => 'detail' },
  ]);
}

function renderPatterns(rows) {
  document.getElementById('patterns').innerHTML = table(rows, [
    { key: 'symbol', label: 'Symbol' },
    { key: 'pattern', label: 'Pattern' },
    { key: 'status', label: 'Status',
      cls: r => r.status === 'breakout_up' ? 'up' : (r.status === 'breakout_down' ? 'down' : '') },
    { key: 'price', label: 'Price' },
    { key: 'target', label: 'Target' },
    { key: 'note', label: 'Note', fmt: r => r.note, cls: () => 'detail' },
  ]);
}

let lastData = null;

function renderAll() {
  if (!lastData) return;
  renderNlh(lastData.nlh_active);
  renderScreen(lastData.screens[screenSelect.value] || []);
  renderPatterns(lastData.patterns);
}
screenSelect.addEventListener('change', renderAll);

async function tick() {
  const banner = document.getElementById('banner');
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    lastData = await res.json();
    document.getElementById('meta').textContent =
      `updated ${lastData.updated_utc || '-'} · universe ${lastData.universe_size}`;
    if (lastData.error) {
      banner.style.display = 'block';
      banner.textContent = 'last refresh failed: ' + lastData.error + ' (showing last good data)';
    } else {
      banner.style.display = 'none';
    }
    renderAll();
  } catch (e) {
    banner.style.display = 'block';
    banner.textContent = 'could not reach /api/state: ' + e;
  }
}
tick();
setInterval(tick, REFRESH_MS);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CLI entry point (local-high-web)
# --------------------------------------------------------------------------- #
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="local-high-web",
        description="Web dashboard for local-high-scanner (screeners, patterns, NLH board).",
    )
    p.add_argument(
        "--config", default="config.yaml", help="path to config.yaml (default: %(default)s)"
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    p.add_argument("--port", type=int, default=8787, help="bind port (default: %(default)s)")
    p.add_argument("--verbose", action="store_true", help="debug-level logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    configure_logging(cfg.log_path, verbose=args.verbose)

    if not os.getenv(WEB_TOKEN_ENV, "").strip():
        LOGGER.warning(
            "%s is not set - the dashboard has no access control. Fine on localhost; "
            "set it before exposing this through a tunnel.",
            WEB_TOKEN_ENV,
        )

    app = build_app(cfg)
    LOGGER.info(
        "starting local-high-web %s on http://%s:%d (refresh every %ds)",
        __version__,
        args.host,
        args.port,
        cfg.web_refresh_seconds,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
