// Vercel serverless health probe: /api/health
// Returns 200 only when BOT_API_URL is configured and the bot answers /healthz.

module.exports = async function handler(req, res) {
  const baseUrl = (process.env.BOT_API_URL || "").trim().replace(/\/+$/, "");
  const configured = Boolean(baseUrl);
  const body = { configured, upstream: baseUrl || null, checked_at: new Date().toISOString() };

  if (!configured) {
    res.status(200).setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(JSON.stringify({ ok: true, ...body, mode: "console_only" }));
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8_000);
  try {
    const upstream = await fetch(`${baseUrl}/healthz`, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    const ok = upstream.ok;
    res.status(ok ? 200 : 503).setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(
      JSON.stringify({ ok, ...body, upstream_status: upstream.status }),
    );
  } catch (error) {
    res.status(503).setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(
      JSON.stringify({ ok: false, ...body, error: String((error && error.message) || error) }),
    );
  } finally {
    clearTimeout(timer);
  }
};
