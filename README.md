# Polymarket Calibrated Trading Bot

A persistent Python 3.11 service for Polymarket. It discovers active markets,
ingests order books over WebSocket with REST fallback, evaluates three
independent strategies, enforces hard risk limits, and writes every signal and
submitted order to a SQLite ledger.

**Default mode is `LIVE=false` (DRY-RUN). No private key is required and no
authenticated CLOB request or order is made.**

This is not a profit guarantee. It is an execution and risk framework around
concrete, measurable edges.

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

## Architecture

```text
scanner.py    Gamma market discovery + normalized CLOB books
stream.py     Public market-channel WebSocket, auto-reconnect
features.py   Canonical feature row ordered exactly like training
model.py      Calibrated stacking model, Bayesian anchor, joblib I/O
strategy.py   Arbitrage, snipe, and model-edge signals
risk.py       Kill switch, exposure, fills, settlements
executor.py   Authenticated py-clob-client GTC execution
ledger.py     SQLite signal/order audit log
main.py       Async 60-second pipeline and lifecycle
train_model.py Out-of-time evaluation and model export
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

Python 3.11 is required.

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

The ledger defaults to `./ledger.db`:

```bash
sqlite3 ledger.db "select ts,market_id,reason,size_usd,executed from signals order by id desc limit 20;"
sqlite3 ledger.db "select ts,order_id,status from orders order by id desc limit 20;"
```

In DRY-RUN, `orders` stays empty. A real order is never submitted.

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
| `LEDGER_PATH` | `ledger.db` | SQLite file. Use a persistent disk on Render. |
| `MODEL_PATH` | `model.joblib` | Optional calibrated model artifact. |
| `SCAN_INTERVAL_SECONDS` | `60` | Pipeline cadence. |
| `MAX_MARKETS_PER_CYCLE` | `40` | Book/strategy working set. |
| `SIGNAL_COOLDOWN_SECONDS` | `3600` | Per-market/strategy dedupe window. |
| `BOOK_STALENESS_SECONDS` | `20` | Maximum WebSocket cache age. |
| `CANCEL_ON_SHUTDOWN` | `true` | Cancel open orders when the worker stops. |
| `LOG_LEVEL` | `INFO` | Logging verbosity. |

All secrets are read from the environment. They are never written to the
repository, SQLite, or logs.

The kill switch is permanent for the process session. A restart is an explicit
operator action, not an automatic recovery path.

---

## Deploy on Render (Background Worker)

1. Push this repository to GitHub.
2. In Render, choose **New > Blueprint** and select the repository.
   `render.yaml` provisions a Starter background worker and a 1 GB persistent
   disk mounted at `/var/data`.
3. Set `POLYGON_PRIVATE_KEY` in the Render dashboard as a secret. Leave it
   empty while testing DRY-RUN.
4. Keep `LIVE=false`. Deploy and verify logs show `DRY-RUN MODE`.
5. Confirm the disk exists, `LEDGER_PATH=/var/data/ledger.db`, and transactions
   survive a redeploy.
6. Run at least one week of DRY-RUN before considering LIVE.

Do not put the backend on Vercel. Serverless functions cannot host a persistent
trading loop. Vercel is only appropriate for an optional static dashboard, which
is not part of this repository.

SQLite and the persistent disk assume exactly **one** worker instance. Do not
scale this service to multiple instances while sharing the same database file.

### Docker alternative

```bash
docker build -t polymarket-bot .
docker run --rm -e LIVE=false -v polymarket-data:/data polymarket-bot
```

The image runs as a non-root user and stores its ledger under `/data`.

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
- Render: place it at `/var/data/model.joblib` on the persistent disk, or build
  a private image that includes it. The file is gitignored by default so model
  artifacts do not accidentally enter source control.
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
6. Put `POLYGON_PRIVATE_KEY` only in the Render secret store.
7. Set `LIVE=true`, deploy, and watch the first order end to end.
8. Verify `orders` contains the API response and the wallet balance moved.
9. Never raise limits to recover a loss. Investigate first.

`LIVE=true` places real GTC limit orders. GTC orders may rest unfilled. This bot
counts an accepted order as exposure immediately, which is intentionally
conservative, but it does not cancel/requote stale resting orders every cycle.

---

## Tests

The required risk and arbitrage tests use only the standard library:

```bash
python -m unittest discover -s tests -v
```

Covered:

- Daily-loss kill switch and permanent session kill.
- Maximum drawdown kill switch.
- Exposure cap enforcement.
- Two-leg YES+NO arbitrage signal shape and pair budget.
- No-arbitrage threshold behavior.
- Snipe time/price/EV gates.
- Quarter-Kelly model sizing and confidence floor.

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
