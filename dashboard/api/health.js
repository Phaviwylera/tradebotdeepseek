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
    // /healthz also reports the loop's vitals (it needs no token). Pass the few
    // fields that answer "is it actually doing anything?" straight through, so
    // this single URL is a complete check.
    let vitals = {};
    try {
      const parsed = JSON.parse(await upstream.text());
      vitals = {
        loop: parsed.loop ?? null,
        mode: parsed.mode ?? null,
        last_cycle_at: parsed.last_cycle_at ?? null,
        seconds_since_cycle: parsed.seconds_since_cycle ?? null,
        markets_scanned: parsed.markets_scanned ?? null,
        markets_evaluated: parsed.markets_evaluated ?? null,
        signals_last_cycle: parsed.signals_last_cycle ?? null,
        signals_total: parsed.counts?.signals_total ?? null,
        orders_total: parsed.counts?.orders_total ?? null,
        last_error: parsed.last_error ?? null,
      };
    } catch {
      vitals = {};
    }
    res.status(ok ? 200 : 503).setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.end(
      JSON.stringify({ ok, ...body, upstream_status: upstream.status, ...vitals }),
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
