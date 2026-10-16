/** Pure helpers for the Worker, kept free of Cloudflare-only imports so they can be unit-tested in Node. */

/** Settings passed into the container: all TESSERA_* strings, the R2 endpoint, and hosted defaults. */
export function containerEnv(env: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(env)) {
    if (k.startsWith("TESSERA_") && typeof v === "string" && v !== "") out[k] = v;
  }
  if (!out.TESSERA_CLOUD_ENDPOINT && typeof env.R2_ACCOUNT_ID === "string" && env.R2_ACCOUNT_ID) {
    out.TESSERA_CLOUD_ENDPOINT = `https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`;
  }
  out.TESSERA_CLOUD_REGION ??= "auto";
  out.TESSERA_SECURE_COOKIES ??= "true"; // visitors always arrive over HTTPS at Cloudflare's edge
  out.TESSERA_TRUST_PROXY_HEADERS = "true"; // only this Worker can reach the container, and it sets the header
  out.TESSERA_LOG_JSON ??= "true";
  return out;
}

/** Copy the request with trustworthy forwarding headers (anything the client sent is replaced). */
export function forwardRequest(request: Request): Request {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  const ip = request.headers.get("cf-connecting-ip") ?? "";
  for (const h of ["x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip", "forwarded"]) headers.delete(h);
  if (ip) headers.set("x-forwarded-for", ip);
  headers.set("x-forwarded-proto", url.protocol.replace(":", ""));
  headers.set("x-forwarded-host", url.host);
  return new Request(request, { headers });
}

function wantsHtml(request: Request): boolean {
  return request.method === "GET" && (request.headers.get("accept") ?? "").includes("text/html");
}

export function startingPage(): Response {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="4"><title>Tessera is starting</title>
<style>
:root{--bg:#F1F3F0;--ink:#1A2420;--muted:#5B6862;--c1:#3150C9;--c2:#13856F;--c3:#C68A12;--c5:#C4432E}
@media (prefers-color-scheme:dark){:root{--bg:#101614;--ink:#E6ECE8;--muted:#95A39C;--c1:#7D97F2;--c2:#4FC3A7;--c3:#E3AE4F;--c5:#F07A6A}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;padding:24px}
main{display:grid;gap:14px;justify-items:center;text-align:center;max-width:34rem}
.t{display:grid;grid-template-columns:repeat(3,14px);gap:4px}.t i{width:14px;height:14px;border-radius:2px;background:var(--c1);animation:p 1.4s ease-in-out infinite}
.t i:nth-child(2n){background:var(--c3)}.t i:nth-child(3n){background:var(--c2)}.t i:nth-child(5){background:var(--c5)}
.t i:nth-child(1){animation-delay:0s}.t i:nth-child(2){animation-delay:.1s}.t i:nth-child(3){animation-delay:.2s}.t i:nth-child(4){animation-delay:.3s}
.t i:nth-child(5){animation-delay:.4s}.t i:nth-child(6){animation-delay:.5s}.t i:nth-child(7){animation-delay:.6s}.t i:nth-child(8){animation-delay:.7s}.t i:nth-child(9){animation-delay:.8s}
@keyframes p{50%{opacity:.25}}@media (prefers-reduced-motion:reduce){.t i{animation:none}}
h1{margin:0;font-size:1.5rem}p{margin:0;color:var(--muted)}
</style></head><body><main><div class="t" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
<h1>Tessera is starting</h1><p>It is restoring your data from storage. This page refreshes on its own, usually within a minute.</p></main></body></html>`;
  return new Response(html, { status: 503, headers: { "content-type": "text/html; charset=utf-8", "retry-after": "4", "cache-control": "no-store" } });
}

export function starting(request: Request): Response {
  if (wantsHtml(request)) return startingPage();
  return Response.json({ detail: "Tessera is starting; retry shortly." }, { status: 503, headers: { "retry-after": "5", "cache-control": "no-store" } });
}

