"use strict";
/* Views: pipelines, SQL, dashboards, agents, admin. */

/* ---------------- pipelines ---------------- */
route("pipelines", async (main, id, sub) => {
  if (id === "run") return runView(main, +sub);
  if (id === "new" || sub === "edit") return pipelineEditor(main, id === "new" ? null : +id);
  if (id) return pipelineView(main, +id);
  const list = await GET("/api/pipelines");
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Orchestration"), h("h1", null, "Pipelines")),
    can("engineer") ? h("button", { class: "primary", onclick: () => go("#/pipelines/new") }, "New pipeline") : null),
    list.length ? h("div", { class: "card flat" }, table([
      { label: "Pipeline", render: (p) => h("div", null, h("b", null, p.name), h("div", { class: "small muted" }, p.description || "")) },
      { label: "Steps", num: true, render: (p) => p.steps.length },
      { label: "Schedule", render: (p) => p.schedule ? h("span", { class: "mono small" }, p.schedule + (p.enabled ? "" : " (paused)")) : chip("manual") },
      { label: "Next run", render: (p) => p.next_run_at && p.enabled ? new Date(p.next_run_at).toLocaleString() : "—" },
      { label: "Last run", render: (p) => p.last_run ? h("div", { class: "row" }, statusChip(p.last_run.status), h("span", { class: "small muted" }, ago(p.last_run.created_at))) : chip("never") },
    ], list, { onclick: (p) => go("#/pipelines/" + p.id) })) : h("div", { class: "empty" }, "No pipelines yet. A pipeline chains ingest, transform, quality gates, PII scans, publishing, exports and webhooks."),
    h("div", { class: "card" }, h("h3", null, "Triggering from Airflow, Dagster, Prefect, ADF or CI"),
      h("p", { class: "small muted" }, "Create an API token with the engineer role under Admin, then call the run endpoint. Runs report back through the pipeline's completion webhook."),
      h("pre", { class: "log" }, `curl -X POST ${location.origin}/api/pipelines/<id>/run \\\n  -H "Authorization: Bearer tsr_…"`)));
});

async function pipelineView(main, id) {
  const [p, runs] = await Promise.all([GET("/api/pipelines").then((l) => l.find((x) => x.id === id)), GET(`/api/pipelines/${id}/runs`)]);
  if (!p) throw new Error("Pipeline not found");
  const last = runs[0] ? await GET(`/api/runs/${runs[0].id}`) : null;
  main.replaceChildren(
    h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/pipelines", class: "small" }, "← Pipelines"), h("h1", null, p.name), h("span", { class: "muted small" }, p.description || "")),
      can("engineer") ? h("div", { class: "row" },
        h("button", { onclick: () => go(`#/pipelines/${id}/edit`) }, "Edit"),
        h("button", { class: "danger", onclick: () => confirmBox(`Delete pipeline ${p.name} and its run history?`, "Delete", async () => { await DEL(`/api/pipelines/${id}`); go("#/pipelines"); }) }, "Delete"),
        h("button", { class: "primary", onclick: async (e) => { const r = await act(e.target, () => POST(`/api/pipelines/${id}/run`)); go("#/pipelines/run/" + r.run_id); } }, "Run now")) : null),
    h("div", { class: "kpis" }, [[p.schedule || "manual", p.schedule ? "cron, evaluated in the server's time zone" : "runs when triggered"],
      [p.next_run_at && p.enabled ? new Date(p.next_run_at).toLocaleString() : "—", "next scheduled run, your local time"],
      [runs.length ? pct(runs.filter((r) => r.status === "success").length / runs.length) : "—", `success rate, last ${runs.length} runs`]].map(([b, sp]) => h("div", { class: "kpi" }, h("b", { style: { fontSize: "1.05rem" } }, b), h("span", null, sp)))),
    h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", null, "Steps"), last ? h("span", { class: "small muted" }, "Colored by the last run") : null), dagView(p.steps, last?.steps)),
    h("div", { class: "card flat" }, runs.length ? table([
      { label: "Run", render: (r) => "#" + r.id }, { label: "Status", render: (r) => statusChip(r.status) }, { label: "Trigger", render: (r) => `${r.trigger} · ${r.triggered_by || ""}` },
      { label: "Started", render: (r) => r.started_at ? new Date(r.started_at).toLocaleString() : "—" },
      { label: "Duration", num: true, render: (r) => r.finished_at && r.started_at ? ((new Date(r.finished_at) - new Date(r.started_at)) / 1000).toFixed(1) + " s" : "—" },
      { label: "Error", render: (r) => h("span", { class: "small" }, r.error || "") },
    ], runs, { onclick: (r) => go("#/pipelines/run/" + r.id) }) : h("p", { class: "muted", style: { padding: "16px" } }, "No runs yet.")));
}

async function runView(main, rid) {
  const draw = async () => {
    const r = await GET(`/api/runs/${rid}`);
    const p = (await GET("/api/pipelines")).find((x) => x.id === r.pipeline_id);
    main.replaceChildren(
      h("div", { class: "pagehead" }, h("div", null, h("a", { href: `#/pipelines/${r.pipeline_id}`, class: "small" }, "← " + (p?.name || "Pipeline")),
        h("div", { class: "row" }, h("h1", null, `Run #${r.id}`), statusChip(r.status)), h("span", { class: "muted small" }, `${r.trigger} by ${r.triggered_by || "—"} · ${r.started_at ? new Date(r.started_at).toLocaleString() : "queued"}`)),
        can("engineer") && ["queued", "running"].includes(r.status) ? h("button", { class: "danger", onclick: (e) => act(e.target, () => POST(`/api/runs/${rid}/cancel`), "Cancel requested") }, "Cancel") : null),
      r.error ? h("div", { class: "notice bad" }, r.error) : null,
      p ? h("div", { class: "card" }, dagView(p.steps, r.steps)) : null,
      h("div", { class: "card flat" }, table([
        { label: "Step", render: ([k]) => h("b", { class: "mono" }, k) }, { label: "Status", render: ([, v]) => statusChip(v.status) },
        { label: "Attempt", num: true, render: ([, v]) => v.attempt ?? "—" }, { label: "Seconds", num: true, render: ([, v]) => v.seconds ?? "—" },
        { label: "Result", render: ([, v]) => h("span", { class: "small mono" }, v.error || v.reason || (v.output ? JSON.stringify(v.output).slice(0, 220) : "")) },
      ], Object.entries(r.steps || {}))),
      h("div", { class: "card" }, h("h3", null, "Log"), h("div", { class: "log" }, (r.log || []).map((l) => h("div", { class: l.level }, `${l.ts.slice(11, 19)}  ${(l.step || "run").padEnd(16)} ${l.msg}`)))));
    return r.status;
  };
  const st = await draw();
  if (["queued", "running"].includes(st)) {
    const t = setInterval(async () => { const s2 = await draw().catch(() => "x"); if (!["queued", "running"].includes(s2)) clearInterval(t); }, 1200);
    T.timers.push(t);
  }
}

async function pipelineEditor(main, id) {
  if (!can("engineer")) throw new Error("Only engineers can edit pipelines.");
  const [meta, conns, dsets, pls] = await Promise.all([GET("/api/pipelines/meta"), GET("/api/connections"), GET("/api/datasets"), GET("/api/pipelines")]);
  const existing = id ? pls.find((p) => p.id === id) : (T.draftPipeline || null);
  T.draftPipeline = null;
  let steps = existing ? JSON.parse(JSON.stringify(existing.steps)) : [];
  const name = h("input", { id: "pl-name", value: existing?.name || "", placeholder: "Orders nightly" });
  const desc = h("input", { id: "pl-desc", value: existing?.description || "", placeholder: "What this pipeline produces and for whom" });
  const sched = h("input", { id: "pl-sched", value: existing?.schedule || "", placeholder: "0 6 * * *  (empty = manual)" });
  const hook = h("input", { id: "pl-hook", value: existing?.webhook_url || "", placeholder: "https://hooks.example.com/tessera" });
  const enabled = h("input", { type: "checkbox", id: "pl-enabled", checked: existing ? !!existing.enabled : true });
  const list = h("div", { class: "stack" });
  const dsOptions = dsets.map((d) => fqn(d));
  const dl = h("datalist", { id: "ds-list" }, dsOptions.map((x) => h("option", { value: x })));
  const json = h("textarea", { id: "pl-json", rows: 16, hidden: true });
  const preview = h("div");

  const inp = (st, key, attrs = {}) => {
    const i = h(attrs.tag || "input", { ...attrs, tag: null, value: st[key] ?? "" });
    i.addEventListener("change", () => { st[key] = attrs.type === "number" ? Number(i.value) : i.value; drawPreview(); });
    return i;
  };
  const drawPreview = () => preview.replaceChildren(steps.length ? dagView(steps, null) : h("p", { class: "muted small" }, "Add steps to see the graph."));
  const draw = () => {
    list.replaceChildren(...steps.map((st, idx) => {
      const others = steps.filter((x) => x !== st).map((x) => x.id);
      const typeSel = h("select", { "aria-label": "Step type" }, Object.entries(meta.step_types).map(([k, v]) => h("option", { value: k, selected: st.type === k }, v.label)));
      typeSel.addEventListener("change", () => { st.type = typeSel.value; draw(); });
      const deps = h("div", { class: "row" }, others.length ? others.map((o) => { const cb = h("input", { type: "checkbox", checked: (st.depends_on || []).includes(o) }); cb.addEventListener("change", () => { st.depends_on = cb.checked ? [...(st.depends_on || []), o] : (st.depends_on || []).filter((x) => x !== o); drawPreview(); }); return h("label", { class: "row small" }, cb, o); }) : h("span", { class: "small muted" }, "none"));
      let body = [];
      if (st.type === "ingest") {
        const cs = h("select", null, h("option", { value: "" }, "Choose…"), conns.map((c) => h("option", { value: c.id, selected: +st.connection_id === c.id }, c.name)));
        cs.addEventListener("change", () => (st.connection_id = +cs.value));
        const srcTa = h("textarea", { rows: 3 });
        srcTa.value = JSON.stringify(st.source || { path: "orders.csv" }, null, 1);
        srcTa.addEventListener("change", () => { try { st.source = JSON.parse(srcTa.value); srcTa.setCustomValidity(""); } catch { srcTa.setCustomValidity("Invalid JSON"); toast("Source must be valid JSON", "bad"); } });
        const mode = h("select", null, ["replace", "append", "incremental", "merge"].map((m) => h("option", { selected: (st.mode || "replace") === m }, m)));
        mode.addEventListener("change", () => (st.mode = mode.value));
        body = [h("div", { class: "form" }, field("Connection", cs), field("Target table", inp(st, "target", { placeholder: "raw.orders", list: "ds-list" })), field("Mode", mode)),
          field("Source (JSON)", srcTa, 'Files/S3: {"path": "orders*.csv"} · Databases: {"table": "public.orders", "cursor_column": "updated_at", "key": ["id"]} or {"query": "select …"} · REST: {"path": "/orders", "records_path": "data", "pagination": {"type": "page"}}')];
      } else if (st.type === "transform") {
        body = [field("Target table", inp(st, "target", { placeholder: "curated.orders_clean", list: "ds-list" })), field("SELECT", inp(st, "sql", { tag: "textarea", rows: 4, placeholder: "select … from raw.orders" }))];
      } else if (["quality", "pii_scan", "publish", "export"].includes(st.type)) {
        body = [h("div", { class: "form" }, field("Dataset", inp(st, "dataset", { placeholder: "curated.orders_clean", list: "ds-list" })),
          st.type === "quality" ? field("Fail the run on", (() => { const s = h("select", null, [["error", "error rules"], ["warn", "any failing rule"], ["never", "never (report only)"]].map(([v, l]) => h("option", { value: v, selected: (st.fail_on || "error") === v }, l))); s.addEventListener("change", () => (st.fail_on = s.value)); return s; })()) : null,
          st.type === "export" ? field("Format", (() => { const s = h("select", null, ["parquet", "csv"].map((v) => h("option", { selected: (st.format || "parquet") === v }, v))); s.addEventListener("change", () => (st.format = s.value)); return s; })()) : null)];
      } else if (st.type === "deliver") {
        const dests = conns.filter((c) => !["rest", "mongodb", "elasticsearch", "iceberg", "sftp", "kafka", "eventhubs", "kinesis", "mqtt"].includes(c.type));
        const cs = h("select", null, h("option", { value: "" }, "Choose…"), dests.map((c) => h("option", { value: c.id, selected: +st.connection_id === c.id }, `${c.name} (${c.type})`)));
        cs.addEventListener("change", () => (st.connection_id = +cs.value));
        const mode = h("select", null, ["replace", "append"].map((m) => h("option", { selected: (st.mode || "replace") === m }, m)));
        mode.addEventListener("change", () => (st.mode = mode.value));
        const fmtSel = h("select", null, ["parquet", "csv"].map((m) => h("option", { selected: (st.format || "parquet") === m }, m)));
        fmtSel.addEventListener("change", () => (st.format = fmtSel.value));
        body = [h("div", { class: "form" }, field("Published dataset", inp(st, "dataset", { placeholder: "serving.orders", list: "ds-list" })), field("Destination", cs),
          field("Table or path", inp(st, "table", { placeholder: "analytics.orders · gold/orders · exports/orders" })), field("Mode", mode), field("File format (S3 and Files)", fmtSel)),
          h("p", { class: "small muted" }, "Only published data can leave Tessera, so masking always applies. Databases and warehouses get a table; Delta connections get a Delta table (readable from Fabric/OneLake shortcuts, Databricks and Spark); S3 and Files get Parquet or CSV.")];
      } else if (st.type === "webhook") {
        body = [field("URL (POST with JSON)", inp(st, "url", { placeholder: "https://…" }))];
      } else if (st.type === "run_pipeline") {
        const ps = h("select", null, h("option", { value: "" }, "Choose…"), pls.filter((p) => p.id !== id).map((p) => h("option", { value: p.id, selected: +st.pipeline_id === p.id }, p.name)));
        ps.addEventListener("change", () => (st.pipeline_id = +ps.value));
        body = [field("Pipeline to trigger", ps)];
      }
      return h("div", { class: "step" },
        h("div", { class: "spread" }, h("div", { class: "row" }, h("span", { class: "eyebrow" }, `Step ${idx + 1}`), typeSel),
          h("div", { class: "row" }, idx > 0 ? h("button", { class: "sm ghost", "aria-label": "Move up", onclick: () => { [steps[idx - 1], steps[idx]] = [steps[idx], steps[idx - 1]]; draw(); } }, "↑") : null,
            h("button", { class: "sm danger", onclick: () => { steps = steps.filter((x) => x !== st); steps.forEach((x) => (x.depends_on = (x.depends_on || []).filter((d) => d !== st.id))); draw(); } }, "Remove"))),
        h("div", { class: "form" }, field("Step id", inp(st, "id", { placeholder: "load_orders" })), field("Retries (0–5)", inp(st, "retries", { type: "number", min: 0, max: 5 }))),
        ...body, field("Runs after", deps));
    }));
    drawPreview();
  };
  const addStep = () => {
    const type = steps.length === 0 ? "ingest" : "transform";
    const prev = steps[steps.length - 1];
    steps.push({ id: `step_${steps.length + 1}`, type, depends_on: prev ? [prev.id] : [], retries: 0 });
    draw();
  };
  const toggleJson = () => {
    if (json.hidden) { json.value = JSON.stringify(steps, null, 2); json.hidden = false; list.hidden = true; }
    else { try { steps = JSON.parse(json.value); json.hidden = true; list.hidden = false; draw(); } catch { toast("Steps JSON is not valid", "bad"); } }
  };
  const save = async (e) => {
    if (!json.hidden) steps = JSON.parse(json.value);
    const body = { name: name.value.trim(), description: desc.value, schedule: sched.value.trim() || null, webhook_url: hook.value.trim() || null, enabled: enabled.checked, steps };
    const p = await act(e.target, () => (id ? PUT(`/api/pipelines/${id}`, body) : POST("/api/pipelines", body)), "Pipeline saved");
    go("#/pipelines/" + p.id);
  };
  draw();
  main.replaceChildren(dl,
    h("div", { class: "pagehead" }, h("div", null, h("a", { href: id ? `#/pipelines/${id}` : "#/pipelines", class: "small" }, "← Back"), h("h1", null, id ? "Edit pipeline" : "New pipeline")),
      h("div", { class: "row" }, h("button", { onclick: toggleJson }, "Edit as JSON"), h("button", { class: "primary", onclick: save }, "Save pipeline"))),
    h("div", { class: "card" }, h("div", { class: "form" }, field("Name", name), field("Description", desc), field("Schedule (cron)", sched, "Minute hour day month weekday, in TESSERA_TIMEZONE (UTC by default)"), field("Completion webhook", hook)),
      h("label", { class: "row small" }, enabled, "Schedule enabled")),
    h("div", { class: "card" }, h("h3", null, "Graph"), preview),
    list, json, h("div", null, h("button", { onclick: addStep }, "+ Add step")));
}

/* ---------------- SQL ---------------- */
route("sql", async (main) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "SQL needs the analyst role.")); return; }
  const dsets = await GET("/api/datasets");
  const ed = h("textarea", { id: "sql-editor", spellcheck: "false", "aria-label": "SQL query" });
  ed.value = sessionStorage.getItem("tessera.sql") || (dsets[0] ? `select *\nfrom ${fqn(dsets[0])}\nlimit 100` : "select 42 as answer");
  const out = h("div", { class: "stack" });
  const meta = h("span", { class: "small muted" });
  let last = null;
  const vtype = h("select", { id: "viz-type", "aria-label": "Chart type" }, ["table", "bar", "line", "area", "kpi", "donut", "treemap", "waterfall", "funnel", "combo", "scatter"].map((t) => h("option", null, t)));
  const vx = h("select", { id: "viz-x", "aria-label": "Category column" }), vy = h("select", { id: "viz-y", "aria-label": "Value column" });
  const chartBox = h("div");
  const drawChart = () => { if (last) chartBox.replaceChildren(renderViz({ type: vtype.value, x: vx.value, y: vy.value }, last, wideScreen())); };
  [vtype, vx, vy].forEach((x) => x.addEventListener("change", drawChart));
  const run = async (btn) => {
    try { sessionStorage.setItem("tessera.sql", ed.value); } catch {}
    const r = await act(btn, () => POST("/api/sql", { sql: ed.value }));
    last = r;
    meta.textContent = `${fmt(r.row_count)} rows${r.truncated ? " (truncated)" : ""} · ${r.elapsed_ms} ms`;
    vx.replaceChildren(...r.columns.map((c) => h("option", null, c)));
    vy.replaceChildren(...r.columns.map((c) => h("option", null, c)));
    if (r.columns[1]) vy.value = r.columns[1];
    out.replaceChildren(h("div", { class: "card" }, h("div", { class: "spread" }, h("div", { class: "row" }, h("b", null, "Visualize"), vtype, vx, vy),
      h("button", { onclick: saveQuery }, "Save as chart")), chartBox));
    drawChart();
  };
  const saveQuery = () => {
    const n = h("input", { id: "q-name", placeholder: "Revenue by province" });
    dialog("Save query", field("Name", n), [{ label: "Save", run: async () => { await POST("/api/queries", { name: n.value.trim() || "Untitled", sql: ed.value, viz: { type: vtype.value, x: vx.value, y: vy.value } }); toast("Saved. Add it to a dashboard from Dashboards."); } }]);
  };
  ed.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(null); } });
  const tree = h("div", { class: "schema-tree card" }, h("span", { class: "eyebrow" }, "Datasets"), dsets.map((d) => {
    const det = h("details", null, h("summary", null, fqn(d)));
    det.addEventListener("toggle", async () => {
      if (!det.open || det.dataset.loaded) return;
      det.dataset.loaded = "1";
      const full = await GET(`/api/datasets/${d.id}`);
      det.append(...full.columns.map((c) => h("div", { class: "col", title: "Insert column name", onclick: () => { ed.setRangeText(c.name, ed.selectionStart, ed.selectionEnd, "end"); ed.focus(); } }, h("span", null, c.name), h("span", null, c.type.toLowerCase().slice(0, 9)))));
    });
    return det;
  }));
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Analytics"), h("h1", null, "SQL")),
    h("span", { class: "small muted" }, can("engineer") ? "DuckDB SQL over raw, curated and serving. Read-only." : "DuckDB SQL over published datasets. Read-only.")),
  h("div", { class: "sqlgrid" }, tree, h("div", { class: "stack", style: { minWidth: 0 } }, ed,
    h("div", { class: "spread" }, h("div", { class: "row" }, h("button", { class: "primary", onclick: (e) => run(e.target) }, "Run  (Ctrl+Enter)"), meta)), out)));
  setTimeout(() => ed.focus(), 0);
});

/* ---------------- agents ---------------- */
route("agents", async (main) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "AI agents need the analyst role.")); return; }
  const [list, prov, sessions] = await Promise.all([GET("/api/agents"), GET("/api/ai/providers"), GET("/api/agents/sessions")]);
  let agent = sessionStorage.getItem("tessera.agent") || "analyst", sid = null;
  const chatBox = h("div", { class: "chat", "aria-live": "polite" });
  const cards = h("div", { class: "agents" });
  const drawCards = () => cards.replaceChildren(...list.map((a) => h("button", { class: "agentcard", "aria-pressed": String(a.key === agent), onclick: () => { agent = a.key; try { sessionStorage.setItem("tessera.agent", agent); } catch {} sid = null; chatBox.replaceChildren(); drawCards(); } },
    h("b", null, a.label), h("span", { class: "small muted" }, a.goal), h("span", { class: "small mono muted" }, a.tools.join(" · ")))));
  drawCards();
  const msg = h("textarea", { id: "agent-msg", rows: 2, placeholder: "Ask about your data, e.g. Which province had the highest revenue last quarter?", style: { fontFamily: "var(--f-body)" } });
  const provSel = h("select", { id: "agent-prov", "aria-label": "Model provider" }, prov.providers.map((p) => h("option", { value: p.id, selected: !!p.is_default }, `${p.name} · ${p.model}`)));
  const bubble = (role, text, extra) => h("div", { class: "msg " + role }, h("div", { class: "text" }, text), extra);
  const artifactView = (a) => {
    if (a.type === "chart") return h("div", { class: "card" }, h("div", { class: "spread" }, h("b", null, a.title),
      h("button", { class: "sm", onclick: (e) => act(e.target, () => POST("/api/queries", { name: a.title, sql: a.sql, viz: a.viz }), "Saved as a query. Add it to a dashboard.") }, "Save chart")), renderViz(a.viz, a.preview), h("pre", { class: "log" }, a.sql));
    if (a.type === "model") return h("div", { class: "card" }, h("div", { class: "spread" }, h("b", null, "Semantic model draft: " + (a.name || a.model.name)),
      can("engineer") ? h("button", { class: "sm primary", onclick: (e) => act(e.target, () => POST("/api/models", { ...a.model, name: a.name || a.model.name }), "Model saved").then((m) => go("#/models/" + m.id)) }, "Save model") : null),
      h("p", { class: "small" }, h("b", null, "Measures: "), a.model.measures.map((m) => m.label).join(", ")), h("p", { class: "small" }, h("b", null, "Dimensions: "), a.model.dimensions.map((m) => m.label).join(", ")));
    if (a.type === "pipeline") return h("div", { class: "card" }, h("div", { class: "spread" }, h("b", null, "Pipeline draft: " + a.name),
      h("button", { class: "sm primary", onclick: () => { T.draftPipeline = { name: a.name, description: a.description, schedule: a.schedule, steps: a.steps, enabled: 1 }; go("#/pipelines/new"); } }, "Open in editor")), dagView(a.steps, null));
    return null;
  };
  const send = async (btn) => {
    const text = msg.value.trim();
    if (!text) return;
    msg.value = "";
    chatBox.append(bubble("user", text));
    const wait = bubble("bot", "Working…");
    chatBox.append(wait);
    chatBox.scrollTop = chatBox.scrollHeight;
    try {
      const r = await act(btn, () => POST("/api/agents/chat", { agent, message: text, session_id: sid, provider_id: +provSel.value || null }));
      sid = r.session_id;
      wait.replaceWith(bubble("bot", r.reply || "(no answer)", [groundingBadge(r.grounding),
        r.trace.length ? h("details", { class: "trace" }, h("summary", null, `${r.trace.length} tool call${r.trace.length > 1 ? "s" : ""} · ${fmt(r.tokens_in + r.tokens_out)} tokens`),
          r.trace.map((t) => h("div", null, h("b", null, (t.ok ? "✓ " : "✕ ") + t.tool), h("pre", null, JSON.stringify(t.input, null, 1)), h("pre", null, t.output_preview)))) : null,
        ...r.artifacts.map(artifactView)]));
    } catch (e) { wait.replaceWith(bubble("bot", "Error: " + e.message)); }
    chatBox.scrollTop = chatBox.scrollHeight;
  };
  msg.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(null); } });
  const history = sessions.length ? h("details", { class: "card" }, h("summary", null, `Recent conversations (${sessions.length})`),
    table([{ label: "Started with", key: "title" }, { label: "Agent", key: "agent" }, { label: "Tokens", num: true, render: (s) => fmt(s.tokens_in + s.tokens_out) }, { label: "When", render: (s) => ago(s.updated_at) }], sessions,
      { onclick: async (s) => { const full = await GET(`/api/agents/sessions/${s.id}`); sid = s.id; agent = s.agent; drawCards(); chatBox.replaceChildren(...full.messages.filter((m) => m.role !== "tool" && m.content).map((m) => bubble(m.role === "user" ? "user" : "bot", m.content))); } })) : null;
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "AI agents"), h("h1", null, "Ask your data")),
    prov.providers.length ? h("label", { class: "row small muted" }, "Model", provSel) : null),
  prov.providers.length ? null : h("div", { class: "notice warn" }, can("admin") ? h("span", null, "No model provider yet. ", h("a", { href: "#/admin/ai" }, "Add one under Admin"), " — Ollama runs fully on this machine; Anthropic and OpenAI need an API key.") : "No model provider is configured. Ask an admin to add one."),
  h("div", { class: "notice" }, prov.allow_raw_pii ? "Admins allowed raw personal data to reach the model. Detected personal data is NOT redacted." : "Agents work with your permissions. Personal data is redacted before anything is sent to the model, and every tool call is audited."),
  cards, h("div", { class: "card" }, chatBox, h("div", { class: "row", style: { alignItems: "end" } }, h("div", { style: { flex: "1 1 300px" } }, msg), h("button", { class: "primary", onclick: (e) => send(e.target) }, "Send"))), history);
});

/* ---------------- admin ---------------- */
route("admin", async (main, tab) => {
  tab = tab || (can("admin") ? "users" : "tokens");
  const tabs = can("admin") ? [["users", "Users"], ["security", "Row-level security"], ["tokens", "API tokens"], ["sso", "Single sign-on"], ["email", "Email"], ["ai", "AI providers"], ["mcp", "MCP server"], ["integrations", "Integrations"], ["provisioning", "Provisioning"], ["ops", "Operations"], ["deploy", "Deployment"], ["audit", "Audit log"]] : [["tokens", "API tokens"]];
  const body = h("div", { class: "stack" });
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Administration"), h("h1", null, can("admin") ? "Admin" : "My API tokens"))),
    h("div", { class: "tabs", role: "tablist" }, tabs.map(([k, l]) => h("button", { role: "tab", "aria-selected": String(k === tab), onclick: () => go("#/admin/" + k) }, l))), body);
  await ({ users: adminUsers, tokens: adminTokens, sso: adminSSO, ai: adminAI, audit: adminAudit, security: adminSecurity, email: adminEmail, deploy: adminDeploy, mcp: adminMCP, integrations: adminIntegrations, provisioning: adminProvisioning, ops: adminOps }[tab] || adminTokens)(body);
});

async function adminUsers(el) {
  const [users, me] = [await GET("/api/users"), T.me];
  const roleHelp = (await GET("/api/auth/me")).role_help;
  el.replaceChildren(h("div", { class: "spread" }, h("p", { class: "small muted" }, Object.entries(roleHelp).map(([k, v]) => `${k}: ${v}`).join("  ·  ")),
    h("button", { class: "primary", onclick: () => {
      const e = h("input", { id: "nu-email", type: "email", placeholder: "name@company.com" }), n = h("input", { id: "nu-name" }), p = h("input", { id: "nu-pw", type: "password", autocomplete: "new-password" });
      const r = h("select", { id: "nu-role" }, ["viewer", "analyst", "engineer", "admin"].map((x) => h("option", { selected: x === "analyst" }, x)));
      dialog("Add user", h("div", { class: "form" }, field("Email", e), field("Name", n), field("Role", r), field("Initial password", p, "12+ characters. Leave empty for SSO-only users.")),
        [{ label: "Add user", run: async () => { await POST("/api/users", { email: e.value, name: n.value, role: r.value, password: p.value || null }); toast("User added"); render(); } }]);
    } }, "Add user")),
  h("div", { class: "card flat" }, table([
    { label: "User", render: (u) => h("div", null, h("b", null, u.name), h("div", { class: "small muted" }, u.email)) },
    { label: "Role", render: (u) => { const s = h("select", { "aria-label": "Role for " + u.email, disabled: u.id === me.id }, ["viewer", "analyst", "engineer", "admin"].map((x) => h("option", { selected: x === u.role }, x))); s.addEventListener("change", () => act(null, () => PATCH(`/api/users/${u.id}`, { role: s.value }), "Role updated")); return s; } },
    { label: "Sign-in", render: (u) => h("div", { class: "row" }, u.sso ? chip("SSO", "info") : chip("password"), u.locked ? chip("locked", "bad") : null, u.active ? null : chip("deactivated", "bad")) },
    { label: "Last sign-in", render: (u) => ago(u.last_login) },
    { label: "", render: (u) => u.id === me.id ? chip("you") : h("div", { class: "row" },
      u.locked ? h("button", { class: "sm", onclick: () => act(null, () => PATCH(`/api/users/${u.id}`, { unlock: true }), "Unlocked").then(render) }, "Unlock") : null,
      h("button", { class: "sm", onclick: () => { const p = h("input", { type: "password", id: "rp-pw", autocomplete: "new-password" }); dialog("Set password for " + u.email, field("New password", p, "Signs the user out everywhere."), [{ label: "Set password", run: async () => { await PATCH(`/api/users/${u.id}`, { password: p.value }); toast("Password set"); } }]); } }, "Set password"),
      h("button", { class: "sm " + (u.active ? "danger" : ""), onclick: () => act(null, () => PATCH(`/api/users/${u.id}`, { active: !u.active }), u.active ? "Deactivated" : "Reactivated").then(render) }, u.active ? "Deactivate" : "Reactivate")) },
  ], users)));
}

async function adminTokens(el) {
  const toks = await GET("/api/tokens");
  el.replaceChildren(h("div", { class: "spread" }, h("p", { class: "small muted" }, "Tokens let schedulers, CI and scripts call the API as you, with a role no higher than yours. Send them as Authorization: Bearer <token>."),
    h("button", { class: "primary", onclick: () => {
      const n = h("input", { id: "tok-name", placeholder: "airflow-prod" });
      const r = h("select", { id: "tok-role" }, ["viewer", "analyst", "engineer", "admin"].filter((x) => can(x)).map((x) => h("option", { selected: x === T.me.role }, x)));
      dialog("Create API token", h("div", { class: "form" }, field("Name", n), field("Role", r)), [{ label: "Create", run: async () => {
        const t = await POST("/api/tokens", { name: n.value.trim() || "token", role: r.value });
        const box = h("input", { value: t.token, readonly: true, id: "tok-value" });
        dialog("Copy your token", h("div", { class: "stack" }, h("div", { class: "notice warn" }, t.note), box, h("button", { onclick: () => copyText(t.token, box) }, "Copy")), []);
        render();
      } }]);
    } }, "Create token")),
  toks.length ? h("div", { class: "card flat" }, table([
    { label: "Name", render: (t) => h("b", null, t.name) }, { label: "Prefix", render: (t) => h("span", { class: "mono" }, t.prefix + "…") }, { label: "Owner", key: "email" },
    { label: "Role", key: "role" }, { label: "Last used", render: (t) => ago(t.last_used) },
    { label: "", render: (t) => t.revoked ? chip("revoked", "bad") : h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/tokens/${t.id}`), "Revoked").then(render) }, "Revoke") },
  ], toks)) : h("div", { class: "empty" }, "No tokens."));
}

async function adminSSO(el) {
  const c = await GET("/api/admin/sso");
  const v = (k, ph) => h("input", { id: "sso-" + k, value: c[k] ?? "", placeholder: ph || "" });
  const f = { issuer: v("issuer", "https://login.microsoftonline.com/<tenant-id>/v2.0"), client_id: v("client_id"), redirect_uri: v("redirect_uri", c.suggested_redirect_uri),
    allowed_domains: v("allowed_domains", "acme.com, acme.ca"), scopes: v("scopes", "openid email profile"), button_label: v("button_label", "Sign in with Microsoft") };
  const secret = h("input", { id: "sso-secret", type: "password", placeholder: c.client_secret_set ? "•••••••• (stored, leave empty to keep)" : "" });
  const role = h("select", { id: "sso-role" }, ["viewer", "analyst", "engineer"].map((x) => h("option", { selected: (c.default_role || "viewer") === x }, x)));
  const groups = h("textarea", { id: "sso-groups", rows: 3, placeholder: '{"engineer": "<group id>", "admin": "<group id>"}' });
  groups.value = c.role_groups ? JSON.stringify(c.role_groups) : "";
  const en = h("input", { type: "checkbox", id: "sso-enabled", checked: !!c.enabled });
  const prov = h("input", { type: "checkbox", id: "sso-prov", checked: c.auto_provision !== false });
  el.replaceChildren(h("div", { class: "card" }, h("p", { class: "small muted" }, "OpenID Connect with PKCE. Works with Microsoft Entra ID, Okta, Google Workspace, Keycloak and Auth0. Register this redirect URI with your identity provider: ", h("code", null, c.suggested_redirect_uri)),
    h("div", { class: "form" }, field("Issuer URL", f.issuer), field("Client ID", f.client_id), field("Client secret (encrypted)", secret), field("Redirect URI", f.redirect_uri),
      field("Allowed email domains", f.allowed_domains), field("Default role for new users", role), field("Scopes", f.scopes), field("Button label", f.button_label)),
    field("Map groups or app roles to Tessera roles (JSON)", groups, "Uses the groups or roles claim in the ID token."),
    h("label", { class: "row small" }, en, "Enable single sign-on"), h("label", { class: "row small" }, prov, "Create accounts on first sign-in"),
    h("div", null, h("button", { class: "primary", onclick: async (e) => {
      let rg = null;
      if (groups.value.trim()) { try { rg = JSON.parse(groups.value); } catch { toast("Group mapping must be JSON", "bad"); return; } }
      await act(e.target, () => PUT("/api/admin/sso", { ...Object.fromEntries(Object.entries(f).map(([k, i]) => [k, i.value.trim()])), client_secret: secret.value || undefined, default_role: role.value, role_groups: rg, enabled: en.checked, auto_provision: prov.checked }), "SSO settings saved");
    } }, "Save"))));
}

async function adminAI(el) {
  const p = await GET("/api/ai/providers");
  const allow = h("input", { type: "checkbox", id: "ai-raw", checked: p.allow_raw_pii });
  allow.addEventListener("change", () => act(null, () => PUT("/api/ai/settings", { allow_raw_pii: allow.checked }), "Saved"));
  el.replaceChildren(h("div", { class: "spread" }, h("p", { class: "small muted" }, "Agents call the provider you choose. Keys are encrypted at rest. For fully offline use, run Ollama on this server."),
    h("button", { class: "primary", onclick: () => {
      const kind = h("select", { id: "ai-kind" }, Object.entries(p.kinds).map(([k, v]) => h("option", { value: k }, v.label)));
      const name = h("input", { id: "ai-name" }), url = h("input", { id: "ai-url" }), model = h("input", { id: "ai-model" }), key = h("input", { id: "ai-key", type: "password", autocomplete: "off" });
      const def = h("input", { type: "checkbox", id: "ai-def", checked: !p.providers.length });
      const sync = () => { const k = p.kinds[kind.value]; url.placeholder = k.base_url; model.placeholder = k.model_hint; name.placeholder = k.label; key.placeholder = k.key ? "required" : "not needed"; };
      kind.addEventListener("change", sync); sync();
      dialog("Add AI provider", h("div", { class: "stack" }, h("div", { class: "form" }, field("Provider", kind), field("Name", name), field("Base URL", url), field("Model", model), field("API key (encrypted)", key)), h("label", { class: "row small" }, def, "Use by default")),
        [{ label: "Add provider", run: async () => { await POST("/api/ai/providers", { kind: kind.value, name: name.value || name.placeholder, base_url: url.value || null, model: model.value || model.placeholder, api_key: key.value || null, is_default: def.checked }); toast("Provider added"); render(); } }]);
    } }, "Add provider")),
  p.providers.length ? h("div", { class: "card flat" }, table([
    { label: "Name", render: (x) => h("div", { class: "row" }, h("b", null, x.name), x.is_default ? chip("default", "info") : null) }, { label: "Kind", render: (x) => p.kinds[x.kind]?.label || x.kind },
    { label: "Model", render: (x) => h("span", { class: "mono" }, x.model) }, { label: "Endpoint", render: (x) => h("span", { class: "mono small" }, x.base_url) },
    { label: "Key", render: (x) => x.key_set ? chip("stored", "ok") : chip("none") },
    { label: "", render: (x) => h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/ai/providers/${x.id}`), "Removed").then(render) }, "Remove") }], p.providers)) : h("div", { class: "empty" }, "No providers yet."),
  h("div", { class: "card" }, h("label", { class: "row" }, allow, h("b", null, "Allow raw personal data to reach the model")),
    h("p", { class: "small muted" }, "Off by default. When off, values that look like emails, SINs, SSNs, card numbers, phone numbers or IP addresses, and columns classified as personal data, are replaced with placeholders before anything leaves Tessera.")));
}

async function adminAudit(el) {
  const rows = await GET("/api/audit?limit=500");
  const q = h("input", { type: "search", id: "audit-q", placeholder: "Filter by action, user or target", style: { maxWidth: "320px" } });
  const box = h("div");
  const draw = () => { const f = q.value.toLowerCase(); box.replaceChildren(h("div", { class: "card flat" }, table([
    { label: "When", render: (a) => h("span", { class: "small mono" }, new Date(a.ts).toLocaleString()) }, { label: "User", render: (a) => a.user_email || "system" },
    { label: "Action", render: (a) => h("span", { class: "mono" }, a.action) }, { label: "Target", render: (a) => h("span", { class: "small" }, a.target) },
    { label: "Detail", render: (a) => h("span", { class: "small mono muted" }, JSON.stringify(a.detail).slice(0, 160)) }, { label: "IP", render: (a) => h("span", { class: "small mono" }, a.ip || "") },
  ], rows.filter((a) => !f || JSON.stringify(a).toLowerCase().includes(f)).slice(0, 300)))); };
  q.addEventListener("input", draw); draw();
  el.replaceChildren(h("div", { class: "spread" }, q, h("span", { class: "small muted" }, "Every sign-in, change, query, export and agent tool call is recorded.")), box);
}
