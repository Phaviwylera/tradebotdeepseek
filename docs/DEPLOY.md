# Deployment playbook

Three ways to run this project. Pick one runner for the bot — never two at once.

| Target | What runs there | Cost | Good for |
|---|---|---|---|
| **Render free** | Bot + console in one Web Service | $0 (sleeps without traffic, no disk) | Getting it live in ten minutes, no card |
| **Render Starter** | Same, always on, persistent ledger | ~$7/mo | Real DRY-RUN history |
| **Oracle / GCP always-free VM** | Bot + console in Docker | $0 forever | A genuinely always-on free host |
| **AWS EC2 + Docker** | Bot + dashboard behind loopback/nginx | t3.small, free tier eligible | Full control, private networking, your own domain |
| **Vercel** | Dashboard only (static + serverless proxy) | Free | A public console pointed at a bot running elsewhere |

Vercel cannot host the bot: serverless functions have no long-lived loop, no
WAL SQLite, and no WebSocket subscription. The bot needs a process that stays up.

---

## 0. Before any deploy

```bash
python -m unittest discover -s tests -v   # 80 tests, no network needed
python scripts/demo_server.py             # look at the console with sample data
```

Keep `LIVE=false`. In DRY-RUN the bot makes no authenticated CLOB request, submits
no order, and does not read `POLYGON_PRIVATE_KEY`. Run at least a week of DRY-RUN
and review the ledger before considering LIVE.

---

## 1. Render (one service, dashboard included)

The blueprint defaults to the **free** plan: no card, no disk, sleeps when idle.
Free forensics first: [FREE_HOSTING.md](FREE_HOSTING.md).

1. Push the repository to GitHub (already done for this fork).
2. In Render: **New → Blueprint**, select the repo, **Apply**. `render.yaml`
   provisions a Web Service with:
   - build `pip install -r requirements.txt`
   - start `python main.py`
   - health check `GET /healthz`
   - a generated `DASHBOARD_TOKEN`
   - `LEDGER_PATH=/tmp/ledger.db` (ephemeral on the free plan)
3. Set the GitHub repository variable `BOT_URL` to the service URL and run
   **Actions → Keep-alive** once, so the free instance does not spin down.
4. Open the service URL. The console loads; paste the `DASHBOARD_TOKEN` value from
   **Environment** into **Connect** if you want token-protected reads.
5. Verify the log lines below, then confirm `orders` stays at `0`.

For 24/7 with a durable ledger: `cp deploy/render-starter.yaml render.yaml`,
upgrade the service to Starter, and re-apply. That adds a 1 GB disk at
`/var/data` and removes the sleep behaviour.

```text
DRY-RUN MODE: no authenticated CLOB requests or orders
Ledger /var/data/ledger.db ready
Read-only status server on http://0.0.0.0:10000 (ledger=/var/data/ledger.db, auth=token, dashboard=on)
Market WebSocket connected
Scanned N eligible markets (volume>5000, liquidity>1000)
Cycle complete: markets=40 books=37 new_signals=0 risk={...}
```

### Why a web service and not a worker

Previously this repo shipped `type: worker`, which meant the bot could not be
reached and Render logged "No open ports detected". The bot now serves its own
read-only HTTP surface (`web.py`) on `$PORT`, so one web service is both the
trading loop and the console.

### Free-tier warning

Render free instances spin down after ~15 minutes without traffic — the loop
stops — and cannot attach a disk. The repo mitigates both (keep-alive workflow,
`/tmp` ledger) but does not pretend they are equivalent to a real host:

- the ledger is lost on every deploy or restart;
- GitHub's scheduler is best-effort, so spin-downs still happen occasionally;
- **never set `LIVE=true` on an ephemeral instance** — risk state resets with it.

See [FREE_HOSTING.md](FREE_HOSTING.md) for the honest comparison, including the
two always-free VM options (Oracle Always Free, Google e2-micro).

### Scaling

SQLite plus a single disk means **exactly one instance**. Do not scale the
service while it shares one database file.

---

## 2. AWS EC2 (Docker)

### One-shot bootstrap (Ubuntu or Amazon Linux, t3.small / 2 GB)

```bash
sudo bash -c "curl -fsSL https://raw.githubusercontent.com/Phaviwylera/tradebotdeepseek/main/deploy/ec2-setup.sh | bash"
```

It installs Docker, clones to `/opt/polymarket-bot`, writes `.env` with a
generated `DASHBOARD_TOKEN` (mode 600, never overwriting an existing file),
starts the stack, waits for `/healthz`, and installs a systemd unit.

### Manual equivalent

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"        # log out and back in
sudo mkdir -p /opt/polymarket-bot && sudo chown "$USER":"$USER" /opt/polymarket-bot
git clone https://github.com/Phaviwylera/tradebotdeepseek.git /opt/polymarket-bot
cd /opt/polymarket-bot
cp .env.aws.example .env && chmod 600 .env
# set DASHBOARD_TOKEN (openssl rand -hex 32); leave LIVE=false
docker compose up -d --build
docker compose logs -f bot
```

### Security group

| Port | Source | Why |
|---|---|---|
| 22 | your IP only | SSH |
| 80 / 443 | 0.0.0.0/0 | only if nginx serves the dashboard publicly |
| 8099 | **nothing** | loopback-only status API |

The compose file publishes the status API as `127.0.0.1:8099:8099`, and the
container binds `0.0.0.0` internally. Both halves are required: Docker forwards
to the container's interface, not to its loopback. Nothing listens publicly.

### Reaching the console

```bash
# simplest: SSH tunnel, no DNS, no TLS, no open port
ssh -N -L 8099:127.0.0.1:8099 ec2-user@<host>
open http://127.0.0.1:8099
```

To publish it properly, front it with nginx using
`deploy/nginx-bot-api.conf`, which keeps only `GET`/`HEAD` and can inject the
bearer token server-side so the browser never holds it.

### Survival and state

```bash
sudo cp deploy/polymarket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now polymarket-bot
```

Docker's `restart: unless-stopped` handles crashes; systemd handles boot. The
named volume `polymarket-data` holds state: `docker compose down` keeps it,
`docker compose down -v` deletes the ledger.

```bash
docker compose exec bot python - <<'PY'
import sqlite3
con = sqlite3.connect("/data/ledger.db")
for table in ("signals", "orders", "runtime"):
    print(table, con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
print(con.execute("SELECT mode, updated_at, markets_scanned, equity FROM runtime").fetchall())
PY

docker run --rm -v polymarket-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/ledger-$(date +%F).tgz -C /data .
```

---

## 3. Vercel (dashboard only)

Deploy the console anywhere, point it at a bot that is already running.

```bash
cd dashboard
npx vercel --prod            # Root Directory = dashboard
```

Then in **Project → Settings → Environment Variables**:

| Name | Value | Required |
|---|---|---|
| `BOT_API_URL` | `https://your-bot.onrender.com` (no trailing slash) | yes |
| `BOT_API_TOKEN` | the bot's `DASHBOARD_TOKEN` | if the bot sets one |

`dashboard/api/status.js` proxies `GET /api/status` server-side, so:

- the browser stays same-origin (no CORS change, no mixed-content block),
- the bot token is never shipped to the browser,
- only `GET`/`HEAD` are forwarded.

`GET /api/health` reports whether the proxy is wired up and whether the bot
answers `/healthz`.

Without `BOT_API_URL` the console still works: it falls back to clearly labelled
sample data and shows a "demo data" banner. You can also point it at any endpoint
at runtime with `?api=https://host&token=...`, stored in `localStorage` only.

---

## 4. Going live with real money

Only after a week of DRY-RUN with clean logs and a calibrated model you trust:

1. Create a **dedicated** Polygon wallet. Fund it with only what you can lose.
2. Add `POLYGON_PRIVATE_KEY` as a secret in the platform's secret store — never
   in `.env` committed to git, never in a Docker image layer, never in chat.
3. Set `POLYMARKET_SIGNATURE_TYPE` (`0` EOA, `1` Magic/email, `2` browser proxy)
   and `POLYMARKET_FUNDER` for proxy wallets.
4. Flip `LIVE=true`, redeploy, and watch the first cycles in the console:
   `mode` must read **LIVE**, and each order row must show an order id.
5. Keep the risk limits at their defaults until you have measured slippage.

The kill switch is permanent for the process session. If it trips, the bot stops
opening positions until you restart it — that is deliberate.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No open ports detected` on Render | deployed as a worker | use the web service in `render.yaml` |
| Render free service keeps sleeping | no pinger configured | the keep-alive reads `deploy/bot-url.txt`; run **Actions → Keep-alive** once |
| Keep-alive has zero runs in Actions | GitHub has not scheduled the new workflow yet | click **Run workflow** once, or add a free 5-min external pinger ([details](FREE_HOSTING.md#if-the-keep-alive-never-fires)) |
| Console says "Bot is waking up" | free instance was stopped | wait ~40 s; the page retries itself. A pinger makes this rare |
| Vercel console shows demo, Render console works | the Vercel proxy has no token | add `BOT_API_TOKEN` = the bot's `DASHBOARD_TOKEN` in Vercel (browser `Connect` only affects your browser) |
| Audit history vanished after a deploy | free plan has no disk | expected on free; use `deploy/render-starter.yaml` |
| Console shows *demo data* | API not reachable or token rejected | open **Connect**, check URL/token; `curl <url>/healthz` |
| `/api/status` returns 401 | `DASHBOARD_TOKEN` set | send `Authorization: Bearer <token>` or `?token=` |
| `ledger not created yet` | bot has not finished its first cycle | wait one interval; check `docker compose logs` |
| `stalled` banner in console | `stale_seconds` > 180 | the loop is stuck, not just the container: read logs |
| Container healthy but no signals | normal in DRY-RUN | arbitrage under 0.985 is rare; snipes need the final 2.4h |
| **No trades at all, ever** | `LIVE=false` | by design: DRY-RUN records signals in SQLite and never submits an order. Flip `LIVE=true` and add `POLYGON_PRIVATE_KEY` only when you mean it |
| No signals *and* the page shows sample data | Vercel has no token | the banner now quotes the bot's public `/healthz`; open `<bot-url>/healthz` for the same numbers without any secret |
| `/healthz` says `loop: error` | the last cycle raised | `last_error` carries the real exception now (e.g. market data unreachable), not "see logs" |
| `/healthz` says `loop: stalled` | no heartbeat for >300 s | the process is up but the pipeline is wedged; read the logs |
| `/healthz` → `model.loaded: false` | no artifact on the instance | set `MODEL_URL` (diskless hosts) or drop a `model.joblib` at `MODEL_PATH`. ARB + SNIPE still run |
| `model.error: ModuleNotFoundError: xgboost` | a `full` artifact on an image without `requirements-ml.txt` | retrain with `--backend light`, or build with `--build-arg INSTALL_ML=true` |
| Training set has < 60 rows | snapshots only exist after `SNAPSHOT_PATH` is set | capture first, train later; the join reports `too_late`/`no_label` counts |
| `Risk blocked` rows | exposure/daily-loss caps | expected; inspect `reason` in the signals table |
| xgboost missing at model load | artifact trained with ML extras | `pip install -r requirements-ml.txt` |

---

## 6. Vercel project settings that bite

Two defaults in a fresh Vercel project surprise people hosting this console:

| Setting | Default | Effect | Fix |
|---|---|---|---|
| **Deployment Protection → Vercel Authentication** | *All deployments except custom domains* on some teams | `*.vercel.app` URLs ask every visitor to log in | Project → Settings → Deployment Protection → apply to preview only (or disable) if the console should be publicly readable |
| **Root Directory** | repo root | the build looks for `public/` at the repo root and finds the Python service | set Root Directory to `dashboard` |

Deployment is then simply `git push` on the production branch, or
`cd dashboard && npx vercel --prod` for a manual deploy. A project pointed at this
repository with Root Directory `dashboard` and Output Directory `public` needs no
build command at all.
