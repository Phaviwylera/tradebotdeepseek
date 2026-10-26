# Operations console

A dependency-free, read-only dashboard for the Polymarket bot. No build step, no
framework, no analytics — three static files plus two serverless functions.

```text
dashboard/
├── public/            # static site (served by the bot AND by Vercel)
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── favicon.svg
├── api/
│   ├── status.js      # GET /api/status -> ${BOT_API_URL}/api/status
│   └── health.js      # GET /api/health -> ${BOT_API_URL}/healthz
├── vercel.json
└── README.md
```

## What it shows

- **Mode** — LIVE / DRY-RUN, plus a demo badge when no API is reachable.
- **KPIs** — equity, exposure, realized PnL, drawdown, risk state, heartbeat age.
- **Pipeline** — markets scanned/evaluated, signals this cycle, 24h and total
  counts, order count, uptime, last error.
- **Signals table** — strategy (ARB / SNIPE / MODEL), market, side, price, size,
  expected value, execution or risk-blocked state, and the raw reason string.
- **Orders table** — order id, token, price, shares, status.
- **Banners** — kill switch, stalled loop (`stale_seconds` over 180s), last error,
  or "demo data" when nothing is connected.

It refreshes every 15s, pauses when the tab is hidden, and can be paused manually.

## Three ways to open it

1. **From the bot itself** — the bot serves this directory on its own port:

   ```text
   http://<bot-host>:8099/          # EC2, via SSH tunnel or nginx
   https://<service>.onrender.com/  # Render, same origin as the API
   ```

2. **From Vercel** — deploy this folder and set `BOT_API_URL` (+ `BOT_API_TOKEN`)
   so `api/status.js` can proxy reads server-side:

   ```bash
   cd dashboard && npx vercel --prod
   ```

3. **Locally, with sample data** — no account, no network:

   ```bash
   python scripts/demo_server.py     # http://127.0.0.1:8099
   ```

## Runtime configuration

The page resolves its endpoint in this order:

1. `?api=<url>&token=<token>` in the URL (one-off links)
2. values saved by the **Connect** dialog (browser `localStorage` only)
3. same-origin `/api/status`
4. built-in sample data, clearly labelled

The bot's optional `DASHBOARD_TOKEN` is sent as a bearer token. It is never
embedded in the bundle, and the Vercel proxy keeps it server-side.

## Security notes

- The page cannot place, cancel, or modify anything: the API is `GET`/`HEAD` only
  and the bot's SQLite connection is `query_only`.
- No private key ever reaches the browser, the static bundle, or the proxy.
- Static assets are public by design (they hold no data); only `/api/*` reads are
  protected when `DASHBOARD_TOKEN` is set.
