"use strict";
/* Alerts, subscriptions, notifications, search palette, monitoring hub, notebooks, transforms, glossary, admin extras. */

const SCHEDULES = [["*/15 * * * *", "Every 15 minutes"], ["0 * * * *", "Hourly"], ["0 8 * * *", "Daily at 08:00"], ["0 8 * * 1", "Weekly, Monday 08:00"]];

/* ---------------- notifications ---------------- */
async function refreshBell() {
  try {
    const n = await GET("/api/notifications");
    const b = $("#bell-count");
    if (b) { b.textContent = n.unread > 99 ? "99+" : n.unread; b.hidden = !n.unread; }
    return n;
  } catch { return null; }
}
function pollNotifications() {
  refreshBell();
  clearInterval(T.bellTimer);
  T.bellTimer = setInterval(refreshBell, 60_000);
}
async function notificationsPanel(anchor) {
  const old = $("#notif-panel");
  if (old) { old.remove(); return; }
  const n = await refreshBell();
  if (!n) return;
  const panel = h("div", { class: "notif", id: "notif-panel", role: "dialog", "aria-label": "Notifications" },
    h("div", { class: "spread" }, h("b", null, "Notifications"), h("button", { class: "sm ghost", onclick: async () => { await POST("/api/notifications/read", {}); panel.remove(); refreshBell(); } }, "Mark all read")),
    n.items.length ? n.items.map((x) => h("button", { class: "ghost notifitem" + (x.read ? "" : " unread"), onclick: async () => { await POST("/api/notifications/read", { ids: [x.id] }); panel.remove(); refreshBell(); if (x.link) location.hash = x.link; } },
      h("span", { class: "tess " + (x.level === "bad" ? "bad" : x.level === "ok" ? "ok" : x.level === "warn" ? "warn" : "") }),
      h("span", { class: "stack", style: { gap: "2px", textAlign: "left" } }, h("b", null, x.title), x.body ? h("span", { class: "small muted" }, x.body) : null, h("span", { class: "small muted" }, ago(x.ts)))))
      : h("p", { class: "muted small" }, "Nothing yet. Alerts, failed pipelines and subscription problems show up here."));
  document.body.append(panel);
  const r = anchor.getBoundingClientRect();
  panel.style.top = r.bottom + 6 + "px";
  panel.style.right = Math.max(8, window.innerWidth - r.right) + "px";
  const close = () => { panel.remove(); document.removeEventListener("click", outside); document.removeEventListener("keydown", esc); window.removeEventListener("hashchange", close); };
  const outside = (e) => { if (!panel.contains(e.target) && !anchor.contains(e.target)) close(); };
  const esc = (e) => { if (e.key === "Escape") { close(); anchor.focus(); } };
  setTimeout(() => document.addEventListener("click", outside), 0);
  document.addEventListener("keydown", esc);
  window.addEventListener("hashchange", close);
  panel.querySelector("button")?.focus();
}

/* ---------------- command palette ---------------- */
function palette() {
  if ($("#palette")) return;
  const input = h("input", { id: "palette-q", placeholder: "Search anything, or ask a question like “revenue by month in BC”", autocomplete: "off", "aria-label": "Search" });
  const list = h("div", { class: "palette-list", role: "listbox" });
  const nav = [["Home", "#/overview"], ["Dashboards", "#/dashboards"], ["Explore", "#/explore"], ["Reports", "#/reports"], ["Catalog", "#/catalog"], ["Semantic models", "#/models"], ["Glossary", "#/glossary"],
    ["Pipelines", "#/pipelines"], ["Monitor", "#/monitor"], ["Alerts", "#/alerts"], ["Notebooks", "#/notebooks"], ["SQL", "#/sql"], ["AI agents", "#/agents"], ["Transforms", "#/transforms"], ["Connections", "#/connections"], ["Admin", "#/admin"]];
  let items = [], idx = 0;
  const d = h("dialog", { id: "palette", class: "palette", "aria-label": "Search" }, input, list);
  const close = () => { d.close(); d.remove(); };
  const open = async (it) => {
    close();
    if (!it.ask) { location.hash = it.link; return; }
    try {
      const r = await POST("/api/ask", { question: it.ask });
      T.exploreSeed = { model_id: r.model_id, measures: r.spec.measures, dimensions: r.spec.dimensions, grains: r.spec.grains || {}, filters: r.spec.filters || [], sort: r.spec.sort || [],
        limit: r.spec.limit < 5000 ? r.spec.limit : undefined, viz: r.viz, compare: !!r.compare, forecast: !!r.viz.forecast, editing: null, asked: { understood: r.understood, notes: r.notes } };
      go("#/explore/" + r.model_id);
    } catch (e) { toast(e.message, "bad"); }
  };
  const draw = () => list.replaceChildren(...items.map((it, i) => h("button", { class: "ghost palette-item", "aria-selected": String(i === idx), role: "option", onclick: () => open(it) },
    chip(it.kind), h("span", { class: "stack", style: { gap: "0", textAlign: "left", minWidth: 0 } }, h("b", null, it.title), it.detail ? h("span", { class: "small muted", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, it.detail) : null),
    it.badge === "certified" ? chip("✓", "ok") : null)));
  const search = debounce(async () => {
    const q = input.value.trim();
    const navHits = nav.filter(([l]) => !q || l.toLowerCase().includes(q.toLowerCase())).map(([l, link]) => ({ kind: "go to", title: l, link }));
    let hits = [];
    if (q.length >= 2) hits = await GET("/api/search?q=" + encodeURIComponent(q)).catch(() => []);
    const askItem = q.split(/\s+/).length >= 3 ? [{ kind: "ask", title: `Ask: ${q}`, detail: "Answer with a chart from your governed semantic models", ask: q }] : [];
    items = [...askItem, ...hits, ...navHits].slice(0, 30);
    idx = 0;
    draw();
  }, 150);
  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { idx = Math.min(items.length - 1, idx + 1); draw(); e.preventDefault(); }
    if (e.key === "ArrowUp") { idx = Math.max(0, idx - 1); draw(); e.preventDefault(); }
    if (e.key === "Enter" && items[idx]) open(items[idx]);
  });
  d.addEventListener("close", () => d.remove());
  d.addEventListener("click", (e) => { if (e.target === d) close(); });
  document.body.append(d);
  d.showModal();
  search();
}
document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && T.me) { e.preventDefault(); palette(); } });

/* ---------------- alerts & subscriptions ---------------- */
async function alertDialog(seed = {}) {
  const [models, queries, dsets, pipes] = await Promise.all([GET("/api/models"), GET("/api/queries"), can("engineer") ? GET("/api/datasets") : [], GET("/api/pipelines")]);
  const name = h("input", { id: "al-name", value: seed.name || "", placeholder: "Revenue dropped" });
  const types = [["model", "A measure from a semantic model"], ["query", "A value from a saved report"], ["pipeline", "A pipeline run fails"]];
  if (can("engineer")) types.push(["quality", "A dataset's quality score"], ["freshness", "Hours since a dataset was loaded"]);
  const type = h("select", { id: "al-type" }, types.map(([v, l]) => h("option", { value: v, selected: seed.source?.type === v }, l)));
  const src = h("div", { class: "form" });
  const op = h("select", { id: "al-op" }, [">", ">=", "<", "<=", "=", "!="].map((x) => h("option", null, x)));
  const thr = h("input", { id: "al-thr", type: "number", step: "any", placeholder: "0.08" });
  const sched = h("select", { id: "al-sched" }, SCHEDULES.map(([v, l]) => h("option", { value: v }, l)));
  const inapp = h("input", { type: "checkbox", id: "al-inapp", checked: true });
  const emails = h("input", { id: "al-email", placeholder: "Optional: ops@company.com" });
  const hook = h("input", { id: "al-hook", placeholder: "Optional: https://hooks.slack.com/…" });
  const condRow = h("div", { class: "form" }, field("Condition", op), field("Threshold", thr, "Percent measures use fractions: 8% is 0.08."));
  let getSource = () => ({});
  const drawSrc = () => {
    const t = type.value;
    condRow.hidden = t === "pipeline";
    if (t === "model") {
      const ms = h("select", { id: "al-model" }, models.map((m) => h("option", { value: m.id, selected: m.id === seed.source?.model_id }, m.name)));
      const meas = h("select", { id: "al-measure" });
      const sync = () => meas.replaceChildren(...(models.find((m) => m.id === +ms.value)?.measures || []).map((x) => h("option", { value: x.name, selected: x.name === seed.source?.measure }, x.label)));
      ms.addEventListener("change", sync); sync();
      src.replaceChildren(field("Model", ms), field("Measure", meas));
      getSource = () => ({ type: "model", model_id: +ms.value, measure: meas.value, filters: seed.source?.type === "model" ? seed.source.filters || [] : [] });
    } else if (t === "query") {
      const qs = h("select", { id: "al-query" }, queries.map((q) => h("option", { value: q.id, selected: q.id === seed.source?.query_id }, q.name)));
      const f = h("input", { id: "al-field", placeholder: "Optional column name" });
      src.replaceChildren(field("Report", qs), field("Column (first row is checked)", f));
      getSource = () => ({ type: "query", query_id: +qs.value, field: f.value || undefined });
    } else if (t === "pipeline") {
      const ps = h("select", { id: "al-pipe" }, pipes.map((p) => h("option", { value: p.id }, p.name)));
      src.replaceChildren(field("Pipeline", ps));
      getSource = () => ({ type: "pipeline", pipeline_id: +ps.value });
    } else {
      const ds = h("select", { id: "al-ds" }, dsets.map((x) => h("option", { value: x.id }, fqn(x))));
      src.replaceChildren(field("Dataset", ds));
      if (t === "quality") { op.value = "<"; thr.placeholder = "95"; } else { op.value = ">"; thr.placeholder = "24"; }
      getSource = () => ({ type: t, dataset_id: +ds.value });
    }
  };
  type.addEventListener("change", drawSrc);
  drawSrc();
  dialog("New alert", h("div", { class: "stack" }, h("div", { class: "form" }, field("Name", name), field("Watch", type)), src, condRow,
    h("div", { class: "form" }, field("Check", sched), field("Email", emails), field("Webhook (Teams, Slack, PagerDuty…)", hook)),
    h("label", { class: "row small" }, inapp, "Notify me in Tessera"), h("p", { class: "small muted" }, "You are notified when the alert starts firing and when it resolves, not on every check.")),
  [{ label: "Create alert", run: async () => {
    const a = await POST("/api/alerts", { name: name.value || "Alert", source: getSource(), condition: { op: op.value, threshold: thr.value === "" ? 0 : +thr.value }, schedule: sched.value,
      channels: { inapp: inapp.checked, email: emails.value || undefined, webhook: hook.value || undefined } });
    toast(`Alert created. Current state: ${a.state}`);
    if (location.hash.startsWith("#/alerts")) render();
  } }]);
}

function alertStateChip(s) { return chip(s, { firing: "bad", ok: "ok", error: "warn" }[s] || ""); }
function describeSource(a, names) {
  const s = a.source;
  return { model: `${names.models[s.model_id] || "model"} · ${s.measure}`, query: `report: ${names.queries[s.query_id] || s.query_id}`, pipeline: `pipeline: ${names.pipes[s.pipeline_id] || s.pipeline_id}`,
    quality: `quality of dataset #${s.dataset_id}`, freshness: `freshness of dataset #${s.dataset_id}` }[s.type] || s.type;
}

route("alerts", async (main) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "Alerts need the analyst role.")); return; }
  const [alerts, subs, models, queries, pipes] = await Promise.all([GET("/api/alerts"), GET("/api/subscriptions"), GET("/api/models"), GET("/api/queries"), GET("/api/pipelines")]);
  const names = { models: Object.fromEntries(models.map((m) => [m.id, m.name])), queries: Object.fromEntries(queries.map((q) => [q.id, q.name])), pipes: Object.fromEntries(pipes.map((p) => [p.id, p.name])) };
  const firing = alerts.filter((a) => a.state === "firing").length;
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Operate"), h("h1", null, "Alerts and subscriptions"),
    h("span", { class: "muted small" }, firing ? `${firing} alert${firing > 1 ? "s" : ""} firing` : "Nothing firing right now")), h("button", { class: "primary", onclick: () => alertDialog() }, "New alert")),
  h("div", { class: "card flat" }, alerts.length ? table([
    { label: "", render: (a) => h("span", { class: "tess " + ({ firing: "bad", ok: "ok", error: "warn" }[a.state] || "") }) },
    { label: "Alert", render: (a) => h("div", null, h("b", null, a.name), h("div", { class: "small muted" }, describeSource(a, names))) },
    { label: "Condition", render: (a) => a.source.type === "pipeline" ? "run fails" : h("span", { class: "mono" }, `${a.condition.op} ${a.condition.threshold}`) },
    { label: "State", render: (a) => alertStateChip(a.state) },
    { label: "Last value", num: true, render: (a) => { try { const v = JSON.parse(a.last_value); return v == null ? "—" : typeof v === "number" ? fmt(v, 4) : String(v); } catch { return "—"; } } },
    { label: "Checked", render: (a) => h("span", { class: "small" }, ago(a.last_checked), a.last_error ? h("div", { class: "small", style: { color: "var(--bad)" } }, a.last_error) : null) },
    { label: "", render: (a) => h("div", { class: "row" },
      h("button", { class: "sm", onclick: (e) => act(e.target, () => POST(`/api/alerts/${a.id}/check`)).then((r) => { toast(`State: ${r.state}${r.value != null ? ", value " + fmt(r.value, 4) : ""}`); render(); }) }, "Check now"),
      h("button", { class: "sm", onclick: () => act(null, () => PUT(`/api/alerts/${a.id}`, { enabled: !a.enabled }), a.enabled ? "Paused" : "Resumed").then(render) }, a.enabled ? "Pause" : "Resume"),
      h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/alerts/${a.id}`), "Deleted").then(render) }, "Delete")) },
  ], alerts) : h("div", { class: "empty" }, "No alerts yet. Watch a measure, a report value, a pipeline, data quality or freshness.")),
  h("h2", null, "Email subscriptions"),
  h("div", { class: "card flat" }, subs.length ? table([
    { label: "Subscription", render: (s) => h("div", null, h("b", null, s.name), h("div", { class: "small muted" }, `${s.dashboard} · ${s.attach}`)) },
    { label: "Schedule", render: (s) => h("span", { class: "mono small" }, s.schedule) },
    { label: "Recipients", render: (s) => h("span", { class: "small" }, s.recipients.join(", ")) },
    { label: "Last sent", render: (s) => h("span", { class: "small" }, s.last_sent_at ? ago(s.last_sent_at) : "never", s.last_status && s.last_status !== "sent" ? h("div", { style: { color: "var(--bad)" } }, s.last_status) : null) },
    { label: "", render: (s) => h("div", { class: "row" },
      h("button", { class: "sm", onclick: (e) => act(e.target, () => POST(`/api/subscriptions/${s.id}/send`), "Sent").then(render) }, "Send now"),
      h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/subscriptions/${s.id}`), "Deleted").then(render) }, "Delete")) },
  ], subs) : h("div", { class: "empty" }, "No subscriptions. Open a dashboard and choose Subscribe to email it on a schedule.")));
});

/* ---------------- monitoring hub ---------------- */
route("monitor", async (main) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "Monitoring needs the analyst role.")); return; }
  const m = await GET("/api/monitor");
  const total = m.runs.length, failed = m.runs.filter((r) => r.status === "failed").length;
  const durs = m.runs.filter((r) => r.seconds != null).map((r) => r.seconds);
  const statusSel = h("select", { id: "mon-status", "aria-label": "Status filter", style: { width: "auto" } }, ["all", "success", "failed", "running", "cancelled"].map((x) => h("option", null, x)));
  const runsBox = h("div");
  const drawRuns = () => runsBox.replaceChildren(h("div", { class: "card flat" }, table([
    { label: "Run", render: (r) => "#" + r.id }, { label: "Pipeline", key: "pipeline" }, { label: "Status", render: (r) => statusChip(r.status) },
    { label: "Trigger", render: (r) => `${r.trigger}` }, { label: "Started", render: (r) => new Date(r.created_at).toLocaleString() },
    { label: "Seconds", num: true, render: (r) => r.seconds != null ? r.seconds.toFixed(2) : "—" }, { label: "Error", render: (r) => h("span", { class: "small" }, r.error || "") },
  ], m.runs.filter((r) => statusSel.value === "all" || r.status === statusSel.value).slice(0, 100), { onclick: (r) => go("#/pipelines/run/" + r.id) })));
  statusSel.addEventListener("change", drawRuns);
  drawRuns();
  const firing = m.alerts.filter((a) => a.state === "firing").length;
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Operate"), h("h1", null, "Monitoring hub"), h("span", { class: "muted small" }, "Last 14 days")),
    h("button", { onclick: () => render() }, "Refresh")),
  h("div", { class: "kpis" }, [[fmt(total), "pipeline runs"], [total ? pct(1 - failed / total) : "—", "run success rate"], [durs.length ? (durs.reduce((a, b) => a + b, 0) / durs.length).toFixed(1) + " s" : "—", "average run time"],
    [String(firing), "alerts firing"], [fmt(m.query_count), "interactive queries"], [m.warehouse_bytes ? (m.warehouse_bytes / 1048576).toFixed(1) + " MB" : "—", "warehouse file size"]]
    .map(([b, sp]) => h("div", { class: "kpi" }, h("b", null, b), h("span", null, sp)))),
  h("div", { class: "grid2" },
    h("div", { class: "card" }, h("h2", null, "Pipelines"), m.pipelines.length ? table([
      { label: "Pipeline", key: "pipeline" }, { label: "Runs", num: true, key: "runs" }, { label: "Failed", num: true, key: "failed" },
      { label: "Success", render: (p) => h("div", { style: { width: "90px" } }, meter(p.success_rate)) }, { label: "Avg s", num: true, render: (p) => p.avg_seconds ?? "—" }], m.pipelines,
    { onclick: (p) => go("#/pipelines/" + p.pipeline_id) }) : h("p", { class: "muted" }, "No runs in this period.")),
    h("div", { class: "card" }, h("h2", null, "Alerts"), m.alerts.length ? table([{ label: "Alert", key: "name" }, { label: "State", render: (a) => alertStateChip(a.state) }, { label: "Checked", render: (a) => ago(a.last_checked) }], m.alerts, { onclick: () => go("#/alerts") })
      : h("p", { class: "muted" }, "No alerts configured."))),
  h("div", { class: "spread" }, h("h2", null, "Runs"), statusSel), runsBox,
  h("div", { class: "grid2" },
    h("div", { class: "card" }, h("h2", null, "Slowest queries"), m.slow_queries.length ? table([{ label: "ms", num: true, render: (q) => fmt(q.ms, 1) }, { label: "User", key: "user" }, { label: "SQL", render: (q) => h("span", { class: "mono small" }, q.sql) }], m.slow_queries) : h("p", { class: "muted" }, "No interactive queries yet.")),
    h("div", { class: "card" }, h("h2", null, "Deliveries"), m.deliveries.length ? table([{ label: "When", render: (x) => ago(x.ts) }, { label: "Kind", key: "kind" }, { label: "Status", render: (x) => chip(x.status, x.status === "sent" || x.status === "ok" ? "ok" : x.status === "firing" || x.status === "failed" ? "bad" : "") }], m.deliveries.slice(0, 20)) : h("p", { class: "muted" }, "No alert or subscription deliveries yet."))),
  m.storage.length ? h("div", { class: "card" }, h("h2", null, "Storage"), table([{ label: "Table", render: (t) => h("span", { class: "mono" }, t.table) }, { label: "Rows (estimate)", num: true, render: (t) => fmt(t.rows) }, { label: "Columns", num: true, key: "columns" }], m.storage)) : null);
});

/* ---------------- notebooks ---------------- */
route("notebooks", async (main, id) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "Notebooks need the analyst role.")); return; }
  if (id) return notebookView(main, +id);
  const list = await GET("/api/notebooks");
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Analyze"), h("h1", null, "Notebooks"), h("span", { class: "muted small" }, "SQL and notes in one document, shared with your team.")),
    h("button", { class: "primary", onclick: async () => { const n = await POST("/api/notebooks", { name: "Untitled notebook" }); go("#/notebooks/" + n.id); } }, "New notebook")),
  list.length ? h("div", { class: "card flat" }, table([{ label: "Notebook", render: (n) => h("b", null, n.name) }, { label: "Cells", num: true, key: "cell_count" }, { label: "Author", key: "created_by" }, { label: "Updated", render: (n) => ago(n.updated_at) }], list,
    { onclick: (n) => go("#/notebooks/" + n.id) })) : h("div", { class: "empty" }, "No notebooks yet."));
});

async function notebookView(main, id) {
  const nb = await GET(`/api/notebooks/${id}`);
  const cells = nb.cells;
  const name = h("input", { id: "nb-name", value: nb.name, class: "titleinput", "aria-label": "Notebook name" });
  const saved = h("span", { class: "small muted" }, "Saved");
  const save = debounce(async () => { saved.textContent = "Saving…"; try { await PUT(`/api/notebooks/${id}`, { name: name.value, cells }); saved.textContent = "Saved"; } catch (e) { saved.textContent = e.message; } }, 700);
  name.addEventListener("input", save);
  const box = h("div", { class: "stack" });
  const results = {};
  const runCell = async (c, out) => {
    out.replaceChildren(h("p", { class: "muted small" }, "Running…"));
    try {
      const r = await POST("/api/sql", { sql: c.source, limit: 5000 });
      results[c.id] = r;
      drawOut(c, out);
    } catch (e) { out.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  const drawOut = (c, out) => {
    const r = results[c.id];
    if (!r) return;
    const vt = h("select", { "aria-label": "Visual" }, ["table", "bar", "line", "area", "donut", "kpi"].map((x) => h("option", { selected: (c.viz?.type || "table") === x }, x)));
    vt.addEventListener("change", () => { c.viz = { type: vt.value }; save(); drawOut(c, out); });
    out.replaceChildren(h("div", { class: "spread" }, h("span", { class: "small muted" }, `${fmt(r.row_count)} rows · ${r.elapsed_ms} ms`), vt), renderViz(c.viz || { type: "table" }, r, { wide: wideScreen() }));
  };
  const draw = () => box.replaceChildren(...cells.map((c, i) => {
    const tools = h("div", { class: "row celltools" },
      i > 0 ? h("button", { class: "sm ghost", "aria-label": "Move up", onclick: () => { [cells[i - 1], cells[i]] = [cells[i], cells[i - 1]]; save(); draw(); } }, "↑") : null,
      h("button", { class: "sm ghost danger", "aria-label": "Delete cell", onclick: () => { cells.splice(i, 1); save(); draw(); } }, "✕"));
    if (c.type === "markdown") {
      const view = h("div", { class: "mdcell", tabindex: "0", title: "Click to edit" }, md(c.source || "*Empty note — click to write.*"));
      const ta = h("textarea", { rows: Math.max(3, (c.source || "").split("\n").length + 1), hidden: true, "aria-label": "Note" });
      ta.value = c.source || "";
      view.addEventListener("click", () => { view.hidden = true; ta.hidden = false; ta.focus(); });
      ta.addEventListener("input", () => { c.source = ta.value; save(); });
      ta.addEventListener("blur", () => { view.replaceChildren(md(c.source)); view.hidden = false; ta.hidden = true; });
      return h("div", { class: "cell md-cell" }, tools, view, ta);
    }
    const ta = h("textarea", { rows: Math.max(3, (c.source || "").split("\n").length + 1), spellcheck: "false", class: "sqlcell", "aria-label": "SQL" });
    ta.value = c.source || "";
    const out = h("div", { class: "cellout" });
    ta.addEventListener("input", () => { c.source = ta.value; save(); });
    ta.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey || e.shiftKey) && e.key === "Enter") { e.preventDefault(); runCell(c, out); } });
    if (results[c.id]) drawOut(c, out);
    return h("div", { class: "cell" }, h("div", { class: "spread" }, h("span", { class: "eyebrow" }, `SQL [${i + 1}]`), h("div", { class: "row" }, h("button", { class: "sm primary", onclick: () => runCell(c, out) }, "Run"), tools)), ta, out);
  }));
  const add = (type) => { cells.push({ id: "c" + Date.now().toString(36), type, source: type === "sql" ? "select * from " : "" }); save(); draw(); };
  const runAll = async (btn) => { btn.disabled = true; try { for (const c of cells) if (c.type === "sql") await runCell(c, { replaceChildren() {} }); draw(); } finally { btn.disabled = false; } };
  draw();
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", { style: { flex: "1 1 300px" } }, h("a", { href: "#/notebooks", class: "small" }, "← Notebooks"), name, saved),
    h("div", { class: "row" }, h("button", { onclick: (e) => runAll(e.target) }, "Run all"), h("button", { onclick: () => add("markdown") }, "+ Note"), h("button", { onclick: () => add("sql") }, "+ SQL"),
      h("button", { class: "danger", onclick: () => confirmBox("Delete this notebook?", "Delete", async () => { await DEL(`/api/notebooks/${id}`); go("#/notebooks"); }) }, "Delete"))),
  h("p", { class: "small muted" }, "Shift+Enter runs a cell. Notebooks save automatically and run with your permissions."), box,
  h("div", { class: "row" }, h("button", { onclick: () => add("sql") }, "+ SQL cell"), h("button", { onclick: () => add("markdown") }, "+ Note")));
}

/* ---------------- visual transforms ---------------- */
route("transforms", async (main) => {
  if (!can("engineer")) { main.replaceChildren(h("div", { class: "notice" }, "Transforms need the engineer role.")); return; }
  const [meta, dsets] = await Promise.all([GET("/api/transforms/ops"), GET("/api/datasets")]);
  const sources = dsets.filter((d) => d.schema_name !== "serving");
  const st = T.transformState || { source: sources[0] ? fqn(sources[0]) : "", steps: [], target: "" };
  T.transformState = st;
  let cols = [];
  const src = h("select", { id: "tr-source" }, sources.map((d) => h("option", { value: fqn(d), selected: fqn(d) === st.source }, fqn(d))));
  const target = h("input", { id: "tr-target", value: st.target, placeholder: "curated.orders_clean" });
  target.addEventListener("input", () => (st.target = target.value));
  const stepsBox = h("div", { class: "stack" });
  const preview = h("div");
  const sqlBox = h("pre", { class: "log" });
  const colSel = (s2, key = "column") => { const x = h("select", { "aria-label": "Column" }, h("option", { value: "" }, "Column…"), cols.map((c) => h("option", { selected: s2[key] === c }, c))); x.addEventListener("change", () => { s2[key] = x.value; refresh(); }); return x; };
  const multi = (s2, key) => { const x = h("input", { value: (s2[key] || []).join(", "), placeholder: "col_a, col_b", "aria-label": "Columns" }); x.addEventListener("change", () => { s2[key] = x.value.split(",").map((v) => v.trim()).filter(Boolean); refresh(); }); return x; };
  const txt = (s2, key, ph) => { const x = h("input", { value: s2[key] ?? "", placeholder: ph || "", "aria-label": key }); x.addEventListener("change", () => { s2[key] = x.value; refresh(); }); return x; };
  const fieldsFor = (s2) => {
    switch (s2.op) {
      case "select": case "drop": return [field("Columns", multi(s2, "columns"))];
      case "rename": return [field("Column", colSel(s2)), field("New name", txt(s2, "to", "region"))];
      case "filter": { const o = h("select", { "aria-label": "Operator" }, meta.filters.map((x) => h("option", { selected: (s2.operator || "=") === x }, x))); o.addEventListener("change", () => { s2.operator = o.value; refresh(); }); return [field("Column", colSel(s2)), field("Operator", o), field("Value", txt(s2, "value", "paid"))]; }
      case "dedupe": return [field("Key columns", multi(s2, "columns")), field("Keep latest by", colSel(s2, "order_by"))];
      case "cast": { const t = h("select", { "aria-label": "Type" }, meta.cast_types.map((x) => h("option", { selected: s2.to === x }, x))); t.addEventListener("change", () => { s2.to = t.value; refresh(); }); s2.to ||= meta.cast_types[0]; return [field("Column", colSel(s2)), field("To", t)]; }
      case "derive": return [field("New column", txt(s2, "name", "is_large")), field("Expression", txt(s2, "expr", "order_total > 200"))];
      case "fill_null": return [field("Column", colSel(s2)), field("Value", txt(s2, "value", "unknown"))];
      case "trim": case "lower": case "upper": return [field("Column", colSel(s2))];
      case "replace_text": return [field("Column", colSel(s2)), field("Find", txt(s2, "find")), field("Replace with", txt(s2, "replace"))];
      case "join": { const t = h("select", { "aria-label": "Table" }, h("option", { value: "" }, "Table…"), dsets.filter((d) => d.schema_name !== "serving").map((d) => h("option", { selected: s2.table === fqn(d) }, fqn(d)))); t.addEventListener("change", () => { s2.table = t.value; refresh(); });
        const how = h("select", { "aria-label": "Join type" }, ["left", "inner"].map((x) => h("option", { selected: (s2.how || "left") === x }, x))); how.addEventListener("change", () => { s2.how = how.value; refresh(); });
        return [field("Table", t), field("Join", how), field("Key here", colSel(s2, "left_key")), field("Key there", txt(s2, "right_key", "customer_id")), field("Columns to add", multi(s2, "columns"))]; }
      case "aggregate": { const aggTxt = h("input", { value: (s2.aggs || []).map((a) => `${a.fn}(${a.column || ""})`).join(", "), placeholder: "sum(order_total), count()", "aria-label": "Summaries" });
        aggTxt.addEventListener("change", () => { s2.aggs = aggTxt.value.split(",").map((x) => x.trim()).filter(Boolean).map((x) => { const m2 = x.match(/^(\w+)\(([^)]*)\)$/); return m2 ? { fn: m2[1], column: m2[2] || undefined } : { fn: "count" }; }); refresh(); });
        return [field("Group by", multi(s2, "group_by")), field("Summaries", aggTxt)]; }
      case "sort": { const d2 = h("input", { type: "checkbox", checked: !!s2.desc }); d2.addEventListener("change", () => { s2.desc = d2.checked; refresh(); }); return [field("Column", colSel(s2)), h("label", { class: "row small" }, d2, "Descending")]; }
      case "limit": return [field("Rows", txt(s2, "n", "1000"))];
      default: return [];
    }
  };
  const drawSteps = () => stepsBox.replaceChildren(...st.steps.map((s2, i) => {
    const opSel = h("select", { "aria-label": "Operation" }, Object.entries(meta.ops).map(([k, l]) => h("option", { value: k, selected: s2.op === k }, l)));
    opSel.addEventListener("change", () => { st.steps[i] = { op: opSel.value }; drawSteps(); refresh(); });
    return h("div", { class: "step" }, h("div", { class: "spread" }, h("div", { class: "row" }, h("span", { class: "eyebrow" }, `Step ${i + 1}`), opSel),
      h("button", { class: "sm danger", onclick: () => { st.steps.splice(i, 1); drawSteps(); refresh(); } }, "Remove")), h("div", { class: "form" }, fieldsFor(s2)));
  }));
  const refresh = debounce(async () => {
    st.source = src.value;
    try {
      const r = await POST("/api/transforms/preview", { source: st.source, steps: st.steps });
      cols = r.result.columns;
      sqlBox.textContent = r.sql;
      preview.replaceChildren(h("p", { class: "small muted" }, `Preview: first ${r.result.row_count} rows · ${cols.length} columns`), dataTable(r.result));
      drawSteps();
    } catch (e) { preview.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  }, 300);
  src.addEventListener("change", refresh);
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Data"), h("h1", null, "Visual transforms"), h("span", { class: "muted small" }, "Clean and shape data step by step. Tessera writes the SQL.")),
    h("div", { class: "row" },
      h("button", { onclick: () => { T.draftPipeline = { name: `Transform into ${st.target || "curated table"}`, description: "Created from a visual transform", steps: [{ id: "transform", type: "transform", sql: sqlBox.textContent, target: st.target || "curated.new_table", retries: 0 }], enabled: 1 }; go("#/pipelines/new"); } }, "Add to a pipeline"),
      h("button", { class: "primary", onclick: async (e) => { const r = await act(e.target, () => POST("/api/transforms/run", { source: src.value, steps: st.steps, target: target.value })); toast(`Wrote ${fmt(r.row_count)} rows to ${r.dataset}`); } }, "Run into table"))),
  h("div", { class: "card" }, h("div", { class: "form" }, field("Start from", src), field("Write to", target, "raw.* or curated.*"))),
  stepsBox, h("div", null, h("button", { onclick: () => { st.steps.push({ op: "filter", operator: "=" }); drawSteps(); } }, "+ Add step")),
  h("div", { class: "card" }, preview), h("details", { class: "card" }, h("summary", null, "Generated SQL"), sqlBox));
  drawSteps();
  refresh();
});

/* ---------------- glossary ---------------- */
route("glossary", async (main) => {
  const terms = await GET("/api/glossary");
  const q = h("input", { type: "search", id: "gl-q", placeholder: "Filter terms", style: { maxWidth: "280px" }, "aria-label": "Filter terms" });
  const box = h("div", { class: "grid2" });
  const draw = () => {
    const f = q.value.toLowerCase();
    const rows = terms.filter((t) => !f || (t.term + t.definition + (t.synonyms || []).join(" ")).toLowerCase().includes(f));
    box.replaceChildren(...(rows.length ? rows.map((t) => h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", null, t.term), t.domain ? chip(t.domain, "info") : null),
      h("p", null, t.definition),
      (t.synonyms || []).length ? h("p", { class: "small muted" }, "Also called: " + t.synonyms.join(", ")) : null,
      (t.columns || []).length ? h("div", { class: "row" }, t.columns.map((c) => h("span", { class: "schema" }, c))) : null,
      h("div", { class: "spread small muted" }, h("span", null, t.owner ? "Owner: " + t.owner : ""), can("engineer") ? h("div", { class: "row" }, h("button", { class: "sm", onclick: () => termDialog(t) }, "Edit"),
        h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/glossary/${t.id}`), "Removed").then(render) }, "Remove")) : null)))
      : [h("div", { class: "empty" }, terms.length ? "No term matches." : "No business terms yet.")]));
  };
  q.addEventListener("input", draw);
  draw();
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Data"), h("h1", null, "Business glossary"), h("span", { class: "muted small" }, "Shared definitions, linked to the columns that implement them.")),
    h("div", { class: "row" }, q, can("engineer") ? h("button", { class: "primary", onclick: () => termDialog() }, "Add term") : null)), box);
});

function termDialog(t = {}) {
  const term = h("input", { id: "gt-term", value: t.term || "" }), def = h("textarea", { id: "gt-def", rows: 4, style: { fontFamily: "var(--f-body)" } });
  def.value = t.definition || "";
  const syn = h("input", { id: "gt-syn", value: (t.synonyms || []).join(", "), placeholder: "sales, GMV" }), dom = h("input", { id: "gt-dom", value: t.domain || "", placeholder: "Sales" });
  const owner = h("input", { id: "gt-owner", value: t.owner || T.me.email }), cols = h("input", { id: "gt-cols", value: (t.columns || []).join(", "), placeholder: "curated.orders_enriched.order_total" });
  dialog(t.id ? "Edit term" : "Add term", h("div", { class: "stack" }, h("div", { class: "form" }, field("Term", term), field("Domain", dom), field("Owner", owner)), field("Definition", def),
    field("Synonyms", syn), field("Linked columns (schema.table.column, comma separated)", cols)),
  [{ label: "Save", run: async () => {
    const body = { term: term.value, definition: def.value, synonyms: syn.value, domain: dom.value || null, owner: owner.value, columns: cols.value.split(",").map((x) => x.trim()).filter(Boolean) };
    if (t.id) await PUT(`/api/glossary/${t.id}`, body); else await POST("/api/glossary", body);
    toast("Saved"); render();
  } }]);
}

/* ---------------- admin extras ---------------- */
async function adminSecurity(el) {
  const [r, dsets] = await Promise.all([GET("/api/rls"), GET("/api/datasets")]);
  const serving = dsets.filter((d) => d.schema_name === "serving");
  const ds = h("select", { id: "rls-ds" }, serving.map((d) => h("option", { value: d.name }, fqn(d))));
  const col = h("select", { id: "rls-col" });
  const syncCols = async () => { const d = serving.find((x) => x.name === ds.value); if (!d) return; const full = await GET(`/api/datasets/${d.id}`); col.replaceChildren(...full.columns.map((c) => h("option", null, c.name))); };
  ds.addEventListener("change", syncCols); syncCols();
  const attr = h("input", { id: "rls-attr", placeholder: "region" });
  const attrsUsed = [...new Set(r.policies.map((p) => p.attribute))];
  el.replaceChildren(
    h("div", { class: "notice" }, "Row-level security filters published datasets for viewers and analysts. Each policy says: a row is visible when its column value is in the user's attribute list. Users without the attribute see no rows; the value * means all rows. Engineers and admins are not filtered."),
    h("div", { class: "card" }, h("h3", null, "Policies"), r.policies.length ? table([
      { label: "Dataset", render: (p) => h("span", { class: "mono" }, "serving." + p.dataset) }, { label: "Column", render: (p) => h("span", { class: "mono" }, p.column_name) },
      { label: "User attribute", render: (p) => chip(p.attribute, "info") }, { label: "", render: (p) => h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/rls/${p.id}`), "Removed").then(render) }, "Remove") }], r.policies)
      : h("p", { class: "muted small" }, "No policies. Everyone with access to a published dataset sees all of its rows."),
    h("div", { class: "row", style: { alignItems: "end" } }, field("Dataset", ds), field("Column", col), field("User attribute", attr), h("button", { class: "primary", onclick: (e) => act(e.target, () => POST("/api/rls", { dataset: ds.value, column_name: col.value, attribute: attr.value }), "Policy added").then(render) }, "Add policy"))),
    h("div", { class: "card" }, h("h3", null, "User attributes"), table([
      { label: "User", render: (u) => h("div", null, h("b", null, u.email), h("div", { class: "small muted" }, u.role)) },
      { label: "Attributes", render: (u) => {
        const i = h("input", { value: Object.entries(u.attributes || {}).map(([k, v]) => `${k}=${(v || []).join("|")}`).join("; "), placeholder: attrsUsed.length ? `${attrsUsed[0]}=BC|AB` : "region=BC|AB", "aria-label": "Attributes for " + u.email });
        i.addEventListener("change", () => {
          const attrs = {};
          i.value.split(";").map((x) => x.trim()).filter(Boolean).forEach((pair) => { const [k, v] = pair.split("="); if (k) attrs[k.trim()] = (v || "").split("|").map((x) => x.trim()).filter(Boolean); });
          act(null, () => PUT(`/api/users/${u.id}/attributes`, { attributes: attrs }), "Attributes saved");
        });
        return i;
      } },
    ], r.users), h("p", { class: "small muted" }, "Format: name=value1|value2; other=value. Example: region=BC|AB")));
}

async function adminEmail(el) {
  const c = await GET("/api/admin/email");
  const v = (k, ph, type) => h("input", { id: "em-" + k, value: c[k] ?? "", placeholder: ph || "", type: type || "text" });
  const f = { host: v("host", "smtp.office365.com"), port: v("port", "587", "number"), username: v("username"), from: v("from", "tessera@company.com") };
  const pw = h("input", { id: "em-pw", type: "password", placeholder: c.password_set ? "•••••••• (stored)" : "", autocomplete: "new-password" });
  const tls = h("input", { type: "checkbox", id: "em-tls", checked: c.starttls !== false });
  const ssl = h("input", { type: "checkbox", id: "em-ssl", checked: !!c.ssl });
  const to = h("input", { id: "em-to", value: T.me.email });
  el.replaceChildren(h("div", { class: "card" }, h("p", { class: "small muted" }, "Used for dashboard subscriptions and alert emails. Microsoft 365, Gmail, SendGrid, Amazon SES and any SMTP server work."),
    h("div", { class: "form" }, field("SMTP host", f.host), field("Port", f.port), field("User", f.username), field("Password (encrypted)", pw), field("From address", f.from)),
    h("div", { class: "row" }, h("label", { class: "row small" }, tls, "STARTTLS"), h("label", { class: "row small" }, ssl, "Implicit TLS (port 465)")),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: (e) => act(e.target, () => PUT("/api/admin/email", { host: f.host.value, port: +f.port.value || null, username: f.username.value, from: f.from.value, password: pw.value || undefined, starttls: tls.checked, ssl: ssl.checked }), "Saved") }, "Save"),
      to, h("button", { onclick: (e) => act(e.target, () => POST("/api/admin/email/test", { to: to.value }), "Test email sent") }, "Send test"))));
}

async function adminDeploy(el) {
  const file = h("input", { type: "file", id: "bundle-file", accept: ".json,application/json" });
  const mode = h("select", { id: "bundle-mode" }, h("option", { value: "skip" }, "Keep existing items with the same name"), h("option", { value: "overwrite" }, "Overwrite items with the same name"));
  const out = h("div");
  el.replaceChildren(
    h("div", { class: "card" }, h("h3", null, "Export this workspace"), h("p", { class: "small muted" }, "Downloads connections (without secrets), pipelines, quality rules, catalog descriptions, semantic models, reports, dashboards, glossary and row-level security policies as one JSON file. Commit it to Git or import it into another Tessera (dev → test → prod)."),
      h("div", null, h("button", { class: "primary", onclick: (e) => act(e.target, () => downloadPost("/api/bundle/export", undefined, "tessera-bundle.json")) }, "Download bundle"))),
    h("div", { class: "card" }, h("h3", null, "Import a bundle"), h("div", { class: "form" }, field("Bundle file", file), field("When names match", mode)),
      h("div", null, h("button", { onclick: async (e) => {
        if (!file.files[0]) { toast("Choose a bundle file", "bad"); return; }
        let bundle;
        try { bundle = JSON.parse(await file.files[0].text()); } catch { toast("That file is not JSON", "bad"); return; }
        const r = await act(e.target, () => POST("/api/bundle/import", { bundle, mode: mode.value }), "Imported");
        out.replaceChildren(table([{ label: "Item", key: "k" }, { label: "Created", num: true, key: "created" }, { label: "Updated", num: true, key: "updated" }, { label: "Skipped", num: true, key: "skipped" }],
          Object.entries(r.report).map(([k, v]) => ({ k, ...v }))), r.warnings.length ? h("div", { class: "notice warn" }, h("ul", null, r.warnings.map((w) => h("li", null, w)))) : null);
      } }, "Import")), out));
}
