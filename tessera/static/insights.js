"use strict";
/* AI-assisted analysis on dashboards: automated insights, explain-a-change, decomposition trees, Ask AI, saved views. */

const signed = (v, f) => (v == null ? "—" : (v >= 0 ? "+" : "−") + fmtVal(Math.abs(v), f));
const SEV = { bad: ["bad", "Needs attention"], warn: ["warn", "Unusual"], good: ["ok", "Good news"], info: ["info", "Context"] };
const KIND_LABEL = { change: "Latest change", driver: "Driver", trend: "Trend", anomaly: "Anomaly", forecast: "Forecast", concentration: "Concentration" };

function groundingBadge(g) {
  if (!g || !g.figures) return null;
  if (!g.unverified.length) return h("span", { class: "chip ok", title: "Every number in this answer was found in data the agent read in this turn." }, `✓ ${g.figures} figure${g.figures > 1 ? "s" : ""} traced to data`);
  return h("span", { class: "chip warn", title: "These figures were not found in any tool result. They may be the model's own arithmetic or a mistake. Check before you use them." },
    `${g.unverified.length} of ${g.figures} figures not found in data: ${g.unverified.slice(0, 4).join(", ")}${g.unverified.length > 4 ? "…" : ""}`);
}

/* ---------------- insights panel ---------------- */
function insightsPanel(dash, getFilters, onExplain) {
  const box = h("div", { class: "stack" }, h("div", { class: "skeleton" }));
  const narr = h("div");
  let facts = [];
  const load = async () => {
    box.replaceChildren(h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
    try {
      const r = await POST(`/api/dashboards/${dash.id}/insights`, { filters: getFilters() });
      facts = r.facts;
      const notes = r.models.flatMap((m) => Object.values(m.series).flatMap((s) => [s.partial_note, s.future_note])).filter(Boolean);
      box.replaceChildren(
        facts.length ? facts.map((f) => factCard(f, onExplain)) : h("p", { class: "muted small" }, "No insights for the current filters. Insights need a semantic model with a time dimension and at least two complete periods."),
        [...new Set(notes)].map((n) => h("p", { class: "small muted" }, n)),
        r.errors.length ? h("div", { class: "notice warn small" }, r.errors.join(" · ")) : null,
        h("details", { class: "small" }, h("summary", null, "How these are calculated"), h("pre", { class: "log", style: { whiteSpace: "pre-wrap" } }, r.methods)));
    } catch (e) { box.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  const summarize = async (btn) => {
    narr.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const r = await act(btn, () => POST(`/api/dashboards/${dash.id}/insights`, { filters: getFilters(), narrative: true }));
      if (r.narrative_error) { narr.replaceChildren(h("div", { class: "notice bad small" }, r.narrative_error)); return; }
      if (!r.narrative) { narr.replaceChildren(h("p", { class: "small muted" }, "Nothing to summarise.")); return; }
      narr.replaceChildren(h("div", { class: "card ai" }, h("span", { class: "eyebrow" }, "AI summary · grounded in the facts below"), md(r.narrative.text),
        r.narrative.unknown_citations.length ? h("div", { class: "notice warn small" }, `The model cited facts that do not exist (${r.narrative.unknown_citations.join(", ")}). Treat those lines with care.`) : null));
    } catch (e) { narr.replaceChildren(h("div", { class: "notice bad small" }, e.message)); }
  };
  load();
  return { el: h("div", { class: "stack" }, h("div", { class: "spread" }, h("h3", null, "Insights"), h("div", { class: "row" },
    h("button", { class: "sm", onclick: load }, "Refresh"), h("button", { class: "sm primary", onclick: (e) => summarize(e.target), title: "Uses your configured AI provider. Only the facts are sent, never rows." }, "Summarise with AI"))),
  narr, box), reload: load };
}

function factCard(f, onExplain) {
  const [kind, label] = SEV[f.severity] || SEV.info;
  return h("div", { class: "fact " + kind },
    h("div", { class: "spread" }, h("span", { class: "eyebrow" }, `${f.id} · ${KIND_LABEL[f.kind] || f.kind}`), chip(label, kind)),
    h("b", null, f.title), h("p", { class: "small" }, f.text),
    ["change", "driver"].includes(f.kind) && onExplain ? h("button", { class: "sm ghost", onclick: () => onExplain(f.model_id, f.measure) }, "Explain this change →") : null);
}

/* ---------------- explain a change ---------------- */
async function explainDialog(modelId, measure, filters, model) {
  const out = h("div", { class: "stack" }, h("div", { class: "skeleton" }));
  const grain = h("select", { "aria-label": "Compare" }, [["month", "Last month vs previous"], ["quarter", "Last quarter vs previous"], ["week", "Last week vs previous"], ["year", "Last year vs previous"]].map(([v, l]) => h("option", { value: v }, l)));
  const d = dialog(`Why did ${model?.measures.find((m) => m.name === measure)?.label || measure} change?`, h("div", { class: "stack" }, h("div", { class: "row" }, grain), out), []);
  d.el.classList.add("wide");
  const load = async () => {
    out.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const r = await POST(`/api/models/${modelId}/explain`, { measure, grain: grain.value, filters });
      const f = { format: r.format, decimals: r.decimals };
      const ch = signed(r.change, f);
      out.replaceChildren(
        h("div", { class: "grid3" }, kpiMini(r.baseline.label, fmtVal(r.baseline.value, f)), kpiMini(r.current.label, fmtVal(r.current.value, f)),
          kpiMini("Change", ch + (r.pct_change != null && r.format !== "percent" ? ` (${(r.pct_change * 100).toFixed(1)}%)` : ""))),
        r.summary ? h("div", { class: "notice" }, r.summary) : null,
        h("p", { class: "small muted" }, r.method),
        r.dimensions.map((dim) => h("div", { class: "card flat" }, h("div", { class: "spread" }, h("b", null, `By ${dim.label}`), h("span", { class: "small muted" }, `${dim.member_count} members`)),
          waterfall(dim.members, f, r.additive))));
    } catch (e) { out.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  grain.addEventListener("change", load);
  load();
}
const kpiMini = (label, value) => h("div", { class: "card flat" }, h("span", { class: "eyebrow" }, label), h("div", { class: "kpival", style: { fontSize: "1.3rem" } }, value));

function waterfall(members, f, additive) {
  const max = Math.max(...members.map((m) => Math.abs(m.change || 0)), 1e-9);
  return h("div", { class: "wf" }, members.map((m) => {
    const w = Math.abs(m.change) / max * 50;
    return h("div", { class: "wfrow" }, h("span", { class: "small", title: m.member }, m.member),
      h("div", { class: "wfbar" }, h("i", { class: m.change >= 0 ? "up" : "down", style: { width: w + "%", [m.change >= 0 ? "left" : "right"]: "50%" } })),
      h("span", { class: "small num" }, signed(m.change, f)),
      h("span", { class: "small muted num" }, additive && m.share_of_change != null ? `${(m.share_of_change * 100).toFixed(0)}% of change` : `${fmtVal(m.baseline, f)} → ${fmtVal(m.current, f)}`));
  }));
}

/* ---------------- decomposition tree ---------------- */
async function decompositionDialog(model, measure, filters) {
  const meas = model.measures.find((m) => m.name === measure) || model.measures[0];
  const msel = h("select", { "aria-label": "Measure" }, model.measures.map((m) => h("option", { value: m.name, selected: m.name === meas.name }, m.label)));
  const cols = h("div", { class: "dtree" });
  const levels = []; // [{node, path, chosen}]
  const d = dialog("Deep dive", h("div", { class: "stack" }, h("div", { class: "row" }, field("Measure", msel), h("p", { class: "small muted", style: { maxWidth: "460px" } }, "Pick a value to split it further. Choose a dimension, or let Tessera pick the split where one value stands out most (high or low).")), cols), []);
  d.el.classList.add("wide");
  const fieldOf = () => ({ format: model.measures.find((m) => m.name === msel.value).format, decimals: model.measures.find((m) => m.name === msel.value).decimals });
  const loadLevel = async (i, path, dimension, mode) => {
    levels.length = i;
    const node = await POST(`/api/models/${model.id}/decompose`, { measure: msel.value, path, dimension, mode, filters });
    levels[i] = { node, path };
    draw();
  };
  const draw = () => {
    const ff = fieldOf();
    cols.replaceChildren(h("div", { class: "dcol" }, h("span", { class: "eyebrow" }, "Total"), h("div", { class: "dnode on" }, h("b", null, fmtVal(levels[0]?.node.total, ff)), h("span", { class: "small muted" }, msel.selectedOptions[0].textContent))),
      levels.map((lv, i) => {
        const n = lv.node;
        if (!n.dimension) return h("div", { class: "dcol" }, h("p", { class: "small muted" }, "No more dimensions to split by."));
        const max = Math.max(...n.children.map((c) => Math.abs(c.value || 0)), 1e-9);
        const nextPath = (c) => [...lv.path, { field: n.dimension, value: c.member }];
        const splitter = (c) => {
          const s = h("select", { "aria-label": "Split next level", class: "sm" }, h("option", { value: "" }, "Split by…"), h("option", { value: "@high" }, "✦ Highest value"), h("option", { value: "@low" }, "✦ Lowest value"),
            n.remaining.map((r) => h("option", { value: r }, model.dimensions.find((x) => x.name === r)?.label || r)));
          s.addEventListener("change", () => { const v = s.value; if (!v) return; act(null, () => loadLevel(i + 1, nextPath(c), v.startsWith("@") ? undefined : v, v === "@low" ? "low" : "high")); });
          return s;
        };
        const chosen = levels[i + 1]?.path?.[lv.path.length]?.value;
        return h("div", { class: "dcol" }, h("span", { class: "eyebrow" }, n.dimension_label), n.why ? h("span", { class: "small muted" }, n.why) : null,
          n.children.map((c) => h("div", { class: "dnode" + (chosen === c.member ? " on" : "") },
            h("div", { class: "spread" }, h("span", { class: "small", title: c.member }, c.member), h("b", { class: "small" }, fmtVal(c.value, ff))),
            h("div", { class: "meter" }, h("i", { style: { width: Math.abs(c.value || 0) / max * 100 + "%" } })),
            c.share != null ? h("span", { class: "small muted" }, `${(c.share * 100).toFixed(1)}% of parent`) : null,
            !c.other && n.remaining.length ? splitter(c) : null)));
      }));
  };
  msel.addEventListener("change", () => act(null, () => loadLevel(0, [], undefined, "high")));
  try { await loadLevel(0, [], undefined, "high"); } catch (e) { cols.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
}

/* ---------------- ask AI about this dashboard ---------------- */
function askPanel(dash, getFilters) {
  let sid = null;
  const log = h("div", { class: "chat", "aria-live": "polite", style: { maxHeight: "460px" } },
    h("p", { class: "small muted" }, "Ask about this dashboard. The analyst agent sees the dashboard's measures and your current filters, uses governed models first, and works with your permissions."));
  const msg = h("textarea", { rows: 2, id: "ask-msg", placeholder: "Why did revenue drop last month?" });
  const send = async (btn) => {
    const text = msg.value.trim();
    if (!text) return;
    msg.value = "";
    log.append(h("div", { class: "msg user" }, h("div", { class: "text" }, text)));
    const wait = h("div", { class: "msg bot" }, h("div", { class: "text muted" }, "Analysing…"));
    log.append(wait);
    log.scrollTop = log.scrollHeight;
    try {
      const r = await act(btn, () => POST(`/api/dashboards/${dash.id}/ask`, { message: text, session_id: sid, filters: getFilters() }));
      sid = r.session_id;
      wait.replaceWith(h("div", { class: "msg bot" }, md(r.reply || "(no answer)"), h("div", { class: "row" }, groundingBadge(r.grounding)),
        r.trace.length ? h("details", { class: "trace" }, h("summary", null, `${r.trace.length} step${r.trace.length > 1 ? "s" : ""}: ${r.trace.map((t) => t.tool).join(", ")}`),
          r.trace.map((t) => h("div", null, h("b", null, (t.ok ? "✓ " : "✕ ") + t.tool), h("pre", null, JSON.stringify(t.input, null, 1))))) : null));
    } catch (e) { wait.replaceWith(h("div", { class: "msg bot" }, h("div", { class: "text", style: { color: "var(--bad)" } }, e.message))); }
    log.scrollTop = log.scrollHeight;
  };
  msg.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(null); } });
  const quick = ["What changed most last month?", "Anything unusual I should know?", "What's the forecast for next month?"].map((q) => h("button", { class: "sm ghost", onclick: () => { msg.value = q; send(null); } }, q));
  return h("div", { class: "stack" }, h("div", { class: "spread" }, h("h3", null, "Ask AI"), h("button", { class: "sm ghost", onclick: () => { sid = null; log.replaceChildren(); } }, "New chat")),
    log, h("div", { class: "row" }, quick), msg, h("button", { class: "primary", onclick: (e) => send(e.target) }, "Ask"));
}

/* ---------------- saved views ---------------- */
function viewsMenu(dash, getState, applyState) {
  const menu = h("div", { class: "menu" });
  const det = h("details", { class: "tilemenu viewsmenu" }, h("summary", { class: "btn" }, "Views ▾"), menu);
  det.addEventListener("toggle", async () => {
    if (!det.open) return;
    const vs = await GET(`/api/dashboards/${dash.id}/views`).catch(() => []);
    menu.replaceChildren(vs.length ? vs.map((v) => h("div", { class: "spread" },
      h("button", { class: "ghost", onclick: () => { applyState(v.state); det.open = false; toast(`View “${v.name}” applied`); } }, v.name, v.shared ? " · shared" : ""),
      v.user_id === T.me.id || can("admin") ? h("button", { class: "sm ghost", "aria-label": "Delete view", onclick: async () => { await DEL(`/api/dashboards/${dash.id}/views/${v.id}`); det.open = false; toast("View deleted"); } }, "✕") : null))
      : h("p", { class: "small muted", style: { padding: "6px 10px" } }, "No saved views yet."),
    h("button", { class: "ghost", onclick: () => {
      det.open = false;
      const n = h("input", { id: "view-name", placeholder: "BC, last quarter" });
      const sh = h("input", { type: "checkbox", id: "view-shared" });
      dialog("Save current view", h("div", { class: "stack" }, field("Name", n), h("label", { class: "row small" }, sh, "Share with everyone who can open this dashboard")),
        [{ label: "Save view", run: async () => { await POST(`/api/dashboards/${dash.id}/views`, { name: n.value, state: getState(), shared: sh.checked }); toast("View saved"); } }]);
    } }, "+ Save current view"));
  });
  return det;
}
