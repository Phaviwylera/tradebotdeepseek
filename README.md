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

- `0 <= ttl_days < 0.10` (under 2.4 hours), and
- the best ask is in `[0.93, 0.97]`, and
- `(0.95 - ask) / ask > MIN_EV`.

The 95% hold probability is an assumption, not an observed fact. With
`MIN_EV=0.02`, practically only the lower part of the price band passes. This
is intentional. A market at 0.97 has little remaining edge under that
assumption.

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

---

---

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
| `MIN_EV` | `0.02` | Minimum edge/return threshold. |
| `LEDGER_PATH` | `ledger.db` | SQLite file. Falls back to `/tmp/polymarket-bot/` with a warning if the path is not writable. |
| `MODEL_PATH` | `model.joblib` | Optional calibrated model artifact. |
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
| `/healthz` | liveness probe | never |
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

Training data must be built from historical, point-in-time market snapshots.
Every row needs the ten features in `features.py` plus `resolved_yes`:

```csv
mid_price,spread,bid_depth_5,ask_depth_5,imbalance,log_volume,log_liquidity,ttl_days,news_sentiment,spread_over_mid,resolved_yes,end_date
0.62,0.02,410.0,350.0,0.079,10.8,8.2,4.5,0.0,0.032,1,2026-01-10T00:00:00Z
```

Do not build labels from the same snapshot used to construct features without
respecting resolution time. Do not use random train/test splits.

```bash
python train_model.py --data resolved_markets.csv --output model.joblib
```

The script prints an out-of-time holdout report including Brier score, log loss,
AUC, and 10-bin expected calibration error (ECE). It warns when ECE exceeds
0.10. Use the artifact only after reviewing calibration and sample size.

To enable it:

- Local: put it at `MODEL_PATH`.
- Render/EC2: place it at `/var/data/model.joblib` on the persistent disk, or
  build an image that includes it (`--build-arg INSTALL_ML=true`). The file is
  gitignored by default so model artifacts do not accidentally enter source
  control. A model trained with xgboost/lightgbm needs those wheels installed to
  load: `pip install -r requirements-ml.txt`.
- Restart the worker. Logs should say `Calibrated model enabled`; otherwise the
  bot will report the degraded mode.

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
