// Operations console for the Polymarket calibrated bot.
//
// The page is a pure read-only client. It resolves a status endpoint in this
// order and never sends anything but GET requests:
//
//   1. ?api=<url> query parameter (handy for a one-off link)
//   2. settings saved in localStorage by the Connect dialog
//   3. same-origin /api/status (works when the bot serves this page, or when the
//      Vercel proxy function is configured with BOT_API_URL)
//   4. built-in demo data, clearly labelled, so the page is never broken
//
// No build step, no dependencies, no analytics.

const REFRESH_SECONDS = 15;
const STORAGE_KEY = "polymarket-console.settings.v1";

const state = {
  settings: loadSettings(),
  source: "demo", // "live" | "demo"
  lastPayload: null,
  lastFetchAt: null,
  timer: null,
  tickTimer: null,
  nextRefreshAt: 0,
  paused: false,
  loading: false,
};

const el = {
  modePill: document.getElementById("mode-pill"),
  connPill: document.getElementById("conn-pill"),
  notice: document.getElementById("notice"),
  noticeTitle: document.getElementById("notice-title"),
  noticeText: document.getElementById("notice-text"),
  noticeAction: document.getElementById("notice-action"),
  refreshBtn: document.getElementById("refresh-btn"),
  connectBtn: document.getElementById("connect-btn"),
  dialog: document.getElementById("connect-dialog"),
  form: document.getElementById("connect-form"),
  apiBase: document.getElementById("api-base"),
  apiToken: document.getElementById("api-token"),
  useDemo: document.getElementById("use-demo"),
  autoRefresh: document.getElementById("auto-refresh"),
  countdown: document.getElementById("countdown"),
  connMeta: document.getElementById("conn-meta"),
  connEndpoint: document.getElementById("conn-endpoint"),
  connLedger: document.getElementById("conn-ledger"),
  connAuth: document.getElementById("conn-auth"),
  connStale: document.getElementById("conn-stale"),
  signalsBody: document.getElementById("signals-body"),
  ordersBody: document.getElementById("orders-body"),
  signalsMeta: document.getElementById("signals-meta"),
  ordersMeta: document.getElementById("orders-meta"),
  pipelineMeta: document.getElementById("pipeline-meta"),
};

// ---------------------------------------------------------------- settings

function loadSettings() {
  const fromQuery = new URLSearchParams(window.location.search);
  let saved = {};
  try {
    saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch {
    saved = {};
  }
  const apiBase = fromQuery.get("api") ?? saved.apiBase ?? "";
  const apiToken = fromQuery.get("token") ?? saved.apiToken ?? "";
  return { apiBase: String(apiBase).trim(), apiToken: String(apiToken).trim() };
}

function saveSettings(settings) {
  state.settings = settings;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* private mode: keep it in memory only */
  }
}

function endpoint(path = "/api/status") {
  const base = state.settings.apiBase.replace(/\/+$/, "");
  return base ? `${base}${path}` : path;
}

// ------------------------------------------------------------------- data

async function fetchStatus() {
  if (state.loading) return;
  state.loading = true;
  setCountdownLabel();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12_000);
  try {
    const headers = {};
    if (state.settings.apiToken) {
      headers.Authorization = `Bearer ${state.settings.apiToken}`;
    }
    const response = await fetch(`${endpoint()}?limit=50&_=${Date.now()}`, {
      headers,
      signal: controller.signal,
      cache: "no-store",
    });

    if (response.status === 401) {
      throw new Error("unauthorized: this endpoint needs a dashboard token");
    }
    if (!response.ok) {
      // A static-only host has no proxy function; treat that as "not wired up".
      if (response.status === 404 || response.status === 405) {
        throw new Error("no status API at this address");
      }
      throw new Error(`status API returned HTTP ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("json")) {
      throw new Error("no status API at this address");
    }
    const payload = await response.json();
    if (payload && payload.configured === false) {
      throw new Error(payload.error || "status proxy is not configured");
    }
    if (!payload || typeof payload !== "object") {
      throw new Error("unexpected payload from status API");
    }
    applyPayload(payload, "live");
  } catch (error) {
    state.source = "demo";
    applyPayload(buildDemoPayload(), "demo", String(error.message || error));
  } finally {
    clearTimeout(timeout);
    state.loading = false;
    state.lastFetchAt = new Date();
    scheduleNext();
  }
}

// ------------------------------------------------------------------- demo

function demoRuntime() {
  const now = Date.now();
  return {
    mode: "dry_run",
    started_at: new Date(now - 1000 * 60 * 184).toISOString(),
    updated_at: new Date(now - 1000 * 7).toISOString(),
    markets_scanned: 40,
    markets_evaluated: 37,
    signals_emitted: 2,
    equity: 200.0,
    cash: 182.4,
    exposure: 17.6,
    realized_pnl: 3.42,
    drawdown_pct: 0.008,
    killed: false,
    kill_reason: null,
    last_error: null,
  };
}

function buildDemoPayload() {
  const now = Date.now();
  const markets = [
    "0x9f2c41ab7de0c8b1e5a37c2f1d8b6a43c9e7f0d2",
    "0x1b8d05cf3a92e64b7d10af82c5e39b471d6c0a88",
    "0x47ae93c1d2065f8b3c94e17a05d2b86f1e3a9c05",
    "0x2c6f18d9b7a04e53c81072fd6b49a3e58c2d0f71",
    "0x83d5ba0f2e64c791d0a8b35f7c14e690ab27d3c8",
    "0x64e07c9a1f8b2d53e0a71c46b95d38f2a6c7e910",
  ];
  const rows = [
    {
      age: 24,
      strategy: "MODEL",
      side: "BUY",
      price: 0.58,
      size: 12.5,
      ev: 1.31,
      reason: "MODEL YES edge=0.0812 ask=0.5800 kelly=0.24 prob=0.6612",
    },
    {
      age: 61,
      strategy: "ARB",
      side: "BUY",
      price: 0.471,
      size: 8.9,
      ev: 0.14,
      reason: "ARB YES+NO=0.9760 profit_per_pair=0.0240 shares=18.44",
    },
    {
      age: 64,
      strategy: "ARB",
      side: "BUY",
      price: 0.505,
      size: 9.3,
      ev: 0.07,
      reason: "ARB YES+NO=0.9760 profit_per_pair=0.0240 shares=18.44",
    },
    {
      age: 188,
      strategy: "SNIPE",
      side: "BUY",
      price: 0.937,
      size: 20.0,
      ev: 0.28,
      reason: "SNIPE YES ttl_h=1.42 ask=0.9370 hold=0.95 ev_roi=0.0139",
    },
    {
      age: 402,
      strategy: "MODEL",
      side: "BUY",
      price: 0.31,
      size: 6.2,
      ev: 0.44,
      reason: "MODEL NO edge=0.0644 ask=0.3100 kelly=0.09 prob=0.3744",
    },
    {
      age: 655,
      strategy: "ARB",
      side: "BUY",
      price: 0.482,
      size: 4.1,
      ev: 0.05,
      reason: "ARB YES+NO=0.9830 profit_per_pair=0.0170 shares=8.42 | RISK_BLOCKED exposure cap",
    },
    {
      age: 912,
      strategy: "SNIPE",
      side: "BUY",
      price: 0.941,
      size: 20.0,
      ev: 0.19,
      reason: "SNIPE NO ttl_h=0.87 ask=0.9410 hold=0.95 ev_roi=0.0096",
    },
    {
      age: 1280,
      strategy: "MODEL",
      side: "BUY",
      price: 0.66,
      size: 9.8,
      ev: 0.72,
      reason: "MODEL YES edge=0.0731 ask=0.6600 kelly=0.22 prob=0.7331",
    },
  ];

  const signals = rows.map((row, index) => {
    const blocked = row.reason.includes("RISK_BLOCKED");
    return {
      id: 1000 - index,
      ts: new Date(now - row.age * 1000).toISOString(),
      market_id: markets[index % markets.length],
      token_id: `7${(index + 3).toString().repeat(12)}${index}9d4f`,
      side: row.side,
      price: row.price,
      size_usd: row.size,
      expected_value: row.ev,
      reason: `${row.reason} | DRY_RUN`,
      executed: false,
      demo_strategy: row.strategy,
      demo_blocked: blocked,
    };
  });

  return {
    ready: true,
    reason: null,
    server_time: new Date(now).toISOString(),
    ledger_path: "/var/data/ledger.db",
    runtime: demoRuntime(),
    stale_seconds: 7.0,
    uptime_seconds: 11_040,
    counts: { signals_total: 184, signals_24h: 41, orders_total: 0 },
    recent_signals: signals,
    recent_orders: [],
    demo: true,
  };
}

// --------------------------------------------------------------- rendering

function applyPayload(payload, source, failureReason) {
  state.lastPayload = payload;
  state.source = source;

  const runtime = payload.runtime || {};
  const isDemo = source === "demo";
  const mode = String(runtime.mode || (isDemo ? "dry_run" : "unknown")).toUpperCase();

  el.modePill.textContent = mode === "LIVE" ? "LIVE" : mode === "DRY_RUN" ? "DRY-RUN" : mode;
  el.modePill.dataset.tone = mode === "LIVE" ? "live" : "dry";

  el.connPill.textContent = isDemo ? "demo" : "live api";
  el.connPill.dataset.tone = isDemo ? "demo" : "live";

  renderNotice(payload, source, failureReason);
  renderKpis(payload, runtime);
  renderPipeline(payload, runtime);
  renderConnection(payload, source);
  renderSignals(payload.recent_signals || []);
  renderOrders(payload.recent_orders || []);
}

function renderNotice(payload, source, failureReason) {
  const runtime = payload.runtime || {};
  const stale = Number(payload.stale_seconds);
  const staleLimit = 180;

  if (source === "demo") {
    showNotice({
      tone: "warn",
      title: "Demo data",
      text: failureReason
        ? `Showing sample data — ${failureReason}. Connect a bot to see live state.`
        : "Showing sample data. Connect a bot to see live state.",
      actionLabel: "Connect",
      action: openDialog,
    });
    return;
  }

  if (!payload.ready) {
    showNotice({
      tone: "info",
      title: "Waiting for the ledger",
      text: payload.reason || "The bot has not written its first cycle yet.",
    });
    return;
  }

  if (runtime.killed) {
    showNotice({
      tone: "danger",
      title: "Kill switch tripped",
      text: `${runtime.kill_reason || "risk limit reached"} — the bot will not open new positions until it is restarted.`,
    });
    return;
  }

  if (Number.isFinite(stale) && stale > staleLimit) {
    showNotice({
      tone: "danger",
      title: "Loop looks stalled",
      text: `No cycle heartbeat for ${formatDuration(stale)}. The container may be up while the pipeline is stuck. Check its logs.`,
    });
    return;
  }

  if (runtime.last_error) {
    showNotice({
      tone: "warn",
      title: "Last cycle reported an error",
      text: String(runtime.last_error),
    });
    return;
  }

  hideNotice();
}

function showNotice({ tone, title, text, actionLabel, action }) {
  el.notice.className = `banner banner-${tone}`;
  el.noticeTitle.textContent = title;
  el.noticeText.textContent = text;
  if (actionLabel && action) {
    el.noticeAction.hidden = false;
    el.noticeAction.textContent = actionLabel;
    el.noticeAction.onclick = action;
  } else {
    el.noticeAction.hidden = true;
    el.noticeAction.onclick = null;
  }
  el.notice.hidden = false;
}

function hideNotice() {
  el.notice.hidden = true;
}

function renderKpis(payload, runtime) {
  const equity = asNumber(runtime.equity);
  const exposure = asNumber(runtime.exposure);
  const pnl = asNumber(runtime.realized_pnl);
  const drawdown = asNumber(runtime.drawdown_pct);

  setText("kpi-equity", equity === null ? "—" : money(equity));
  setText(
    "kpi-equity-foot",
    equity === null ? "no runtime row yet" : "USDC mark-to-cost",
  );

  const exposurePct = equity && equity > 0 && exposure !== null ? (exposure / equity) * 100 : null;
  setText("kpi-exposure", exposure === null ? "—" : money(exposure));
  setText(
    "kpi-exposure-foot",
    exposurePct === null ? "share of equity" : `${exposurePct.toFixed(1)}% of equity`,
  );

  setText("kpi-pnl", pnl === null ? "—" : signedMoney(pnl));
  setClass("kpi-pnl", pnl === null ? "" : pnl > 0 ? "is-good" : pnl < 0 ? "is-bad" : "");
  setText(
    "kpi-pnl-foot",
    payload.demo ? "session (sample)" : "session, realized",
  );

  setText("kpi-drawdown", drawdown === null ? "—" : `${(drawdown * 100).toFixed(2)}%`);
  setClass(
    "kpi-drawdown",
    drawdown === null ? "" : drawdown >= 0.2 ? "is-bad" : drawdown > 0 ? "is-warn" : "is-good",
  );

  const killed = Boolean(runtime.killed);
  setText("kpi-risk", killed ? "HALTED" : runtime.mode ? "ARMED" : "—");
  setClass("kpi-risk", killed ? "is-bad" : runtime.mode ? "is-good" : "");
  setText("kpi-risk-foot", killed ? runtime.kill_reason || "kill switch" : "within limits");

  const stale = asNumber(payload.stale_seconds);
  setText("kpi-heartbeat", stale === null ? "—" : `${formatDuration(stale)} ago`);
  setClass("kpi-heartbeat", stale === null ? "" : stale > 180 ? "is-bad" : stale > 90 ? "is-warn" : "is-good");
  setText("kpi-heartbeat-foot", payload.demo ? "sample heartbeat" : "last pipeline cycle");
}

function renderPipeline(payload, runtime) {
  const counts = payload.counts || {};
  setText("stat-scanned", valueOrDash(runtime.markets_scanned));
  setText("stat-evaluated", valueOrDash(runtime.markets_evaluated));
  setText("stat-cycle-signals", valueOrDash(runtime.signals_emitted));
  setText("stat-signals-24h", valueOrDash(counts.signals_24h));
  setText("stat-signals-total", valueOrDash(counts.signals_total));
  setText("stat-orders-total", valueOrDash(counts.orders_total));
  setText(
    "stat-uptime",
    payload.uptime_seconds ? formatDuration(payload.uptime_seconds) : "—",
  );
  setText("stat-error", runtime.last_error || "none");

  const updated = parseTimestamp(runtime.updated_at);
  el.pipelineMeta.textContent = updated
    ? `last cycle ${updated.toLocaleTimeString([], { hour12: false })} UTC`
    : "no cycle yet";
}

function renderConnection(payload, source) {
  el.connEndpoint.textContent = source === "demo" ? "demo data (no API)" : endpoint();
  el.connEndpoint.title = el.connEndpoint.textContent;
  el.connLedger.textContent = payload.ledger_path || "—";
  el.connLedger.title = payload.ledger_path || "";
  setText("conn-auth", state.settings.apiToken ? "bearer token" : "none");

  const stale = asNumber(payload.stale_seconds);
  setText(
    "conn-stale",
    stale === null ? "—" : formatDuration(stale),
  );
  el.connMeta.textContent = state.paused ? "auto-refresh paused" : `auto-refresh ${REFRESH_SECONDS}s`;
}

function renderSignals(signals) {
  el.signalsBody.replaceChildren();
  if (!signals.length) {
    el.signalsBody.append(emptyRow(9, "No signals yet. DRY-RUN records signals without placing orders."));
    el.signalsMeta.textContent = "0 rows";
    return;
  }

  for (const signal of signals) {
    const row = document.createElement("tr");
    const reason = String(signal.reason || "");
    const blocked = reason.includes("RISK_BLOCKED") || signal.demo_blocked;
    const strategy = signal.demo_strategy || detectStrategy(reason);

    row.append(
      cell(formatTs(signal.ts), "mono"),
      tagCell(strategy),
      cell(shortId(signal.market_id), "mono", String(signal.market_id || "")),
      cell(String(signal.side || "").toUpperCase()),
      cell(asNumber(signal.price)?.toFixed(3) ?? "—", "num"),
      cell(asNumber(signal.size_usd)?.toFixed(2) ?? "—", "num"),
      cell(asNumber(signal.expected_value)?.toFixed(2) ?? "—", "num"),
      statusCell(signal.executed, blocked, reason),
      cell(reason, "reason", reason),
    );
    el.signalsBody.append(row);
  }
  el.signalsMeta.textContent = `${signals.length} shown`;
}

function renderOrders(orders) {
  el.ordersBody.replaceChildren();
  if (!orders.length) {
    el.ordersBody.append(emptyRow(7, "No orders. DRY-RUN never submits an order."));
    el.ordersMeta.textContent = "0 rows";
    return;
  }
  for (const order of orders) {
    const row = document.createElement("tr");
    row.append(
      cell(formatTs(order.ts), "mono"),
      cell(shortId(order.order_id || "—"), "mono", String(order.order_id || "")),
      cell(shortId(order.token_id), "mono", String(order.token_id || "")),
      cell(String(order.side || "").toUpperCase()),
      cell(asNumber(order.price)?.toFixed(4) ?? "—", "num"),
      cell(asNumber(order.size_shares)?.toFixed(2) ?? "—", "num"),
      tagCell(String(order.status || "unknown"), orderStatusTone(order.status)),
    );
    el.ordersBody.append(row);
  }
  el.ordersMeta.textContent = `${orders.length} shown`;
}

// ---------------------------------------------------------------- helpers

function detectStrategy(reason) {
  if (reason.startsWith("ARB")) return "ARB";
  if (reason.startsWith("SNIPE")) return "SNIPE";
  if (reason.startsWith("MODEL")) return "MODEL";
  return "OTHER";
}

function strategyTone(strategy) {
  if (strategy === "ARB") return "arb";
  if (strategy === "SNIPE") return "snipe";
  if (strategy === "MODEL") return "model";
  return "neutral";
}

function orderStatusTone(status) {
  const value = String(status || "").toLowerCase();
  if (["error", "failed", "rejected", "unmatched", "exception"].includes(value)) return "bad";
  if (["submitted", "live", "matched", "filled"].includes(value)) return "good";
  return "neutral";
}

function tagCell(label, tone = "neutral") {
  const td = document.createElement("td");
  const span = document.createElement("span");
  span.className = `tag tag-${tone}`;
  span.textContent = label;
  td.append(span);
  return td;
}

function statusCell(executed, blocked, reason) {
  if (blocked) return tagCell("blocked", "bad");
  if (executed) return tagCell("executed", "good");
  if (String(reason).includes("DRY_RUN")) return tagCell("dry run", "neutral");
  return tagCell("recorded", "neutral");
}

function cell(text, className, title) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  if (title) td.title = title;
  return td;
}

function emptyRow(columns, message) {
  const row = document.createElement("tr");
  row.className = "empty-row";
  const td = document.createElement("td");
  td.colSpan = columns;
  td.textContent = message;
  row.append(td);
  return row;
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

function setClass(id, className) {
  const node = document.getElementById(id);
  if (!node) return;
  // Preserve the markup's base classes (e.g. "kpi-value kpi-value-sm") and only
  // swap the tone modifier.
  if (!node.dataset.base) node.dataset.base = node.className;
  node.className = `${node.dataset.base} ${className}`.trim();
}

function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function money(value) {
  return `$${Number(value).toFixed(2)}`;
}

function signedMoney(value) {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}$${Math.abs(Number(value)).toFixed(2)}`;
}

function valueOrDash(value) {
  return value === null || value === undefined || Number.isNaN(Number(value))
    ? "—"
    : String(value);
}

function shortId(value) {
  const text = String(value || "");
  if (!text) return "—";
  return text.length > 14 ? `${text.slice(0, 6)}…${text.slice(-4)}` : text;
}

function formatTs(value) {
  const date = parseTimestamp(value);
  if (!date) return "—";
  return date.toISOString().replace("T", " ").slice(0, 19);
}

function parseTimestamp(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  if (total < 86_400) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  return `${Math.floor(total / 86_400)}d ${Math.floor((total % 86_400) / 3600)}h`;
}

// -------------------------------------------------------------- scheduling

function scheduleNext() {
  clearTimeout(state.timer);
  state.nextRefreshAt = Date.now() + REFRESH_SECONDS * 1000;
  setCountdownLabel();
  if (!state.paused) {
    state.timer = setTimeout(fetchStatus, REFRESH_SECONDS * 1000);
  }
}

function setCountdownLabel() {
  if (!el.countdown) return;
  if (state.paused) {
    el.countdown.textContent = "paused";
    return;
  }
  if (state.loading) {
    el.countdown.textContent = "loading…";
    return;
  }
  const remaining = Math.max(0, Math.round((state.nextRefreshAt - Date.now()) / 1000));
  el.countdown.textContent = `${remaining}s`;
}

function startTick() {
  clearInterval(state.tickTimer);
  state.tickTimer = setInterval(() => {
    setCountdownLabel();
    if (state.lastPayload) {
      const runtime = state.lastPayload.runtime || {};
      const updated = parseTimestamp(runtime.updated_at);
      if (updated) {
        const stale = (Date.now() - updated.getTime()) / 1000;
        setText("kpi-heartbeat", `${formatDuration(stale)} ago`);
        setText("conn-stale", formatDuration(stale));
      }
    }
  }, 1000);
}

// --------------------------------------------------------------- controls

function openDialog() {
  el.apiBase.value = state.settings.apiBase;
  el.apiToken.value = state.settings.apiToken;
  if (typeof el.dialog.showModal === "function") {
    el.dialog.showModal();
  } else {
    el.dialog.setAttribute("open", "");
  }
}

function wireControls() {
  el.refreshBtn.addEventListener("click", () => fetchStatus());
  el.connectBtn.addEventListener("click", openDialog);

  el.useDemo.addEventListener("click", () => {
    saveSettings({ apiBase: "", apiToken: "" });
    el.dialog.close();
    state.paused = false;
    el.autoRefresh.checked = true;
    applyPayload(buildDemoPayload(), "demo", "demo data selected");
    scheduleNext();
  });

  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings({
      apiBase: el.apiBase.value.trim(),
      apiToken: el.apiToken.value.trim(),
    });
    el.dialog.close();
    state.paused = false;
    el.autoRefresh.checked = true;
    fetchStatus();
  });

  el.autoRefresh.addEventListener("change", () => {
    state.paused = !el.autoRefresh.checked;
    el.connMeta.textContent = state.paused
      ? "auto-refresh paused"
      : `auto-refresh ${REFRESH_SECONDS}s`;
    if (state.paused) {
      clearTimeout(state.timer);
      setCountdownLabel();
    } else {
      scheduleNext();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.timer);
    } else if (!state.paused) {
      fetchStatus();
    }
  });
}

function boot() {
  wireControls();
  startTick();
  applyPayload(buildDemoPayload(), "demo");
  fetchStatus();
}

boot();
