// Vercel serverless proxy: /api/status -> <BOT_API_URL>/api/status
//
// The console is a static page. When it is hosted on Vercel (or anywhere other
// than the bot itself), the browser must not call the bot directly: an HTTPS
// page cannot fetch a plain-HTTP endpoint, and the bot should not need to widen
// its CORS policy. This function forwards the read-only GET instead.
//
// Environment variables (Vercel project settings):
//   BOT_API_URL        e.g. https://polymarket-bot.onrender.com   (required)
//   BOT_API_TOKEN      value of the bot's DASHBOARD_TOKEN          (optional)
//   BOT_WAKE_BUDGET_MS total time to spend waking a sleeping bot   (default 50000)
//   BOT_RETRY_DELAY_MS pause between wake-up attempts              (default 6000)
//
// Free hosting tiers stop an idle instance. A cold start answers with an HTML
// error page for tens of seconds, which used to surface as "bot returned a
// non-JSON response". This proxy now rides that out: it retries until the bot
// answers JSON, then returns either the payload or an explicit `waking: true`
// signal the console can explain to the user.
//
// It never forwards anything but GET/HEAD and never accepts a request body.

const ATTEMPT_TIMEOUT_MS = Number(process.env.BOT_ATTEMPT_TIMEOUT_MS || 18_000);
const WAKE_BUDGET_MS = Number(process.env.BOT_WAKE_BUDGET_MS || 50_000);
const RETRY_DELAY_MS = Number(process.env.BOT_RETRY_DELAY_MS || 6_000);
const MAX_ATTEMPTS = 4;

// Render (and similar platforms) serve these while an instance boots.
const COLD_START_MARKERS =
  /Allocating compute resources|Starting the instance|Application loading|Optimizing deployment|Preparing instance/i;

function json(res, status, payload, extraHeaders = {}) {
  res.status(status);
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.setHeader("X-Content-Type-Options", "nosniff");
  for (const [key, value] of Object.entries(extraHeaders)) res.setHeader(key, value);
  res.end(JSON.stringify(payload));
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function looksLikeColdStart(status, text) {
  if (status === 502 || status === 503 || status === 504) return true;
  return COLD_START_MARKERS.test(text || "");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchOnce(target, headers, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target, {
      method: "GET",
      headers,
      signal: controller.signal,
      redirect: "follow",
    });
    const text = await response.text();
    return { status: response.status, ok: response.ok, text };
  } finally {
    clearTimeout(timer);
  }
}

async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    return json(res, 405, { error: "method_not_allowed" });
  }

  const baseUrl = (process.env.BOT_API_URL || "").trim().replace(/\/+$/, "");
  if (!baseUrl) {
    return json(res, 503, {
      configured: false,
      error:
        "BOT_API_URL is not set. Add it in the Vercel project's environment " +
        "variables, pointing at your bot's public URL.",
    });
  }

  const limits = new URLSearchParams();
  const requested = Number.parseInt(String(req.query.limit || "50"), 10);
  limits.set("limit", String(Math.min(200, Math.max(1, requested || 50))));

  const target = `${baseUrl}/api/status?${limits.toString()}`;
  const headers = { Accept: "application/json" };
  const token = (process.env.BOT_API_TOKEN || "").trim();
  if (token) headers.Authorization = `Bearer ${token}`;

  const deadline = Date.now() + WAKE_BUDGET_MS;
  let lastStatus = 0;
  let lastText = "";

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    try {
      const upstream = await fetchOnce(target, headers, ATTEMPT_TIMEOUT_MS);
      lastStatus = upstream.status;
      lastText = upstream.text;

      if (upstream.ok) {
        const payload = parseJson(upstream.text);
        if (payload) return json(res, 200, payload);
        return json(res, 502, {
          configured: true,
          error: "bot answered 200 with a non-JSON body",
          upstream_status: upstream.status,
          body_preview: upstream.text.slice(0, 240),
        });
      }

      if (upstream.status === 401 || upstream.status === 403) {
        return json(res, 401, {
          configured: true,
          error: "unauthorized",
          hint: token
            ? "BOT_API_TOKEN does not match the bot's DASHBOARD_TOKEN."
            : "Set BOT_API_TOKEN in the Vercel project to the bot's DASHBOARD_TOKEN, or leave the bot's DASHBOARD_TOKEN empty.",
        });
      }

      const payload = parseJson(upstream.text);
      if (payload && !looksLikeColdStart(upstream.status, "")) {
        // A real, structured error from the bot (ledger unavailable, etc.).
        return json(res, upstream.status, payload);
      }
    } catch (error) {
      lastStatus = 0;
      lastText = String((error && error.message) || error);
    }

    const remaining = deadline - Date.now();
    const isColdStart = looksLikeColdStart(lastStatus, lastText);
    if (!isColdStart || remaining <= RETRY_DELAY_MS || attempt === MAX_ATTEMPTS) {
      break;
    }
    await sleep(RETRY_DELAY_MS);
  }

  if (looksLikeColdStart(lastStatus, lastText)) {
    return json(
      res,
      503,
      {
        configured: true,
        waking: true,
        error:
          "the bot is starting up (a free instance was asleep); " +
          "this page will retry automatically",
        upstream_status: lastStatus || null,
      },
      { "Retry-After": "20" },
    );
  }

  return json(res, 502, {
    configured: true,
    error: "could not reach the bot",
    upstream_status: lastStatus || null,
    detail: String(lastText || "").slice(0, 240),
  });
}

module.exports = handler;
// Exported for the unit test in status.test.mjs.
module.exports.looksLikeColdStart = looksLikeColdStart;
module.exports.parseJson = parseJson;
