"use strict";
/* More visuals: combo (bars + line on a second axis), multi-line, scatter, treemap, waterfall, funnel, and KPI with target.
   Same rules as charts.js: one scale per axis, labels only where the axis reaches, values in tooltips. */

function colIdx(res, name, fallback) { return name && res.columns.includes(name) ? res.columns.indexOf(name) : fallback; }
function measureIdx(res) {
  const fm = fieldMap(res);
  return res.columns.map((c, i) => i).filter((i) => (fm[res.columns[i]] ? fm[res.columns[i]].kind === "measure" : res.rows.some((r) => typeof r[i] === "number")));
}
function legendRow(items) {
  return h("div", { class: "row small chartlegend" }, items.map(([label, color, kind]) => h("span", { class: "row", style: { gap: "5px" } },
    h("span", { class: "tess" + (kind === "line" ? " line" : ""), style: { background: color } }), label)));
}

/* Bars for the first measure, a line for the second on its own right-hand axis. */
function comboChart(viz, res, W, opts) {
  const fm = fieldMap(res), mi = measureIdx(res);
  const xi = colIdx(res, viz.x, res.columns.findIndex((_, i) => !mi.includes(i)));
  const yi = colIdx(res, viz.y, mi[0]), y2i = colIdx(res, viz.y2, mi.find((i) => i !== yi));
  if (y2i == null || y2i < 0) return h("p", { class: "muted small" }, "A combo chart needs two measures.");
  const rows = res.rows.slice(0, 40);
  const xf = fm[res.columns[xi]], yf = fm[res.columns[yi]] || {}, y2f = fm[res.columns[y2i]] || {};
  const H = 260, padL = 52, padR = 52, padT = 14, padB = 38;
  const t1 = niceTicks(Math.max(0, ...rows.map((r) => +r[yi] || 0))), t2 = niceTicks(Math.max(0, ...rows.map((r) => +r[y2i] || 0)));
  const top1 = t1[t1.length - 1] || 1, top2 = t2[t2.length - 1] || 1;
  const bw = (W - padL - padR) / Math.max(1, rows.length);
  const cx = (i) => padL + bw * (i + 0.5);
  const sy1 = (v) => padT + (1 - v / top1) * (H - padT - padB), sy2 = (v) => padT + (1 - v / top2) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${yf.label || "Bars"} and ${y2f.label || "line"} by ${xf?.label || "category"}` });
  t1.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy1(t), y2: sy1(t), class: "gridline" }), s("text", { x: padL - 6, y: sy1(t) + 4, "text-anchor": "end" }, shortVal(t, yf))));
  t2.forEach((t) => svg.append(s("text", { x: W - padR + 6, y: sy2(t) + 4, "text-anchor": "start", fill: "var(--c3)" }, shortVal(t, y2f))));
  const every = Math.max(1, Math.ceil(rows.length / (W > 800 ? 16 : 8)));
  rows.forEach((r, i) => {
    const v = +r[yi] || 0;
    const dim = opts.selected != null && String(opts.selected) !== String(r[xi]);
    const rect = s("rect", { x: cx(i) - bw * 0.35, y: sy1(Math.max(0, v)), width: bw * 0.7, height: Math.abs(sy1(0) - sy1(v)), rx: 2, fill: "var(--c1)", "fill-opacity": dim ? 0.3 : 0.9, class: opts.onPick ? "pickable" : null },
      s("title", null, `${fmtVal(r[xi], xf)}: ${fmtVal(v, yf)} · ${fmtVal(r[y2i], y2f)}`));
    if (opts.onPick) rect.addEventListener("click", () => opts.onPick(res.columns[xi], r[xi]));
    svg.append(rect);
    if (i % every === 0) svg.append(s("text", { x: cx(i), y: H - padB + 14, "text-anchor": "middle" }, String(fmtVal(r[xi], xf)).slice(0, 12)));
  });
  const pts = rows.map((r, i) => `${cx(i).toFixed(1)},${sy2(+r[y2i] || 0).toFixed(1)}`);
  svg.append(s("path", { d: "M" + pts.join(" L"), fill: "none", stroke: "var(--c3)", "stroke-width": 2.2 }));
  rows.forEach((r, i) => svg.append(s("circle", { cx: cx(i), cy: sy2(+r[y2i] || 0), r: 3, fill: "var(--c3)" }, s("title", null, `${fmtVal(r[xi], xf)}: ${fmtVal(r[y2i], y2f)}`))));
  return h("div", { class: "stack", style: { gap: "6px" } }, legendRow([[yf.label || res.columns[yi], "var(--c1)"], [`${y2f.label || res.columns[y2i]} (right axis)`, "var(--c3)", "line"]]), svg);
}

/* One line per series (up to 6), e.g. revenue by month for each channel. */
function multilineChart(viz, res, W, opts) {
  const fm = fieldMap(res), mi = measureIdx(res);
  const dimIdx = res.columns.map((_, i) => i).filter((i) => !mi.includes(i));
  const xi = colIdx(res, viz.x, dimIdx.find((i) => fm[res.columns[i]]?.type === "time") ?? dimIdx[0]);
  const si = colIdx(res, viz.series, dimIdx.find((i) => i !== xi));
  const yi = colIdx(res, viz.y, mi[0]);
  const xs = [...new Set(res.rows.map((r) => r[xi]))].sort();
  const totals = {};
  res.rows.forEach((r) => (totals[r[si]] = (totals[r[si]] || 0) + (+r[yi] || 0)));
  const ss = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, 6);
  const val = {};
  res.rows.forEach((r) => (val[r[xi] + "\u0000" + r[si]] = +r[yi] || 0));
  const xf = fm[res.columns[xi]], yf = fm[res.columns[yi]] || {};
  const H = 250, padL = 52, padR = 16, padT = 12, padB = 30;
  const ticks = niceTicks(Math.max(0, ...Object.values(val))), top = ticks[ticks.length - 1] || 1;
  const sx = (i) => padL + (xs.length < 2 ? 0.5 : i / (xs.length - 1)) * (W - padL - padR), sy = (v) => padT + (1 - v / top) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${yf.label || ""} over time by ${fm[res.columns[si]]?.label || "series"}` });
  ticks.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, shortVal(t, yf))));
  [0, xs.length - 1].forEach((i, k) => xs.length && svg.append(s("text", { x: sx(i), y: H - 8, "text-anchor": k ? "end" : "start" }, fmtVal(xs[i], xf))));
  ss.forEach((sv, k) => {
    const pts = xs.map((x, i) => `${sx(i).toFixed(1)},${sy(val[x + "\u0000" + sv] || 0).toFixed(1)}`);
    const dim = opts.selected != null && String(opts.selected) !== String(sv);
    const path = s("path", { d: "M" + pts.join(" L"), fill: "none", stroke: SERIES[k], "stroke-width": 2, opacity: dim ? 0.25 : 1, class: opts.onPick ? "pickable" : null }, s("title", null, String(sv)));
    if (opts.onPick) path.addEventListener("click", () => opts.onPick(res.columns[si], sv));
    svg.append(path);
    xs.forEach((x, i) => svg.append(s("circle", { cx: sx(i), cy: sy(val[x + "\u0000" + sv] || 0), r: 5, fill: "transparent" }, s("title", null, `${sv} · ${fmtVal(x, xf)}: ${fmtVal(val[x + "\u0000" + sv] || 0, yf)}`))));
  });
  return h("div", { class: "stack", style: { gap: "6px" } }, legendRow(ss.map((sv, k) => [fmtVal(sv, fm[res.columns[si]]), SERIES[k], "line"])), svg);
}

/* Two measures per item: x = first measure, y = second, one dot per category. */
function scatterChart(viz, res, W, opts) {
  const fm = fieldMap(res), mi = measureIdx(res);
  // x and y must be measures; ignore category settings carried over from another visual
  const asMeasure = (name, fallback) => { const i = colIdx(res, name, -1); return mi.includes(i) ? i : fallback; };
  const xi = asMeasure(viz.x, mi[0]);
  const yi = asMeasure(viz.y2 && viz.x && !mi.includes(colIdx(res, viz.x, -1)) ? viz.y2 : viz.y, mi.find((i) => i !== xi));
  const li = colIdx(res, viz.label, res.columns.findIndex((_, i) => !mi.includes(i)));
  if (yi == null || yi < 0 || yi === xi) return h("p", { class: "muted small" }, "A scatter chart needs two measures.");
  const rows = res.rows.filter((r) => typeof r[xi] === "number" && typeof r[yi] === "number").slice(0, 500);
  const xf = fm[res.columns[xi]] || {}, yf = fm[res.columns[yi]] || {}, lf = fm[res.columns[li]];
  const H = 290, padL = 56, padR = 20, padT = 26, padB = 40;
  const tx = niceTicks(Math.max(0, ...rows.map((r) => r[xi]))), ty = niceTicks(Math.max(0, ...rows.map((r) => r[yi])));
  const sx = (v) => padL + (v / (tx[tx.length - 1] || 1)) * (W - padL - padR), sy = (v) => padT + (1 - v / (ty[ty.length - 1] || 1)) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${yf.label || "y"} against ${xf.label || "x"}` });
  ty.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, shortVal(t, yf))));
  tx.forEach((t) => svg.append(s("text", { x: sx(t), y: H - padB + 16, "text-anchor": "middle" }, shortVal(t, xf))));
  svg.append(s("text", { x: (padL + W - padR) / 2, y: H - 4, "text-anchor": "middle", class: "lbl" }, xf.label || res.columns[xi]),
    s("text", { x: 8, y: 12, class: "lbl" }, yf.label || res.columns[yi]));
  rows.forEach((r) => {
    const dim = opts.selected != null && String(opts.selected) !== String(r[li]);
    const c = s("circle", { cx: sx(r[xi]), cy: sy(r[yi]), r: 5, fill: "var(--c1)", "fill-opacity": dim ? 0.2 : 0.75, stroke: "var(--surface)", class: opts.onPick ? "pickable" : null },
      s("title", null, `${li >= 0 ? fmtVal(r[li], lf) + ": " : ""}${fmtVal(r[xi], xf)} · ${fmtVal(r[yi], yf)}`));
    if (opts.onPick && li >= 0) c.addEventListener("click", () => opts.onPick(res.columns[li], r[li]));
    svg.append(c);
    if (li >= 0 && rows.length <= 25) svg.append(s("text", { x: sx(r[xi]) + 7, y: sy(r[yi]) + 4 }, String(fmtVal(r[li], lf)).slice(0, 14)));
  });
  return svg;
}

/* Squarified treemap of positive values. */
function treemapChart(viz, res, W, opts) {
  const fm = fieldMap(res), { x, y } = pick(res, viz);
  const xf = fm[res.columns[x]], yf = fm[res.columns[y]] || {};
  const items = res.rows.map((r) => ({ raw: r[x], k: fmtVal(r[x], xf), v: +r[y] || 0 })).filter((d) => d.v > 0).sort((a, b) => b.v - a.v).slice(0, 40);
  const H = Math.round(W * 0.5), total = items.reduce((a, d) => a + d.v, 0) || 1;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart treemap", role: "img", "aria-label": `Treemap of ${yf.label || ""}` });
  const out = [];
  const layout = (list, rx, ry, rw, rh) => {
    if (!list.length) return;
    const sum = list.reduce((a, d) => a + d.v, 0);
    const wide = rw >= rh, side = wide ? rh : rw;
    let row = [], best = Infinity, i = 0;
    const worst = (rr) => { const sm = rr.reduce((a, d) => a + d.v, 0) / sum * rw * rh; const mx = Math.max(...rr.map((d) => d.v / sum * rw * rh)), mn = Math.min(...rr.map((d) => d.v / sum * rw * rh)); return Math.max(side * side * mx / (sm * sm), (sm * sm) / (side * side * mn)); };
    for (; i < list.length; i++) { const cand = [...row, list[i]]; const w = worst(cand); if (w > best && row.length) break; row = cand; best = w; }
    const rs = row.reduce((a, d) => a + d.v, 0), thick = (rs / sum) * (wide ? rw : rh);
    let off = 0;
    row.forEach((d) => { const len = (d.v / rs) * side; out.push(wide ? { d, x: rx, y: ry + off, w: thick, h: len } : { d, x: rx + off, y: ry, w: len, h: thick }); off += len; });
    if (wide) layout(list.slice(i), rx + thick, ry, rw - thick, rh); else layout(list.slice(i), rx, ry + thick, rw, rh - thick);
  };
  layout(items, 0, 0, W, H);
  out.forEach((c, k) => {
    const dim = opts.selected != null && String(opts.selected) !== String(c.d.raw);
    const g = s("g", { class: opts.onPick ? "pickable" : null },
      s("rect", { x: c.x + 1, y: c.y + 1, width: Math.max(0, c.w - 2), height: Math.max(0, c.h - 2), rx: 3, fill: SERIES[k % SERIES.length], "fill-opacity": dim ? 0.25 : 0.85 },
        s("title", null, `${c.d.k}: ${fmtVal(c.d.v, yf)} (${((c.d.v / total) * 100).toFixed(1)}%)`)));
    if (c.w > 70 && c.h > 44) g.append(s("text", { x: c.x + 10, y: c.y + 24, class: "tm-l" }, c.d.k.slice(0, Math.floor(c.w / 10))), s("text", { x: c.x + 10, y: c.y + 44, class: "tm-v" }, `${shortVal(c.d.v, yf)} · ${((c.d.v / total) * 100).toFixed(0)}%`));
    if (opts.onPick) g.addEventListener("click", () => opts.onPick(res.columns[x], c.d.raw));
    svg.append(g);
  });
  return svg;
}

/* Contributions stacking up to the total, largest first. */
function waterfallChart(viz, res, W, opts) {
  const fm = fieldMap(res), { x, y } = pick(res, viz);
  const xf = fm[res.columns[x]], yf = fm[res.columns[y]] || {};
  let items = res.rows.map((r) => ({ raw: r[x], k: String(fmtVal(r[x], xf)), v: +r[y] || 0 }));
  if (!viz.keep_order) items.sort((a, b) => b.v - a.v);
  if (items.length > 12) { const rest = items.slice(11); items = items.slice(0, 11).concat([{ raw: null, k: `Other (${rest.length})`, v: rest.reduce((a, d) => a + d.v, 0) }]); }
  const total = items.reduce((a, d) => a + d.v, 0);
  const H = 260, padL = 56, padR = 12, padT = 14, padB = 40;
  let run = 0, lo = 0, hi = 0;
  const bars = items.map((d) => { const a = run; run += d.v; lo = Math.min(lo, a, run); hi = Math.max(hi, a, run); return { ...d, a, b: run }; });
  const ticks = niceTicks(hi), top = ticks[ticks.length - 1] || 1;
  const n = bars.length + 1, bw = (W - padL - padR) / n;
  const sy = (v) => padT + (1 - (v - lo) / (top - lo || 1)) * (H - padT - padB);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `Waterfall of ${yf.label || ""}` });
  ticks.forEach((t) => svg.append(s("line", { x1: padL, x2: W - padR, y1: sy(t), y2: sy(t), class: "gridline" }), s("text", { x: padL - 6, y: sy(t) + 4, "text-anchor": "end" }, shortVal(t, yf))));
  bars.forEach((d, i) => {
    const x0 = padL + i * bw + bw * 0.15;
    const rect = s("rect", { x: x0, y: sy(Math.max(d.a, d.b)), width: bw * 0.7, height: Math.max(1, Math.abs(sy(d.a) - sy(d.b))), rx: 2, fill: d.v >= 0 ? "var(--c2)" : "var(--bad)", class: opts.onPick && d.raw != null ? "pickable" : null },
      s("title", null, `${d.k}: ${d.v >= 0 ? "+" : ""}${fmtVal(d.v, yf)} (running total ${fmtVal(d.b, yf)})`));
    if (opts.onPick && d.raw != null) rect.addEventListener("click", () => opts.onPick(res.columns[x], d.raw));
    svg.append(rect);
    if (i < bars.length - 1) svg.append(s("line", { x1: x0 + bw * 0.7, x2: x0 + bw, y1: sy(d.b), y2: sy(d.b), stroke: "var(--muted)", "stroke-dasharray": "2 2" }));
    svg.append(s("text", { x: x0 + bw * 0.35, y: H - padB + 14, "text-anchor": "middle" }, d.k.slice(0, Math.max(4, Math.floor(bw / 7)))));
  });
  const tx = padL + bars.length * bw + bw * 0.15;
  svg.append(s("rect", { x: tx, y: sy(Math.max(0, total)), width: bw * 0.7, height: Math.abs(sy(0) - sy(total)), rx: 2, fill: "var(--c1)" }, s("title", null, `Total: ${fmtVal(total, yf)}`)),
    s("text", { x: tx + bw * 0.35, y: H - padB + 14, "text-anchor": "middle", class: "lbl" }, "Total"),
    s("text", { x: tx + bw * 0.35, y: sy(Math.max(0, total)) - 5, "text-anchor": "middle", class: "lbl" }, shortVal(total, yf)));
  return svg;
}

/* Stages in their given order, with conversion from the first stage and from the previous one. */
function funnelChart(viz, res, W, opts) {
  const fm = fieldMap(res), { x, y } = pick(res, viz);
  const xf = fm[res.columns[x]], yf = fm[res.columns[y]] || {};
  const items = res.rows.slice(0, 10).map((r) => ({ raw: r[x], k: String(fmtVal(r[x], xf)), v: +r[y] || 0 }));
  if (!viz.keep_order) items.sort((a, b) => b.v - a.v);
  const first = items[0]?.v || 1, rowH = 34, H = items.length * rowH + 8, mid = W / 2, maxW = W * 0.62;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `Funnel of ${yf.label || ""}` });
  items.forEach((d, i) => {
    const w = Math.max(4, (d.v / first) * maxW), y0 = 4 + i * rowH;
    const rect = s("rect", { x: mid - w / 2, y: y0, width: w, height: rowH - 6, rx: 3, fill: SERIES[i % SERIES.length], "fill-opacity": 0.85, class: opts.onPick ? "pickable" : null },
      s("title", null, `${d.k}: ${fmtVal(d.v, yf)}`));
    if (opts.onPick) rect.addEventListener("click", () => opts.onPick(res.columns[x], d.raw));
    svg.append(rect, s("text", { x: mid - maxW / 2 - 10, y: y0 + rowH / 2 + 1, "text-anchor": "end", class: "lbl" }, d.k.slice(0, 18)),
      s("text", { x: mid + maxW / 2 + 10, y: y0 + rowH / 2 + 1 }, `${shortVal(d.v, yf)} · ${((d.v / first) * 100).toFixed(0)}%${i ? ` (${((d.v / (items[i - 1].v || 1)) * 100).toFixed(0)}% of previous)` : ""}`));
  });
  return svg;
}

/* KPI against a target, as a bullet bar. */
function kpiTarget(viz, res, y, yf) {
  const v = res.rows[0]?.[y], t = +viz.target;
  const base = kpiView(viz, res, y, yf);
  if (!(t > 0) || typeof v !== "number") return base;
  const max = Math.max(v, t) * 1.1, pct = v / t;
  const good = viz.direction === "down" ? v <= t : v >= t;
  base.append(h("div", { class: "bullet", role: "img", "aria-label": `${fmtVal(v, yf)} against target ${fmtVal(t, yf)}` },
    h("i", { class: good ? "ok" : "bad", style: { width: `${(v / max) * 100}%` } }), h("b", { style: { left: `${(t / max) * 100}%` } })),
  h("div", { class: "small muted" }, `${(pct * 100).toFixed(0)}% of target ${fmtVal(t, yf)}`));
  return base;
}

const EXTRA_VIZ = { combo: comboChart, multiline: multilineChart, scatter: scatterChart, treemap: treemapChart, waterfall: waterfallChart, funnel: funnelChart };
