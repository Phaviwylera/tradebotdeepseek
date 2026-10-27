# Polymarket Calibrated Trading Bot

[![CI](https://github.com/Phaviwylera/tradebotdeepseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Phaviwylera/tradebotdeepseek/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![Mode](https://img.shields.io/badge/default-DRY--RUN-2ea043)

A persistent Python 3.11 service for Polymarket. It discovers active markets,
ingests order books over WebSocket with REST fallback, evaluates three
independent strategies, enforces hard risk limits, and writes every signal and
submitted order to a SQLite ledger. A read-only operations console ships in the
same repository and the same process.

**Default mode is `LIVE=false` (DRY-RUN). No private key is required and no
authenticated CLOB request or order is made.**

This is not a profit guarantee. It is an execution and risk framework around
concrete, measurable edges.

---

## Quick start

```bash
git clone https://github.com/Phaviwylera/tradebotdeepseek.git
cd tradebotdeepseek
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m unittest discover -s tests -v   # 80 tests, no network needed
python scripts/demo_server.py             # console on http://127.0.0.1:8099
python main.py                            # the real DRY-RUN loop
```

`make help` lists the same targets (`make install`, `make test`, `make lint`,
`make demo`, `make docker-build`).

---

## What runs where

| Piece | Process | Public? |
|---|---|---|
| Trading loop (`main.py`) | long-lived async worker | never listens publicly by choice |
| Read-only API + console (`web.py`) | thread inside the bot, or `dashboard_api.py` | `$PORT` on Render, loopback + nginx on EC2 |
| Console UI (`dashboard/public/`) | static files, served by the bot or Vercel | yes, contains no data |
| Vercel proxy (`dashboard/api/`) | serverless function | forwards `GET` reads to the bot |

One service is enough: the bot serves the API and the console. Vercel is only for
hosting the console elsewhere (and proxying reads to your bot). Vercel cannot run
the bot — serverless functions have no persistent loop, no WAL SQLite and no
WebSocket subscription.

### Deploy

| Target | Cost | Command / link |
|---|---|---|
| **Render** (default: free plan) | $0, no card | [Blueprint deploy](https://render.com/deploy?repo=https://github.com/Phaviwylera/tradebotdeepseek) — web service + `/healthz` + keep-alive workflow |
| **Oracle Cloud Always Free VM** | $0 forever | `sudo bash -c "$(curl -fsSL .../deploy/ec2-setup.sh)"` |
| **Google Cloud e2-micro** | $0 forever | same bootstrap script |
| **AWS EC2** | free tier / ~$8/mo | same bootstrap script |
| **Vercel** | $0 | `cd dashboard && npx vercel --prod`, then set `BOT_API_URL` |

No budget at all? Read **[docs/FREE_HOSTING.md](docs/FREE_HOSTING.md)** — it covers
what each free tier actually gives you (free Render sleeps and has no disk; the
repo ships a keep-alive workflow and an ephemeral-ledger mode for exactly that),
plus the two always-free VM options.

Full playbook, security groups, troubleshooting: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

---

## Strategies

### 1. YES + NO arbitrage

If `yes_ask + no_ask < 0.985`, the engine buys equal shares of both outcomes.
The pair's total cost is capped at `MAX_BET`, not `2 * MAX_BET`. The
`0.985` threshold leaves a 1.5-cent buffer for fees, rounding, and execution
risk. The reason string records the pair cost and profit.

The API cannot place both legs atomically. If the second leg fails, the bot
logs a critical unhedged-position incident, attempts to cancel the first leg,
and conservatively books the first leg in the risk ledger.

### 2. Near-resolution sniping

Only when:

- stage 1: `ttl_days < 0.10` (under 2.4 hours), **or** up to
  `SNIPE_OVERDUE_HOURS` (default 24) *past* the scheduled close, and
- stage 2: the best ask is in `[0.93, 0.9314)`, and
- stage 3: `(0.95 - ask) / ask > MIN_EV`.

Gamma's `endDate` is the scheduled close, which for a sports market is kick-off:
a match in its 88th minute has a *negative* ttl. Those in-play markets are
exactly where a 0.93 ask usually means "this resolves YES", so rejecting
`ttl < 0` used to hide the whole case.

**Discovery matters as much as the gate.** Until recently the pipeline only ever
evaluated the highest-volume markets, which essentially never close within
hours — so this strategy could not fire no matter what the prices did. The
scanner now runs a second lane (`end_date_min`/`end_date_max`, volume floor
ignored) and `NEAR_RESOLUTION_SLOTS` of the per-cycle budget are reserved for
it. `NEAR_RESOLUTION_WINDOW_HOURS` (default 6) sets how far ahead to look.

The 95% hold probability is an assumption, not an observed fact. With
`MIN_EV=0.02`, practically only the lower part of the price band passes. This
is intentional. A market at 0.97 has little remaining edge under that
assumption.

**Widening the band is a decision, not a fix.** The strict defaults produce
`[0.9300, 0.9314)` — 0.14 cents wide — which is honest but almost never trades.
`MIN_EV`, `SNIPE_MIN_ASK`, `SNIPE_MAX_ASK`, `SNIPE_HOLD_PROBABILITY`,
`SNIPE_MAX_TTL_HOURS` and `ARB_THRESHOLD` are all env vars, so a deployment can
opt into a wider band explicitly (`render.yaml` ships `MIN_EV=0.01` and
`SNIPE_MIN_ASK=0.90`, i.e. `[0.9000, 0.9406)`). The cost is real: the 0.95 hold
assumption is being applied at 0.90, where it is least reliable, so the same
expected value buys a weaker bet. The live band is published on `/healthz`
(`gates.snipe_band`) and in the console banner — never hard-coded in the page.

The second and third conditions are the same inequality, so the code reconciles
them: the highest ask that can ever fire is
`hold / (1 + MIN_EV)` = `0.95 / 1.02` ≈ **0.9314**, not 0.97. At startup the
engine logs the resulting gates so a quiet console is explainable rather than
mysterious:

```text
Signal gates: ARB fires when yes_ask + no_ask < 0.985; SNIPE fires when
ttl_days < 0.10 (or up to 24h past the scheduled close) and the best ask is in
[0.9000, 0.9406) with hold=0.95 min_ev=0.0100
```

The numbers in that line and on the console come from the running process, so a
widened deployment reports the widened band.

Both gates are genuinely rare: a complete YES+NO set below 0.985, or a market
resolving in under 2.4 hours trading under 0.9314. `/healthz` reports
`markets_scanned`, `markets_evaluated` and `signals_last_cycle`, plus a `gates`
block that says how close the cycle actually got:

```json
"gates": {"near_resolution_candidates": 12, "near_resolution_selected": 8,
          "markets_in_snipe_band": 0,
          "closest_snipe_price": {"ask": 0.9123, "distance": 0.0177},
          "best_arb_sum": 0.9902, "closest_ttl_hours": 1.25}
```

So "no signals" always has a cause you can read: no markets closing soon, none
inside the price band, or the cheapest pair still 0.5 cents above the arb
threshold.

### 3. Calibrated model divergence

`model.py` builds a time-ordered stacking ensemble over XGBoost, LightGBM,
HistGradientBoosting, ExtraTrees, and RandomForest, with a logistic final
estimator and feature passthrough. Out-of-fold base predictions are generated
with five expanding-window `TimeSeriesSplit` folds, then isotonic-calibrated.

> scikit-learn's `StackingClassifier` calls `cross_val_predict`, which rejects
> `TimeSeriesSplit` because its folds overlap. To avoid lookahead leakage this
> project uses a small `TimeSeriesStackingClassifier` with the same base
> estimators, logistic meta-model, five time folds, and `passthrough=True`,
> rather than silently switching to random K-fold.

The calibrated probability is shrunk 30% toward the market prior:

```text
anchored = 0.70 * model_probability + 0.30 * market_probability
```

A directional signal requires:

- anchored edge at least `MIN_EV` versus the executable best ask,
- selected-outcome probability at least 0.60,
- positive quarter-Kelly size, capped by `MAX_BET`.

If `model.joblib` is missing or fails to load, the bot deliberately runs
arbitrage and sniping only.

### 4. NegRisk basket parity

A NegRisk event has `M` mutually exclusive outcomes of which exactly one resolves
YES, so their prices must sum to 1.00 at settlement. If all `M` best asks can be
bought for less than 1.00 **after taker fees**, the payoff is certain: the trade
needs no forecast and no wallet, only market orders.

`negrisk.py` scores those baskets and refuses the two mistakes that make them
look better than they are:

- **partial baskets**: three of twelve outcomes sum to ~0.3, which reads as a 70%
  edge. `evaluate_basket(complete=False)` will not score one.
- **ignoring fees**: `M` legs means `M` taker fees, charged as `rate · p · (1−p)`.
  Three 0.32 legs read as a 4% edge at 0.96; three 5% fees turn that into 0.7%.

The cycle collects NegRisk events, fetches each event's members in one Gamma call
(`/markets?event_id=` silently ignores the filter, so membership comes from the
event), prices the complete basket, and records one signal per leg with
`executed=False`:

```text
NEGRISK <event> basket_cost=0.9315 profit_per_basket=0.0685 baskets=21 fee=0.0105 | DRY_RUN multi-leg execution not implemented
```

Recording rather than ordering is deliberate. The executor submits one order per
signal, so `M` signals can fill asymmetrically and leave a naked directional
position — the opposite of risk-free. `/healthz` publishes `negrisk_groups`,
`negrisk_checked` (events priced this cycle), `negrisk_incomplete` (events whose
members could not all be priced), `negrisk_best_cost` and `negrisk_best_profit`.
All four reset every cycle, so `{"checked": 2, "incomplete": 2, "best_cost": null}`
says exactly what happened rather than leaving a bare null. `NEGRISK_ENABLED=false` turns the lane off.

### 5. Growth feasibility and market-making quotes (tools, not strategies)

`research/growth.py` answers "can this bankroll reach this number" with
arithmetic instead of optimism. `$20 → $6,000 in 3 days` is 300x, **+569.43%/day**,
**3.54** consecutive all-in wins at 0.20, and a fair-priced survival probability
of **0.333%** — ruin 99.667% before costs. The useful identity: at a fair price
the chance of surviving an `M`-fold target is exactly `1/M`. Compounding converts
a tiny per-trade disadvantage into a near-certain wipeout.

`marketmaking.py` computes Avellaneda–Stoikov quotes (reservation price, optimal
spread, inventory skew) for the venue where makers pay no fee and receive a share
of taker fees. It computes only — resting orders need L2 credentials, a funded
wallet, and a host Polymarket does not geoblock. Note the parameter scale: `k` is
decay per unit of *price*, and these prices live in `(0, 1)`, so the FX default
`k≈1.5` quotes 0.001/0.999. `DEFAULT_K=100` gives a two-cent spread at a 0.60 mid.

Both are runnable without touching the bot:

```bash
python scripts/quant_report.py --start 20 --target 6000 --days 3
python scripts/quant_report.py --mid 0.62 --inventory 200
```

`docs/QUANT_STRATEGIES.md` records what was adopted from the quant memo this
implements, and what was rejected (its fee table is ~5x wrong; `convertPositions`
needs a funded wallet and a non-geoblocked host).

---

---

## Fees, and why the gates had to change

Polymarket charges taker fees: `fee = shares × rate × p × (1 − p)`, with the
rate varying by category (sports 0.05, crypto 0.07, politics/tech/finance 0.04,
geopolitics free). Gamma reports the parameters on every market as
`feeSchedule`.

This matters enormously at the prices this bot trades. At an ask of 0.94 with a
5% fee, the fee is **0.00282 a share against a 0.010 edge — 28% of the trade**.
The gates originally priced edge as `(hold − ask) / ask`, so they were blind to
it:

| ask 0.94, hold 0.95 | no fee | 5% taker fee |
|---|---|---|
| edge per share | 0.01000 | 0.00718 |
| return on capital | +1.06% | **+0.76%** |

With a 1% EV gate that trade should never have fired; it did, because the fee
was free. And at the original defaults the band was *empty*: the fee-aware cap
is `0.9281` at `MIN_EV=0.02`, below the `0.93` floor.

Now:

- `Market` carries the real `fee_rate`, and a market with no fee block is
  assumed to charge the sports rate rather than being treated as free.
- `check_snipe` charges the fee and its cap is fee-dependent
  (`[0.9000, 0.9377)` at `MIN_EV=0.01` with a 5% fee).
- `check_arbitrage` only takes a pair that clears 1.000 **after** both legs: at
  0.4925/0.4925 the old 0.985 threshold approved a pair costing **1.0096**.

## Measuring the assumption (the research loop)

Every snipe signal rests on one number that was never measured: the probability
that a market priced ~0.93 resolves YES. `docs/STRATEGY_RESEARCH.md` describes
the loop, and `scripts/strategy_lab.py` runs it:

```bash
python scripts/strategy_lab.py --markets 200 --lead-hours 2 \
    --min-ask 0.90 --max-ask 0.94 --out evidence/study.json
```

It reports a calibration table with Wilson intervals, charges the venue's fee,
corrects for the number of buckets tested, and returns `ACCEPT` / `REVISE` /
`REJECT` / `INSUFFICIENT`. `EVIDENCE_PATH` points the bot at the result, which
can only make the hold assumption *more* conservative:

```json
"evidence": {"source": "measured", "effective_hold": 0.884, "n": 214, "verdict": "REJECT"}
```

## Architecture

```text
scanner.py    Gamma market discovery + normalized CLOB books
stream.py     Public market-channel WebSocket, auto-reconnect
features.py   Canonical feature row ordered exactly like training
model.py      Calibrated stacking model, Bayesian anchor, joblib I/O
strategy.py   Arbitrage, snipe, and model-edge signals
risk.py       Kill switch, exposure, fills, settlements
executor.py   Authenticated py-clob-client GTC execution
ledger.py     SQLite signal/order audit log and runtime heartbeat
web.py        Read-only JSON API + static console (single source of routes)
main.py       Async 60-second pipeline and lifecycle
train_model.py Out-of-time evaluation and model export
dashboard/    Static console + Vercel proxy functions (no build step)
```

Pipeline each cycle:

1. Scan Gamma (`closed=false`, `volume > 5000`, `liquidity > 1000`, two CLOB
   token IDs).
2. Subscribe the WebSocket to the top markets and use fresh streamed books;
   fall back to CLOB REST snapshots.
3. Evaluate arbitrage, sniping, and model divergence.
4. Apply risk controls before each order. Record all signals, including
   risk-blocked and dry-run signals, with `executed=0`.
5. In LIVE, submit GTC limit orders and record the full API response.
6. Sleep 60 seconds. Errors are logged, followed by a 10-second retry sleep.

Signals are deduplicated per market and strategy for
`SIGNAL_COOLDOWN_SECONDS` (default 1 hour). The dedupe is stored in SQLite, so a
restart does not immediately re-fire the same order.

---

## Local setup (DRY-RUN)

Python 3.11 is required (3.12 works too; CI tests both).

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # optional; export the values you use

python -m unittest discover -s tests -v
python main.py
```

You do **not** need `POLYGON_PRIVATE_KEY` for DRY-RUN. The bot uses a public
CLOB client for books and the public market WebSocket.

Only the runtime dependencies are installed above. `pip install -r
requirements-ml.txt` adds `xgboost` and `lightgbm`, which are needed **only** to
train a model or to load an artifact trained with them (that file is ~1.5 GB
because of the CUDA runtime xgboost pulls in). The bot without a model runs
arbitrage and sniping only, by design.

The ledger defaults to `./ledger.db`:

```bash
sqlite3 ledger.db "select ts,market_id,reason,size_usd,executed from signals order by id desc limit 20;"
sqlite3 ledger.db "select ts,order_id,status from orders order by id desc limit 20;"
```

In DRY-RUN, `orders` stays empty. A real order is never submitted.

### Look at the console with sample data

```bash
python scripts/demo_server.py           # http://127.0.0.1:8099
python scripts/demo_server.py --port 3000 --host 0.0.0.0
```

It seeds a throwaway SQLite ledger and serves the real endpoints — no wallet, no
network, no account. The console is also deployed publicly from
`dashboard/public/`; see [dashboard/README.md](dashboard/README.md).

---

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `LIVE` | `false` | Master switch. `true` enables authenticated order placement. |
| `BANKROLL` | `200` | Starting USDC risk equity. |
| `MAX_BET` | `20` | Hard per-signal/per-arb-pair cap. |
| `POLYGON_PRIVATE_KEY` | — | Required only for LIVE. Use a secret manager. |
| `POLYMARKET_SIGNATURE_TYPE` | `0` | `0` EOA, `1` Magic/email, `2` browser proxy. |
| `POLYMARKET_FUNDER` | — | Proxy/funder address for signature types 1 and 2. |
| `MAX_DAILY_LOSS_PCT` | `0.20` | Kill switch at 20% daily equity loss. |
| `MAX_DRAWDOWN_PCT` | `0.40` | Kill switch at 40% peak drawdown. |
| `MAX_EXPOSURE_PCT` | `0.30` | Open cost basis as a fraction of equity. |
| `MIN_EV` | `0.02` | Minimum edge/return threshold. Also sets the snipe cap: `hold / (1 + MIN_EV)`. |
| `SNIPE_MIN_ASK` | `0.93` | Lower edge of the snipe price band. `0.93` → a 0.14-cent band; `0.90` → ~4 cents. |
| `SNIPE_MAX_ASK` | `0.97` | Upper edge the strategy is willing to look at (the EV cap usually binds first). |
| `SNIPE_HOLD_PROBABILITY` | `0.95` | Assumed probability a near-resolution market at the target price resolves YES. |
| `SNIPE_MAX_TTL_HOURS` | `2.4` | How far *before* the scheduled close sniping starts. |
| `ARB_THRESHOLD` | `0.985` | YES+NO ask must cost less than this to count as arbitrage. |
| `KELLY_FRACTION` | `0.25` | Fraction of full Kelly used to size model-edge signals. Validated to `(0, 1]`; `>1` is over-betting. |
| `NEGRISK_ENABLED` | `true` | Score complete NegRisk baskets priced under 1.00 after fees. |
| `NEGRISK_MAX_EVENTS` | `3` | How many NegRisk events to price per cycle (each is one extra Gamma call). |
| `NEGRISK_MIN_PROFIT` | `0.005` | Minimum post-fee profit per basket before a signal is recorded. |
| `LEDGER_PATH` | `ledger.db` | SQLite file. Falls back to `/tmp/polymarket-bot/` with a warning if the path is not writable. |
| `MODEL_PATH` | `model.joblib` | Optional calibrated model artifact. |
| `MODEL_URL` | *(empty)* | Download the artifact at startup when `MODEL_PATH` is absent. How a diskless host gets a model. |
| `SNAPSHOT_PATH` | *(empty)* | Append pre-resolution feature snapshots (training data). Off by default; needs a disk. |
| `NEAR_RESOLUTION_SLOTS` | `8` | Book-fetch slots reserved each cycle for markets closing soon. `0` restores volume-only selection (and makes SNIPE unreachable). |
| `NEAR_RESOLUTION_WINDOW_HOURS` | `6` | How far ahead the near-resolution lane looks. |
| `NEAR_RESOLUTION_OVERDUE_HOURS` | `24` | How far *past* `endDate` the lane still includes (in-play markets). |
| `SNIPE_OVERDUE_HOURS` | `24` | How late a market may be and still be snipeable. `0` requires `ttl >= 0`. |
| `EVIDENCE_PATH` | *(empty)* | Measured hold evidence from `scripts/strategy_lab.py`. Can only lower the hold assumption. |
| `SCAN_INTERVAL_SECONDS` | `60` | Pipeline cadence. |
| `MAX_MARKETS_PER_CYCLE` | `40` | Book/strategy working set. |
| `SIGNAL_COOLDOWN_SECONDS` | `3600` | Per-market/strategy dedupe window. |
| `BOOK_STALENESS_SECONDS` | `20` | Maximum WebSocket cache age. |
| `CANCEL_ON_SHUTDOWN` | `true` | Cancel open orders when the worker stops. |
| `LOG_LEVEL` | `INFO` | Logging verbosity. |
| `WEB_ENABLED` | `true` | Start the read-only HTTP server inside the bot. |
| `SERVE_DASHBOARD` | `true` | Also serve `dashboard/public/` from the bot. |
| `API_HOST` | `127.0.0.1`, or `0.0.0.0` when `PORT` is set | Bind address. |
| `API_PORT` | `8099`, or `$PORT` when set | Bind port. |
| `DASHBOARD_TOKEN` | — | Optional bearer token for `/api/*` reads. |
| `ALLOWED_ORIGIN` | — | Optional CORS origin for a split-origin console. |

All secrets are read from the environment. They are never written to the
repository, SQLite, or logs. `Settings.public_summary()` is the only config
object that is ever logged, and a test asserts it cannot leak a key or token.

The kill switch is permanent for the process session. A restart is an explicit
operator action, not an automatic recovery path.

---

## The read-only console

`web.py` is the single implementation of the HTTP surface. `main.py` starts it on
a daemon thread next to the trading loop; `dashboard_api.py` runs the same code as
a standalone sidecar when you want the API in a process that cannot trade.

| Path | Contents | Auth |
|---|---|---|
| `/healthz` | liveness probe **plus loop vitals**: `loop` (`ok`/`stalled`/`error`), last cycle age, markets scanned/evaluated, signals, last error | never |
| `/` | static console shell | never (no data) |
| `/api/status` | runtime mode, risk state, counts, recent rows | `DASHBOARD_TOKEN` |
| `/api/signals?limit=50` | recent signals | `DASHBOARD_TOKEN` |
| `/api/orders?limit=50` | recent orders | `DASHBOARD_TOKEN` |

Read-only by construction: `GET`/`HEAD` only (everything else is 405), the SQLite
connection is `PRAGMA query_only=ON`, there is no order route, and the process
never reads `POLYGON_PRIVATE_KEY`. Static assets are limited to a fixed extension
whitelist inside one directory, with path traversal rejected after resolution.

The console itself shows mode, equity, exposure, realized PnL, drawdown, risk
state, heartbeat age, pipeline counts, and the signal/order tables. It banners a
tripped kill switch, a stalled loop, the last error, or "demo data" when nothing
is connected. It refreshes every 15s, pauses when the tab is hidden, and never
needs a build step.

Typical `/api/status` payload:

```json
{
  "ready": true,
  "runtime": {
    "mode": "dry_run",
    "started_at": "2026-10-26T10:43:15.000+00:00",
    "updated_at": "2026-10-26T10:45:19.000+00:00",
    "markets_scanned": 40,
    "markets_evaluated": 37,
    "signals_emitted": 0,
    "equity": 200.0,
    "cash": 200.0,
    "exposure": 0.0,
    "realized_pnl": 0.0,
    "drawdown_pct": 0.0,
    "killed": false,
    "kill_reason": null,
    "last_error": null
  },
  "stale_seconds": 4.2,
  "uptime_seconds": 124.0,
  "counts": {"signals_total": 0, "signals_24h": 0, "orders_total": 0},
  "recent_signals": [],
  "recent_orders": []
}
```

Show `stale_seconds` in any UI you build: if it exceeds roughly three scan
intervals, the loop is stalled even though the container is up.

---

## Deploy

### Render (one service, console included)

1. Push this repository to GitHub.
2. [Deploy the blueprint](https://render.com/deploy?repo=https://github.com/Phaviwylera/tradebotdeepseek)
   (or **New → Blueprint** and pick the repo). `render.yaml` provisions a **free**
   Web Service with a health check on `/healthz` and a generated
   `DASHBOARD_TOKEN`.
3. Leave the keep-alive as shipped: it pings the URL in
   [`deploy/bot-url.txt`](deploy/bot-url.txt) every 5 minutes so the free instance
   stops spinning down (a repo variable `BOT_URL` overrides it). If Actions shows
   no Keep-alive runs yet, click **Run workflow** once or add a free external
   pinger — see [keeping it awake](docs/FREE_HOSTING.md#if-the-keep-alive-never-fires).
4. Leave `LIVE=false`. Confirm the logs show `DRY-RUN MODE`.
5. Open the service URL — the bot is serving the console on the same port.
   The URL is `https://<name>.onrender.com`, where `<name>` is
   `services[0].name` in `render.yaml`; a blueprint id like `exs-…` or a service id
   like `srv-…` is *not* a URL. Probe `https://<name>.onrender.com/healthz` to
   check. The blueprint's generated `DASHBOARD_TOKEN` makes `/api/*` answer 401
   until you paste it into **Connect** — see
   [docs/FREE_HOSTING.md](docs/FREE_HOSTING.md#the-console-shows-demo-data-and-401s).

The free plan has no disk, so the ledger lives in `/tmp` and is lost on restart;
`deploy/render-starter.yaml` is the $7/mo upgrade that adds a 1 GB disk at
`/var/data` and removes the sleep. Details and the other free options:
[docs/FREE_HOSTING.md](docs/FREE_HOSTING.md).

The previous `type: worker` service logged `No open ports detected` because a
background worker cannot serve HTTP, and workers are paid-only.

SQLite assumes exactly **one** instance. Do not scale this service to multiple
instances while sharing the same database file.

### AWS EC2 (Docker)

```bash
sudo bash -c "curl -fsSL https://raw.githubusercontent.com/Phaviwylera/tradebotdeepseek/main/deploy/ec2-setup.sh | bash"
```

The script installs Docker, clones to `/opt/polymarket-bot`, writes `.env` with a
generated `DASHBOARD_TOKEN` (mode 600, never overwriting), starts the stack, waits
for `/healthz`, and installs the systemd unit so it survives a reboot.

Security group: `22` from your IP, `80`/`443` only if nginx serves the console.
**Do not open `8099`.** The container publishes it on host loopback and nginx
(`deploy/nginx-bot-api.conf`) is the supported way to expose it.

```bash
ssh -N -L 8099:127.0.0.1:8099 ec2-user@<host>   # then http://127.0.0.1:8099
```

### Docker anywhere

```bash
docker build -t polymarket-bot .            # ~350 MB, runtime only
docker build -t polymarket-bot --build-arg INSTALL_ML=true .   # + xgboost/lightgbm
docker run --rm -p 127.0.0.1:8099:8099 -e API_HOST=0.0.0.0 \
  -e LIVE=false -v polymarket-data:/data polymarket-bot
```

The image runs as a non-root user, stores state under `/data`, and has a
`HEALTHCHECK` that probes `/healthz`.

### Vercel (console only)

```bash
cd dashboard && npx vercel --prod
```

Set `BOT_API_URL` (and `BOT_API_TOKEN` if the bot uses one). The proxy keeps the
token server-side, so the browser never holds it and no CORS or mixed-content
change is needed. Without it the console falls back to labelled sample data.

Do not put the trading loop on Vercel. Serverless functions cannot host a
persistent trading loop, a WAL SQLite file, or a WebSocket subscription.

See [docs/DEPLOY.md](docs/DEPLOY.md) for the complete playbook.

---

## Training and swapping in `model.joblib`

The MODEL strategy only runs when an artifact is present. Everything below is
the honest path to one — including the part most projects skip: **where the
training data comes from**.

### 1. Capture pre-resolution snapshots

A closed market's Gamma record reports its *final* prices (1 and 0). Build
features from those and the label is inside the feature vector: the model
backtests perfectly and predicts nothing. There is no historical archive to fall
back on, so the bot has to record the inputs while markets are still open:

```bash
SNAPSHOT_PATH=/data/snapshots.csv python main.py
```

One CSV row per evaluated market per cycle (`condition_id, captured_at,
end_date` plus the ten features). Off by default; ~5 MB/day at twenty markets a
minute, so keep it on a host with a disk (`deploy/render-starter.yaml` or EC2),
not the ephemeral free tier.

### 2. Fetch the outcomes and join them

```bash
# labels only: which way resolved markets actually settled
python scripts/fetch_resolved_markets.py --out resolved.csv --pages 10

# join: last snapshot per market taken >= 30 min before resolution
python scripts/fetch_resolved_markets.py \
    --snapshots /data/snapshots.csv --labels resolved.csv --out training.csv
```

The join drops any snapshot captured at or after resolution, and reports why
rows were discarded (`no_label`, `too_late`, `no_timestamp`) so a thin dataset
has a visible cause. Training needs at least 60 usable rows.

Every row needs the ten features in `features.py` plus `resolved_yes`:

```csv
mid_price,spread,bid_depth_5,ask_depth_5,imbalance,log_volume,log_liquidity,ttl_days,news_sentiment,spread_over_mid,resolved_yes,end_date
0.62,0.02,410.0,350.0,0.079,10.8,8.2,4.5,0.0,0.032,1,2026-01-10T00:00:00Z
```

Do not build labels from the same snapshot used to construct features without
respecting resolution time. Do not use random train/test splits.

```bash
# light: scikit-learn only, small enough to load on a 512 MB free instance
python train_model.py --data training.csv --output model.joblib --backend light

# full: XGBoost + LightGBM + sklearn, stronger but needs requirements-ml.txt
pip install -r requirements-ml.txt
python train_model.py --data training.csv --output model.joblib --backend full
```

The script prints an out-of-time holdout report including Brier score, log loss,
AUC, and 10-bin expected calibration error (ECE). It warns when ECE exceeds
0.10. Use the artifact only after reviewing calibration and sample size.

### 3. Deliver the artifact to the bot

| Where the bot runs | How the model gets there |
|---|---|
| local | put it at `MODEL_PATH` |
| EC2 / Docker with a volume | drop it at `/var/data/model.joblib` |
| **free host, no disk** | set `MODEL_URL` — the bot downloads it to `MODEL_PATH` at startup |

`MODEL_URL` can be a GitHub release asset, a raw file, or any static host. The
download is atomic (`.part` then rename) and size-capped, so a wrong URL can
neither be half-loaded nor fill the instance. The artifact stays out of source
control (`*.joblib` is gitignored); a `light` artifact is a few MB.

Verify, do not assume: `/healthz` reports what actually loaded —

```json
"model": {"loaded": true, "source": "downloaded", "backend": "light",
          "trained_at": "2026-10-26T...", "samples": 812, "error": null}
```

`"loaded": false` with `"source": "absent"` means only ARB + SNIPE are running,
and the console banner says so. `"error"` carries the real exception (typically
`ModuleNotFoundError: xgboost` when a `full` artifact is served by an image
without `requirements-ml.txt`).

Do not force it: if the holdout report shows ECE above 0.10, or a Brier score
no better than a constant base rate, the model adds noise, not edge. Leave it
off.

The model predicts P(YES resolves true). Training samples must be chronological
and free of outcome leakage. A model that is accurate but poorly calibrated is
not tradable by this bot.

---

## Going LIVE with a small bankroll

Complete this checklist:

1. Run DRY-RUN for at least one week and inspect every signal in SQLite.
2. Backtest and calibrate the model on genuinely resolved historical markets.
3. Use a dedicated wallet. Fund it with only the amount you can lose.
4. If using an EOA/MetaMask wallet, set USDC and conditional-token allowances
   for the Polymarket exchange contracts. Magic/email and browser proxy wallets
   use `POLYMARKET_SIGNATURE_TYPE=1` or `2` plus the funder address.
5. Set `BANKROLL=200`, `MAX_BET=20`, and keep `MAX_EXPOSURE_PCT` low. With the
   defaults, total open cost basis is capped near $60.
6. Put `POLYGON_PRIVATE_KEY` only in the platform's secret store.
7. Set `LIVE=true`, deploy, and watch the first order end to end. The console's
   mode pill must read **LIVE** and the order row must carry an order id.
8. Verify `orders` contains the API response and the wallet balance moved.
9. Never raise limits to recover a loss. Investigate first.

`LIVE=true` places real GTC limit orders. GTC orders may rest unfilled. This bot
counts an accepted order as exposure immediately, which is intentionally
conservative, but it does not cancel/requote stale resting orders every cycle.

---

## Tests and CI

```bash
python -m unittest discover -s tests -v     # 80 tests, offline, ~2s
python -m ruff check .                      # lint
python -m ruff format --check .             # formatting
```

Covered:

| File | Covers |
|---|---|
| `test_risk.py` | Daily-loss + drawdown kill switches, permanent session kill, exposure cap |
| `test_strategy.py` | Arbitration pair shape and budget, snipe gates, quarter-Kelly sizing, confidence floor |
| `test_scanner.py` | Gamma market parsing, filters, outcome ordering, level aggregation, TTL math |
| `test_features.py` | Exactly ten features in training order, finiteness, book edge cases |
| `test_ledger.py` | Schema, WAL mode, signal/order round-trip, cooldown windows, heartbeat, restart |
| `test_web.py` | Payload shape, token auth, 405 on writes, static whitelist, path traversal, bind resolution, parallel reads |
| `test_executor.py` | DRY-RUN never initializes or submits, LIVE without a key fails fast |
| `test_config.py` | Defaults, validation, secret redaction in `public_summary()` |

`.github/workflows/ci.yml` runs lint, the suite on 3.11 and 3.12, `node --check`
on the console bundle and Vercel functions, and a container build that must answer
`/healthz`.

---

## Operational limits you must respect

- **`py-clob-client` is archived upstream.** This project follows the requested
  stack and pins version `0.34.6`; the upstream README now says it is no longer
  maintained and points to the unified `py-sdk`. Migrate the executor before
  relying on this for long-term production capital.
- Polymarket fees, tick sizes, minimum order sizes, and allowance requirements
  are enforced by the exchange and can change. The 1.5% arbitrage buffer is not
  a complete fee model.
- WebSocket books and REST books can be momentarily inconsistent. The bot uses
  a freshness window, not a formal cross-venue consistency proof.
- The news sentiment feature defaults to `0.0`; no news feed is wired in.
  Training and inference must use the same definition.
- The bot does not automatically discover on-chain resolution payouts.
  `RiskManager.record_settlement()` is the audited settlement hook for an
  operator or separate settlement job. Until settlements are recorded, kill
  switch math reflects fills but may not reflect final resolution outcomes.
- Risk state is in memory and is reinitialized from `BANKROLL` on every process
  start. Reconcile the wallet and open positions before restarting a LIVE
  worker; do not use a restart to bypass a kill switch.
- One process, one SQLite file. Do not run duplicate workers.

---

## Hard truths

- No model guarantees profits. Edge comes from arbitrage, sniping, and
  validated calibrated divergence.
- Backtest the model on historical resolved markets and check calibration
  before any live capital.
- Run DRY-RUN for at least one week first.
- Start LIVE with small `BANKROLL` and strict `MAX_BET`.
- Prediction markets carry resolution risk: positions can go to zero.
