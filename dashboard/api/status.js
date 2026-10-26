// Vercel serverless proxy: /api/status -> <BOT_API_URL>/api/status
//
// The console is a static page. When it is hosted on Vercel (or anywhere other
// than the bot itself), the browser must not call the bot directly: an HTTPS
// page cannot fetch a plain-HTTP endpoint, and the bot should not need to widen
// its CORS policy. This function forwards the read-only GET instead.
//
// Environment variables (Vercel project settings):
//   BOT_API_URL     e.g. https://polymarket-bot.onrender.com   (required)
//   BOT_API_TOKEN   value of the bot's DASHBOARD_TOKEN          (optional)
//
// It never forwards anything but GET/HEAD and never accepts a request body.

const TIMEOUT_MS = 12_000;

function json(res, status, payload) {
  res.status(status);
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.end(JSON.stringify(payload));
}

module.exports = async function handler(req, res) {
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
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const headers = { Accept: "application/json" };
    const token = (process.env.BOT_API_TOKEN || "").trim();
    if (token) headers.Authorization = `Bearer ${token}`;

    const upstream = await fetch(target, {
      method: "GET",
      headers,
      signal: controller.signal,
      redirect: "follow",
    });

    const text = await upstream.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      return json(res, 502, {
        configured: true,
        error: "bot returned a non-JSON response",
        upstream_status: upstream.status,
        body_preview: text.slice(0, 240),
      });
    }

    return json(res, upstream.status, payload);
  } catch (error) {
    const aborted = error && error.name === "AbortError";
    return json(res, aborted ? 504 : 502, {
      configured: true,
      error: aborted
        ? `bot did not answer within ${TIMEOUT_MS / 1000}s`
        : `could not reach the bot: ${error && error.message ? error.message : "unknown error"}`,
    });
  } finally {
    clearTimeout(timer);
  }
};
