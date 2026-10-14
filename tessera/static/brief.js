"use strict";
/* Executive brief: leadership view of a dashboard's measures with year-over-year, year to date, full-year landing
   forecast, target likelihood, what-if adjustment, drivers and segments. */

const ncdf = (x) => { // standard normal CDF (Abramowitz-Stegun 7.1.26)
  const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-(x * x) / 2);
  return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
};
const pctTxt = (p, d = 1) => (p == null ? "—" : `${p >= 0 ? "+" : "−"}${Math.abs(p * 100).toFixed(d)}%`);
const STATUS = { on_track: ["ok", "On track"], at_risk: ["warn", "At risk"], off_track: ["bad", "Off track"] };

route("brief", async (main, id) => {
  const d = await GET(`/api/dashboards/${id}`);
  let grain = "month";
  const body = h("div", { class: "stack" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
  const narr = h("div");
  const filters = (() => { try { return briefFilters(JSON.parse(sessionStorage.getItem(`tessera.dash.${id}`) || "{}"), d); } catch { return []; } })();
  let data = null;
  const load = async () => {
    body.replaceChildren(h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
    try {
      data = await POST(`/api/dashboards/${id}/brief`, { filters, grain });
      draw();
    } catch (e) { body.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  const draw = () => {
    const ms = data.sections.flatMap((s) => s.measures.map((m) => ({ ...m, model_id: s.model_id })));
    if (!ms.length) { body.replaceChildren(h("div", { class: "empty" }, "No measures to brief on. The dashboard needs tiles built on a semantic model with a time dimension."), data.errors.length ? h("div", { class: "notice warn" }, data.errors.join(" · ")) : null); return; }
    const top = data.headlines.slice(0, 6), rest = data.headlines.slice(6);
    body.replaceChildren(
      h("section", { class: "card brief-heads" }, h("span", { class: "eyebrow" }, `What to know · as of ${new Date(data.as_of + "T12:00:00").toLocaleDateString(undefined, { dateStyle: "medium" })}`),
        h("ol", null, top.map(headLi)),
        rest.length ? h("details", { class: "noprint-open" }, h("summary", { class: "small" }, `${rest.length} more`), h("ol", { start: 7 }, rest.map(headLi))) : null),
      narr,
      h("div", { class: "brief-grid" }, ms.map((m) => scorecard(m))),
      data.errors.length ? h("div", { class: "notice warn small" }, data.errors.join(" · ")) : null,
      h("details", { class: "small card flat", style: { padding: "10px 14px" } }, h("summary", null, "How these numbers are produced"), h("pre", { class: "log", style: { whiteSpace: "pre-wrap" } }, data.methods)));
  };
  const summarize = async (btn) => {
    narr.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const r = await act(btn, () => POST(`/api/dashboards/${id}/brief`, { filters, grain, narrative: true }));
      if (r.narrative_error || !r.narrative) { narr.replaceChildren(h("div", { class: "notice bad small" }, r.narrative_error || "Nothing to summarise.")); return; }
      narr.replaceChildren(h("div", { class: "card ai" }, h("span", { class: "eyebrow" }, "AI summary · written only from the numbered statements above"), md(r.narrative.text),
        r.narrative.unknown_citations.length ? h("div", { class: "notice warn small" }, `Cites statements that do not exist (${r.narrative.unknown_citations.join(", ")}); check those lines.`) : null));
    } catch (e) { narr.replaceChildren(h("div", { class: "notice bad small" }, e.message)); }
  };
  const copy = () => copyText(`${d.name} — executive brief (${data?.as_of || ""})\n` + (data?.headlines || []).map((x) => `• ${x.text}`).join("\n"));
  const gsel = h("select", { "aria-label": "Period", id: "brief-grain", style: { width: "auto" } }, [["month", "Monthly"], ["quarter", "Quarterly"]].map(([v, l]) => h("option", { value: v }, l)));
  gsel.addEventListener("change", () => { grain = gsel.value; load(); });
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/dashboards/" + id, class: "small noprint" }, "← " + d.name), h("h1", null, "Executive brief"),
    h("span", { class: "muted small" }, d.name + (filters.length ? ` · ${filters.length} filter${filters.length > 1 ? "s" : ""} from the dashboard applied` : ""))),
  h("div", { class: "row noprint" }, gsel,
    can("analyst") ? h("button", { class: "ai-btn", onclick: (e) => summarize(e.target) }, "✦ Summarise with AI") : null,
    h("button", { onclick: copy }, "Copy summary"),
    h("button", { onclick: () => window.print() }, "Print / PDF"),
    can("analyst") ? h("button", { onclick: () => emailBrief(d, filters) }, "Email weekly") : null,
    can("analyst") ? h("button", { onclick: () => targetsDialog(data, load) }, "Targets") : null)),
  body);
  load();
});

function briefFilters(state, d) {
  const out = [];
  (d.filters || []).forEach((f, i) => {
    const v = (state.values || {})[i];
    if (f.type === "select" && v && v.length) out.push({ model_id: f.model_id, field: f.field, op: "in", value: v });
  });
  (state.cross || []).forEach((c) => out.push({ model_id: c.model_id, field: c.field, op: "in", value: [c.value] }));
  return out;
}

function headLi(x) {
  const k = { good: "ok", bad: "bad", warn: "warn", info: "info" }[x.severity] || "info";
  return h("li", { class: "head-" + k }, h("span", { class: "dot " + k, "aria-hidden": "true" }), h("span", null, x.text), h("span", { class: "hid mono small muted" }, x.id));
}

function scorecard(m) {
  const f = { format: m.format, decimals: m.decimals };
  const goodDir = (delta) => (delta == null ? "" : (delta > 0) === (m.direction === "up") ? "ok" : "bad");
  const delta = (lbl, o) => o && o.pct != null ? h("span", { class: "chip " + goodDir(o.delta ?? o.pct) },
    m.format === "percent" && o.delta != null ? `${o.delta >= 0 ? "+" : "−"}${Math.abs(o.delta * 100).toFixed(1)} pts ${lbl}` : `${pctTxt(o.pct)} ${lbl}`) : null;
  const card = h("article", { class: "card scorecard" });
  const landingBox = h("div");
  const L = m.landing, T = m.target;
  const drawLanding = (adj) => {
    if (!L) {
      landingBox.replaceChildren(h("div", { class: "landing" }, h("span", { class: "eyebrow" }, `Forecast · ${m.next.label}`),
        h("div", { class: "landval" }, fmtVal(m.next.value, f)), h("span", { class: "small muted" }, `likely ${fmtVal(m.next.lower, f)} to ${fmtVal(m.next.upper, f)}`),
        T ? h("div", { class: "row" }, chip(STATUS[T.status][1], STATUS[T.status][0]), h("span", { class: "small muted" }, `target ${fmtVal(T.value, f)} · ${fmtVal(m.ytd.value, f)} year to date`)) : null));
      return;
    }
    const rem = L.forecast_remaining * (1 + adj);
    const val = L.actual_to_date + rem;
    const sd = L.sd * (1 + Math.abs(adj));
    let prob = null, status = null;
    if (T && T.probability != null) {
      const z = sd ? (val - T.value) / sd : (val >= T.value ? 9 : -9);
      prob = m.direction === "up" ? ncdf(z) : 1 - ncdf(z);
      status = prob >= 0.7 ? "on_track" : prob >= 0.3 ? "at_risk" : "off_track";
    }
    const scaleMax = Math.max(val + 1.96 * sd, T ? T.value : 0, L.prior_year || 0) * 1.04 || 1;
    const pc = (v) => `${Math.max(0, Math.min(100, (v / scaleMax) * 100))}%`;
    landingBox.replaceChildren(h("div", { class: "landing" },
      h("div", { class: "spread" }, h("span", { class: "eyebrow" }, `${L.label} full-year forecast`), prob != null ? chip(`${STATUS[status][1]} · ${(prob * 100).toFixed(0)}% likely`, STATUS[status][0]) : null),
      h("div", { class: "landval" }, fmtVal(val, f), h("span", { class: "small muted" }, ` ${pctTxt(L.prior_year ? (val - L.prior_year) / Math.abs(L.prior_year) : null)} vs ${fmtVal(L.prior_year, f)} last year`)),
      h("div", { class: "landbar", role: "img", "aria-label": `Actual ${fmtVal(L.actual_to_date, f)}, forecast ${fmtVal(rem, f)}${T ? `, target ${fmtVal(T.value, f)}` : ""}` },
        h("i", { class: "act", style: { width: pc(L.actual_to_date) } }),
        h("i", { class: "fc", style: { left: pc(L.actual_to_date), width: `calc(${pc(val)} - ${pc(L.actual_to_date)})` } }),
        h("i", { class: "range", style: { left: pc(val - 1.96 * sd), width: `calc(${pc(val + 1.96 * sd)} - ${pc(val - 1.96 * sd)})` } }),
        T ? h("b", { class: "tgt", style: { left: pc(T.value) }, title: `Target ${fmtVal(T.value, f)}` }) : null),
      h("div", { class: "row small muted landlegend" }, h("span", null, h("i", { class: "sw act" }), `${fmtVal(L.actual_to_date, f)} actual`), h("span", null, h("i", { class: "sw fc" }), `${fmtVal(rem, f)} forecast, ${L.remaining_periods} ${m.grain}${L.remaining_periods === 1 ? "" : "s"}`),
        T ? h("span", null, h("i", { class: "sw tgt" }), `target ${fmtVal(T.value, f)}`) : null),
      h("span", { class: "small muted" }, `Likely range ${fmtVal(val - 1.96 * sd, f)} to ${fmtVal(val + 1.96 * sd, f)}.` + (T && T.required_per_period != null && adj === 0 ? ` Target needs ${fmtVal(T.required_per_period, f)} per ${m.grain}; forecast run rate ${fmtVal(T.forecast_per_period, f)}.` : ""))));
  };
  const slider = L ? h("input", { type: "range", min: -20, max: 20, step: 1, value: 0, "aria-label": "What-if adjustment to the remaining forecast", class: "noprint" }) : null;
  const sliderLbl = h("span", { class: "small mono" }, "0%");
  if (slider) slider.addEventListener("input", () => { sliderLbl.textContent = `${slider.value > 0 ? "+" : ""}${slider.value}%`; drawLanding(+slider.value / 100); });
  drawLanding(0);
  const acc = m.forecast.accuracy;
  const dr = m.drivers?.dimensions?.[0];
  card.append(
    h("div", { class: "spread" }, h("h3", null, m.label), h("span", { class: "small muted" }, m.current.label)),
    h("div", { class: "kpival" }, fmtVal(m.current.value, f)),
    h("div", { class: "row" }, delta("vs " + m.vs_prior.label, m.vs_prior), delta("vs last year", m.vs_last_year), m.additive ? delta("YTD", m.ytd) : null,
      m.extreme ? chip(`${m.extreme.kind} in ${m.extreme.periods} ${m.grain}s`, (m.extreme.kind === "highest") === (m.direction === "up") ? "ok" : "bad") : null),
    trendChart(m, f),
    landingBox,
    slider ? h("label", { class: "row small whatif noprint" }, "What if the rest of the year runs", slider, sliderLbl, "vs forecast") : null,
    h("p", { class: "small muted" }, `${m.forecast.method_label}` + (acc && acc.mape != null ? ` · was off by ${(acc.mape * 100).toFixed(1)}% on average over the last ${acc.holdout_periods} ${m.grain}s when tested` : "") +
      (acc && acc.vs_naive != null && acc.vs_naive > 0 ? `, ${(acc.vs_naive * 100).toFixed(0)}% better than repeating the last value` : "") + "."),
    m.pace ? h("p", { class: "small" }, h("b", null, `${m.pace.label} so far: `), `${fmtVal(m.pace.to_date, f)} with ${(m.pace.elapsed * 100).toFixed(0)}% of the ${m.grain} gone (straight-line pace ${fmtVal(m.pace.projected, f)}).`) : null,
    dr && dr.members.length ? h("div", { class: "small" }, h("b", null, `Biggest change vs ${m.drivers.baseline}, by ${dr.label}: `),
      dr.members.filter((x) => x.change).slice(0, 3).map((x, i) => [i ? " · " : "", `${x.member} `, h("span", { class: goodDir(x.change) === "ok" ? "pos" : "neg" }, (x.change >= 0 ? "+" : "−") + fmtVal(Math.abs(x.change), f))])) : null,
    segmentsBlock(m, f));
  return card;
}

function segmentsBlock(m, f) {
  const rows = m.segments.flatMap((s) => [...s.growing.slice(0, 2).map((x) => ({ ...x, up: true, dim: s.label })), ...s.declining.slice(0, 2).map((x) => ({ ...x, up: false, dim: s.label }))])
    .filter((x) => Math.abs(x.relative_slope) >= 0.015).sort((a, b) => Math.abs(b.relative_slope) - Math.abs(a.relative_slope)).slice(0, 4);
  if (!rows.length) return null;
  return h("div", { class: "segs" }, h("span", { class: "eyebrow" }, "Trending segments, last 12 " + m.grain + "s"), rows.map((x) => {
    const good = x.up === (m.direction === "up");
    return h("div", { class: "segrow" }, h("span", { class: good ? "pos" : "neg" }, x.up ? "▲" : "▼"), h("span", { class: "small" }, `${x.dim}: ${x.member}`), spark(x.values, good),
      h("span", { class: "small mono muted" }, `${x.relative_slope >= 0 ? "+" : "−"}${Math.abs(x.relative_slope * 100).toFixed(1)}%/${m.grain.slice(0, 2)}`));
  }));
}

function spark(vals, good) {
  const W = 80, H = 20, mx = Math.max(...vals), mn = Math.min(...vals), span = mx - mn || 1;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * W},${H - 2 - ((v - mn) / span) * (H - 4)}`).join(" ");
  return s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "spark", "aria-hidden": "true" }, s("polyline", { points: pts, fill: "none", stroke: good ? "var(--ok)" : "var(--bad)", "stroke-width": 1.5 }));
}

/* Actuals (last 24 periods), the same periods a year earlier, and the forecast with its range. */
function trendChart(m, f) {
  const W = 520, H = 170, padL = 46, padR = 10, padT = 10, padB = 24;
  const act = m.series.slice(-24);
  const per = m.grain === "quarter" ? 4 : 12;
  const ly = act.map((p, i) => { const j = m.series.indexOf(p) - per; return j >= 0 ? m.series[j].value : null; });
  const fc = m.forecast.points.slice(0, Math.max(3, m.landing?.remaining_periods || 3));
  const n = act.length + fc.length;
  const vals = [...act.map((p) => p.value), ...ly.filter((v) => v != null), ...fc.flatMap((p) => [p.upper, p.lower])];
  const lo = Math.min(0, ...vals), hiRaw = Math.max(...vals);
  const ticks = niceTicks(hiRaw); const hi = ticks[ticks.length - 1] || 1;
  const sx = (i) => padL + (i / Math.max(1, n - 1)) * (W - padL - padR);
  const sy = (v) => padT + (1 - (v - lo) / (hi - lo || 1)) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart briefchart", role: "img", "aria-label": `${m.label}: last ${act.length} ${m.grain}s, a year earlier, and forecast` });
  ticks.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, shortVal(t, f))));
  const path = (pts) => "M" + pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L");
  const lyPts = ly.map((v, i) => (v == null ? null : [sx(i), sy(v)])).filter(Boolean);
  if (lyPts.length > 1) svg.append(s("path", { d: path(lyPts), fill: "none", stroke: "var(--muted)", "stroke-width": 1.4, "stroke-dasharray": "2 3", opacity: 0.8 }, s("title", null, "Same period a year earlier")));
  if (fc.length) {
    const a = act.length - 1;
    const up = [[sx(a), sy(act[a].value)], ...fc.map((p, k) => [sx(a + 1 + k), sy(p.upper)])];
    const dn = [...fc.map((p, k) => [sx(a + 1 + k), sy(p.lower)]).reverse(), [sx(a), sy(act[a].value)]];
    svg.append(s("path", { d: path(up) + " L" + dn.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L") + " Z", fill: "var(--c3)", "fill-opacity": 0.16 }),
      s("path", { d: path([[sx(a), sy(act[a].value)], ...fc.map((p, k) => [sx(a + 1 + k), sy(p.value)])]), fill: "none", stroke: "var(--c3)", "stroke-width": 2, "stroke-dasharray": "5 4" }));
    fc.forEach((p, k) => svg.append(s("circle", { cx: sx(a + 1 + k), cy: sy(p.value), r: 6, fill: "transparent" }, s("title", null, `${p.label} forecast ${fmtVal(p.value, f)} (${fmtVal(p.lower, f)} – ${fmtVal(p.upper, f)})`))));
  }
  svg.append(s("path", { d: path(act.map((p, i) => [sx(i), sy(p.value)])), fill: "none", stroke: "var(--c1)", "stroke-width": 2.2, "stroke-linejoin": "round" }));
  act.forEach((p, i) => svg.append(s("circle", { cx: sx(i), cy: sy(p.value), r: 6, fill: "transparent" }, s("title", null, `${p.label}: ${fmtVal(p.value, f)}${ly[i] != null ? ` (a year earlier ${fmtVal(ly[i], f)})` : ""}`))));
  const last = act.length - 1;
  svg.append(s("circle", { cx: sx(last), cy: sy(act[last].value), r: 3.5, fill: "var(--c1)" }));
  [[0, act[0].label], [last, act[last].label], ...(fc.length ? [[n - 1, fc[fc.length - 1].label]] : [])].forEach(([i, l], k) =>
    svg.append(s("text", { x: sx(i), y: H - 6, "text-anchor": k === 0 ? "start" : "end" }, l)));
  return h("div", null, svg, h("div", { class: "row small muted chartkey" }, h("span", null, h("i", { class: "sw act" }), "actual"), h("span", null, h("i", { class: "sw ly" }), "a year earlier"), h("span", null, h("i", { class: "sw fc" }), "forecast and likely range")));
}

async function targetsDialog(data, reload) {
  if (!data) return;
  const sec = data.sections;
  const rows = [];
  for (const s of sec) {
    const existing = await GET(`/api/models/${s.model_id}/targets`);
    for (const m of s.measures) {
      const year = m.landing?.label || m.ytd.label.split(" ")[0];
      const cur = existing.find((t) => t.measure === m.measure && t.period === year);
      const inp = h("input", { type: "number", step: "any", value: cur ? (m.format === "percent" ? +(cur.value * 100).toFixed(4) : cur.value) : "", placeholder: "no target", "aria-label": `${m.label} target` });
      rows.push({ s, m, year, inp, cur });
    }
  }
  dialog("Full-year targets", h("div", { class: "stack" }, h("p", { class: "small muted" }, "Targets for the current year. Sums and counts are compared with the full-year forecast; averages and rates with the year-to-date value. Enter percentages as numbers (6 for 6%)."),
    h("div", { class: "form" }, rows.map((r) => field(`${r.m.label} · ${r.year}${r.m.format === "percent" ? " (%)" : ""}`, r.inp)))),
  [{ label: "Save targets", run: async () => {
    for (const r of rows) {
      const v = r.inp.value.trim();
      if (v === "" && r.cur) await DEL(`/api/models/${r.s.model_id}/targets/${r.cur.id}`);
      else if (v !== "") await PUT(`/api/models/${r.s.model_id}/targets`, { measure: r.m.measure, period: r.year, value: r.m.format === "percent" ? +v / 100 : +v });
    }
    toast("Targets saved"); reload();
  } }]);
}

function emailBrief(d, filters) {
  const n = h("input", { id: "eb-name", value: `${d.name} executive brief` });
  const r = h("input", { id: "eb-to", value: T.me.email });
  const s2 = h("select", { id: "eb-when" }, [["0 7 * * 1", "Weekly, Monday 07:00"], ["0 7 * * *", "Daily at 07:00"], ["0 7 1 * *", "Monthly, 1st at 07:00"]].map(([v, l]) => h("option", { value: v }, l)));
  dialog("Email the executive brief", h("div", { class: "form" }, field("Name", n), field("Recipients", r, "Comma-separated. Everyone gets the numbers you can see, including your row-level security."), field("Schedule (server time zone)", s2)),
    [{ label: "Subscribe", run: async () => {
      const sub = await POST("/api/subscriptions", { name: n.value, dashboard_id: d.id, recipients: r.value, schedule: s2.value, attach: "xlsx", include_brief: true, filters });
      toast("Subscribed");
      try { await POST(`/api/subscriptions/${sub.id}/send`); toast("First copy sent"); } catch (e) { toast(e.message, "bad"); }
    } }]);
}
