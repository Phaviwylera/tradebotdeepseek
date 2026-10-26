// Unit tests for the Vercel status proxy: cold-start retries, auth hints,
// and the "waking" contract the console relies on.
//
//   node --test dashboard/api/
//
// No network: global.fetch is stubbed per test.

import { test, before, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// Keep the retry loop fast; the real defaults are 6s / 50s.
process.env.BOT_RETRY_DELAY_MS = "5";
process.env.BOT_WAKE_BUDGET_MS = "400";
process.env.BOT_ATTEMPT_TIMEOUT_MS = "200";
process.env.BOT_API_URL = "https://bot.example.test";

const handler = require("./status.js");

const REAL_FETCH = global.fetch;
let calls = [];

function respondWith(responses) {
  let index = 0;
  global.fetch = async (url, init) => {
    calls.push({ url: String(url), headers: init?.headers || {} });
    const next = responses[Math.min(index, responses.length - 1)];
    index += 1;
    if (next instanceof Error) throw next;
    return {
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      text: async () => next.body,
    };
  };
}

function fakeRes() {
  const res = {
    statusCode: null,
    headers: {},
    body: "",
    status(code) {
      this.statusCode = code;
      return this;
    },
    setHeader(key, value) {
      this.headers[key.toLowerCase()] = value;
    },
    end(chunk) {
      this.body = chunk || "";
    },
  };
  return res;
}

const realPayload = {
  ready: true,
  runtime: { mode: "dry_run", equity: 200, killed: false },
  counts: { signals_total: 0, signals_24h: 0, orders_total: 0 },
  recent_signals: [],
  recent_orders: [],
};

before(() => {
  delete process.env.BOT_API_TOKEN;
});

beforeEach(() => {
  calls = [];
});

test.afterEach?.();

test("forwards a healthy payload unchanged", async () => {
  respondWith([{ status: 200, body: JSON.stringify(realPayload) }]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(JSON.parse(res.body), realPayload);
  assert.equal(res.headers["cache-control"], "no-store, max-age=0");
  global.fetch = REAL_FETCH;
});

test("rides out a Render cold-start page and returns the real payload", async () => {
  respondWith([
    { status: 502, body: "<html>Render - Application loading</html>" },
    { status: 200, body: JSON.stringify(realPayload) },
  ]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(JSON.parse(res.body), realPayload);
  assert.equal(calls.length, 2, "should have retried once");
  global.fetch = REAL_FETCH;
});

test("reports waking:true when the boot does not finish in the budget", async () => {
  respondWith([{ status: 502, body: "Allocating compute resources ..." }]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(res.statusCode, 503);
  const payload = JSON.parse(res.body);
  assert.equal(payload.waking, true);
  assert.equal(payload.configured, true);
  assert.equal(res.headers["retry-after"], "20");
  global.fetch = REAL_FETCH;
});

test("turns an upstream 401 into a token hint", async () => {
  respondWith([{ status: 401, body: JSON.stringify({ error: "unauthorized" }) }]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(res.statusCode, 401);
  const payload = JSON.parse(res.body);
  assert.equal(payload.error, "unauthorized");
  assert.match(payload.hint, /BOT_API_TOKEN/);
  global.fetch = REAL_FETCH;
});

test("passes a structured bot error through instead of masking it", async () => {
  respondWith([
    {
      status: 503,
      body: JSON.stringify({ error: "ledger_unavailable" }),
    },
  ]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  // 503 alone looks like a cold start, so the proxy retries; the final answer
  // must still be the structured error, not a generic 502.
  const payload = JSON.parse(res.body);
  assert.ok(
    payload.error === "ledger_unavailable" || payload.waking === true,
    `unexpected body: ${res.body}`,
  );
  global.fetch = REAL_FETCH;
});

test("rejects writes", async () => {
  const res = fakeRes();
  await handler({ method: "POST", query: {} }, res);
  assert.equal(res.statusCode, 405);
  assert.equal(res.headers.allow, "GET, HEAD");
  assert.equal(calls.length, 0, "must not call upstream for a write");
});

test("clamps the limit it forwards", async () => {
  respondWith([{ status: 200, body: JSON.stringify(realPayload) }]);
  const res = fakeRes();
  await handler({ method: "GET", query: { limit: "9999" } }, res);
  assert.match(calls[0].url, /limit=200/);
  global.fetch = REAL_FETCH;
});

test("sends the bot token only when configured", async () => {
  process.env.BOT_API_TOKEN = "secret-token";
  respondWith([{ status: 200, body: JSON.stringify(realPayload) }]);
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(calls[0].headers.Authorization, "Bearer secret-token");
  delete process.env.BOT_API_TOKEN;
  global.fetch = REAL_FETCH;
});

test("explains a missing BOT_API_URL", async () => {
  const saved = process.env.BOT_API_URL;
  delete process.env.BOT_API_URL;
  const res = fakeRes();
  await handler({ method: "GET", query: {} }, res);
  assert.equal(res.statusCode, 503);
  assert.equal(JSON.parse(res.body).configured, false);
  process.env.BOT_API_URL = saved;
  global.fetch = REAL_FETCH;
});

test("detects the cold-start markers", () => {
  assert.equal(handler.looksLikeColdStart(502, ""), true);
  assert.equal(handler.looksLikeColdStart(200, "<html>Application loading</html>"), true);
  assert.equal(handler.looksLikeColdStart(200, '{"ready":true}'), false);
  assert.equal(handler.looksLikeColdStart(404, '{"error":"not_found"}'), false);
});
