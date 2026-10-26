# Running this bot for $0

The bot needs one thing a free tier rarely gives: **a process that stays up**.
Here is what actually works without spending money, with the trade-offs stated
plainly instead of buried.

| Option | Cost | Always on? | Persistent ledger | Card required | Realistic? |
|---|---|---|---|---|---|
| **Render free web service + keep-alive ping** | $0 | *mostly* (sleeps without traffic; a pinger fixes it) | ✗ lost on restart | no | **Best zero-setup start** |
| **Oracle Cloud Always Free ARM VM** | $0 forever | ✓ | ✓ | for identity check only | **Best if you can sign up** |
| **Google Cloud e2-micro free tier** | $0 forever | ✓ | ✓ | for identity check only | Good; 1 GB egress/month |
| Your own PC / Raspberry Pi + Cloudflare Tunnel | $0 | while it is on | ✓ | no | Solid, needs hardware |
| GitHub Actions cron | $0 | ✗ (5-min minimum, best effort) | ✗ | no | Demo only, not trading |
| Vercel | $0 | ✗ | ✗ | no | Console only — no persistent loop |
| Railway / Fly / Koyeb / Heroku | trial credit or card | varies | paid | yes | Not free |

Free tiers change. This page reflects what the configuration in this repo
supports; verify limits with the provider before relying on them.

---

## Option A — Render free web service (no card, ~10 minutes)

Render's free plan requires no payment method. It cannot attach a disk and it
stops an idle service, so the repo is set up accordingly:

- `render.yaml` uses `plan: free`, **no disk**, `LEDGER_PATH=/tmp/ledger.db`.
- `MAX_MARKETS_PER_CYCLE=20` to stay comfortable on 512 MB / 0.1 CPU.
- `.github/workflows/keepalive.yml` pings `/healthz` every 5 minutes so the
  service does not spin down.

### Steps

1. **Deploy the blueprint**
   [render.com/deploy?repo=Phaviwylera/tradebotdeepseek](https://render.com/deploy?repo=https://github.com/Phaviwylera/tradebotdeepseek)
   → **Apply**. Wait for the first build (`pip install -r requirements.txt`).
2. **Copy the service URL**, e.g. `https://polymarket-calibrated-bot.onrender.com`.
3. **Keep-alive is already wired.** The workflow reads the repository variable
   `BOT_URL` if it exists, and otherwise falls back to the first URL in
   [`deploy/bot-url.txt`](../deploy/bot-url.txt) — which ships filled in with
   `https://polymarket-calibrated-bot.onrender.com`. So there is nothing to
   configure unless you deploy under a different name:

   | Where to change it | When |
   |---|---|
   | edit `deploy/bot-url.txt` | simplest; the URL is public anyway |
   | repo variable `BOT_URL` (Settings → Secrets and variables → Actions → Variables) | you would rather not commit the URL; the variable wins |

   Enable it once: **Actions → Keep-alive → Run workflow**. It should print
   `OK (attempt 1): {"ok":true,...}`. After that it runs every 5 minutes on its
   own. (GitHub disables schedules after ~60 days without repo activity; any push
   turns it back on.)
4. **Open the console** at `https://<service>.onrender.com/` — the bot serves it.
   Paste the `DASHBOARD_TOKEN` from Render → **Environment** into **Connect**.
   Or point the Vercel console at it: set `BOT_API_URL` in Vercel and it appears
   under the same UI.
5. **Verify DRY-RUN.** Logs must show:

   ```text
   DRY-RUN MODE: no authenticated CLOB requests or orders
   Read-only status server on http://0.0.0.0:10000 ...
   Scanned N eligible markets (volume>5000, liquidity>1000)
   Cycle complete: markets=20 books=18 new_signals=0 risk={...}
   ```

### Where is my URL? (blueprint id vs service URL)

Render gives you two different ids and only one of them is a URL:

| What you see | Example | What it is |
|---|---|---|
| Blueprint instance id | `exs-dbfmfie7bikc73e4bteg` | the deploy *bundle*, not reachable |
| Service id | `srv-xxxxxxxx` | the running service, still not a URL |
| Service URL | `https://polymarket-calibrated-bot.onrender.com` | **this is the one** |

The URL is derived from `services[0].name` in `render.yaml`
(`polymarket-calibrated-bot`), with a suffix if that name is taken:

```text
https://<name>.onrender.com
```

Three ways to find it:

1. Dashboard → the blueprint → its resource list → click the service. The URL is
   in the header of the service page.
2. Dashboard → **Services** → `polymarket-calibrated-bot`.
3. Guess it and check: open `https://<name>.onrender.com/healthz`. A live bot
   answers `{"ok":true,"service":"polymarket-status",...}`.

A blueprint only creates the service after you click **Apply**; until the build
finishes there is nothing to open. If the build failed, the service page shows the
log tail — the usual cause is a red `render.yaml`, which
`python scripts/check_configs.py` catches locally.

### The console shows "Demo data" and 401s

That is the read-only API asking for its bearer token, which the blueprint
generates for you. Both are correct behaviour — pick one:

- **Keep it private:** Render → the service → **Environment** → reveal
  `DASHBOARD_TOKEN`, then paste it into the console's **Connect** dialog (or set
  `BOT_API_TOKEN` to the same value in Vercel). The value stays in your browser's
  `localStorage` / in the Vercel function; it is never bundled into the page.
- **Make the console public:** set `DASHBOARD_TOKEN` to an empty string in Render
  and redeploy. `/api/status` then answers without a token. Reasonable while
  `LIVE=false` (no funds, no keys) — but re-add a token before you ever flip
  `LIVE=true`.

### Cold starts: what a 40-second wake looks like now

Without traffic, the free instance is stopped. The next request boots it, and
Render serves an HTML "Application loading" page in the meantime (~40–60 s). That
used to look like a broken deploy. Now:

| Layer | Behaviour |
|---|---|
| Render | boots the instance on the first request |
| `dashboard/api/status.js` | **retries through the boot** (up to ~50 s) before giving up |
| The console | shows an info banner **"Bot is waking up"** and retries every 10 s |
| `keepalive.yml` | makes this rare by pinging every 5 minutes |

Once awake the console switches to live data on its own. Nothing needs a refresh.

### What you give up on the free plan

- **The ledger is ephemeral.** Every deploy or crash restarts `/tmp`; signal
  history and cooldown state disappear. The bot keeps trading correctly, it just
  forgets its audit trail. Export before you care: `GET /api/signals?limit=200`.
- **Spin-down is only mitigated, not solved.** GitHub's scheduler is best-effort
  and can be late; scheduled workflows are disabled after ~60 days of repo
  inactivity (any push re-enables them). If GitHub is late, the instance sleeps,
  the next ping cold-starts it (~30–60 s), and cycles resume.
- **750 free instance-hours/month** per workspace. One service at 24/7 is ~730 h,
  so it fits — but do not run a second always-on free service.
- **Never set `LIVE=true` here.** An ephemeral ledger plus a sleeping process is
  the opposite of what real money needs. `BANKROLL`/`MAX_BET` caps still apply,
  but risk state resets on every restart.

When free hosting stops being enough, `deploy/render-starter.yaml` is the $7/mo
upgrade: same service, `plan: starter`, a 1 GB disk at `/var/data`, and a 24/7
guarantee.

---

## Option B — Oracle Cloud Always Free (genuinely free, always on)

The best free option if signup succeeds: an ARM VM (`VM.Standard.A1.Flex`, up to
4 OCPU / 24 GB, or an `E2.1.Micro`) that stays up forever, with 10 TB of monthly
egress — no tuning, no pinger.

1. Create an **Always Free** Ampere A1 or E2.1.Micro instance, Oracle Linux 9 or
   Ubuntu 22.04, 1–2 OCPU, 1–4 GB RAM.
2. SSH in and run the same bootstrap used for EC2:

   ```bash
   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Phaviwylera/tradebotdeepseek/main/deploy/ec2-setup.sh)"
   ```

   It detects `dnf` (Oracle Linux) or `apt` (Ubuntu), installs Docker, clones to
   `/opt/polymarket-bot`, writes `.env` with a generated `DASHBOARD_TOKEN`, starts
   the stack, waits for `/healthz`, and installs a systemd unit.
3. Open the console through an SSH tunnel — no ingress rules, no public port:

   ```bash
   ssh -N -L 8099:127.0.0.1:8099 opc@<instance-ip>    # Ubuntu: ubuntu@
   # then browse http://127.0.0.1:8099
   ```

   The ARM64 `python:3.11-slim` image builds normally on Ampere.

Caveats: signup needs a card for identity verification (Always Free resources are
not charged), and capacity for free ARM instances is sometimes unavailable in a
region — retry or pick another availability domain.

---

## Option C — Google Cloud e2-micro free tier

One `e2-micro` VM per month is free forever in `us-central1`, `us-west1` and
`us-east1`, with a 30 GB standard disk. Same bootstrap command as Option B.

The catch is **1 GB of egress per month**. Market polling plus WebSocket traffic
can approach that. If you use GCP, slow the loop down:

```bash
# /opt/polymarket-bot/.env
SCAN_INTERVAL_SECONDS=180
MAX_MARKETS_PER_CYCLE=10
```

Then `docker compose up -d` to apply. Also set a **budget alert** at $1 so a
traffic spike cannot surprise you.

---

## Option D — your own machine (or a Raspberry Pi)

Free, private, and the ledger persists. Two commands:

```bash
docker build -t polymarket-bot . && docker run -d --restart unless-stopped \
  -p 127.0.0.1:8099:8099 -e API_HOST=0.0.0.0 -e LIVE=false \
  -v polymarket-data:/data polymarket-bot
```

To reach the console from outside your network without opening a port, run
`cloudflared tunnel --url http://127.0.0.1:8099` (free) or use `tailscale serve`.
Do not port-forward the bot to the open internet.

---

## Option E — GitHub Actions as a runner (demo only)

Public repos get unlimited Actions minutes, so a scheduled workflow can run
`python main.py` for a few minutes every 5 minutes. It is genuinely free and
genuinely bad for trading:

- GitHub cron has a 5-minute floor and is often late;
- each run is a fresh container, so SQLite state and cooldowns vanish;
- scheduled workflows stop after ~60 days of repo inactivity;
- a job is capped at 6 hours, and you would be abusing CI for a workload it is
  not designed for.

Fine for a periodic DRY-RUN sanity probe, not for execution. The repo does not
ship this workflow.

---

## Tuning for small/free machines

All optional, all read from the environment:

| Variable | Free-tier suggestion | Why |
|---|---|---|
| `SCAN_INTERVAL_SECONDS` | `60` on Render, `180` on GCP | fewer cycles = less CPU and egress |
| `MAX_MARKETS_PER_CYCLE` | `20` (free Render), `10` (GCP) | bounds book-fetch fan-out |
| `MAX_BET` / `BANKROLL` | keep `20` / `200` | risk caps are unrelated to hosting cost |
| `LOG_LEVEL` | `INFO` | `DEBUG` on 512 MB fills the log buffer |
| `BOOK_STALENESS_SECONDS` | `20` | higher = fewer REST fallbacks |

If `LEDGER_PATH` is not writable, the bot now falls back to
`/tmp/polymarket-bot/ledger.db` and logs a warning instead of crashing:

```text
WARNING LEDGER_PATH /var/data/ledger.db is not writable (...); using /tmp/... instead.
This ledger is EPHEMERAL and will be lost on restart.
```

That is a safety net for a misconfigured free host, not a storage strategy.
