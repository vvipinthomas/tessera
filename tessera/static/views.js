"use strict";
/* Views: overview, catalog, dataset detail, connections. */

/* ---------------- overview ---------------- */
route("overview", async (main) => {
  const o = await GET("/api/overview");
  const head = h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Overview"), h("h1", null, "Data health at a glance")),
    can("engineer") ? h("div", { class: "row" }, h("button", { onclick: () => go("#/connections/new") }, "Add connection"), h("button", { class: "primary", onclick: () => go("#/pipelines/new") }, "New pipeline")) : null);
  const kpis = h("div", { class: "kpis" }, [
    [fmt(o.datasets), "datasets in the catalog"], [fmt(o.rows), "rows in raw and curated"], [fmt(o.rules), "active quality rules"],
    [fmt(o.pipelines), "pipelines"], [fmt(o.published), "published, masked datasets"],
  ].map(([b, sp]) => h("div", { class: "kpi" }, h("b", null, b), h("span", null, sp))));
  if (!o.datasets) {
    main.replaceChildren(head, kpis, h("div", { class: "empty" }, h("h2", null, "No data yet"),
      h("p", null, can("engineer") ? "Connect a source or upload a file to start profiling, testing and protecting your data." : "An engineer needs to connect and publish data before it appears here."),
      can("admin") ? h("button", { class: "primary", onclick: (e) => act(e.target, () => POST("/api/sample"), "Sample workspace installed").then(render) }, "Load the sample retail workspace") : null));
    return;
  }
  const ready = [...o.readiness].sort((a, b) => b.score - a.score);
  const readiness = h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", null, "AI readiness"), h("span", { class: "muted small" }, "Quality, completeness, documentation, protection, freshness, lineage, uniqueness")),
    table([
      { label: "Dataset", render: (r) => h("span", { class: "mono" }, r.dataset) },
      { label: "Readiness", render: (r) => h("div", { class: "row" }, h("div", { style: { width: "110px" } }, meter(r.score / 100, r.score >= 85 ? "ok" : r.score >= 65 ? "warn" : "bad")), readinessChip(r.score)) },
      { label: "Grade", key: "grade" },
      { label: "Quality", num: true, render: (r) => pct(r.quality) },
      { label: "Rows", num: true, render: (r) => fmt(r.rows) },
    ], ready, { onclick: (r) => go("#/catalog/" + r.id) }));
  const runs = h("div", { class: "card" }, h("h2", null, "Pipeline runs, last 14 days"), runsChart(o.runs_by_day),
    o.recent_runs.length ? table([
      { label: "Pipeline", key: "pipeline" }, { label: "Status", render: (r) => statusChip(r.status) }, { label: "Trigger", key: "trigger" },
      { label: "Started", render: (r) => ago(r.created_at) }], o.recent_runs, { onclick: (r) => go("#/pipelines/run/" + r.id) }) : h("p", { class: "muted" }, "No runs yet."));
  main.replaceChildren(head, kpis, h("div", { class: "grid2" }, readiness, runs));
});

/* ---------------- catalog ---------------- */
route("catalog", async (main, id, sub) => {
  if (id) return datasetView(main, +id, sub);
  const list = await GET("/api/datasets");
  const q = h("input", { type: "search", id: "cat-q", placeholder: "Filter datasets", "aria-label": "Filter datasets", style: { maxWidth: "280px" } });
  const body = h("div");
  const draw = () => {
    const f = q.value.toLowerCase();
    const rows = list.filter((d) => !f || fqn(d).toLowerCase().includes(f) || (d.description || "").toLowerCase().includes(f));
    body.replaceChildren(rows.length ? h("div", { class: "card flat" }, table([
      { label: "Dataset", render: (d) => h("div", { class: "row" }, schemaTag(d.schema_name), h("b", { class: "mono" }, d.name), endorsementChip(d.endorsement), d.rls ? chip("RLS") : null) },
      { label: "Domain", render: (d) => d.domain ? chip(d.domain, "info") : "—" },
      { label: "Description", render: (d) => h("span", { class: "muted small" }, (d.description || "").slice(0, 90) || "—") },
      { label: "Rows", num: true, render: (d) => fmt(d.row_count) },
      { label: "Quality", render: (d) => d.quality == null ? chip(d.rules ? "not run" : "no rules") : h("div", { class: "row" }, h("span", { class: "tess " + scoreKind(d.quality) }), pct(d.quality)) },
      { label: "Readiness", render: (d) => readinessChip(d.readiness) },
      { label: "Personal data", render: (d) => d.pii_scanned_at ? (d.sensitive ? chip(d.sensitive + " columns", "warn") : chip("none found", "ok")) : chip("not scanned") },
      { label: "Loaded", render: (d) => ago(d.last_loaded_at) },
    ], rows, { onclick: (d) => go("#/catalog/" + d.id) })) : h("div", { class: "empty" }, list.length ? "No dataset matches that filter." : "The catalog is empty. Upload a file or ingest from a connection."));
  };
  q.addEventListener("input", draw);
  draw();
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Catalog"), h("h1", null, "Datasets")),
    h("div", { class: "row" }, q, can("engineer") ? h("button", { class: "primary", onclick: uploadDialog }, "Upload file") : null)),
    can("engineer") ? null : h("div", { class: "notice" }, "You see published datasets. Personal data in them is already masked."), body);
});

function governanceDialog(d) {
  const e = h("select", { id: "gov-end" }, [["none", "Not endorsed"], ["promoted", "Promoted: recommended for use"], ["certified", "Certified: meets the organization's standards (admins)"]]
    .map(([v, l]) => h("option", { value: v, selected: (d.endorsement || "none") === v, disabled: v === "certified" && !can("admin") }, l)));
  const dom = h("input", { id: "gov-dom", value: d.domain || "", placeholder: "Sales, Finance, Operations…" });
  dialog(`Govern ${fqn(d)}`, h("div", { class: "form" }, field("Endorsement", e), field("Domain", dom)),
    [{ label: "Save", run: async () => { await PATCH(`/api/datasets/${d.id}/governance`, { endorsement: e.value, domain: dom.value }); toast("Saved"); render(); } }]);
}

function uploadDialog() {
  const file = h("input", { type: "file", id: "up-file", accept: ".csv,.tsv,.txt,.json,.ndjson,.jsonl,.parquet,.xlsx,.db,.sqlite" });
  const target = h("input", { id: "up-target", placeholder: "raw.my_table" });
  file.addEventListener("change", () => {
    const n = (file.files[0]?.name || "").replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
    if (n && !target.value) target.value = "raw." + (/^[a-z_]/.test(n) ? n : "t_" + n);
  });
  dialog("Upload a file", h("div", { class: "stack" }, field("File (CSV, TSV, JSON, NDJSON, Parquet, Excel, SQLite)", file),
    field("Load into table", target, "Leave empty to only store the file for a Files connection. SQLite files are used through a SQLite connection."),
    h("p", { class: "small muted" }, "The file is stored in Tessera's data folder on this server and never sent anywhere else.")),
  [{ label: "Upload", run: async (e) => {
    if (!file.files[0]) throw new Error("Choose a file first.");
    const fd = new FormData(); fd.append("file", file.files[0]); fd.append("target", target.value.trim());
    const r = await act(e.target, () => api("POST", "/api/uploads", fd));
    toast(r.dataset ? `Loaded ${fmt(r.dataset.rows)} rows into ${r.dataset.name}` : "File stored");
    if (r.dataset) go("#/catalog/" + r.dataset.id); else render();
  } }]);
}

async function datasetView(main, id, tab) {
  tab = tab || "overview";
  const d = await GET(`/api/datasets/${id}`);
  const eng = can("engineer");
  const tabs = [["overview", "Readiness"], ["columns", "Columns"], ["preview", "Preview"], ["profile", "Profile"], ["quality", "Quality"], ["lineage", "Lineage"]];
  const actions = eng ? h("div", { class: "row" },
    h("button", { onclick: (e) => act(e.target, () => POST(`/api/datasets/${id}/pii-scan`), "Scan complete").then(() => go(`#/catalog/${id}/columns`)) }, "Scan for personal data"),
    d.schema_name !== "serving" ? h("button", { class: "primary", onclick: (e) => act(e.target, () => POST(`/api/datasets/${id}/publish`)).then((r) => { toast(`Published ${r.dataset} (${r.masked_columns.length} masked columns)`); render(); }) }, "Publish masked copy") : null,
    h("button", { onclick: (e) => act(e.target, () => POST(`/api/datasets/${id}/export`, { format: "parquet" })).then((r) => toast(`Exported ${r.file} with datacard`)) }, "Export Parquet"),
    h("button", { class: "danger", onclick: () => confirmBox(`Drop ${fqn(d)}? The table is deleted from the warehouse.`, "Drop table", async () => { await DEL(`/api/datasets/${id}`); toast("Dropped"); go("#/catalog"); }) }, "Drop")) : null;
  const desc = eng ? h("textarea", { id: "ds-desc", rows: 2, placeholder: "Describe what one row means, where it comes from and who owns it.", style: { fontFamily: "var(--f-body)" } }) : null;
  if (desc) { desc.value = d.description || ""; desc.addEventListener("change", () => act(null, () => PATCH(`/api/datasets/${id}`, { description: desc.value }), "Description saved")); }
  const content = h("div", { class: "stack" });
  main.replaceChildren(
    h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/catalog", class: "small" }, "← Catalog"),
      h("div", { class: "row" }, schemaTag(d.schema_name), h("h1", { class: "mono" }, d.name), endorsementChip(d.endorsement), d.domain ? chip(d.domain, "info") : null,
        d.schema_name === "serving" && d.rls ? chip("row-level security") : null,
        eng ? h("button", { class: "sm ghost", onclick: () => governanceDialog(d) }, "Endorse / domain") : null),
      h("span", { class: "muted small" }, `${fmt(d.row_count)} rows · ${d.columns.length} columns · loaded ${ago(d.last_loaded_at)}${d.published_at ? " · published " + ago(d.published_at) : ""}`)),
      actions),
    desc ? field("Description", desc) : (d.description ? h("p", null, d.description) : null),
    h("div", { class: "tabs", role: "tablist" }, tabs.map(([k, l]) => h("button", { role: "tab", "aria-selected": String(k === tab), onclick: () => go(`#/catalog/${id}/${k}`) }, l))),
    content);
  await ({ overview: dsReadiness, columns: dsColumns, preview: dsPreview, profile: dsProfile, quality: dsQuality, lineage: dsLineage }[tab] || dsReadiness)(content, d);
}

async function dsReadiness(el, d) {
  const r = d.readiness;
  const fresh = can("engineer") ? h("input", { id: "ds-sla", type: "number", min: "1", step: "1", value: d.freshness_sla_hours ?? "", placeholder: "24", style: { width: "90px" } }) : null;
  if (fresh) fresh.addEventListener("change", () => act(null, () => PATCH(`/api/datasets/${d.id}`, { freshness_sla_hours: fresh.value || null }), "Freshness target saved").then(render));
  el.replaceChildren(
    h("div", { class: "card" }, h("div", { class: "spread" }, h("div", { class: "row" }, h("h2", null, "AI readiness"), readinessChip(r.score), chip(r.grade, r.score >= 85 ? "ok" : r.score >= 65 ? "warn" : "bad")),
      fresh ? h("label", { class: "row small muted" }, "Freshness target (hours)", fresh) : null),
    table([
      { label: "Check", render: (c) => h("div", { class: "row" }, h("span", { class: "tess " + scoreKind(c.score, 0.95, 0.6) }), h("b", null, c.label)) },
      { label: "Weight", num: true, key: "weight" },
      { label: "Score", render: (c) => h("div", { style: { width: "100px" } }, meter(c.score, scoreKind(c.score, 0.95, 0.6))) },
      { label: "Detail", render: (c) => h("span", { class: "small" }, c.detail) },
      { label: "How to improve", render: (c) => c.score < 0.95 ? h("span", { class: "small muted" }, c.fix) : chip("done", "ok") },
    ], r.checks)),
    can("analyst") ? h("p", { class: "small muted" }, "The datacard (schema, descriptions, quality, sensitivity, lineage) is written next to every export so ML and RAG teams receive context with the data.") : null);
}

async function dsColumns(el, d) {
  const eng = can("engineer");
  const pol = await GET("/api/pii/policies");
  el.replaceChildren(h("div", { class: "card flat" }, table([
    { label: "Column", render: (c) => h("b", { class: "mono" }, c.name) },
    { label: "Type", render: (c) => h("span", { class: "mono small muted" }, c.type) },
    { label: "Description", render: (c) => eng ? (() => { const i = h("input", { value: c.description || "", "aria-label": `Description of ${c.name}`, id: `desc-${c.name}` }); i.addEventListener("change", () => act(null, () => PATCH(`/api/datasets/${d.id}/columns/${encodeURIComponent(c.name)}`, { description: i.value }), "Saved")); return i; })() : (c.description || "—") },
    { label: "Detected", render: (c) => c.pii ? chip((c.pii_method === "column name" ? "name: " : "") + c.pii.replace(/_/g, " "), "warn") : (d.pii_scanned_at ? chip("none") : "—") },
    { label: "Sensitivity", render: (c) => eng && d.schema_name !== "serving" ? (() => { const s = h("select", { id: `lvl-${c.name}`, "aria-label": `Sensitivity of ${c.name}` }, pol.levels.map((l) => h("option", { value: l, selected: c.level === l }, l))); s.addEventListener("change", () => act(null, () => PATCH(`/api/datasets/${d.id}/columns/${encodeURIComponent(c.name)}`, { level: s.value }), "Saved")); return s; })() : chip(c.level || "internal", c.level === "restricted" ? "bad" : c.level === "confidential" ? "warn" : "") },
    { label: d.schema_name === "serving" ? "Masked with" : "Masking on publish", render: (c) => d.schema_name === "serving" ? (c.masked_from && c.masked_from !== "none" ? chip(pol.masks[c.masked_from] || c.masked_from, "ok") : "—") : eng ? (() => { const s = h("select", { id: `mask-${c.name}`, "aria-label": `Masking for ${c.name}` }, Object.entries(pol.masks).map(([k, l]) => h("option", { value: k, selected: c.mask === k }, l))); s.addEventListener("change", () => act(null, () => PATCH(`/api/datasets/${d.id}/columns/${encodeURIComponent(c.name)}`, { mask: s.value }), "Masking saved. Publish to apply.")); return s; })() : (pol.masks[c.mask] || "—") },
  ], d.columns)), d.pii_scanned_at ? null : h("div", { class: "notice warn" }, "Not scanned for personal data yet. Scanning samples up to 1,000 values per column and checks patterns such as emails, SINs, card numbers (Luhn-checked) and phone numbers."));
}

async function dsPreview(el, d) {
  const r = await GET(`/api/datasets/${d.id}/preview?limit=200`);
  el.replaceChildren(h("p", { class: "small muted" }, `First ${r.row_count} rows${d.schema_name !== "serving" ? ". Raw values, visible to engineers only." : ""}`), resultTable(r));
}

async function dsProfile(el, d) {
  const p = await GET(`/api/datasets/${d.id}/profile`);
  el.replaceChildren(h("div", { class: "card flat" }, table([
    { label: "Column", render: (c) => h("b", { class: "mono" }, c.column_name) }, { label: "Type", render: (c) => h("span", { class: "mono small muted" }, c.column_type) },
    { label: "Empty", render: (c) => h("div", { class: "row" }, h("div", { style: { width: "70px" } }, meter(1 - (c.null_percentage || 0) / 100)), (c.null_percentage ?? 0) + "%") },
    { label: "Distinct (approx.)", num: true, render: (c) => fmt(c.approx_unique) },
    { label: "Min", render: (c) => h("span", { class: "mono small" }, String(c.min ?? "—").slice(0, 28)) },
    { label: "Max", render: (c) => h("span", { class: "mono small" }, String(c.max ?? "—").slice(0, 28)) },
    { label: "Mean", num: true, render: (c) => c.avg == null ? "—" : fmt(+c.avg, 2) },
  ], p)));
}

async function dsQuality(el, d) {
  const eng = can("engineer");
  const [rules, kinds] = await Promise.all([GET(`/api/datasets/${d.id}/rules`), GET("/api/quality/kinds")]);
  const list = rules.length ? h("div", { class: "card flat" }, table([
    { label: "", render: (r) => h("span", { class: "tess " + (!r.last_result ? "" : r.last_result.error ? "bad" : r.last_result.passed ? "ok" : r.severity === "warn" ? "warn" : "bad") }) },
    { label: "Rule", render: (r) => h("div", null, h("b", null, r.description_auto), r.last_result?.error ? h("div", { class: "small", style: { color: "var(--bad)" } }, r.last_result.error) : null) },
    { label: "Severity", render: (r) => chip(r.severity, r.severity === "error" ? "bad" : "warn") },
    { label: "Failing rows", num: true, render: (r) => r.last_result ? fmt(r.last_result.failing) : "—" },
    { label: "Last run", render: (r) => r.last_result ? ago(r.last_result.run_at) : "never" },
    { label: "", render: (r) => h("div", { class: "row" },
      r.last_result?.sample ? h("button", { class: "sm", onclick: () => dialog("Failing examples", resultTable({ ...r.last_result.sample, types: [] })) }, "Examples") : null,
      eng ? h("button", { class: "sm", onclick: () => act(null, () => PATCH(`/api/rules/${r.id}`, { enabled: !r.enabled })).then(render) }, r.enabled ? "Disable" : "Enable") : null,
      eng ? h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/rules/${r.id}`), "Rule removed").then(render) }, "Remove") : null) },
  ], rules)) : h("div", { class: "empty" }, "No rules yet. Ask Tessera to suggest rules from the data, or add one.");
  const sugBox = h("div");
  const suggest = h("button", { onclick: async (e) => {
    const s = await act(e.target, () => POST(`/api/datasets/${d.id}/rules/suggest`));
    sugBox.replaceChildren(s.length ? h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", null, `${s.length} suggestions from the data`),
      eng ? h("button", { class: "primary sm", onclick: async (ev) => { await act(ev.target, async () => { for (const r of s) await POST(`/api/datasets/${d.id}/rules`, r); }, "Rules added"); render(); } }, "Accept all") : null),
      table([{ label: "Rule", render: (r) => h("b", null, `${r.column_name || "table"}: ${kinds.kinds[r.kind].label}${r.params?.label ? " (" + r.params.label + ")" : r.params?.min != null ? " ≥ " + r.params.min : ""}`) },
        { label: "Why", render: (r) => h("span", { class: "small muted" }, r.reason) }, { label: "Severity", render: (r) => chip(r.severity, r.severity === "error" ? "bad" : "warn") },
        { label: "", render: (r) => eng ? h("button", { class: "sm", onclick: (ev) => act(ev.target, () => POST(`/api/datasets/${d.id}/rules`, r), "Rule added").then(() => { ev.target.replaceWith(chip("added", "ok")); }) }, "Add") : null }], s))
      : h("p", { class: "muted" }, "No new suggestions. Existing rules already cover what the data shows."));
  } }, "Suggest rules");
  el.replaceChildren(h("div", { class: "spread" }, h("div", { class: "row" }, h("h2", null, "Quality rules"), d.quality.score != null ? chip("score " + pct(d.quality.score), scoreKind(d.quality.score)) : null),
    h("div", { class: "row" }, suggest, eng ? h("button", { onclick: () => ruleDialog(d, kinds) }, "Add rule") : null,
      eng && rules.length ? h("button", { class: "primary", onclick: (e) => act(e.target, () => POST(`/api/datasets/${d.id}/quality/run`)).then((r) => { toast(`Score ${pct(r.score)} · ${r.failed_errors} error and ${r.failed_warnings} warning rules failing`); render(); }) }, "Run checks") : null)),
  sugBox, list);
}

function ruleDialog(d, kinds) {
  const kind = h("select", { id: "rule-kind" }, Object.entries(kinds.kinds).map(([k, v]) => h("option", { value: k }, v.label)));
  const col = h("select", { id: "rule-col" }, d.columns.map((c) => h("option", { value: c.name }, c.name)));
  const sev = h("select", { id: "rule-sev" }, h("option", { value: "error" }, "error (fails pipelines)"), h("option", { value: "warn" }, "warn (reported only)"));
  const params = h("div", { class: "form" });
  const inputs = {};
  const draw = () => {
    const k = kinds.kinds[kind.value];
    col.disabled = !k.column;
    params.replaceChildren(...k.params.map((p) => {
      let i;
      if (p === "pattern") {
        i = h("select", { id: "rule-pattern" }, Object.entries(kinds.patterns).map(([n, rx]) => h("option", { value: rx }, n)), h("option", { value: "" }, "custom…"));
        const custom = h("input", { id: "rule-pattern-custom", placeholder: "^[A-Z]{3}-\\d+$" });
        inputs.pattern = { get value() { return i.value || custom.value; }, get label() { return i.value ? i.selectedOptions[0].textContent : null; } };
        return h("div", { class: "stack" }, field("Pattern", i), field("Custom regular expression", custom));
      }
      if (p === "sql") { i = h("textarea", { id: "rule-sql", rows: 4, placeholder: `select * from ${fqn(d)} where …` }); inputs.sql = i; return field("SELECT returning failing rows", i); }
      i = h("input", { id: "rule-" + p, placeholder: p === "values" ? "paid, shipped, refunded" : "" });
      inputs[p] = i;
      return field({ min: "Minimum", max: "Maximum", values: "Allowed values (comma separated)", hours: "Hours" }[p] || p, i);
    }));
  };
  kind.addEventListener("change", draw);
  draw();
  dialog(`Add rule to ${fqn(d)}`, h("div", { class: "stack" }, h("div", { class: "form" }, field("Check", kind), field("Column", col), field("Severity", sev)), params),
    [{ label: "Add rule", run: async () => {
      const k = kind.value, p = {};
      for (const [name, inp] of Object.entries(inputs)) {
        if (!kinds.kinds[k].params.includes(name)) continue;
        const v = inp.value;
        if (name === "values") p.values = v.split(",").map((x) => x.trim()).filter(Boolean);
        else if (name === "pattern") { p.pattern = v; if (inp.label) p.label = inp.label; }
        else if (name === "sql") p.sql = v;
        else if (v !== "") p[name] = Number(v);
      }
      await POST(`/api/datasets/${d.id}/rules`, { kind: k, column_name: kinds.kinds[k].column ? col.value : null, params: p, severity: sev.value });
      toast("Rule added"); render();
    } }]);
}

async function dsLineage(el, d) {
  const lin = await GET(`/api/datasets/${d.id}/lineage`);
  el.replaceChildren(lin.edges.length ? h("div", { class: "card" }, h("p", { class: "small muted" }, "Upstream on the left, downstream on the right. Recorded automatically by ingest, transform and publish steps."), lineageView(lin))
    : h("div", { class: "empty" }, "No lineage yet. Load this dataset through a connection or pipeline and Tessera records where it came from."));
}

/* ---------------- connections ---------------- */
route("connections", async (main, id) => {
  if (!can("engineer")) { main.replaceChildren(h("div", { class: "notice" }, "Connections are managed by engineers.")); return; }
  const cat = await GET("/api/connectors");
  if (id === "new") return newConnection(main, cat);
  if (id) return connectionView(main, +id, cat);
  const list = await GET("/api/connections");
  const types = Object.fromEntries(cat.types.map((t) => [t.type, t]));
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Connections"), h("h1", null, "Sources")),
    h("button", { class: "primary", onclick: () => go("#/connections/new") }, "New connection")),
    list.length ? h("div", { class: "card flat" }, table([
      { label: "Name", render: (c) => h("b", null, c.name) }, { label: "Type", render: (c) => types[c.type]?.label || c.type },
      { label: "Status", render: (c) => c.last_test_ok == null ? chip("not tested") : c.last_test_ok ? chip("reachable", "ok") : chip("failing", "bad") },
      { label: "Last test", render: (c) => h("span", { class: "small muted" }, c.last_test_msg || "—") },
    ], list, { onclick: (c) => go("#/connections/" + c.id) })) : h("div", { class: "empty" }, "No connections yet."),
    connectorCatalog(cat));
});

function connectorCatalog(cat) {
  const groups = {};
  cat.types.forEach((t) => (groups[t.category] = groups[t.category] || []).push(t));
  return h("div", { class: "card" }, h("h2", null, "Connector catalog"),
    h("p", { class: "small muted" }, "Verified connectors run in Tessera's automated test suite against real services. Driver connectors use the vendor's standard driver; install it on the server to enable them."),
    h("div", { class: "grid2" }, Object.entries(groups).map(([g, ts]) => h("div", { class: "stack" }, h("span", { class: "eyebrow" }, g),
      ts.map((t) => h("div", { class: "spread" }, h("span", null, t.label), t.status === "verified" ? chip("verified", "ok") : t.driver_installed ? chip("driver installed", "info") : chip("pip install " + (t.pip || "").split(" ")[0]))))),
      h("div", { class: "stack" }, h("span", { class: "eyebrow" }, "Through generic connectors"), cat.via_generic.map((v) => h("div", { class: "spread" }, h("span", null, v.name), chip("via " + v.via)))),
      h("div", { class: "stack" }, h("span", { class: "eyebrow" }, "On the roadmap"), cat.roadmap.map((r) => h("div", { class: "spread" }, h("span", { class: "muted" }, r), chip("planned"))))));
}

function newConnection(main, cat) {
  const type = h("select", { id: "nc-type" }, cat.types.map((t) => h("option", { value: t.type }, `${t.label} (${t.category})`)));
  const name = h("input", { id: "nc-name", placeholder: "Sales Postgres (read-only)" });
  const fields = h("div", { class: "form" });
  const info = h("div");
  const inputs = {};
  const draw = () => {
    const t = cat.types.find((x) => x.type === type.value);
    Object.keys(inputs).forEach((k) => delete inputs[k]);
    fields.replaceChildren(...t.fields.map((f) => {
      const i = f.type === "select" ? h("select", { id: "nc-" + f.key }, f.options.map((o) => h("option", null, o))) : f.type === "textarea" ? h("textarea", { id: "nc-" + f.key, rows: 3, placeholder: f.placeholder || "" }) : h("input", { id: "nc-" + f.key, type: f.secret ? "password" : f.type === "number" ? "number" : "text", placeholder: f.placeholder || (t.default_port && f.key === "port" ? String(t.default_port) : ""), autocomplete: "off" });
      inputs[f.key] = i;
      return field(f.label + (f.required ? " *" : "") + (f.secret ? " (encrypted)" : ""), i);
    }));
    info.replaceChildren(t.description ? h("p", { class: "small muted" }, t.description) : null,
      t.status !== "verified" && !t.driver_installed ? h("div", { class: "notice warn" }, `Driver not installed on this server. Run: pip install ${t.pip}`) : null);
  };
  type.addEventListener("change", draw);
  draw();
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/connections", class: "small" }, "← Connections"), h("h1", null, "New connection"))),
    h("div", { class: "card" }, h("div", { class: "form" }, field("Connector", type), field("Name", name)), info, fields,
      h("p", { class: "small muted" }, "Use a read-only account. Secrets are encrypted with AES-256-GCM using the server's master key and are never shown again."),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async (e) => {
        const config = {};
        Object.entries(inputs).forEach(([k, i]) => { if (i.value !== "") config[k] = i.value; });
        const c = await act(e.target, () => POST("/api/connections", { type: type.value, name: name.value.trim(), config }));
        const t = await POST(`/api/connections/${c.id}/test`).catch((err) => ({ ok: false, message: err.message }));
        toast(t.ok ? "Saved and reachable: " + t.message : "Saved, but the test failed: " + t.message, t.ok ? null : "bad");
        go("#/connections/" + c.id);
      } }, "Save and test"))));
}

async function connectionView(main, id, cat) {
  const c = (await GET("/api/connections")).find((x) => x.id === id);
  if (!c) throw new Error("Connection not found");
  const t = cat.types.find((x) => x.type === c.type) || { fields: [], label: c.type };
  if (t.stream) return streamConnectionView(main, c, t);
  const status = h("div");
  const setStatus = (ok, msg) => status.replaceChildren(h("div", { class: "notice " + (ok ? "" : "bad") }, msg));
  if (c.last_test_msg) setStatus(!!c.last_test_ok, c.last_test_msg);
  const objects = h("div");
  const isSql = !["file", "rest", "s3", "mongodb", "elasticsearch", "delta", "iceberg", "sftp"].includes(c.type);
  const isDoc = ["mongodb", "elasticsearch"].includes(c.type);
  const src = h("input", { id: "ing-src", placeholder: { file: "orders*.csv", s3: "exports/2026/*.parquet", rest: "/v1/orders", mongodb: "orders", elasticsearch: "logs-*", delta: "sales/orders", iceberg: "sales.orders", sftp: "daily/*.csv" }[c.type] || "public.orders" });
  const docq = isDoc ? h("textarea", { id: "ing-docq", rows: 3, placeholder: c.type === "mongodb" ? '{"status": "paid"}' : '{"match": {"service": "checkout"}}' }) : null;
  const version = c.type === "delta" ? h("input", { id: "ing-version", type: "number", min: 0, placeholder: "latest" })
    : c.type === "iceberg" ? h("input", { id: "ing-version", placeholder: "current snapshot" }) : null;
  const rowFilter = c.type === "iceberg" ? h("input", { id: "ing-filter", placeholder: "amount > 0 and region = 'west'" }) : null;
  const query = isSql ? h("textarea", { id: "ing-query", rows: 3, placeholder: "Optional: SELECT ... (used instead of a table)" }) : null;
  const target = h("input", { id: "ing-target", placeholder: "raw.orders" });
  const mode = h("select", { id: "ing-mode" }, ["replace", "append", "incremental"].map((m) => h("option", null, m)));
  const cursor = h("input", { id: "ing-cursor", placeholder: "updated_at" });
  const records = c.type === "rest" ? h("input", { id: "ing-records", placeholder: "data.items" }) : null;
  const pag = c.type === "rest" ? h("select", { id: "ing-pag" }, ["none", "page", "offset", "cursor", "next_url", "link_header"].map((m) => h("option", null, m))) : null;
  const source = () => {
    if (c.type === "rest") return { path: src.value, records_path: records.value || null, pagination: { type: pag.value, max_pages: 200 }, cursor_column: cursor.value || undefined };
    const jq = () => { if (!docq || !docq.value.trim()) return undefined; try { return JSON.parse(docq.value); } catch { throw new Error("The filter or query must be valid JSON."); } };
    if (c.type === "mongodb") return { collection: src.value, filter: jq(), cursor_column: cursor.value || undefined };
    if (c.type === "elasticsearch") return { index: src.value, query: jq(), cursor_column: cursor.value || undefined };
    if (c.type === "delta") return { table: src.value, version: version.value === "" ? undefined : +version.value, cursor_column: cursor.value || undefined };
    if (c.type === "iceberg") return { table: src.value, snapshot_id: version.value.trim() || undefined, row_filter: rowFilter.value.trim() || undefined, cursor_column: cursor.value || undefined };
    if (isSql) return query.value.trim() ? { query: query.value, cursor_column: cursor.value || undefined } : { table: src.value, cursor_column: cursor.value || undefined };
    return { path: src.value, cursor_column: cursor.value || undefined };
  };
  main.replaceChildren(
    h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/connections", class: "small" }, "← Connections"), h("h1", null, c.name), h("span", { class: "muted small" }, t.label)),
      h("div", { class: "row" },
        h("button", { onclick: async (e) => { const r = await act(e.target, () => POST(`/api/connections/${id}/test`)); setStatus(r.ok, r.message); } }, "Test"),
        h("button", { onclick: async (e) => { const o = await act(e.target, () => GET(`/api/connections/${id}/browse`)); objects.replaceChildren(o.length ? h("div", { class: "card flat" }, table([{ label: "Object", render: (x) => h("span", { class: "mono" }, x.name) }, { label: "Size", num: true, render: (x) => x.size != null ? fmt(x.size) + " B" : "—" }], o.slice(0, 300), { onclick: (x) => { src.value = x.name; if (!target.value) target.value = "raw." + x.name.split("/").pop().split(".")[isSql ? 1 : 0].toLowerCase().replace(/[^a-z0-9_]/g, "_"); } })) : h("p", { class: "muted" }, "Nothing to list.")); } }, "Browse"),
        h("button", { class: "danger", onclick: () => confirmBox(`Delete connection ${c.name}? Pipelines that use it will fail.`, "Delete", async () => { await DEL(`/api/connections/${id}`); go("#/connections"); }) }, "Delete"))),
    status,
    h("div", { class: "card" }, h("h2", null, "Load data"),
      h("div", { class: "form" }, field({ file: "Path or pattern", s3: "Path or pattern", rest: "Endpoint path", mongodb: "Collection", elasticsearch: "Index or pattern", delta: "Table folder", iceberg: "Table (namespace.table)", sftp: "Path or pattern" }[c.type] || "Table", src),
        docq ? field(c.type === "mongodb" ? "Filter (JSON, optional)" : "Query DSL (JSON, optional)", docq) : null, version ? field(c.type === "iceberg" ? "Snapshot id (time travel, optional)" : "Version (time travel, optional)", version) : null,
        rowFilter ? field("Row filter (pushed down, optional)", rowFilter) : null,
        records ? field("Records path in JSON", records) : null, pag ? field("Pagination", pag) : null,
        field("Target table", target), field("Mode", mode), field("Cursor column (incremental)", cursor, c.type === "sftp" ? "Use _source_modified to load only new or changed files." : null)),
      query ? field("Or a source query", query) : null,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async (e) => {
        const r = await act(e.target, async () => POST(`/api/connections/${id}/ingest`, { source: source(), target: target.value.trim(), mode: mode.value }));
        toast(`Loaded ${fmt(r.rows_loaded)} rows into ${r.dataset} (${fmt(r.row_count)} total)`);
      } }, "Load now"), h("span", { class: "small muted" }, "To run this on a schedule, add an ingest step to a pipeline."))),
    objects);
}

/* Streaming connections: test, list topics, and start a stream. No batch "Load now". */
function streamConnectionView(main, c, t) {
  const status = h("div"), objects = h("div");
  const setStatus = (ok, msg) => status.replaceChildren(h("div", { class: "notice " + (ok ? "" : "bad") }, msg));
  if (c.last_test_msg) setStatus(!!c.last_test_ok, c.last_test_msg);
  main.replaceChildren(
    h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/connections", class: "small" }, "← Connections"), h("h1", null, c.name), h("span", { class: "muted small" }, t.label)),
      h("div", { class: "row" },
        h("button", { onclick: async (e) => { const r = await act(e.target, () => POST(`/api/connections/${c.id}/test`)); setStatus(r.ok, r.message); } }, "Test"),
        c.type !== "mqtt" ? h("button", { onclick: async (e) => { const o = await act(e.target, () => GET(`/api/connections/${c.id}/browse`));
          objects.replaceChildren(o.length ? h("div", { class: "card flat" }, table([{ label: c.type === "kinesis" ? "Stream" : "Topic", render: (x) => h("span", { class: "mono" }, x.name) },
            { label: "Partitions", num: true, render: (x) => x.partitions ?? "—" }], o)) : h("p", { class: "muted" }, "Nothing to list.")); } }, c.type === "kinesis" ? "List streams" : "List topics") : null,
        h("button", { class: "primary", onclick: () => go(`#/streams/new/${c.id}`) }, "Create a stream"),
        h("button", { class: "danger", onclick: () => confirmBox(`Delete connection ${c.name}? Streams that use it will fail.`, "Delete", async () => { await DEL(`/api/connections/${c.id}`); go("#/connections"); }) }, "Delete"))),
    status,
    h("div", { class: "card" }, h("h2", null, "Streaming source"), h("p", { class: "muted" }, t.description || "Events are read continuously by a stream and land in a raw table."),
      h("p", { class: "small muted" }, "Delivery is at-least-once: positions are saved only after each batch is written. Set a de-duplication key on the stream to make replays harmless.")),
    objects);
}
