"use strict";
/* Explore (no-code report builder), semantic models and the reports library. */

const VIZ_TYPES = [["auto", "Auto"], ["kpi", "KPI"], ["bar", "Bar"], ["line", "Line"], ["area", "Area"], ["multiline", "Lines"], ["combo", "Combo"], ["donut", "Donut"], ["treemap", "Treemap"],
  ["stacked", "Stacked"], ["waterfall", "Waterfall"], ["funnel", "Funnel"], ["scatter", "Scatter"], ["pivot", "Pivot"], ["table", "Table"]];
const GRAINS = ["year", "quarter", "month", "week", "day"];
const FILTER_OPS = [["in", "is any of"], ["not_in", "is none of"], ["=", "="], ["!=", "≠"], [">", ">"], [">=", "≥"], ["<", "<"], ["<=", "≤"], ["between", "between"], ["contains", "contains"], ["not_null", "is not empty"], ["is_null", "is empty"]];

function autoViz(spec, model) {
  const dims = (spec.dimensions || []).map((n) => model.dimensions.find((d) => d.name === n)).filter(Boolean);
  const nm = (spec.measures || []).length;
  if (!dims.length) return { type: "kpi" };
  if (dims.length === 1 && dims[0].type === "time") return { type: "area", x: dims[0].name, y: spec.measures[0] };
  if (dims.length === 1) return nm === 2 ? { type: "combo", x: dims[0].name, y: spec.measures[0], y2: spec.measures[1] } : nm > 2 ? { type: "table" } : { type: "bar", x: dims[0].name, y: spec.measures[0] };
  if (dims.length === 2 && nm === 1) { const t = dims.find((d) => d.type === "time"); return t ? { type: "multiline", x: t.name, series: dims.find((d) => d !== t).name, y: spec.measures[0] } : { type: "stacked", x: dims[0].name, series: dims[1].name, y: spec.measures[0] }; }
  return { type: "table" };
}
function endorsementChip(e) {
  return e === "certified" ? chip("✓ certified", "ok") : e === "promoted" ? chip("promoted", "info") : null;
}

/* ---------------- explore ---------------- */
route("explore", async (main, id) => {
  const models = await GET("/api/models");
  if (!models.length) {
    main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Explore"), h("h1", null, "Explore without SQL"))),
      h("div", { class: "empty" }, h("h2", null, "No semantic models yet"), h("p", null, "A semantic model defines business measures (Revenue, Orders) and dimensions (Province, Month) once, so anyone can build reports by picking fields."),
        can("engineer") ? h("button", { class: "primary", onclick: () => go("#/models/new") }, "Create a semantic model") : h("p", { class: "small" }, "Ask an engineer to create one.")));
    return;
  }
  const model = id ? await GET(`/api/models/${id}`) : models[0];
  const seed = T.exploreSeed && T.exploreSeed.model_id === model.id ? T.exploreSeed : null;
  T.exploreSeed = null;
  const st = seed ? JSON.parse(JSON.stringify(seed)) : {
    model_id: model.id, measures: [model.measures[0].name], dimensions: [], grains: {}, filters: [], viz: { type: "auto" }, sort: [], compare: false, editing: null,
  };
  let last = null;
  const chart = h("div", { class: "explore-chart" });
  const sqlBox = h("pre", { class: "log" });
  const tableBox = h("div");
  const status = h("span", { class: "small muted" });
  const spec = () => ({ measures: st.measures, dimensions: st.dimensions, grains: st.grains, filters: st.filters.filter((f) => f.field && (f.op.includes("null") || (Array.isArray(f.value) ? f.value.length && f.value.some((x) => x !== "") : f.value !== "" && f.value != null))), sort: st.sort, limit: st.limit || 5000 });
  const vizNow = () => (st.viz.type === "auto" ? { ...autoViz(st, model), ...(st.viz.target ? { target: st.viz.target } : {}) } : { ...autoViz(st, model), ...st.viz });
  const run = debounce(async () => {
    if (!st.measures.length && !st.dimensions.length) { chart.replaceChildren(h("div", { class: "empty" }, "Pick a measure or a dimension.")); return; }
    status.textContent = "Running…";
    try {
      last = await POST(`/api/models/${model.id}/query`, { spec: spec(), compare: st.compare, forecast: st.forecast ? 3 : 0, anomalies: !!st.anomalies });
      status.textContent = `${fmt(last.row_count)} rows · ${last.elapsed_ms} ms`;
      chart.replaceChildren(renderViz(vizNow(), last, { wide: wideScreen(), onPick: (field, value) => {
        if (!model.dimensions.some((d) => d.name === field && d.type !== "time")) return;
        st.filters.push({ field, op: "in", value: [String(value)] });
        drawFilters(); run();
      } }));
      sqlBox.textContent = last.sql;
      tableBox.replaceChildren(dataTable(last));
    } catch (e) { status.textContent = ""; chart.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  }, 200);

  const pickList = (items, key, render) => h("div", { class: "fieldlist" }, items.map((it) => {
    const cb = h("input", { type: "checkbox", id: `f-${key}-${it.name}`, checked: st[key].includes(it.name) });
    cb.addEventListener("change", () => {
      st[key] = cb.checked ? [...st[key], it.name] : st[key].filter((x) => x !== it.name);
      if (key === "dimensions" && it.type === "time" && cb.checked && !st.grains[it.name]) st.grains[it.name] = "month";
      if (st.limit) { st.limit = undefined; st.sort = []; }  // a "top N" from a question no longer applies once fields change by hand
      if (typeof understood !== "undefined" && understood.childNodes.length) understood.replaceChildren(h("span", { class: "muted" }, "Edited by hand since the question."));
      drawFields(); run();
    });
    return h("label", { class: "fieldrow", for: `f-${key}-${it.name}`, title: it.description || "" }, cb, h("span", null, it.label), render ? render(it) : null);
  }));
  const fields = h("div", { class: "stack" });
  const drawFields = () => fields.replaceChildren(
    h("div", { class: "stack", style: { gap: "4px" } }, h("span", { class: "eyebrow" }, "Measures"), pickList(model.measures, "measures", (m) => h("span", { class: "fmt" }, { currency: "$", percent: "%", integer: "#", number: "#" }[m.format] || ""))),
    h("div", { class: "stack", style: { gap: "4px" } }, h("span", { class: "eyebrow" }, "Dimensions"), pickList(model.dimensions, "dimensions", (d) => d.type === "time" && st.dimensions.includes(d.name)
      ? (() => { const g = h("select", { class: "grain", "aria-label": "Time grain", onclick: (e) => e.stopPropagation() }, GRAINS.map((x) => h("option", { selected: (st.grains[d.name] || "month") === x }, x))); g.addEventListener("change", () => { st.grains[d.name] = g.value; run(); }); return g; })()
      : d.type === "time" ? h("span", { class: "fmt" }, "date") : null)));
  const filtersBox = h("div", { class: "stack", style: { gap: "6px" } });
  const valueCache = {};
  const drawFilters = () => filtersBox.replaceChildren(h("span", { class: "eyebrow" }, "Filters"), ...st.filters.map((f, i) => {
    const fieldSel = h("select", { "aria-label": "Filter field" }, h("option", { value: "" }, "Field…"),
      h("optgroup", { label: "Dimensions" }, model.dimensions.map((d) => h("option", { value: d.name, selected: f.field === d.name }, d.label))),
      h("optgroup", { label: "Measures" }, model.measures.map((m) => h("option", { value: m.name, selected: f.field === m.name }, m.label))));
    const opSel = h("select", { "aria-label": "Operator" }, FILTER_OPS.map(([v, l]) => h("option", { value: v, selected: f.op === v }, l)));
    const isTime = model.dimensions.find((d) => d.name === f.field)?.type === "time";
    let valEl;
    if (f.op === "between") {
      const a = h("input", { type: isTime ? "date" : "text", value: (f.value || [])[0] || "", "aria-label": "From" }), b = h("input", { type: isTime ? "date" : "text", value: (f.value || [])[1] || "", "aria-label": "To" });
      const upd = () => { f.value = [a.value, b.value]; run(); };
      a.addEventListener("change", upd); b.addEventListener("change", upd);
      valEl = h("div", { class: "row", style: { flexWrap: "nowrap" } }, a, b);
    } else if (!f.op.includes("null")) {
      const dl = h("datalist", { id: `vals-${i}` });
      valEl = h("input", { list: `vals-${i}`, type: isTime ? "date" : "text", value: Array.isArray(f.value) ? f.value.join(", ") : f.value ?? "", placeholder: f.op.includes("in") ? "BC, AB" : "value", "aria-label": "Value" });
      valEl.addEventListener("change", () => { f.value = f.op.includes("in") ? valEl.value.split(",").map((x) => x.trim()).filter(Boolean) : (isNaN(+valEl.value) || isTime || valEl.value === "" ? valEl.value : +valEl.value); run(); });
      if (f.field && model.dimensions.some((d) => d.name === f.field && d.type !== "time")) {
        (valueCache[f.field] ||= GET(`/api/models/${model.id}/values?field=${encodeURIComponent(f.field)}`)).then((vals) => dl.replaceChildren(...vals.slice(0, 300).map((v) => h("option", { value: v })))).catch(() => {});
      }
      valEl = h("div", null, valEl, dl);
    }
    fieldSel.addEventListener("change", () => { f.field = fieldSel.value; drawFilters(); run(); });
    opSel.addEventListener("change", () => { f.op = opSel.value; f.value = f.op === "between" ? ["", ""] : f.op.includes("in") ? [] : ""; drawFilters(); run(); });
    return h("div", { class: "filterrow" }, fieldSel, opSel, valEl, h("button", { class: "sm ghost", "aria-label": "Remove filter", onclick: () => { st.filters.splice(i, 1); drawFilters(); run(); } }, "✕"));
  }), h("button", { class: "sm", onclick: () => { st.filters.push({ field: "", op: "in", value: [] }); drawFilters(); } }, "+ Add filter"));

  const vizBar = h("div", { class: "seg", role: "group", "aria-label": "Visual type" });
  const drawVizBar = () => vizBar.replaceChildren(...VIZ_TYPES.map(([k, l]) => h("button", { class: "sm", "aria-pressed": String(st.viz.type === k), onclick: () => { st.viz = { type: k }; drawVizBar(); run(); } }, l)));
  drawVizBar();
  const cmp = h("input", { type: "checkbox", id: "ex-compare", checked: st.compare });
  cmp.addEventListener("change", () => { st.compare = cmp.checked; run(); });
  const fcb = h("input", { type: "checkbox", id: "ex-forecast", checked: !!st.forecast });
  fcb.addEventListener("change", () => { st.forecast = fcb.checked; run(); });
  const anb = h("input", { type: "checkbox", id: "ex-anom", checked: !!st.anomalies });
  anb.addEventListener("change", () => { st.anomalies = anb.checked; run(); });
  const tgt = h("input", { type: "number", step: "any", id: "ex-target", placeholder: "Target", title: "KPI target (shown as a bullet bar on KPI visuals)", style: { width: "96px" }, value: st.viz.target ?? "" });
  tgt.addEventListener("change", () => { st.viz = { ...st.viz, target: tgt.value === "" ? undefined : +tgt.value }; run(); });
  const extraViz = () => ({ forecast: st.forecast ? 3 : undefined, anomalies: st.anomalies || undefined, target: st.viz.target || undefined });

  const saveReport = () => {
    const n = h("input", { id: "rep-name", value: st.editing?.name || "", placeholder: "Revenue by province, last quarter" });
    const viz = vizNow();
    dialog(st.editing ? "Save report" : "Save as report", h("div", { class: "stack" }, field("Name", n), h("p", { class: "small muted" }, `Visual: ${viz.type}. Dashboards that use this report can filter it by the model's dimensions.`)),
      [{ label: st.editing ? "Save changes" : "Save report", run: async () => {
        const body = { name: n.value.trim() || "Untitled report", kind: "model", model_id: model.id, spec: spec(), viz: { ...viz, ...extraViz(), compare: st.compare || undefined, crossfilter: ["bar", "donut", "stacked"].includes(viz.type) || undefined } };
        const q = st.editing ? await PUT(`/api/queries/${st.editing.id}`, body) : await POST("/api/queries", body);
        st.editing = { id: q.id, name: q.name };
        toast("Report saved");
      } }]);
  };
  const addToDashboard = async () => {
    const dashes = await GET("/api/dashboards");
    const sel = h("select", { id: "atd-dash" }, h("option", { value: "new" }, "New dashboard…"), dashes.map((d) => h("option", { value: d.id }, d.name)));
    const nn = h("input", { id: "atd-new", placeholder: "New dashboard name" });
    const n = h("input", { id: "atd-name", placeholder: "Report name" });
    const w = h("select", { id: "atd-w" }, [3, 4, 6, 8, 12].map((x) => h("option", { value: x, selected: x === 6 }, `${x}/12 width`)));
    dialog("Add to dashboard", h("div", { class: "form" }, field("Dashboard", sel), field("New dashboard name", nn), field("Report name", n), field("Tile width", w)), [{ label: "Add", run: async () => {
      const viz = vizNow();
      const q = await POST("/api/queries", { name: n.value.trim() || model.measures.find((m) => m.name === st.measures[0])?.label || "Report", kind: "model", model_id: model.id, spec: spec(),
        viz: { ...viz, ...extraViz(), compare: st.compare || undefined, crossfilter: ["bar", "donut", "stacked"].includes(viz.type) || undefined } });
      let d;
      if (sel.value === "new") d = await POST("/api/dashboards", { name: nn.value.trim() || "My dashboard", tiles: [] });
      else d = await GET(`/api/dashboards/${sel.value}`);
      await PUT(`/api/dashboards/${d.id}`, { tiles: [...d.tiles, { query_id: q.id, w: +w.value }] });
      toast("Added to " + d.name);
      go("#/dashboards/" + d.id);
    } }]);
  };
  const alertFrom = () => {
    const m = model.measures.find((x) => x.name === st.measures[0]);
    if (!m) { toast("Pick a measure first", "bad"); return; }
    alertDialog({ source: { type: "model", model_id: model.id, measure: m.name, filters: spec().filters }, name: `${m.label} alert` });
  };

  const modelSel = h("select", { id: "ex-model", "aria-label": "Semantic model" }, models.map((m) => h("option", { value: m.id, selected: m.id === model.id }, m.name + (m.endorsement === "certified" ? " ✓" : ""))));
  modelSel.addEventListener("change", () => go("#/explore/" + modelSel.value));
  /* ---- ask in plain English (parsed on the server, no AI provider needed) ---- */
  const askIn = h("input", { id: "ex-ask", type: "search", placeholder: "Ask a question, e.g. top 5 provinces by revenue last quarter", autocomplete: "off", "aria-label": "Ask a question" });
  const understood = h("div", { class: "small", "aria-live": "polite" });
  const applyAnswer = (r) => {
    st.measures = r.spec.measures; st.dimensions = r.spec.dimensions; st.grains = r.spec.grains || {}; st.filters = r.spec.filters || [];
    st.sort = r.spec.sort || []; st.limit = r.spec.limit < 5000 ? r.spec.limit : undefined; st.viz = { ...r.viz }; st.compare = !!r.compare; st.forecast = !!r.viz.forecast;
    cmp.checked = st.compare; fcb.checked = st.forecast;
    understood.replaceChildren(h("span", { class: "muted" }, "Understood as: "), h("b", null, r.understood),
      ...(r.notes || []).map((n) => h("span", { class: "chip warn", style: { marginLeft: "6px" } }, n)));
    drawFields(); drawFilters(); drawVizBar(); run();
  };
  const ask = async (btn) => {
    const q = askIn.value.trim();
    if (!q) return;
    const r = await act(btn, () => POST(`/api/models/${model.id}/ask`, { question: q, run: false }));
    applyAnswer(r);
  };
  askIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(null); } });
  if (seed && seed.asked) setTimeout(() => understood.replaceChildren(h("span", { class: "muted" }, "Understood as: "), h("b", null, seed.asked.understood),
    ...(seed.asked.notes || []).map((n) => h("span", { class: "chip warn", style: { marginLeft: "6px" } }, n))), 0);
  drawFields();
  drawFilters();
  main.replaceChildren(
    h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Explore"), h("div", { class: "row" }, h("h1", null, st.editing ? st.editing.name : model.name), endorsementChip(model.endorsement)),
      h("span", { class: "muted small" }, model.description || `Built on ${model.base}`)),
      h("div", { class: "row" }, h("button", { onclick: alertFrom }, "Create alert"),
        h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/models/${model.id}/export`, { spec: spec(), format: "xlsx", name: model.name })) }, "Excel"),
        h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/models/${model.id}/export`, { spec: spec(), format: "csv", name: model.name })) }, "CSV"),
        can("analyst") ? h("button", { onclick: addToDashboard }, "Add to dashboard") : null,
        can("analyst") ? h("button", { class: "primary", onclick: saveReport }, st.editing ? "Save report" : "Save as report") : null)),
    h("div", { class: "explore" },
      h("aside", { class: "card explore-side" }, field("Model", modelSel), fields, filtersBox),
      h("div", { class: "stack", style: { minWidth: 0 } },
        h("div", { class: "card askcard" }, h("div", { class: "row", style: { flexWrap: "nowrap" } }, h("span", { class: "askicon", "aria-hidden": "true" }, "?"), askIn, h("button", { class: "primary", onclick: (e) => ask(e.target) }, "Ask")), understood),
        h("div", { class: "card" }, h("div", { class: "spread" }, vizBar, h("div", { class: "row" }, h("label", { class: "row small", title: "Needs a 'between' date filter" }, cmp, "Compare with previous period"),
          h("label", { class: "row small", title: "Line and area charts over time: Holt forecast with an approximate 95% range" }, fcb, "Forecast"),
          h("label", { class: "row small", title: "Line and area charts over time: mark points far from the trend" }, anb, "Flag anomalies"),
          tgt, status)), chart,
          h("p", { class: "small muted" }, "Tip: click a bar or slice to filter by it.")),
        h("details", { class: "card" }, h("summary", null, "Data"), tableBox),
        h("details", { class: "card" }, h("summary", null, "Generated SQL"), sqlBox))));
  run();
});

/* ---------------- reports library ---------------- */
route("reports", async (main, id) => {
  if (id) return reportView(main, +id);
  const [qs, models] = await Promise.all([GET("/api/queries"), GET("/api/models")]);
  const mname = Object.fromEntries(models.map((m) => [m.id, m.name]));
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Analyze"), h("h1", null, "Reports")),
    h("div", { class: "row" }, h("button", { onclick: () => go("#/sql") }, "New SQL report"), h("button", { class: "primary", onclick: () => go("#/explore") }, "New report"))),
  qs.length ? h("div", { class: "card flat" }, table([
    { label: "Report", render: (q) => h("b", null, q.name) },
    { label: "Built with", render: (q) => q.kind === "model" ? chip("model · " + (mname[q.model_id] || "hidden"), "info") : chip("SQL") },
    { label: "Visual", render: (q) => q.viz?.type || "table" },
    { label: "Owner", render: (q) => h("span", { class: "small muted" }, q.created_by) },
    { label: "Updated", render: (q) => ago(q.updated_at) },
  ], qs, { onclick: (q) => go("#/reports/" + q.id) })) : h("div", { class: "empty" }, "No reports yet. Build one in Explore and save it."));
});

async function reportView(main, id) {
  const q = (await GET("/api/queries")).find((x) => x.id === id);
  if (!q) throw new Error("Report not found");
  const body = h("div", null, h("p", { class: "muted" }, "Running…"));
  const draw = async () => {
    try { const r = await POST(`/api/queries/${id}/run`, {}); body.replaceChildren(renderViz(q.viz, r.result, { wide: wideScreen() }), h("details", { class: "card", style: { marginTop: "12px" } }, h("summary", null, "Data"), dataTable(r.result))); }
    catch (e) { body.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/reports", class: "small" }, "← Reports"), h("h1", null, q.name), h("span", { class: "muted small" }, `${q.kind === "model" ? "Semantic model report" : "SQL report"} · by ${q.created_by}`)),
    h("div", { class: "row" },
      h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/queries/${id}/export`, { format: "xlsx" })) }, "Excel"),
      h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/queries/${id}/export`, { format: "csv" })) }, "CSV"),
      can("analyst") ? h("button", { onclick: () => alertDialog({ source: { type: "query", query_id: id }, name: `${q.name} alert` }) }, "Create alert") : null,
      can("analyst") ? h("button", { onclick: () => {
        if (q.kind === "model") { T.exploreSeed = { model_id: q.model_id, measures: q.spec.measures || [], dimensions: q.spec.dimensions || [], grains: q.spec.grains || {}, filters: q.spec.filters || [], sort: q.spec.sort || [], viz: { type: q.viz?.type || "auto" }, compare: !!q.viz?.compare, forecast: !!q.viz?.forecast, anomalies: !!q.viz?.anomalies, editing: { id: q.id, name: q.name } }; go("#/explore/" + q.model_id); }
        else { try { sessionStorage.setItem("tessera.sql", q.sql); } catch {} go("#/sql"); }
      } }, "Edit") : null,
      can("analyst") ? h("button", { class: "danger", onclick: () => confirmBox(`Delete report ${q.name}? Dashboard tiles using it will show as missing.`, "Delete", async () => { await DEL(`/api/queries/${id}`); go("#/reports"); }) }, "Delete") : null)),
  h("div", { class: "card" }, body));
  draw();
}

/* ---------------- semantic models ---------------- */
route("models", async (main, id, sub) => {
  if (id === "new" || sub === "edit") return modelEditor(main, id === "new" ? null : +id);
  const models = await GET("/api/models");
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Data"), h("h1", null, "Semantic models"),
    h("span", { class: "muted small" }, "Define business measures and dimensions once. Explore, dashboards, alerts and AI agents all use the same definitions.")),
  can("engineer") ? h("button", { class: "primary", onclick: () => go("#/models/new") }, "New model") : null),
  models.length ? h("div", { class: "grid2" }, models.map((m) => h("div", { class: "card" },
    h("div", { class: "spread" }, h("h2", null, m.name), endorsementChip(m.endorsement)),
    h("p", { class: "small muted" }, m.description || "No description"),
    h("div", { class: "small" }, h("span", { class: "mono" }, m.base), m.joins.length ? ` + ${m.joins.length} join${m.joins.length > 1 ? "s" : ""}` : ""),
    h("div", { class: "row small" }, chip(`${m.measures.length} measures`), chip(`${m.dimensions.length} dimensions`)),
    h("div", { class: "small muted" }, m.measures.slice(0, 6).map((x) => x.label).join(" · ")),
    h("div", { class: "row" }, h("button", { class: "primary sm", onclick: () => go("#/explore/" + m.id) }, "Explore"),
      can("engineer") ? h("button", { class: "sm", onclick: () => go(`#/models/${m.id}/edit`) }, "Edit") : null,
      can("engineer") && m.endorsement !== "promoted" && m.endorsement !== "certified" ? h("button", { class: "sm", onclick: (e) => act(e.target, () => PATCH(`/api/models/${m.id}/endorsement`, { endorsement: "promoted" }), "Promoted").then(render) }, "Promote") : null,
      can("admin") && m.endorsement !== "certified" ? h("button", { class: "sm", onclick: (e) => act(e.target, () => PATCH(`/api/models/${m.id}/endorsement`, { endorsement: "certified" }), "Certified").then(render) }, "Certify") : null)))) :
    h("div", { class: "empty" }, "No models yet."));
});

async function modelEditor(main, id) {
  if (!can("engineer")) throw new Error("Only engineers edit semantic models.");
  const dsets = await GET("/api/datasets");
  let m = id ? await GET(`/api/models/${id}`) : { name: "", description: "", base: "", joins: [], dimensions: [], measures: [] };
  m = JSON.parse(JSON.stringify(m));
  const colCache = {};
  const colsOf = async (fq) => {
    if (!fq) return [];
    if (!colCache[fq]) { const d = dsets.find((x) => fqn(x) === fq); colCache[fq] = d ? GET(`/api/datasets/${d.id}`).then((x) => x.columns.map((c) => c.name)) : Promise.resolve([]); }
    return colCache[fq];
  };
  const name = h("input", { id: "me-name", value: m.name, placeholder: "Sales" });
  const desc = h("input", { id: "me-desc", value: m.description || "", placeholder: "What the numbers mean and who owns them" });
  const base = h("select", { id: "me-base" }, h("option", { value: "" }, "Choose a table…"), dsets.map((d) => h("option", { value: fqn(d), selected: fqn(d) === m.base }, fqn(d))));
  const body = h("div", { class: "stack" });
  const allCols = async () => {
    const out = (await colsOf(m.base)).map((c) => c);
    for (const j of m.joins) (await colsOf(j.table)).forEach((c) => out.push(`${j.alias}.${c}`));
    return out;
  };
  const sel = (opts, value, onchange, label) => { const s2 = h("select", { "aria-label": label }, opts.map((o) => Array.isArray(o) ? h("option", { value: o[0], selected: o[0] === value }, o[1]) : h("option", { selected: o === value }, o))); s2.addEventListener("change", () => onchange(s2.value)); return s2; };
  const inp = (obj, key, ph, label) => { const i = h("input", { value: obj[key] ?? "", placeholder: ph || "", "aria-label": label || key }); i.addEventListener("change", () => (obj[key] = i.value)); return i; };
  const draw = async () => {
    const cols = await allCols();
    const tables = dsets.map((d) => fqn(d));
    body.replaceChildren(
      h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", null, "Joins"), h("button", { class: "sm", onclick: () => { m.joins.push({ table: "", alias: `j${m.joins.length + 1}`, left_key: "", right_key: "" }); draw(); } }, "+ Join a lookup table")),
        m.joins.length ? table([
          { label: "Table", render: (j) => sel(["", ...tables], j.table, (v) => { j.table = v; draw(); }, "Joined table") },
          { label: "Alias", render: (j) => inp(j, "alias", "c", "Alias") },
          { label: "Base key", render: (j) => sel(["", ...cols.filter((c) => !c.includes("."))], j.left_key, (v) => (j.left_key = v), "Base key") },
          { label: "Lookup key", render: (j) => { const s2 = sel([""], j.right_key, (v) => (j.right_key = v), "Lookup key"); colsOf(j.table).then((cs) => s2.replaceChildren(...["", ...cs].map((c) => h("option", { selected: c === j.right_key }, c)))); return s2; } },
          { label: "", render: (j) => h("button", { class: "sm danger", onclick: () => { m.joins = m.joins.filter((x) => x !== j); draw(); } }, "Remove") },
        ], m.joins) : h("p", { class: "small muted" }, "Optional. Left joins add columns from lookup tables, such as customers or products.")),
      h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", null, "Measures"), h("button", { class: "sm", onclick: () => { m.measures.push({ name: `measure_${m.measures.length + 1}`, label: "", agg: "sum", column: "", format: "number", decimals: 2 }); draw(); } }, "+ Measure")),
        table([
          { label: "Key", render: (x) => inp(x, "name", "revenue", "Measure key") },
          { label: "Label", render: (x) => inp(x, "label", "Revenue", "Measure label") },
          { label: "Calculation", render: (x) => sel([["sum", "Sum"], ["avg", "Average"], ["count", "Count"], ["count_distinct", "Distinct count"], ["min", "Min"], ["max", "Max"], ["median", "Median"], ["count_rows", "Row count"], ["custom", "Custom SQL"]], x.agg, (v) => { x.agg = v; draw(); }, "Calculation") },
          { label: "Column or expression", render: (x) => x.agg === "custom" ? inp(x, "expr", "sum(order_total) FILTER (WHERE status = 'paid')", "Expression") : x.agg === "count_rows" ? h("span", { class: "muted small" }, "all rows") : sel(["", ...cols], x.column, (v) => (x.column = v), "Column") },
          { label: "Format", render: (x) => sel(["number", "integer", "currency", "percent"], x.format, (v) => (x.format = v), "Format") },
          { label: "Description", render: (x) => inp(x, "description", "What it means", "Measure description") },
          { label: "Better when", render: (x) => sel([["up", "higher"], ["down", "lower"]], x.direction || "up", (v) => (x.direction = v), "Better when") },
          { label: "Sums over time", render: (x) => ["sum", "count", "count_rows"].includes(x.agg) ? h("span", { class: "muted small", title: "Sums and counts always add up across periods" }, "yes")
            : (() => { const cb = h("input", { type: "checkbox", checked: !!x.additive_over_time, "aria-label": "Adds up across periods", title: "Tick when monthly values add up to the year, e.g. a distinct count of order IDs (each order has one date). Enables full-year forecasts and targets." });
              cb.addEventListener("change", () => (x.additive_over_time = cb.checked)); return cb; })() },
          { label: "", render: (x) => h("button", { class: "sm danger", onclick: () => { m.measures = m.measures.filter((y) => y !== x); draw(); } }, "✕") },
        ], m.measures)),
      h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", null, "Dimensions"), h("button", { class: "sm", onclick: () => { m.dimensions.push({ name: `dim_${m.dimensions.length + 1}`, label: "", column: "", type: "category" }); draw(); } }, "+ Dimension")),
        table([
          { label: "Key", render: (x) => inp(x, "name", "province", "Dimension key") },
          { label: "Label", render: (x) => inp(x, "label", "Province", "Dimension label") },
          { label: "Column", render: (x) => sel(["", ...cols], x.column, (v) => (x.column = v), "Column") },
          { label: "Type", render: (x) => sel([["category", "Category"], ["time", "Date / time"], ["number", "Number"]], x.type, (v) => (x.type = v), "Type") },
          { label: "", render: (x) => h("button", { class: "sm danger", onclick: () => { m.dimensions = m.dimensions.filter((y) => y !== x); draw(); } }, "✕") },
        ], m.dimensions)));
  };
  base.addEventListener("change", () => { m.base = base.value; draw(); });
  const draft = async (e) => {
    if (!base.value) { toast("Choose a base table first", "bad"); return; }
    const d = await act(e.target, () => POST("/api/models/suggest", { base: base.value }));
    m = { ...d, name: name.value || d.name, description: desc.value };
    name.value = m.name;
    draw();
    toast(`Drafted ${d.measures.length} measures and ${d.dimensions.length} dimensions. Review, then save.`);
  };
  const save = async (e) => {
    const bodyM = { ...m, name: name.value.trim(), description: desc.value, base: base.value };
    const r = await act(e.target, () => (id ? PUT(`/api/models/${id}`, bodyM) : POST("/api/models", bodyM)), "Model saved");
    go("#/explore/" + r.id);
  };
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/models", class: "small" }, "← Semantic models"), h("h1", null, id ? "Edit model" : "New semantic model")),
    h("div", { class: "row" }, id ? h("button", { class: "danger", onclick: () => confirmBox("Delete this model?", "Delete", async () => { await DEL(`/api/models/${id}`); go("#/models"); }) }, "Delete") : null, h("button", { class: "primary", onclick: save }, "Save and explore"))),
  h("div", { class: "card" }, h("div", { class: "form" }, field("Name", name), field("Base table", base, "Use a serving table so analysts and viewers can use the model."), field("Description", desc)),
    h("div", null, h("button", { onclick: draft }, "Draft measures and dimensions from the table"))), body);
  draw();
}
