"use strict";
/* Small SVG chart kit: KPI, bar, line, area, table. One scale per chart; labels only at values the axis reaches. */
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  kids.flat().forEach((k) => k != null && el.append(k instanceof Node ? k : document.createTextNode(String(k))));
  return el;
}
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw);
  const out = [];
  for (let v = 0; v <= max + step * 0.001; v += step) out.push(+v.toFixed(10));
  if (out[out.length - 1] < max) out.push(+(out[out.length - 1] + step).toFixed(10));
  return out;
}
const short = (v) => {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
  if (a >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(+v.toFixed(2));
};
const SERIES = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fieldMap(res) { return Object.fromEntries((res.fields || []).map((f) => [f.name, f])); }
function fmtVal(v, f) {
  if (v == null) return "—";
  if (f && f.kind === "dimension" && f.type === "time" && typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v)) {
    const y = v.slice(0, 4), m = +v.slice(5, 7);
    if (f.grain === "year") return y;
    if (f.grain === "quarter") return `Q${Math.floor((m - 1) / 3) + 1} ${y}`;
    if (f.grain === "month" || !f.grain) return `${MONTHS[m - 1]} ${y}`;
    return v.slice(0, 10);
  }
  if (typeof v !== "number") return String(v);
  const fm = f?.format, d = f?.decimals ?? 2;
  if (fm === "currency") return "$" + v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  if (fm === "percent") return (Math.abs(v) <= 1.5 ? v * 100 : v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }) + "%";
  if (fm === "integer") return Math.round(v).toLocaleString();
  return v.toLocaleString(undefined, { maximumFractionDigits: f ? d : 2 });
}
function shortVal(v, f) {
  if (f?.format === "percent") return fmtVal(v, { ...f, decimals: 0 });
  const t = short(v);
  return f?.format === "currency" ? "$" + t : t;
}
function pick(res, viz) {
  const cols = res.columns, fm = fieldMap(res);
  const numericIdx = cols.map((_, i) => i).filter((i) => fm[cols[i]] ? fm[cols[i]].kind === "measure" : res.rows.some((r) => typeof r[i] === "number"));
  const x = viz.x && cols.includes(viz.x) ? cols.indexOf(viz.x) : cols.findIndex((_, i) => !numericIdx.includes(i));
  const y = viz.y && cols.includes(viz.y) ? cols.indexOf(viz.y) : numericIdx.find((i) => i !== x);
  return { x: x < 0 ? 0 : x, y: y == null ? (numericIdx[0] ?? 0) : y, numericIdx };
}
function dataTable(res, viz = {}) {
  if (!res || !res.columns.length) return h("p", { class: "muted" }, "No columns returned.");
  const fm = fieldMap(res);
  const heatIdx = viz.heat ? res.columns.indexOf(viz.heat) : -1;
  let lo = Infinity, hi = -Infinity;
  if (heatIdx >= 0) res.rows.forEach((r) => { const v = r[heatIdx]; if (typeof v === "number") { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  return h("div", { class: "tablewrap results" }, h("table", null,
    h("thead", null, h("tr", null, res.columns.map((c, i) => h("th", { class: fm[c]?.kind === "measure" || (!res.fields && typeof res.rows[0]?.[i] === "number") ? "num" : null, title: res.types?.[i] || "" }, fm[c]?.label || c)))),
    h("tbody", null, res.rows.map((r) => h("tr", null, r.map((v, i) => {
      if (v === null) return h("td", { class: "null" }, "null");
      const f = fm[res.columns[i]];
      const style = i === heatIdx && hi > lo ? { background: `color-mix(in srgb, var(--c2) ${Math.round(8 + 42 * (v - lo) / (hi - lo))}%, transparent)` } : null;
      return h("td", { title: String(v), class: typeof v === "number" ? "num" : null, style }, f ? fmtVal(v, f) : (typeof v === "number" ? fmt(v, 4) : String(v)));
    }))))));
}

/* opts: { wide, onPick(field, rawValue), selected } */
function renderViz(viz, res, opts) {
  if (typeof opts === "boolean") opts = { wide: opts };
  opts = opts || {};
  viz = viz || {};
  const type = viz.type || "table";
  if (!res || !res.rows) return h("p", { class: "muted" }, "No data.");
  if (type === "table") return dataTable(res, viz);
  if (!res.rows.length) return h("p", { class: "muted small" }, "No rows match.");
  const fm = fieldMap(res);
  const { x, y } = pick(res, viz);
  const xf = fm[res.columns[x]], yf = fm[res.columns[y]];
  const W = opts.wide ? 1100 : 600;
  if (type === "kpi") return viz.target ? kpiTarget(viz, res, y, yf) : kpiView(viz, res, y, yf);
  if (typeof EXTRA_VIZ !== "undefined" && EXTRA_VIZ[type]) return EXTRA_VIZ[type](viz, res, W, opts);
  if (type === "pivot") return pivotView(viz, res);
  if (type === "stacked") return stackedChart(viz, res, W, opts);
  const data = res.rows.map((r) => ({ raw: r[x], k: fmtVal(r[x], xf), v: Number(r[y]) || 0 }));
  if (type === "bar") return barChart(data, yf || { label: res.columns[y] }, W, opts, res.columns[x]);
  if (type === "donut" || type === "pie") return donutChart(data, yf || {}, opts, res.columns[x]);
  return lineChart(data, yf || { label: res.columns[y] }, type === "area", W, { ...opts, analysis: res.analysis, fmtX: (v) => fmtVal(v, xf) }, res.columns[x]);
}

function kpiView(viz, res, y, yf) {
  const v = res.rows[0][y];
  const main = (viz.prefix || "") + (typeof v === "number" ? fmtVal(v, yf) : v ?? "—") + (viz.suffix || "");
  let delta = null;
  if (res.previous && res.previous[0] && typeof res.previous[0][y] === "number" && typeof v === "number" && res.previous[0][y] !== 0) {
    const p = res.previous[0][y], ch = (v - p) / Math.abs(p);
    delta = h("div", { class: "delta " + (ch >= 0 ? "up" : "down") }, `${ch >= 0 ? "▲" : "▼"} ${Math.abs(ch * 100).toFixed(1)}%`, h("span", { class: "muted" }, ` vs ${fmtVal(p, yf)} previous period`));
  }
  return h("div", { class: "kpibox" }, h("div", { class: "kpival" }, main), delta);
}

function barChart(data, yf, W = 600, opts = {}, xName) {
  data = data.slice(0, 40);
  const rowH = 24, padL = Math.min(170, 12 + Math.max(...data.map((d) => d.k.length)) * 6.6), padR = 64;
  const H = data.length * rowH + 28;
  const max = Math.max(0, ...data.map((d) => d.v));
  const min = Math.min(0, ...data.map((d) => d.v));
  const span = max - min || 1;
  const sx = (v) => padL + ((v - min) / span) * (W - padL - padR);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `Bar chart of ${yf.label || ""}` });
  niceTicks(max).forEach((t) => {
    if (t > max * 1.0001 && t !== 0) return;
    svg.append(s("line", { x1: sx(t), x2: sx(t), y1: 4, y2: H - 22, class: "gridline" }), s("text", { x: sx(t), y: H - 6, "text-anchor": "middle" }, shortVal(t, yf)));
  });
  data.forEach((d, i) => {
    const y0 = 6 + i * rowH;
    const x0 = sx(Math.min(0, d.v)), w = Math.max(1, Math.abs(sx(d.v) - sx(0)));
    const dim = opts.selected != null && String(opts.selected) !== String(d.raw);
    const rect = s("rect", { x: x0, y: y0 + 3, width: w, height: rowH - 9, rx: 2, fill: d.v < 0 ? "var(--bad)" : "var(--c1)", "fill-opacity": dim ? 0.3 : 1,
      class: opts.onPick ? "pickable" : null }, s("title", null, `${d.k}: ${fmtVal(d.v, yf)}`));
    if (opts.onPick) rect.addEventListener("click", () => opts.onPick(xName, d.raw));
    svg.append(s("text", { x: padL - 8, y: y0 + 14, "text-anchor": "end", class: "lbl" }, d.k.length > 24 ? d.k.slice(0, 23) + "…" : d.k), rect,
      s("text", { x: sx(Math.max(0, d.v)) + 5, y: y0 + 14 }, shortVal(d.v, yf)));
  });
  return svg;
}

function lineChart(data, yf, area, W = 600, opts = {}, xName) {
  const H = 240, padL = 52, padR = 16, padT = 12, padB = 34;
  const an = opts.analysis;
  const fc = (an && an.forecast) || [];
  if (fc.length) {
    // place forecast periods after the last actual period that is not in the future
    const lastActual = data.reduce((m, d, i) => (String(d.raw).slice(0, 10) < fc[0].period ? i : m), -1);
    data = data.slice(0, lastActual + 1).concat(fc.map((p) => ({ raw: p.period, k: opts.fmtX ? opts.fmtX(p.period) : p.period.slice(0, 7), v: null, f: p })));
  }
  const vals = data.flatMap((d) => d.f ? [d.f.upper, d.f.value] : [d.v]);
  const max = Math.max(0, ...vals);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;
  const sx = (i) => padL + (data.length === 1 ? (W - padL - padR) / 2 : (i / (data.length - 1)) * (W - padL - padR));
  const sy = (v) => padT + (1 - v / top) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${area ? "Area" : "Line"} chart of ${yf.label || ""}` });
  ticks.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, shortVal(t, yf))));
  const every = Math.max(1, Math.ceil(data.length / (W > 800 ? 12 : 7)));
  data.forEach((d, i) => {
    if (!(i % every === 0 || i === data.length - 1) || (i !== data.length - 1 && data.length - 1 - i < every / 2)) return;
    const anchor = data.length > 1 && i === data.length - 1 ? "end" : i === 0 && data.length > 1 ? "start" : "middle";
    svg.append(s("text", { x: sx(i), y: H - 12, "text-anchor": anchor }, d.k.length > 12 ? d.k.slice(0, 12) : d.k));
  });
  const fcIdx = data.map((d, i) => (d.f ? i : -1)).filter((i) => i >= 0);
  if (fcIdx.length) {
    const a0 = fcIdx[0] - 1;
    const upper = fcIdx.map((i) => `${sx(i).toFixed(1)},${sy(Math.max(0, data[i].f.upper)).toFixed(1)}`);
    const lower = fcIdx.slice().reverse().map((i) => `${sx(i).toFixed(1)},${sy(Math.max(0, data[i].f.lower)).toFixed(1)}`);
    const anchor = a0 >= 0 ? `${sx(a0).toFixed(1)},${sy(data[a0].v).toFixed(1)}` : null;
    svg.append(s("path", { d: `M${[anchor, ...upper].filter(Boolean).join(" L")} L${lower.join(" L")}${anchor ? " L" + anchor : ""} Z`, fill: "var(--c3)", "fill-opacity": "0.16" }, s("title", null, "Forecast range (approx. 95%)")),
      s("path", { d: "M" + [anchor, ...fcIdx.map((i) => `${sx(i).toFixed(1)},${sy(data[i].f.value).toFixed(1)}`)].filter(Boolean).join(" L"), fill: "none", stroke: "var(--c3)", "stroke-width": 2, "stroke-dasharray": "5 4" }));
    fcIdx.forEach((i) => svg.append(s("circle", { cx: sx(i), cy: sy(data[i].f.value), r: 3, fill: "var(--c3)" }, s("title", null, `Forecast ${data[i].k}: ${fmtVal(data[i].f.value, yf)} (${fmtVal(data[i].f.lower, yf)} – ${fmtVal(data[i].f.upper, yf)})`))));
    data = data.filter((d) => !d.f);
  }
  const pts = data.map((d, i) => `${sx(i).toFixed(1)},${sy(d.v).toFixed(1)}`);
  if (area) svg.append(s("path", { d: `M${sx(0)},${sy(0)} L${pts.join(" L")} L${sx(data.length - 1)},${sy(0)} Z`, fill: "var(--c1)", "fill-opacity": "0.14" }));
  svg.append(s("path", { d: "M" + pts.join(" L"), fill: "none", stroke: "var(--c1)", "stroke-width": 2, "stroke-linejoin": "round" }));
  (an?.anomalies || []).forEach((a) => {
    const i = data.findIndex((d) => String(d.raw).slice(0, 10) === a.period);
    if (i >= 0) svg.append(s("circle", { cx: sx(i), cy: sy(data[i].v), r: 6, fill: "none", stroke: "var(--bad)", "stroke-width": 2, class: "anomaly" },
      s("title", null, `Unusual: ${fmtVal(a.value, yf)} vs about ${fmtVal(a.expected, yf)} expected (robust z ${a.z.toFixed(1)})`)));
  });
  const last = data.length - 1;
  svg.append(s("circle", { cx: sx(last), cy: sy(data[last].v), r: 3.5, fill: "var(--c1)" }),
    s("text", { x: sx(last) - 8, y: sy(data[last].v) - 8, "text-anchor": "end", class: "lbl" }, shortVal(data[last].v, yf)));
  data.forEach((d, i) => {
    const c = s("circle", { cx: sx(i), cy: sy(d.v), r: 7, fill: "transparent", class: opts.onPick ? "pickable" : null }, s("title", null, `${d.k}: ${fmtVal(d.v, yf)}`));
    if (opts.onPick) c.addEventListener("click", () => opts.onPick(xName, d.raw));
    svg.append(c);
  });
  return svg;
}

function donutChart(data, yf, opts = {}, xName) {
  data = data.filter((d) => d.v > 0);
  const total = data.reduce((a, d) => a + d.v, 0) || 1;
  const top = data.length > 6 ? data.slice(0, 5) : data.slice();
  if (data.length > 6) top.push({ k: `Other (${data.length - 5})`, raw: null, v: data.slice(5).reduce((a, d) => a + d.v, 0) });
  const R = 80, r = 50, cx = 100, cy = 100;
  const svg = s("svg", { viewBox: "0 0 200 200", class: "chart donut", role: "img", "aria-label": "Donut chart" });
  let a0 = -Math.PI / 2;
  top.forEach((d, i) => {
    const a1 = a0 + (d.v / total) * Math.PI * 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (ang, rad) => `${cx + rad * Math.cos(ang)},${cy + rad * Math.sin(ang)}`;
    const dim = opts.selected != null && String(opts.selected) !== String(d.raw);
    const path = s("path", { d: top.length === 1 ? `M${cx},${cy - R} A${R},${R} 0 1 1 ${cx - 0.01},${cy - R} L${cx - 0.01},${cy - r} A${r},${r} 0 1 0 ${cx},${cy - r} Z`
      : `M${p(a0, R)} A${R},${R} 0 ${large} 1 ${p(a1, R)} L${p(a1, r)} A${r},${r} 0 ${large} 0 ${p(a0, r)} Z`, fill: SERIES[i % SERIES.length], "fill-opacity": dim ? 0.3 : 1,
      class: opts.onPick && d.raw != null ? "pickable" : null }, s("title", null, `${d.k}: ${fmtVal(d.v, yf)} (${((d.v / total) * 100).toFixed(1)}%)`));
    if (opts.onPick && d.raw != null) path.addEventListener("click", () => opts.onPick(xName, d.raw));
    svg.append(path);
    a0 = a1;
  });
  svg.append(s("text", { x: cx, y: cy - 2, "text-anchor": "middle", class: "lbl", "font-size": 15 }, shortVal(total, yf)), s("text", { x: cx, y: cy + 15, "text-anchor": "middle" }, "total"));
  const legend = h("div", { class: "legend-list" }, top.map((d, i) => h("div", { class: "row small" }, h("span", { class: "tess", style: { background: SERIES[i % SERIES.length] } }),
    h("span", null, d.k), h("span", { class: "muted" }, `${((d.v / total) * 100).toFixed(1)}%`))));
  return h("div", { class: "donutwrap" }, svg, legend);
}

function stackedChart(viz, res, W, opts) {
  const fm = fieldMap(res);
  const cols = res.columns;
  const xi = viz.x && cols.includes(viz.x) ? cols.indexOf(viz.x) : 0;
  const si = viz.series && cols.includes(viz.series) ? cols.indexOf(viz.series) : 1;
  const yi = viz.y && cols.includes(viz.y) ? cols.indexOf(viz.y) : cols.length - 1;
  const xs = [...new Set(res.rows.map((r) => r[xi]))].slice(0, 30);
  const ss = [...new Set(res.rows.map((r) => r[si]))].slice(0, 6);
  const val = {};
  res.rows.forEach((r) => (val[r[xi] + "\u0000" + r[si]] = (val[r[xi] + "\u0000" + r[si]] || 0) + (Number(r[yi]) || 0)));
  const totals = xs.map((xv) => ss.reduce((a, sv) => a + (val[xv + "\u0000" + sv] || 0), 0));
  const order = xs.map((_, i) => i).sort((a, b) => totals[b] - totals[a]);
  const rowH = 24, xf = fm[cols[xi]], yf = fm[cols[yi]] || {};
  const labels = xs.map((v) => fmtVal(v, xf));
  const padL = Math.min(170, 12 + Math.max(...labels.map((l) => l.length)) * 6.6), padR = 64, H = xs.length * rowH + 28;
  const max = Math.max(1, ...totals), ticks = niceTicks(max), top = ticks[ticks.length - 1];
  const sx = (v) => padL + (v / top) * (W - padL - padR);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Stacked bar chart" });
  ticks.forEach((t) => svg.append(s("line", { x1: sx(t), x2: sx(t), y1: 4, y2: H - 22, class: "gridline" }), s("text", { x: sx(t), y: H - 6, "text-anchor": "middle" }, shortVal(t, yf))));
  order.forEach((xiIdx, row) => {
    const y0 = 6 + row * rowH;
    let acc = 0;
    svg.append(s("text", { x: padL - 8, y: y0 + 14, "text-anchor": "end", class: "lbl" }, labels[xiIdx].slice(0, 24)));
    ss.forEach((sv, si2) => {
      const v = val[xs[xiIdx] + "\u0000" + sv] || 0;
      if (!v) return;
      const rect = s("rect", { x: sx(acc), y: y0 + 3, width: Math.max(0.5, sx(acc + v) - sx(acc)), height: rowH - 9, fill: SERIES[si2], class: opts.onPick ? "pickable" : null },
        s("title", null, `${labels[xiIdx]} · ${sv}: ${fmtVal(v, yf)}`));
      if (opts.onPick) rect.addEventListener("click", () => opts.onPick(cols[xi], xs[xiIdx]));
      svg.append(rect);
      acc += v;
    });
    svg.append(s("text", { x: sx(acc) + 5, y: y0 + 14 }, shortVal(acc, yf)));
  });
  const legend = h("div", { class: "row small" }, ss.map((sv, i) => h("span", { class: "row", style: { gap: "4px" } }, h("span", { class: "tess", style: { background: SERIES[i] } }), fmtVal(sv, fm[cols[si]]))));
  return h("div", { class: "stack", style: { gap: "6px" } }, legend, svg);
}

function pivotView(viz, res) {
  const fm = fieldMap(res);
  const cols = res.columns;
  const dims = cols.filter((c) => fm[c]?.kind === "dimension");
  const meas = cols.filter((c) => fm[c]?.kind === "measure");
  if (dims.length < 2 || !meas.length) return dataTable(res, viz);
  const [ri, ci, vi] = [cols.indexOf(dims[0]), cols.indexOf(dims[1]), cols.indexOf(meas[0])];
  const rk = [...new Set(res.rows.map((r) => r[ri]))], ck = [...new Set(res.rows.map((r) => r[ci]))].slice(0, 20);
  const m = {};
  res.rows.forEach((r) => (m[r[ri] + "\u0000" + r[ci]] = r[vi]));
  const vf = fm[meas[0]];
  const vals = res.rows.map((r) => r[vi]).filter((v) => typeof v === "number");
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const rowTot = (rv) => ck.reduce((a, cv) => a + (Number(m[rv + "\u0000" + cv]) || 0), 0);
  return h("div", { class: "tablewrap results" }, h("table", { class: "pivot" },
    h("thead", null, h("tr", null, h("th", null, `${fm[dims[0]].label} \\ ${fm[dims[1]].label}`), ck.map((c) => h("th", { class: "num" }, fmtVal(c, fm[dims[1]]))), h("th", { class: "num" }, "Total"))),
    h("tbody", null, rk.map((rv) => h("tr", null, h("th", null, fmtVal(rv, fm[dims[0]])), ck.map((cv) => {
      const v = m[rv + "\u0000" + cv];
      const pctv = typeof v === "number" && hi > lo ? (v - lo) / (hi - lo) : 0;
      return h("td", { class: "num", style: typeof v === "number" ? { background: `color-mix(in srgb, var(--c1) ${Math.round(6 + 40 * pctv)}%, transparent)` } : null }, v == null ? "—" : fmtVal(v, vf));
    }), h("td", { class: "num" }, h("b", null, vf?.format === "percent" ? "—" : fmtVal(rowTot(rv), vf))))))));
}

/* stacked daily run bars for the overview */
function runsChart(byDay) {
  const days = [];
  for (let i = 13; i >= 0; i--) days.push(new Date(Date.now() - i * 864e5).toISOString().slice(0, 10));
  const val = (d, st) => byDay.filter((r) => r.day === d && r.status === st).reduce((a, r) => a + r.n, 0);
  const rows = days.map((d) => ({ d, ok: val(d, "success"), bad: val(d, "failed") + val(d, "cancelled") }));
  const max = Math.max(1, ...rows.map((r) => r.ok + r.bad));
  const ticks = niceTicks(max, 2), top = ticks[ticks.length - 1];
  const W = 600, H = 150, padL = 28, padB = 22, bw = (W - padL) / days.length;
  const sy = (v) => 8 + (1 - v / top) * (H - 8 - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Pipeline runs per day, last 14 days" });
  ticks.forEach((t) => svg.append(s("line", { x1: padL, x2: W, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, t)));
  rows.forEach((r, i) => {
    const x = padL + i * bw + 3, w = bw - 6;
    if (r.ok) svg.append(s("rect", { x, y: sy(r.ok), width: w, height: sy(0) - sy(r.ok), fill: "var(--ok)", rx: 1 }, s("title", null, `${r.d}: ${r.ok} succeeded`)));
    if (r.bad) svg.append(s("rect", { x, y: sy(r.ok + r.bad), width: w, height: sy(r.ok) - sy(r.ok + r.bad), fill: "var(--bad)", rx: 1 }, s("title", null, `${r.d}: ${r.bad} failed`)));
    if (i % 2 === 1) svg.append(s("text", { x: x + w / 2, y: H - 6, "text-anchor": "middle" }, r.d.slice(5)));
  });
  return svg;
}

/* DAG layout: layer by longest dependency path */
function dagView(steps, states) {
  const byId = Object.fromEntries(steps.map((st) => [st.id, st]));
  const depth = {};
  const d = (id, seen = new Set()) => {
    if (depth[id] != null) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = (byId[id]?.depends_on || []).filter((x) => byId[x]);
    return (depth[id] = deps.length ? 1 + Math.max(...deps.map((x) => d(x, seen))) : 0);
  };
  steps.forEach((st) => d(st.id));
  const layers = [];
  steps.forEach((st) => (layers[depth[st.id]] = layers[depth[st.id]] || []).push(st));
  const NW = 168, NH = 46, GX = 54, GY = 14;
  const pos = {};
  layers.forEach((L, li) => L.forEach((st, i) => (pos[st.id] = { x: 10 + li * (NW + GX), y: 10 + i * (NH + GY) })));
  const W = 20 + layers.length * (NW + GX) - GX, H = 20 + Math.max(...layers.map((L) => L.length)) * (NH + GY) - GY;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Pipeline steps graph" });
  steps.forEach((st) => (st.depends_on || []).forEach((dep) => {
    if (!pos[dep]) return;
    const a = pos[dep], b = pos[st.id];
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    svg.append(s("path", { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 4},${y2}`, "marker-end": "url(#arr)" }));
  }));
  svg.prepend(s("defs", null, s("marker", { id: "arr", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto" }, s("path", { d: "M0,0 L8,4 L0,8 z", fill: "var(--muted)", stroke: "none" }))));
  steps.forEach((st) => {
    const p = pos[st.id], state = states?.[st.id]?.status || "";
    svg.append(s("g", { class: "node " + state, transform: `translate(${p.x},${p.y})` },
      s("rect", { width: NW, height: NH, rx: 6 }),
      s("text", { x: 10, y: 19 }, st.id.length > 20 ? st.id.slice(0, 19) + "…" : st.id),
      s("text", { x: 10, y: 35, class: "t" }, st.type + (state ? " · " + state : "")),
      s("title", null, JSON.stringify(st, null, 1))));
  });
  return h("div", { class: "dag" }, svg);
}

function lineageView(lin) {
  const me = lin.node;
  const up = new Map(), down = new Map();
  const level = (start, edges, key, other) => {
    const m = new Map([[start, 0]]);
    let frontier = [start];
    for (let i = 1; i < 8 && frontier.length; i++) {
      const next = [];
      frontier.forEach((n) => edges.filter((e) => e[key] === n).forEach((e) => { if (!m.has(e[other])) { m.set(e[other], i); next.push(e[other]); } }));
      frontier = next;
    }
    return m;
  };
  level(me, lin.edges, "dst", "src").forEach((v, k) => k !== me && up.set(k, v));
  level(me, lin.edges, "src", "dst").forEach((v, k) => k !== me && down.set(k, v));
  const cols = new Map();
  up.forEach((v, k) => cols.set(k, -v));
  down.forEach((v, k) => cols.set(k, v));
  cols.set(me, 0);
  const keys = [...new Set(cols.values())].sort((a, b) => a - b);
  const NW = 210, NH = 30, GX = 50, GY = 10;
  const pos = {};
  keys.forEach((c, ci) => [...cols].filter(([, v]) => v === c).forEach(([n], i) => (pos[n] = { x: 10 + ci * (NW + GX), y: 10 + i * (NH + GY) })));
  const W = 20 + keys.length * (NW + GX) - GX, H = 20 + Math.max(1, ...keys.map((c) => [...cols.values()].filter((v) => v === c).length)) * (NH + GY);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Lineage graph" });
  lin.edges.forEach((e) => {
    const a = pos[e.src], b = pos[e.dst];
    if (!a || !b) return;
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    svg.append(s("path", { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` }, s("title", null, e.kind)));
  });
  Object.entries(pos).forEach(([n, p]) => svg.append(s("g", { transform: `translate(${p.x},${p.y})` },
    s("rect", { width: NW, height: NH, rx: 4, class: n === me ? "me" : null }),
    s("text", { x: 8, y: 19 }, n.length > 30 ? n.slice(0, 29) + "…" : n), s("title", null, n))));
  return h("div", { class: "lineage tablewrap" }, svg);
}
