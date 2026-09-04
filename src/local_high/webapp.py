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
from local_high.indicators import Candle, closed_only, parse_kucoin_candles
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
        # recent OHLC window per symbol, for the chart panel - only kept for
        # symbols that currently match something, not the whole universe.
        self.candles: dict[str, list[dict[str, Any]]] = {}
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

    async def get_candles(self, symbol: str) -> list[dict[str, Any]] | None:
        async with self._lock:
            rows = self.candles.get(symbol)
            return list(rows) if rows else None

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
        "level": ev.state.breakout_level or None,
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


def _candle_rows(candles: list[Candle], limit: int) -> list[dict[str, Any]]:
    return [
        {"t": c.open_time_ms, "o": c.open, "h": c.high, "l": c.low, "c": c.close}
        for c in candles[-limit:]
    ]


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
        candles_acc: dict[str, list[dict[str, Any]]] = {}
        # Must match the window detect_pattern() fits lines over exactly, so a
        # pattern's value_start/value_now line up with index 0 / index -1 here.
        chart_window = cfg.pattern_lookback_candles

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
            interesting = False

            if len(candles) >= need_nlh:
                prior = symbol_states.get(symbol)
                ev = evaluate(symbol, candles, tickers.get(symbol), cfg, prior)
                symbol_states.put(ev.state)
                if ev.active:
                    nlh_acc.append(_nlh_row(ev))
                    interesting = True

            for name, screen in SCREENS.items():
                match = screen.fn(symbol, candles, cfg)
                if match is not None:
                    screens_acc[name].append(_screen_row(match))
                    interesting = True

            pattern = detect_pattern(symbol, candles, cfg)
            if pattern is not None:
                patterns_acc.append(_pattern_row(pattern))
                interesting = True

            if interesting and len(candles) >= 4:
                candles_acc[symbol] = _candle_rows(candles, chart_window)

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
        candles=candles_acc,
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


async def _handle_candles(request: web.Request) -> web.Response:
    web_state = request.app[_WEB_STATE_KEY]
    symbol = request.match_info["symbol"].upper()
    candles = await web_state.get_candles(symbol)
    if candles is None:
        raise web.HTTPNotFound(text=f"no cached candles for {symbol} (not currently matched)")
    return web.json_response({"symbol": symbol, "candles": candles})


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
    app.router.add_get("/api/candles/{symbol}", _handle_candles)

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
  :root {
    color-scheme: dark;
    --bg: #0a0d13; --bg-side: #0c0f16; --bg-card: #11161f; --bg-card-2: #161d29;
    --border: #212a38; --text: #dde3ea; --text-dim: #8a93a3; --text-faint: #57606f;
    --accent: #2dd4a7; --accent-dim: rgba(45,212,167,0.12);
    --up: #2dd4a7; --down: #ff6b6b; --warn: #f5b942; --radius: 10px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text); font-size: 13.5px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
      Arial, sans-serif;
  }
  a { color: var(--accent); }
  .app { display: flex; height: 100vh; overflow: hidden; }

  /* sidebar */
  .sidebar {
    width: 216px; flex: 0 0 216px; background: var(--bg-side);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
    overflow-y: auto;
  }
  .brand {
    display: flex; align-items: center; gap: 9px; padding: 18px 20px 16px;
    font-weight: 700; font-size: 14.5px; letter-spacing: 0.02em;
  }
  .brand-mark { color: var(--accent); font-size: 18px; }
  .nav-group { padding: 8px 0; border-top: 1px solid var(--border); }
  .nav-group:first-of-type { border-top: none; padding-top: 0; }
  .nav-label {
    padding: 10px 20px 6px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--text-faint); font-weight: 700;
  }
  .nav-item {
    display: flex; align-items: center; gap: 10px; padding: 8px 20px;
    color: var(--text-dim); text-decoration: none; font-size: 13px; cursor: pointer;
    border-left: 2px solid transparent;
  }
  .nav-item:hover { color: var(--text); background: rgba(255,255,255,0.03); }
  .nav-item.active {
    color: var(--accent); background: var(--accent-dim);
    border-left-color: var(--accent); font-weight: 600;
  }
  .nav-icon { width: 15px; text-align: center; font-size: 12.5px; }
  .nav-badge {
    margin-left: auto; background: var(--bg-card-2); color: var(--text-dim);
    font-size: 10px; padding: 1px 6px; border-radius: 999px; min-width: 16px;
    text-align: center;
  }
  .nav-item.active .nav-badge { background: var(--accent); color: #04241b; }
  .sidebar-footer { margin-top: auto; padding: 16px 20px; }
  .pill {
    display: inline-block; font-size: 10px; color: var(--text-faint);
    border: 1px solid var(--border); border-radius: 999px; padding: 3px 9px;
  }

  /* main column */
  .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .topbar {
    padding: 14px 24px; border-bottom: 1px solid var(--border); display: flex;
    align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }
  .topbar h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .topbar-stats {
    display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim);
  }
  .dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
    display: inline-block;
  }
  .banner {
    padding: 8px 24px; background: #3a1f1f; color: #ffb4b4; font-size: 12.5px;
    display: none;
  }

  .chart-drawer {
    border-bottom: 1px solid var(--border); background: var(--bg-card); padding: 14px 24px;
  }
  .chart-drawer[hidden] { display: none; }
  .chart-head {
    display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; flex-wrap: wrap;
  }
  .chart-head h2 { font-size: 13px; margin: 0; font-weight: 600; }
  .chart-head .sub { color: var(--text-dim); font-size: 12px; }
  #chart-close {
    margin-left: auto; color: var(--text-dim); text-decoration: none; font-size: 12px;
  }
  #chart-canvas {
    width: 100%; height: 300px; display: block; background: var(--bg-card-2);
    border: 1px solid var(--border); border-radius: 6px;
  }
  .legend {
    display: flex; gap: 16px; margin-top: 8px; font-size: 11px; color: var(--text-dim);
  }
  .legend .swatch {
    display: inline-block; width: 10px; height: 2px; margin-right: 4px;
    vertical-align: middle;
  }

  .scroll-area { flex: 1; overflow-y: auto; padding: 20px 24px 40px; }
  .panel { display: none; }
  .panel.active { display: block; }

  .card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px 18px; margin-bottom: 18px;
  }
  .card h3 {
    margin: 0 0 12px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-dim); font-weight: 700; display: flex; align-items: center; gap: 10px;
  }
  .card h3 .pill { text-transform: none; letter-spacing: 0; font-weight: 500; margin-left: auto; }

  .stat-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px; margin-bottom: 20px;
  }
  .stat-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 16px;
  }
  .stat-value { font-size: 25px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .stat-label { color: var(--text-dim); font-size: 11.5px; margin-top: 3px; }
  .stat-value.up { color: var(--up); }

  .screen-picker { margin-bottom: 12px; }
  select {
    background: var(--bg-card-2); color: var(--text); border: 1px solid var(--border);
    padding: 6px 10px; font: inherit; border-radius: 6px;
  }

  table {
    border-collapse: collapse; width: 100%; font-size: 12.5px;
    font-variant-numeric: tabular-nums;
  }
  th, td { text-align: right; padding: 7px 10px; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th {
    color: var(--text-dim); font-weight: 700; font-size: 10.5px; text-transform: uppercase;
    letter-spacing: 0.03em; border-bottom: 1px solid var(--border);
  }
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:last-child { border-bottom: none; }
  tr.clickable { cursor: pointer; }
  tr.clickable:hover { background: rgba(255,255,255,0.03); }
  tr.selected td:first-child { color: var(--accent); font-weight: 700; }
  td.detail { text-align: left; white-space: normal; color: var(--text-dim); }
  .up { color: var(--up); } .down { color: var(--down); }
  .empty { color: var(--text-faint); font-style: italic; padding: 10px 0; }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 10px;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap;
  }
  .badge-breakout, .badge-rebreak, .badge-breakout_up {
    background: rgba(45,212,167,0.15); color: var(--up);
  }
  .badge-pullback { background: rgba(245,185,66,0.15); color: var(--warn); }
  .badge-watch, .badge-emerging, .badge-idle {
    background: rgba(138,147,163,0.15); color: var(--text-dim);
  }
  .badge-fakeout, .badge-breakout_down {
    background: rgba(255,107,107,0.15); color: var(--down);
  }

  footer.disclaimer {
    padding: 12px 24px; color: var(--text-faint); font-size: 11px;
    border-top: 1px solid var(--border);
  }

  @media (max-width: 760px) {
    .sidebar { width: 60px; flex-basis: 60px; }
    .nav-label, .brand-name, .nav-item .label, .nav-badge { display: none; }
    .nav-item { justify-content: center; padding: 10px 0; }
    .brand { justify-content: center; padding: 16px 0; }
  }
</style>
</head>
<body>
<div class="app">
  <nav class="sidebar">
    <div class="brand">
      <span class="brand-mark">&#9670;</span><span class="brand-name">LOCAL HIGH</span>
    </div>
    <div class="nav-group">
      <a class="nav-item" data-section="overview">
        <span class="nav-icon">&#9638;</span><span class="label">Overview</span>
      </a>
    </div>
    <div class="nav-group">
      <div class="nav-label">Crypto Analytics</div>
      <a class="nav-item" data-section="nlh">
        <span class="nav-icon">&#8599;</span><span class="label">New Local High</span>
        <span class="nav-badge" id="nav-badge-nlh">0</span>
      </a>
      <a class="nav-item" data-section="screener">
        <span class="nav-icon">&#9906;</span><span class="label">Screener</span>
      </a>
      <a class="nav-item" data-section="patterns">
        <span class="nav-icon">&#9686;</span><span class="label">Chart Patterns</span>
        <span class="nav-badge" id="nav-badge-patterns">0</span>
      </a>
    </div>
    <div class="sidebar-footer"><span class="pill">research-only</span></div>
  </nav>
  <div class="main">
    <header class="topbar">
      <h1 id="page-title">Overview</h1>
      <div class="topbar-stats"><span class="dot"></span><span id="meta">loading…</span></div>
    </header>
    <div class="banner" id="banner"></div>
    <div class="chart-drawer" id="chart-drawer" hidden>
      <div class="chart-head">
        <h2>Chart — <span id="chart-symbol"></span></h2>
        <span class="sub" id="chart-sub"></span>
        <a href="#" id="chart-close">close &#10005;</a>
      </div>
      <canvas id="chart-canvas" width="1100" height="300"></canvas>
      <div class="legend" id="chart-legend"></div>
    </div>
    <main class="scroll-area">
      <section id="section-overview" class="panel">
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-value" id="stat-universe">-</div>
            <div class="stat-label">Universe scanned</div></div>
          <div class="stat-card"><div class="stat-value up" id="stat-nlh">-</div>
            <div class="stat-label">Active NLH setups</div></div>
          <div class="stat-card"><div class="stat-value" id="stat-patterns">-</div>
            <div class="stat-label">Patterns forming</div></div>
          <div class="stat-card"><div class="stat-value up" id="stat-breakouts">-</div>
            <div class="stat-label">Pattern breakouts</div></div>
        </div>
        <div class="card">
          <h3>Top New Local High setups</h3>
          <div id="overview-nlh"></div>
        </div>
        <div class="card">
          <h3>Latest pattern breakouts</h3>
          <div id="overview-patterns"></div>
        </div>
      </section>
      <section id="section-nlh" class="panel">
        <div class="card">
          <h3>New Local High — active setups</h3>
          <div id="nlh"></div>
        </div>
      </section>
      <section id="section-screener" class="panel">
        <div class="card">
          <h3>Preset screeners <span class="pill" id="screen-count"></span></h3>
          <div class="screen-picker"><select id="screen-select"></select></div>
          <div id="screen-table"></div>
        </div>
      </section>
      <section id="section-patterns" class="panel">
        <div class="card">
          <h3>Chart patterns</h3>
          <div id="patterns"></div>
        </div>
      </section>
    </main>
    <footer class="disclaimer">
      Research-only. Not financial advice — nothing here places an order.
    </footer>
  </div>
</div>
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

function badge(text, cls) {
  return `<span class="badge badge-${cls}">${text}</span>`;
}

function table(rows, cols) {
  if (!rows.length) return '<div class="empty">no matches</div>';
  const head = cols.map(c => `<th>${c.label}</th>`).join('');
  const body = rows.map(r => {
    const sel = r.symbol === selectedSymbol ? ' selected' : '';
    const attrs = r.symbol ? ` class="clickable${sel}" data-symbol="${r.symbol}"` : '';
    return '<tr' + attrs + '>' + cols.map(c => {
      const cls = c.cls ? c.cls(r) : '';
      const val = c.fmt ? c.fmt(r) : fmt(r[c.key]);
      return `<td class="${cls}">${val}</td>`;
    }).join('') + '</tr>';
  }).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// --------------------------------------------------------------------- //
// Section routing (sidebar nav <-> #hash <-> visible panel)
// --------------------------------------------------------------------- //
const SECTION_TITLES = {
  overview: 'Overview', nlh: 'New Local High', screener: 'Preset Screeners',
  patterns: 'Chart Patterns',
};
function showSection(name) {
  if (!SECTION_TITLES[name]) name = 'overview';
  document.querySelectorAll('.panel').forEach(
    el => el.classList.toggle('active', el.id === 'section-' + name)
  );
  document.querySelectorAll('.nav-item').forEach(
    el => el.classList.toggle('active', el.dataset.section === name)
  );
  document.getElementById('page-title').textContent = SECTION_TITLES[name];
}
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', (e) => { e.preventDefault(); location.hash = el.dataset.section; });
});
window.addEventListener('hashchange', () => showSection(location.hash.slice(1)));

// Delegate row clicks once per container - innerHTML gets replaced every
// tick, so binding per-row would leak listeners.
function onRowClick(containerId) {
  document.getElementById(containerId).addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-symbol]');
    if (tr) openChart(tr.dataset.symbol);
  });
}
['nlh', 'screen-table', 'patterns', 'overview-nlh', 'overview-patterns'].forEach(onRowClick);

const NLH_COLS = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'state', label: 'State', fmt: r => badge(r.state, r.state.toLowerCase()) },
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
];
const SCREEN_COLS = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'price', label: 'Price' },
  { key: 'detail', label: 'Detail', fmt: r => r.detail, cls: () => 'detail' },
];
const PATTERN_COLS = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'pattern', label: 'Pattern' },
  { key: 'status', label: 'Status', fmt: r => badge(r.status.replace('_', ' '), r.status) },
  { key: 'price', label: 'Price' },
  { key: 'target', label: 'Target' },
  { key: 'note', label: 'Note', fmt: r => r.note, cls: () => 'detail' },
];

function renderInto(containerId, rows, cols) {
  document.getElementById(containerId).innerHTML = table(rows, cols);
}

let lastData = null;
let selectedSymbol = null;

function renderOverview() {
  document.getElementById('stat-universe').textContent = lastData.universe_size;
  document.getElementById('stat-nlh').textContent = lastData.nlh_active.length;
  document.getElementById('stat-patterns').textContent = lastData.patterns.length;
  const breakouts = lastData.patterns.filter(p => p.status !== 'emerging');
  document.getElementById('stat-breakouts').textContent = breakouts.length;
  renderInto('overview-nlh', lastData.nlh_active.slice(0, 5), NLH_COLS);
  renderInto('overview-patterns', breakouts.slice(0, 5), PATTERN_COLS);
}

function renderAll() {
  if (!lastData) return;
  renderInto('nlh', lastData.nlh_active, NLH_COLS);
  renderInto('screen-table', lastData.screens[screenSelect.value] || [], SCREEN_COLS);
  renderInto('patterns', lastData.patterns, PATTERN_COLS);
  renderOverview();
  document.getElementById('nav-badge-nlh').textContent = lastData.nlh_active.length;
  document.getElementById('nav-badge-patterns').textContent =
    lastData.patterns.filter(p => p.status !== 'emerging').length;
  const total = Object.values(lastData.screens).reduce((n, rows) => n + rows.length, 0);
  document.getElementById('screen-count').textContent = `${total} matches across all presets`;
}
screenSelect.addEventListener('change', renderAll);

// --------------------------------------------------------------------- //
// Chart drawer: a candlestick canvas with the pattern's fitted support/
// resistance lines (or the NLH breakout level/stop/target) drawn on top.
// --------------------------------------------------------------------- //
const LINE_COLORS = { resistance: '#ff9f5a', support: '#5ac8ff', target: '#2dd4a7',
  level: '#8a93a3', stop: '#ff6b6b', tp1: '#2dd4a7' };

function buildOverlay(symbol, n) {
  const lines = [];
  const titleParts = [];
  if (!lastData) return { lines, title: '' };

  const p = lastData.patterns.find(x => x.symbol === symbol);
  if (p) {
    lines.push({ x0: 0, y0: p.resistance.value_start, x1: n - 1, y1: p.resistance.value_now,
      color: LINE_COLORS.resistance, dash: [5, 3], label: 'resistance' });
    lines.push({ x0: 0, y0: p.support.value_start, x1: n - 1, y1: p.support.value_now,
      color: LINE_COLORS.support, dash: [5, 3], label: 'support' });
    if (p.target !== null && p.target !== undefined) {
      lines.push({ x0: 0, y0: p.target, x1: n - 1, y1: p.target,
        color: LINE_COLORS.target, dash: [2, 3], label: 'target' });
    }
    titleParts.push(`${p.pattern} · ${p.status}`);
  }

  const nlh = lastData.nlh_active.find(x => x.symbol === symbol);
  if (nlh) {
    if (nlh.level) {
      lines.push({ x0: 0, y0: nlh.level, x1: n - 1, y1: nlh.level,
        color: LINE_COLORS.level, dash: [5, 3], label: 'level' });
    }
    if (nlh.stop) {
      lines.push({ x0: 0, y0: nlh.stop, x1: n - 1, y1: nlh.stop,
        color: LINE_COLORS.stop, dash: [2, 3], label: 'stop' });
    }
    if (nlh.tp1) {
      lines.push({ x0: 0, y0: nlh.tp1, x1: n - 1, y1: nlh.tp1,
        color: LINE_COLORS.tp1, dash: [2, 3], label: 'tp1' });
    }
    titleParts.push(nlh.state);
  }

  return { lines, title: titleParts.join(' · ') };
}

function drawChart(canvas, candles, overlay) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (!candles.length) return;

  const padL = 6, padR = 66, padT = 14, padB = 10;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const n = candles.length;

  let lo = Math.min(...candles.map(c => c.l));
  let hi = Math.max(...candles.map(c => c.h));
  for (const line of overlay.lines) {
    lo = Math.min(lo, line.y0, line.y1);
    hi = Math.max(hi, line.y0, line.y1);
  }
  const span = (hi - lo) || Math.abs(hi) || 1;
  lo -= span * 0.05; hi += span * 0.05;

  const xAt = i => padL + (i + 0.5) * (plotW / n);
  const yAt = v => padT + plotH - ((v - lo) / (hi - lo)) * plotH;
  const bw = Math.max(1.5, (plotW / n) * 0.6);

  ctx.lineWidth = 1;
  for (let i = 0; i < n; i++) {
    const c = candles[i];
    const x = xAt(i);
    const up = c.c >= c.o;
    ctx.strokeStyle = ctx.fillStyle = up ? '#2dd4a7' : '#ff6b6b';
    ctx.beginPath();
    ctx.moveTo(x, yAt(c.h)); ctx.lineTo(x, yAt(c.l)); ctx.stroke();
    const yo = yAt(c.o), yc = yAt(c.c);
    ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
  }

  ctx.lineWidth = 1.5;
  ctx.font = '11px -apple-system, "Segoe UI", sans-serif';
  ctx.textAlign = 'left';
  for (const line of overlay.lines) {
    ctx.strokeStyle = line.color;
    ctx.setLineDash(line.dash || []);
    ctx.beginPath();
    ctx.moveTo(xAt(line.x0), yAt(line.y0));
    ctx.lineTo(xAt(line.x1), yAt(line.y1));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = line.color;
    ctx.fillText(fmt(line.y1), padL + plotW + 4, yAt(line.y1) + 3);
  }

  ctx.fillStyle = '#8a93a3';
  ctx.fillText(fmt(hi), padL + plotW + 4, padT + 4);
  ctx.fillText(fmt(lo), padL + plotW + 4, padT + plotH);
}

function renderLegend(overlay) {
  const el = document.getElementById('chart-legend');
  if (!overlay.lines.length) { el.innerHTML = ''; return; }
  el.innerHTML = overlay.lines.map(l =>
    `<span><span class="swatch" style="background:${l.color}"></span>${l.label}</span>`
  ).join('');
}

async function refreshChart() {
  if (!selectedSymbol) return;
  const drawer = document.getElementById('chart-drawer');
  const canvas = document.getElementById('chart-canvas');
  const subEl = document.getElementById('chart-sub');
  document.getElementById('chart-symbol').textContent = selectedSymbol;
  drawer.hidden = false;
  try {
    const res = await fetch(
      '/api/candles/' + encodeURIComponent(selectedSymbol), { cache: 'no-store' }
    );
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const overlay = buildOverlay(selectedSymbol, data.candles.length);
    subEl.textContent = overlay.title || '';
    drawChart(canvas, data.candles, overlay);
    renderLegend(overlay);
  } catch (e) {
    subEl.textContent = 'no chart data cached yet — appears once this symbol matches again';
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    document.getElementById('chart-legend').innerHTML = '';
  }
}

async function openChart(symbol) {
  selectedSymbol = symbol;
  renderAll();          // re-render tables so the selected row highlights
  await refreshChart();
}

document.getElementById('chart-close').addEventListener('click', (e) => {
  e.preventDefault();
  selectedSymbol = null;
  document.getElementById('chart-drawer').hidden = true;
  renderAll();
});

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
    if (selectedSymbol) await refreshChart();
  } catch (e) {
    banner.style.display = 'block';
    banner.textContent = 'could not reach /api/state: ' + e;
  }
}
tick();
setInterval(tick, REFRESH_MS);
showSection(location.hash.slice(1) || 'overview');
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
