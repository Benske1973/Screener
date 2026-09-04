# local-high-scanner

Research-only **"New Local High"** breakout scanner for KuCoin USDT spot markets,
built on the altFINS *"Find Big Breakouts Early / Use New Local High"* idea:
*strength is followed by more strength* — watch for altcoins that **close above a
prior swing high** on volume, not for oversold "cheap" coins.

It continuously scans the liquid KuCoin USDT spot universe on a configurable
timeframe (**1h by default** — catches the break near the level instead of after
a 4h candle has already run), tracks each candidate through a **breakout →
pullback → re-break** state machine, scores confluence (relative volume, 24h
move, breakout strength, trend), writes every transition to CSV, and sends one
Telegram message per fresh signal.

> Public market data only. No API key, no private endpoints, no orders. A
> scanner hit is a research signal, not advice.

## Decision path

```
UNIVERSE  ─ enabled KuCoin *-USDT spot, no stables, no leveraged tokens
   │
LIQUIDITY ─ 24h quote volume ≥ min_24h_quote_volume
   │
NEW LOCAL HIGH ─ last closed candle's close > max high of the prior N candles
   │              (N = each entry in `lookbacks`; "fresh" = the prior candle was not already a high)
   │
STATE MACHINE
   WATCH     within near_high_pct below resistance (optional heads-up)
   BREAKOUT  fresh close above resistance          → alert, entry 1 (50%), stop just below the level
   PULLBACK  price retests the broken level as support
   REBREAK   new high after the pullback            → alert, add remaining 50%, stop → pullback low
   FAKEOUT   close back below the level (× invalidate_pct)  → alert, setup closed
```

Confluence is a **score, not a gate** — the only hard filters are liquidity and
the local-high test itself. `min_rvol` optionally silences low-volume breakout
pings without stopping the state machine.

## Trade plan (entry / stop / targets)

Every `BREAKOUT` and `REBREAK` carries a **deterministic, rule-based plan** — the
same fields an altFINS-style setup shows, computed from the strategy, not from a
language model:

| Field | BREAKOUT | REBREAK |
|---|---|---|
| entry | breakout candle close | re-break candle close |
| stop | `breakout level × (1 − stop_buffer_pct)` | pullback low |
| `1R` | `entry − stop` (initial risk), also shown as % of entry | same |
| targets | merged & sorted from the three methods below | same |
| `extended_pct` | how far entry sits above the broken level — a warning when large | — |

Target methods (each toggleable, results merged and de-duplicated, capped at `max_targets`):

1. **R-multiples** — `entry + N × 1R` for each `target_r_multiples` value (default 2R, 3R, 5R).
2. **Measured move** — base height (`breakout level − base low`) projected above the level.
3. **Structure** — prior swing-high resistance sitting above the entry (`pivot highs`
   over `candle_history`, window `structure_swing_window`).

Each target reports its price, R-multiple, and % gain. The plan also notes
"move stop to breakeven after TP`n`" (`breakeven_after_target`) and, when the
entry is `extended_entry_warn_pct` above the broken level, that waiting for the
pullback re-break gives a tighter stop and better R:R.

> These are mechanical levels from a documented strategy, not advice, and the
> scanner never opens, sizes, or manages a position — it prints the plan and
> stops. Every alert is tagged *research-signaal, geen order geplaatst*.

## Install (Windows 11 / PowerShell)

```powershell
cd C:\Users\benny\high
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run

```powershell
local-high-scanner --config config.yaml            # continuous, cycle every cycle_seconds
local-high-scanner --config config.yaml --once      # a single scan, then exit
local-high-scanner --config config.yaml --once --dry-run   # single scan, never touch Telegram
local-high-scanner --config config.yaml --list-universe    # print eligible symbols and exit
```

`python -m local_high ...` works too. `Ctrl+C` stops the loop cleanly.

## Preset screeners

altFINS-style "does this symbol match right now" filters — stateless, read-only,
independent of the New Local High state machine above. One command scans the
whole universe once and prints matches, then exits.

```powershell
local-high-scanner --config config.yaml --list-screens        # see what's available
local-high-scanner --config config.yaml --screen new_local_high
local-high-scanner --config config.yaml --screen strong_uptrend
```

| Screen | Matches when... |
|---|---|
| `new_local_high` / `new_local_low` | close breaks the highest high / lowest low of the prior `screen_lookback` candles |
| `strong_uptrend` | close above the fast EMA, fast EMA above the slow EMA |
| `pullback_in_uptrend` | uptrend intact, but price dipped off its recent high by `screen_pullback_min_pct` |
| `very_oversold` | RSI at or below `screen_rsi_oversold` |
| `oversold_in_uptrend` | mildly oversold RSI while price still trades above the slow EMA |
| `bullish_ema_crossover` | fast EMA just crossed above the slow EMA |
| `bullish_macd_crossover` | MACD line just crossed above its signal line |
| `bollinger_breakout` | close broke above the upper Bollinger Band |
| `rvol_spike_in_uptrend` | relative-volume spike while price trades above the slow EMA |

Tune the thresholds under `# --- Preset screeners ---` in [`config.yaml`](config.yaml).

## Chart patterns

Fits a resistance line through recent swing highs and a support line through
recent swing lows (least-squares over `pivot_highs`/`pivot_lows`), classifies
the pair by slope (triangle / wedge / channel), and reports whether price is
still trading between the lines (`emerging`) or just cleared one
(`breakout_up` / `breakout_down`), with a measured-move target on a breakout.

```powershell
local-high-scanner --config config.yaml --patterns                       # all pattern types
local-high-scanner --config config.yaml --patterns --pattern falling_wedge
```

Patterns detected: `ascending_triangle`, `descending_triangle`, `rising_wedge`,
`falling_wedge`, `ascending_channel`, `descending_channel`. Head-and-shoulders
and double top/bottom need a different detector (peak/trough counting, not a
two-line fit) and are intentionally out of scope for now. Tune the fit under
`# --- Chart-pattern detection ---` in [`config.yaml`](config.yaml) —
`pattern_min_r2` rejects noisy/non-genuine lines, `pattern_flat_slope_pct` and
`pattern_parallel_tol_pct` control triangle-vs-wedge-vs-channel classification.

## Web dashboard

`local-high-web` serves the New Local High board, every preset screener, and
chart patterns as one page, refreshed on a timer (`web_refresh_seconds` in
config.yaml, default 60s) — one candle fetch per cycle powers all three, so it
doesn't re-hit KuCoin per screen.

```powershell
local-high-web --config config.yaml                # http://127.0.0.1:8787
local-high-web --config config.yaml --port 8080
```

It's read-only: no orders, no Telegram, just the same analytics the CLI
prints, in a browser. `python -m local_high.webapp ...` works too.

Click any symbol in the New Local High, screener or pattern tables to open an
altFINS-style candlestick chart for it — patterns draw their fitted
support/resistance lines and measured-move target, New Local High setups draw
the broken level, stop and TP1. Charts are canvas-drawn, no charting library.
Only symbols currently matching something are cached for charting (not the
whole universe) — the chart appears once a symbol next matches.

### Expose it through Cloudflare (no port-forwarding needed)

1. Set an access token first — the dashboard has **no login of its own**, and
   without a token anyone with the URL can view it:
   ```powershell
   $env:LOCAL_HIGH_WEB_TOKEN='pick-a-long-random-string'
   local-high-web --config config.yaml
   ```
   Visit once with `?token=...` in the URL; the dashboard sets a cookie so you
   don't need to repeat it. A `Authorization: Bearer <token>` header works too.
2. Install `cloudflared` ([Cloudflare's downloads page](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)).
3. **Quickest option — no Cloudflare account needed**, a temporary public URL:
   ```powershell
   cloudflared tunnel --url http://localhost:8787
   ```
   Prints a `https://<random-words>.trycloudflare.com` URL that proxies to your
   local dashboard. It changes every time you restart the tunnel — fine for
   personal use, not for a link you want to keep.
4. **Stable URL on your own domain** (needs a domain added to a Cloudflare
   account):
   ```powershell
   cloudflared tunnel login
   cloudflared tunnel create local-high
   cloudflared tunnel route dns local-high scanner.yourdomain.com
   ```
   Then a `config.yml` for cloudflared:
   ```yaml
   tunnel: local-high
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: scanner.yourdomain.com
       service: http://localhost:8787
     - service: http_status:404
   ```
   Run it with `cloudflared tunnel run local-high`, or `cloudflared service
   install` to run it as a Windows service alongside the scanner. For an extra
   layer beyond the token, put the hostname behind **Cloudflare Access**
   (Zero Trust → Access → Applications) to require an email login before
   `cloudflared` even forwards the request.

Keep `local-high-web` bound to `127.0.0.1` (the default) — the tunnel reaches
it locally; there's no need to bind `0.0.0.0` or open a firewall port.

### Telegram

Credentials come from environment variables, never `config.yaml`:

```powershell
$env:LOCAL_HIGH_TELEGRAM_BOT_TOKEN='paste-bot-token'
$env:LOCAL_HIGH_TELEGRAM_CHAT_ID='paste-chat-id'
local-high-scanner --config config.yaml
```

If either variable is missing, alerts still print to the console and the run
continues. Revoke any bot token that has been pasted into a chat or a file.

### Keep it running

The plain command already loops forever (`cycle_seconds` between scans). To keep
it alive on Windows:

- **Simplest:** leave the PowerShell window open. It reconnects and backs off on
  KuCoin errors on its own.
- **Survives logout / reboot:** Task Scheduler → *Create Task* → trigger *At log on*
  (or *At startup*), action `Start a program`:
  - Program: `C:\Users\benny\high\.venv\Scripts\python.exe`
  - Arguments: `-m local_high --config config.yaml`
  - Start in: `C:\Users\benny\high`
  - Set the two `LOCAL_HIGH_TELEGRAM_*` values as **user** environment variables
    (System → Environment Variables) so the scheduled task inherits them.
  - Tick *Run whether user is logged on or not* and *If the task fails, restart
    every 1 minute*.

State lives in `data/state.json`, so a restart never re-fires alerts you already
got (subject to `alert_cooldown_seconds`).

## Configuration

Everything lives in [`config.yaml`](config.yaml); percentage fields are
percentage points (`1.5` = 1.5%). The values that matter most:

| Key | Meaning | Default |
|---|---|---:|
| `timeframe` | KuCoin candle type | `1hour` |
| `lookbacks` | candle windows for the local-high test | `[24, 120]` |
| `candle_history` | candles fetched per symbol (via `startAt`/`endAt`) | `720` |
| `breakout_source` / `prior_high_source` | what must clear / how resistance is measured | `close` / `high` |
| `min_breakout_pct` | minimum close over the prior high | `0.05` |
| `require_fresh` | only fire on the just-closed breakout candle | `true` |
| `near_high_pct` | WATCH proximity band below resistance | `1.5` |
| `max_breakout_distance_pct` | skip the BREAKOUT ping when the close is already this far above the level (0 = off; state still tracked) | `5.0` |
| `min_24h_quote_volume` | liquidity gate (USDT turnover) | `500000` |
| `min_rvol` | alert gate for BREAKOUT/REBREAK | `1.0` |
| `pullback_retest_pct` | how close a low must come to the level to count as a retest | `0.5` |
| `pullback_max_candles` | give up on the setup after this many candles | `36` |
| `invalidate_pct` | close this far below the level ⇒ FAKEOUT | `1.0` |
| `stop_buffer_pct` | suggested stop = level × (1 − this%) | `0.5` |
| `target_r_multiples` | TP levels as multiples of initial risk | `[2, 3, 5]` |
| `target_measured_move` / `target_structure` | extra target methods | `true` / `true` |
| `max_targets` | cap on targets per setup | `5` |
| `extended_entry_warn_pct` | warn when entry is this far above the level | `8.0` |
| `alert_on` | which transitions ping (add `WATCH` for pre-breakout heads-ups) | `[BREAKOUT, REBREAK, FAKEOUT]` |
| `alert_cooldown_seconds` | per symbol + event kind | `21600` |
| `cycle_seconds` | seconds between scans | `300` |

`lookbacks: [24, 120]` on 1h ≈ a 1-day high (early momentum) and a 5-day high
(structure). For slower, bigger setups use `timeframe: 4hour`,
`lookbacks: [30, 90]`; for the video's daily semantics `timeframe: 1day`,
`lookbacks: [15, 50]`. Keep `candle_history` well past `max(lookbacks) + 3`.

**Catching the break early vs. chasing:** on 1h the BREAKOUT fires within an hour
of price clearing the level, so entries sit ~0-3% above resistance instead of the
10-20% you'd see waiting for a 4h close. `max_breakout_distance_pct` is the guard
for gap-ups; add `WATCH` to `alert_on` to be pinged *before* the break while a
coin is within `near_high_pct` of its local high.

## Output

| Path | Contents |
|---|---|
| stdout | ranked table of active setups after every cycle, plus `*** ALERT ***` blocks |
| `data/events.csv` | one row per state transition + `risk_pct`, `tp1..tp3`, `rr_last` (append-only) |
| `data/state.json` | per-symbol state machine snapshot (atomic writes, survives restarts) |
| `logs/scanner.jsonl` | structured run log |

`data/` and `logs/` are git-ignored.

## Tests

```powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q src
```

## Layout

```
src/local_high/
  config.py       config.yaml loader + validation
  kucoin.py       public REST client (symbols, tickers, candles) with backoff
  universe.py     listing + liquidity gates
  indicators.py   candle parsing, rolling extremes, EMA/SMA/RSI/MACD/Bollinger, swings, linear regression
  strategy.py     detect_nlh() + the per-symbol state machine  (evaluate())
  targets.py      deterministic trade plan: entry, stop, R-multiple/measured-move/structure targets
  scoring.py      0-100 confluence score
  screens.py      preset "does this symbol match now" filters (--screen / --list-screens)
  patterns.py     triangle/wedge/channel detection from fitted support/resistance lines (--patterns)
  state.py        JSON state store
  events.py       CSV event log
  notifier.py     Telegram sender + message/table formatting
  scanner.py      cycle orchestration
  main.py         CLI (local-high-scanner)
  webapp.py       read-only web dashboard (local-high-web)
```

## Limitations

- KuCoin spot only; no shorts, margin, futures, balances, keys, or orders.
- `require_fresh` means a breakout that already happened before the scanner
  started is *seeded* silently (state tracked, no alert unless `alert_on_seed`).
- Wicks: with `breakout_source: close` a signal needs a candle **close** above
  resistance, which ignores intrabar spikes by design — and it fires only once the
  candle closes, so the break is confirmed up to one `timeframe` late.
- KuCoin's candle endpoint returns only ~100 rows unless a time range is sent, so
  the client passes `startAt`/`endAt` from `candle_history`; a symbol listed more
  recently than `max(lookbacks)` candles ago is skipped (logged once per cycle).
- A candidate is not evidence of profitability. Validate against `data/events.csv`
  and forward paper testing before trusting it.
- Preset screeners and chart patterns are rule-based heuristics, not backtested
  strategies — unlike the New Local High state machine, there's no historical
  win-rate behind them yet. Treat matches as ideas to investigate, not signals.
- Pattern detection only covers triangles, wedges and channels (a two-line
  fit); head-and-shoulders and double top/bottom aren't implemented.
