// Run with: node --test --experimental-strip-types test/   (Node 22.6+)
import { test } from "node:test";
import assert from "node:assert/strict";
import { containerEnv, forwardRequest, starting } from "../src/helpers.ts";

test("only TESSERA_* settings reach the container, with hosted defaults", () => {
  const env = containerEnv({ TESSERA_MASTER_KEY: "k", TESSERA_TIMEZONE: "America/Vancouver", R2_ACCOUNT_ID: "acc1", OTHER_SECRET: "no", TESSERA_EMPTY: "" });
  assert.equal(env.TESSERA_MASTER_KEY, "k");
  assert.equal(env.TESSERA_CLOUD_ENDPOINT, "https://acc1.r2.cloudflarestorage.com");
  assert.equal(env.TESSERA_SECURE_COOKIES, "true");
  assert.equal(env.TESSERA_TRUST_PROXY_HEADERS, "true");
  assert.equal(env.OTHER_SECRET, undefined);
  assert.equal(env.TESSERA_EMPTY, undefined);
});

test("an explicit endpoint wins over the account id", () => {
  assert.equal(containerEnv({ TESSERA_CLOUD_ENDPOINT: "https://s3.example.com", R2_ACCOUNT_ID: "a" }).TESSERA_CLOUD_ENDPOINT, "https://s3.example.com");
});

test("forwarding headers come from Cloudflare, not from the caller", () => {
  const req = new Request("https://tessera.example.com/api/x", {
    headers: { "cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8", forwarded: "for=9.9.9.9" },
  });
  const out = forwardRequest(req);
  assert.equal(out.headers.get("x-forwarded-for"), "203.0.113.7");
  assert.equal(out.headers.get("x-forwarded-proto"), "https");
  assert.equal(out.headers.get("x-forwarded-host"), "tessera.example.com");
  assert.equal(out.headers.get("x-real-ip"), null);
  assert.equal(out.headers.get("forwarded"), null);
});

test("a caller without Cloudflare's header cannot pick an address", () => {
  const out = forwardRequest(new Request("https://t.example.com/", { headers: { "x-forwarded-for": "6.6.6.6" } }));
  assert.equal(out.headers.get("x-forwarded-for"), null);
});

test("browsers get a starting page, API clients a retryable 503", async () => {
  const page = starting(new Request("https://t/", { headers: { accept: "text/html" } }));
  assert.equal(page.status, 503);
  assert.match(await page.text(), /Tessera is starting/);
  const api = starting(new Request("https://t/api/datasets", { headers: { accept: "application/json" } }));
  assert.equal(api.status, 503);
  assert.equal(api.headers.get("retry-after"), "5");
});
