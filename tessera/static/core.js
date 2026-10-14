"use strict";
/* Core: DOM helper, API client, router, toasts, dialogs. */
const T = (window.T = { me: null, routes: {}, timers: [] });

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") for (const [sk, sv] of Object.entries(v)) { if (sk.startsWith("--")) el.style.setProperty(sk, sv); else el.style[sk] = sv; }
      else if (k === "html") el.innerHTML = v; // only for trusted static markup (icons)
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
/* Let replaceChildren/append accept null, false and nested arrays, like h(). */
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const m of ["replaceChildren", "append"]) {
    const orig = proto[m];
    proto[m] = function (...kids) { return orig.apply(this, kids.flat(Infinity).filter((k) => k != null && k !== false)); };
  }
}
const $ = (s, r = document) => r.querySelector(s);
const fmt = (n, d = 0) => (n == null || n === "" ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const pct = (x) => (x == null ? "—" : (x * 100).toFixed(x >= 0.999 || x === 0 ? 0 : 1) + "%");
const ago = (iso) => {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
};
const fqn = (d) => `${d.schema_name}.${d.name}`;
const can = (role) => ["viewer", "analyst", "engineer", "admin"].indexOf(T.me?.role) >= ["viewer", "analyst", "engineer", "admin"].indexOf(role);

class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(method, url, body, opts = {}) {
  const headers = { "x-tessera-csrf": "1" };
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  let data = null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("json")) data = await r.json();
  if (!r.ok) {
    if (r.status === 401 && !opts.quiet401) { T.me = null; renderAuth(); }
    const d = data && data.detail;
    throw new ApiError(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${r.status})`, r.status);
  }
  return data;
}
const GET = (u) => api("GET", u);
const POST = (u, b) => api("POST", u, b ?? {});
const PUT = (u, b) => api("PUT", u, b);
const PATCH = (u, b) => api("PATCH", u, b);
const DEL = (u) => api("DELETE", u);

function toast(msg, kind) {
  let t = $("#toast");
  if (!t) { t = h("div", { id: "toast", class: "toast", role: "status" }); document.body.append(t); }
  t.textContent = msg; t.className = "toast" + (kind ? " " + kind : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), kind === "bad" ? 6000 : 2800);
}
async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, "bad"); throw e; }
  finally { if (btn) btn.disabled = false; }
}
const safe = (p) => p.catch(() => {});

function dialog(title, body, actions = []) {
  const d = h("dialog", { "aria-label": title });
  const close = () => { d.close(); d.remove(); };
  d.append(
    h("div", { class: "dhead" }, h("h2", null, title), h("button", { class: "ghost", onclick: close, "aria-label": "Close" }, "✕")),
    h("div", { class: "dbody" }, body),
    h("div", { class: "dfoot" }, h("button", { onclick: close }, "Cancel"),
      actions.map((a) => h("button", { class: a.class || "primary", onclick: async (e) => { try { if ((await a.run(e)) !== false) close(); } catch (err) { toast(err.message, "bad"); } } }, a.label)))
  );
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  d.showModal();
  return { el: d, close };
}
function confirmBox(text, label, run) {
  dialog("Please confirm", h("p", null, text), [{ label, class: "primary danger", run }]);
}
function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", null, label), input, hint ? h("span", { class: "small" }, hint) : null);
}
function chip(text, kind) { return h("span", { class: "chip " + (kind || "") }, text); }
function schemaTag(s) { return h("span", { class: "schema " + s }, s); }
function statusChip(s) {
  const k = { success: "ok", failed: "bad", running: "info", queued: "info", cancelled: "warn", skipped: "warn" }[s] || "";
  return chip(s || "—", k);
}
function scoreKind(v, okAt = 0.99, warnAt = 0.9) { return v == null ? "" : v >= okAt ? "ok" : v >= warnAt ? "warn" : "bad"; }
function meter(v, kind) {
  return h("div", { class: "meter " + (kind || scoreKind(v)), title: pct(v) }, h("i", { style: { width: Math.max(0, Math.min(1, v || 0)) * 100 + "%" } }));
}
function readinessChip(score) {
  if (score == null) return chip("—");
  return chip(Math.round(score) + "/100", score >= 85 ? "ok" : score >= 65 ? "warn" : "bad");
}
function table(columns, rows, opts = {}) {
  const thead = h("thead", null, h("tr", null, columns.map((c) => h("th", { class: c.num ? "num" : null }, c.label))));
  const tbody = h("tbody", null, rows.map((r) => h("tr", { class: opts.onclick ? "click" : null, onclick: opts.onclick ? () => opts.onclick(r) : null },
    columns.map((c) => { const v = c.render ? c.render(r) : r[c.key]; return h("td", { class: c.num ? "num" : c.mono ? "mono" : null }, v == null ? "—" : v); }))));
  return h("div", { class: "tablewrap " + (opts.cls || "") }, h("table", null, thead, tbody));
}
function resultTable(res) {
  if (!res || !res.columns.length) return h("p", { class: "muted" }, "No columns returned.");
  return h("div", { class: "tablewrap results" }, h("table", null,
    h("thead", null, h("tr", null, res.columns.map((c, i) => h("th", { title: res.types?.[i] || "" }, c)))),
    h("tbody", null, res.rows.map((r) => h("tr", null, r.map((v) => v === null ? h("td", { class: "null" }, "null") : h("td", { title: String(v), class: typeof v === "number" ? "num" : null }, typeof v === "number" ? fmt(v, 4) : String(v))))))));
}
/* Download a server-generated file (CSV, Excel, bundles) and keep auth + CSRF headers. */
async function downloadPost(url, body, fallback) {
  const r = await fetch(url, { method: body === undefined ? "GET" : "POST", headers: { "x-tessera-csrf": "1", "content-type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  if (!r.ok) { let m = `Download failed (${r.status})`; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  const cd = r.headers.get("content-disposition") || "";
  const name = (cd.match(/filename="([^"]+)"/) || [])[1] || fallback || "download";
  const blob = await r.blob();
  const a = h("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast("Downloaded " + name);
}
const wideScreen = () => window.innerWidth > 860;
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
/* Minimal, escape-first Markdown for notes and text tiles: headings, bold, italic, code, lists, http(s) links. */
function md(src) {
  const esc = (t) => t.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const inline = (t) => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\*([^*]+)\*/g, "<i>$1</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = []; let list = false;
  for (const line of String(src || "").split("\n")) {
    const m = line.match(/^(#{1,3})\s+(.*)$/), li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (m) out.push(`<h${m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1}>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return h("div", { class: "md", html: out.join("") });
}
async function copyText(text, el) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { if (el && el.select) { el.select(); toast("Selected. Press Ctrl+C to copy."); } }
}

/* router */
function route(path, view) { T.routes[path] = view; }
function go(hash) { if (location.hash === hash) render(); else location.hash = hash; }
function clearTimers() { T.timers.forEach(clearInterval); T.timers = []; }
async function render() {
  clearTimers();
  if (!T.me) return renderAuth();
  const [, name, id, sub] = (location.hash || "#/overview").split("/");
  const view = T.routes[name] || T.routes.overview;
  document.querySelectorAll(".rail a[data-r]").forEach((a) => a.setAttribute("aria-current", a.dataset.r === name ? "page" : "false"));
  const main = $("#main");
  main.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try { await view(main, id, sub); }
  catch (e) { main.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", render);
