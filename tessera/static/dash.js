"use strict";
/* Interactive dashboards: slicers, date ranges, cross-filtering, drill-down, exports, print, TV mode, subscriptions. */

const DATE_PRESETS = [["all", "All time"], ["30d", "Last 30 days"], ["90d", "Last 90 days"], ["mtd", "Month to date"], ["lm", "Last month"], ["qtd", "Quarter to date"], ["lq", "Last quarter"], ["ytd", "Year to date"], ["ly", "Last year"], ["custom", "Custom range"]];
function presetRange(p) {
  const t = new Date(), iso = (d) => d.toISOString().slice(0, 10), y = t.getFullYear(), m = t.getMonth(), q = Math.floor(m / 3);
  const day = (d) => new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const today = day(t);
  switch (p) {
    case "30d": return [iso(new Date(today - 29 * 864e5)), iso(today)];
    case "90d": return [iso(new Date(today - 89 * 864e5)), iso(today)];
    case "mtd": return [iso(new Date(Date.UTC(y, m, 1))), iso(today)];
    case "lm": return [iso(new Date(Date.UTC(y, m - 1, 1))), iso(new Date(Date.UTC(y, m, 0)))];
    case "qtd": return [iso(new Date(Date.UTC(y, q * 3, 1))), iso(today)];
    case "lq": return [iso(new Date(Date.UTC(y, q * 3 - 3, 1))), iso(new Date(Date.UTC(y, q * 3, 0)))];
    case "ytd": return [iso(new Date(Date.UTC(y, 0, 1))), iso(today)];
    case "ly": return [iso(new Date(Date.UTC(y - 1, 0, 1))), iso(new Date(Date.UTC(y - 1, 11, 31)))];
    default: return null;
  }
}

route("dashboards", async (main, id, sub) => {
  if (id) return dashboardView(main, +id, sub === "edit");
  const list = await GET("/api/dashboards");
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Analyze"), h("h1", null, "Dashboards")),
    can("analyst") ? h("button", { class: "primary", onclick: () => { const n = h("input", { id: "dash-name", placeholder: "Weekly operations" }); dialog("New dashboard", field("Name", n), [{ label: "Create", run: async () => { const d = await POST("/api/dashboards", { name: n.value.trim() || "Untitled", tiles: [] }); go(`#/dashboards/${d.id}/edit`); } }]); } }, "New dashboard") : null),
  list.length ? h("div", { class: "grid2" }, list.map((d) => h("button", { class: "card agentcard", onclick: () => go("#/dashboards/" + d.id) },
    h("b", null, d.name), h("span", { class: "small muted" }, d.description || `${d.tiles.length} tiles`),
    h("span", { class: "row small muted" }, `${d.tiles.length} tiles`, (d.filters || []).length ? ` · ${(d.filters || []).length} filters` : "", " · updated " + ago(d.updated_at)))))
    : h("div", { class: "empty" }, "No dashboards yet. Build a report in Explore and add it to a dashboard."));
});

async function dashboardView(main, id, edit) {
  const [d, queries, models] = await Promise.all([GET(`/api/dashboards/${id}`), GET("/api/queries"), GET("/api/models")]);
  const qById = Object.fromEntries(queries.map((q) => [q.id, q]));
  const mById = Object.fromEntries(models.map((m) => [m.id, m]));
  const tiles = d.tiles.slice();
  const filtersDef = (d.filters || []).slice();
  const key = `tessera.dash.${id}`;
  let state = { values: {}, cross: [], grains: {} };
  try { state = { ...state, ...(JSON.parse(sessionStorage.getItem(key) || "{}")) }; } catch {}
  const persist = () => { try { sessionStorage.setItem(key, JSON.stringify(state)); } catch {} };
  const activeFilters = (exceptQuery) => {
    const out = [];
    filtersDef.forEach((f, i) => {
      const v = state.values[i];
      if (f.type === "date_range" && v && v.preset !== "all") {
        const r = v.preset === "custom" ? [v.from, v.to] : presetRange(v.preset);
        if (r && (r[0] || r[1])) out.push({ model_id: f.model_id, field: f.field, op: "between", value: r });
      } else if (f.type === "select" && v && v.length) out.push({ model_id: f.model_id, field: f.field, op: "in", value: v });
    });
    // the visual you clicked keeps all its bars and highlights the selection; every other visual is filtered
    state.cross.filter((c) => c.query_id !== exceptQuery).forEach((c) => out.push({ model_id: c.model_id, field: c.field, op: "in", value: [c.value] }));
    return out;
  };
  const hasDate = () => activeFilters().some((f) => f.op === "between");
  const saveTiles = () => PUT(`/api/dashboards/${id}`, { tiles, filters: filtersDef });

  /* ---- filter bar ---- */
  const bar = h("div", { class: "filterbar" });
  const drawBar = () => {
    const parts = filtersDef.map((f, i) => {
      if (f.type === "date_range") {
        const v = state.values[i] || { preset: "all" };
        const sel = h("select", { "aria-label": f.label }, DATE_PRESETS.map(([k, l]) => h("option", { value: k, selected: v.preset === k }, l)));
        const from = h("input", { type: "date", value: v.from || "", "aria-label": "From" }), to = h("input", { type: "date", value: v.to || "", "aria-label": "To" });
        const upd = () => { state.values[i] = { preset: sel.value, from: from.value, to: to.value }; persist(); drawBar(); drawTiles(); };
        sel.addEventListener("change", upd); from.addEventListener("change", upd); to.addEventListener("change", upd);
        return h("div", { class: "slicer" }, h("span", { class: "eyebrow" }, f.label), h("div", { class: "row", style: { flexWrap: "nowrap" } }, sel, v.preset === "custom" ? [from, to] : null));
      }
      const chosen = state.values[i] || [];
      const box = h("div", { class: "optlist" }, h("p", { class: "small muted" }, "Loading…"));
      const det = h("details", { class: "multi" }, h("summary", null, chosen.length ? `${chosen.slice(0, 2).join(", ")}${chosen.length > 2 ? ` +${chosen.length - 2}` : ""}` : "All"), box);
      det.addEventListener("toggle", async () => {
        if (!det.open) return;
        const vals = await GET(`/api/models/${f.model_id}/values?field=${encodeURIComponent(f.field)}`).catch(() => []);
        box.replaceChildren(...vals.map((v) => { const cb = h("input", { type: "checkbox", checked: chosen.includes(String(v)) }); cb.addEventListener("change", () => {
          const cur = new Set(state.values[i] || []); cb.checked ? cur.add(String(v)) : cur.delete(String(v)); state.values[i] = [...cur]; persist(); drawTiles();
          det.querySelector("summary").textContent = state.values[i].length ? state.values[i].slice(0, 2).join(", ") + (state.values[i].length > 2 ? ` +${state.values[i].length - 2}` : "") : "All";
        }); return h("label", { class: "row small" }, cb, String(v)); }), h("button", { class: "sm", onclick: () => { state.values[i] = []; persist(); drawBar(); drawTiles(); } }, "Clear"));
      });
      return h("div", { class: "slicer" }, h("span", { class: "eyebrow" }, f.label), det);
    });
    const chips = state.cross.map((c, i) => h("span", { class: "chip info crosschip" }, `${c.label}: ${c.value}`, h("button", { class: "sm ghost", "aria-label": "Remove filter", onclick: () => { state.cross.splice(i, 1); persist(); drawBar(); drawTiles(); } }, "✕")));
    const any = activeFilters().length;
    bar.replaceChildren(...parts, chips.length ? h("div", { class: "slicer" }, h("span", { class: "eyebrow" }, "Selected"), h("div", { class: "row" }, chips)) : null,
      any ? h("button", { class: "sm ghost", style: { alignSelf: "end" }, onclick: () => { state = { values: {}, cross: [], grains: {} }; persist(); drawBar(); drawTiles(); } }, "Reset filters") : null);
    bar.hidden = !parts.length && !chips.length;
  };

  /* ---- tiles ---- */
  const grid = h("div", { class: "dash" + (edit ? " designing" : "") });
  let dragFrom = null;
  // edit mode: drag a tile to reorder, drag its right edge to resize (snaps to the 12-column grid)
  const designable = (el, t, i) => {
    if (!edit) return el;
    el.draggable = true;
    el.classList.add("designable");
    el.addEventListener("dragstart", (e) => { if (e.target.closest(".resize, select, button, input")) { e.preventDefault(); return; } dragFrom = i; el.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", String(i)); } catch {} });
    el.addEventListener("dragend", () => { el.classList.remove("dragging"); grid.querySelectorAll(".dropbefore,.dropafter").forEach((x) => x.classList.remove("dropbefore", "dropafter")); });
    el.addEventListener("dragover", (e) => { if (dragFrom == null || dragFrom === i) return; e.preventDefault(); const r = el.getBoundingClientRect(); const after = e.clientX > r.left + r.width / 2;
      el.classList.toggle("dropafter", after); el.classList.toggle("dropbefore", !after); });
    el.addEventListener("dragleave", () => el.classList.remove("dropbefore", "dropafter"));
    el.addEventListener("drop", async (e) => {
      e.preventDefault();
      if (dragFrom == null || dragFrom === i) return;
      const r = el.getBoundingClientRect(), after = e.clientX > r.left + r.width / 2;
      const [moved] = tiles.splice(dragFrom, 1);
      let to = i + (after ? 1 : 0); if (dragFrom < to) to -= 1;
      tiles.splice(to, 0, moved); dragFrom = null;
      await saveTiles(); drawTiles();
    });
    const handle = h("span", { class: "resize", title: "Drag to resize", "aria-hidden": "true" });
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault(); e.stopPropagation();
      handle.setPointerCapture(e.pointerId);
      const col = grid.clientWidth / 12, left = el.getBoundingClientRect().left;
      const move = (ev) => { const w = Math.max(2, Math.min(12, Math.round((ev.clientX - left) / col))); el.style.setProperty("--w", w); t.w = w; };
      const up = async () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); await saveTiles(); drawTiles(); };
      handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
    });
    el.append(handle);
    return el;
  };
  const drawTiles = () => {
    grid.replaceChildren(...tiles.map((t, i) => {
      const controls = edit ? h("div", { class: "row" },
        h("select", { "aria-label": "Width", onchange: async (e) => { t.w = +e.target.value; await saveTiles(); drawTiles(); } }, [3, 4, 6, 8, 12].map((w) => h("option", { value: w, selected: (t.w || 6) === w }, `${w}/12`))),
        t.type === "text" ? null : h("select", { "aria-label": "Height", onchange: async (e) => { t.h = e.target.value || undefined; await saveTiles(); drawTiles(); } },
          [["", "auto height"], ["s", "short"], ["m", "medium"], ["l", "tall"]].map(([v, l]) => h("option", { value: v, selected: (t.h || "") === v }, l))),
        i > 0 ? h("button", { class: "sm", "aria-label": "Move earlier", onclick: async () => { [tiles[i - 1], tiles[i]] = [tiles[i], tiles[i - 1]]; await saveTiles(); drawTiles(); } }, "←") : null,
        t.type === "text" ? h("button", { class: "sm", onclick: () => textTileDialog(t, async () => { await saveTiles(); drawTiles(); }) }, "Edit") : null,
        h("button", { class: "sm danger", onclick: async () => { tiles.splice(i, 1); await saveTiles(); drawTiles(); } }, "Remove")) : null;
      if (t.type === "text") return designable(h("div", { class: "tile text", style: { "--w": t.w || 12 } }, controls ? h("div", { class: "spread" }, h("span"), controls) : null, md(t.text)), t, i);
      const q = qById[t.query_id];
      const body = h("div", { class: "tilebody" }, h("div", { class: "skeleton" }));
      const title = t.title || q?.name || "Missing report";
      const timeDim = q?.kind === "model" ? (q.spec.dimensions || []).find((n) => mById[q.model_id]?.dimensions.find((x) => x.name === n)?.type === "time") : null;
      const grainSel = timeDim && !edit ? (() => { const g = h("select", { class: "grain", "aria-label": "Drill level" }, GRAINS.map((x) => h("option", { selected: (state.grains[t.query_id] || q.spec.grains?.[timeDim] || "month") === x }, x))); g.addEventListener("change", () => { state.grains[t.query_id] = g.value; persist(); drawTiles(); }); return g; })() : null;
      const menu = !edit && q ? h("details", { class: "tilemenu" }, h("summary", { "aria-label": "Tile actions" }, "⋯"), h("div", { class: "menu" },
        h("button", { class: "ghost", onclick: () => viewData(q, title) }, "View data"),
        h("button", { class: "ghost", onclick: (e) => act(e.target, () => downloadPost(`/api/queries/${q.id}/export`, { format: "xlsx", filters: activeFilters() })) }, "Export Excel"),
        h("button", { class: "ghost", onclick: (e) => act(e.target, () => downloadPost(`/api/queries/${q.id}/export`, { format: "csv", filters: activeFilters() })) }, "Export CSV"),
        q.kind === "model" && mById[q.model_id] ? h("button", { class: "ghost", onclick: () => decompositionDialog(mById[q.model_id], (q.spec.measures || [])[0], activeFilters(q.id).filter((f) => f.model_id === q.model_id).map(({ model_id, ...f }) => f)) }, "Deep dive") : null,
        q.kind === "model" && mById[q.model_id]?.dimensions.some((x) => x.type === "time") && (q.spec.measures || []).length ? h("button", { class: "ghost", onclick: () => explainDialog(q.model_id, q.spec.measures[0], activeFilters(q.id).filter((f) => f.model_id === q.model_id && f.op !== "between").map(({ model_id, ...f }) => f), mById[q.model_id]) }, "Explain change") : null,
        h("button", { class: "ghost", onclick: () => go("#/reports/" + q.id) }, "Open report"),
        can("analyst") ? h("button", { class: "ghost", onclick: () => alertDialog({ source: { type: "query", query_id: q.id }, name: `${title} alert` }) }, "Create alert") : null)) : null;
      const tile = h("div", { class: "tile" + (q?.viz?.type === "kpi" ? " kpi-tile" : ""), style: { "--w": t.w || 6 } }, h("div", { class: "spread" }, h("h3", null, title), h("div", { class: "row", style: { gap: "4px" } }, grainSel, menu, controls)), body);
      const viewData = async (qq, ttl) => {
        const r = await POST(`/api/queries/${qq.id}/run`, overrides(qq));
        dialog(ttl, dataTable(r.result));
      };
      const overrides = (qq) => ({ filters: activeFilters(qq.id), grains: timeDim && state.grains[qq.id] ? { [timeDim]: state.grains[qq.id] } : undefined, compare: (qq.viz?.type === "kpi" && hasDate()) || undefined });
      if (q) {
        POST(`/api/queries/${q.id}/run`, overrides(q)).then((r) => {
          const pickable = q.kind === "model" && (q.viz?.crossfilter || ["bar", "donut", "stacked"].includes(q.viz?.type));
          const sel = state.cross.find((c) => c.query_id === q.id);
          body.replaceChildren(renderViz(q.viz, r.result, { wide: (t.w || 6) >= 8 && wideScreen(), selected: sel?.value, onPick: pickable && !edit ? (field, value) => {
            const model = mById[q.model_id];
            const dim = model?.dimensions.find((x) => x.name === field);
            if (!dim || dim.type === "time") return;
            const existing = state.cross.findIndex((c) => c.model_id === q.model_id && c.field === field);
            if (existing >= 0 && state.cross[existing].value === String(value)) state.cross.splice(existing, 1);
            else if (existing >= 0) state.cross[existing] = { model_id: q.model_id, field, value: String(value), label: dim.label, query_id: q.id };
            else state.cross.push({ model_id: q.model_id, field, value: String(value), label: dim.label, query_id: q.id });
            persist(); drawBar(); drawTiles();
          } : null }));
        }).catch((e) => body.replaceChildren(h("p", { class: "small", style: { color: "var(--bad)" } }, e.message)));
      } else body.replaceChildren(h("p", { class: "muted small" }, "This tile's report was deleted."));
      if (t.h) tile.style.setProperty("--h", { s: "180px", m: "300px", l: "440px" }[t.h] || "auto");
      return designable(tile, t, i);
    }));
  };

  const addTile = () => {
    const qs = h("select", { id: "tile-q" }, h("option", { value: "text" }, "Text or heading"), queries.map((q) => h("option", { value: q.id }, `${q.name} (${q.viz?.type || "table"})`)));
    const w = h("select", { id: "tile-w" }, [3, 4, 6, 8, 12].map((x) => h("option", { value: x, selected: x === 6 }, `${x}/12 width`)));
    dialog("Add tile", h("div", { class: "stack" }, h("div", { class: "form" }, field("Content", qs), field("Width", w)), h("p", { class: "small muted" }, "New reports are built in Explore or SQL, then saved.")),
      [{ label: "Add", run: async () => {
        if (qs.value === "text") tiles.push({ type: "text", w: +w.value, text: "## Heading\nWrite context for readers here." });
        else tiles.push({ query_id: +qs.value, w: +w.value });
        await saveTiles(); drawTiles();
      } }]);
  };
  const addFilter = () => {
    const ms = h("select", { id: "fl-model" }, models.map((m) => h("option", { value: m.id }, m.name)));
    const fs = h("select", { id: "fl-field" });
    const syncF = () => fs.replaceChildren(...(mById[+ms.value]?.dimensions || []).map((x) => h("option", { value: x.name }, `${x.label}${x.type === "time" ? " (date range)" : ""}`)));
    ms.addEventListener("change", syncF); syncF();
    dialog("Add a filter", h("div", { class: "stack" }, h("div", { class: "form" }, field("Model", ms), field("Field", fs)), h("p", { class: "small muted" }, "Filters apply to every tile built on the same semantic model.")),
      [{ label: "Add filter", run: async () => {
        const m = mById[+ms.value], dim = m.dimensions.find((x) => x.name === fs.value);
        filtersDef.push({ model_id: m.id, field: dim.name, label: dim.label, type: dim.type === "time" ? "date_range" : "select" });
        await saveTiles(); drawBar(); drawTiles();
      } }]);
  };
  const tv = async () => {
    const el = $("#main");
    try { await el.requestFullscreen(); } catch { toast("Full screen is not available here", "bad"); return; }
    el.classList.add("tv");
    const t = setInterval(drawTiles, 60_000);
    const off = () => { if (!document.fullscreenElement) { el.classList.remove("tv"); clearInterval(t); document.removeEventListener("fullscreenchange", off); } };
    document.addEventListener("fullscreenchange", off);
  };
  const subscribe = () => {
    const n = h("input", { id: "sub-name", value: `${d.name} digest` });
    const r = h("input", { id: "sub-to", value: T.me.email, placeholder: "a@company.com, b@company.com" });
    const s2 = h("select", { id: "sub-when" }, [["0 8 * * *", "Daily at 08:00"], ["0 8 * * 1", "Weekly, Monday 08:00"], ["0 8 1 * *", "Monthly, 1st at 08:00"]].map(([v, l]) => h("option", { value: v }, l)));
    const att = h("select", { id: "sub-att" }, [["xlsx", "Excel workbook"], ["csv", "CSV files"], ["none", "No attachment"]].map(([v, l]) => h("option", { value: v }, l)));
    const keep = h("input", { type: "checkbox", id: "sub-keep", checked: activeFilters().length > 0 });
    const eb = h("input", { type: "checkbox", id: "sub-brief", checked: true });
    dialog("Email this dashboard", h("div", { class: "stack" }, h("div", { class: "form" }, field("Name", n), field("Recipients", r), field("Schedule (server time zone)", s2), field("Attachment", att)),
      h("label", { class: "row small" }, keep, "Use the filters currently applied"), h("label", { class: "row small" }, eb, "Start with the executive brief (headlines, full-year forecast, target likelihood)"), h("p", { class: "small muted" }, "Recipients get a summary with KPI values and tables. Data reflects your access, including row-level security.")),
      [{ label: "Subscribe", run: async () => {
        const sub = await POST("/api/subscriptions", { name: n.value, dashboard_id: id, recipients: r.value, schedule: s2.value, attach: att.value, include_brief: eb.checked, filters: keep.checked ? activeFilters() : [] });
        toast("Subscription created. Manage it under Alerts.");
        try { await POST(`/api/subscriptions/${sub.id}/send`); toast("First copy sent"); } catch (e) { toast(e.message, "bad"); }
      } }]);
  };
  /* ---- AI side panel: insights and Ask AI ---- */
  const side = h("aside", { class: "sidepanel noprint", hidden: true, "aria-label": "AI analysis" });
  const wrap = h("div", { class: "dashwrap" }, grid, side);
  let sideMode = null;
  const explain = (mid, measure) => explainDialog(mid, measure, activeFilters().filter((f) => f.model_id === mid && f.op !== "between").map(({ model_id, ...f }) => f), mById[mid]);
  const openSide = (mode) => {
    if (sideMode === mode) { sideMode = null; side.hidden = true; wrap.classList.remove("open"); return; }
    sideMode = mode; side.hidden = false; wrap.classList.add("open");
    side.replaceChildren(h("div", { class: "spread" }, h("span"), h("button", { class: "sm ghost", "aria-label": "Close panel", onclick: () => openSide(mode) }, "✕")),
      mode === "insights" ? insightsPanel(d, () => activeFilters(), explain).el : askPanel(d, () => activeFilters()));
  };
  const viewState = () => JSON.parse(JSON.stringify(state));
  const applyView = (st) => { state = { values: {}, cross: [], grains: {}, ...(st || {}) }; persist(); drawBar(); drawTiles(); };
  // Live mode: redraw every 5 seconds while the tab is visible. Remembered per dashboard in this browser.
  const liveKey = `tessera.live.${id}`;
  let live = false, liveTimer = null;
  try { live = localStorage.getItem(liveKey) === "1"; } catch {}
  const liveBtn = h("button", { class: "livebtn", "aria-pressed": "false", title: "Refresh every 5 seconds (for streaming data)" }, h("i", { class: "pulse", "aria-hidden": "true" }), "Live");
  const startLive = () => { liveBtn.setAttribute("aria-pressed", "true"); liveTimer = setInterval(() => { if (!document.hidden) drawTiles(); }, 5000); T.timers.push(liveTimer); };
  liveBtn.addEventListener("click", () => {
    live = !live;
    try { localStorage.setItem(liveKey, live ? "1" : "0"); } catch {}
    if (live) { startLive(); toast("Live: refreshing every 5 seconds"); } else { clearInterval(liveTimer); liveBtn.setAttribute("aria-pressed", "false"); }
  });
  const settingsBtn = edit ? h("div", { class: "row" }, h("span", { class: "small muted" }, "Drag tiles to reorder · drag the right edge to resize"), h("button", { onclick: addFilter }, "Add filter"), h("button", { onclick: addTile }, "Add tile"),
    filtersDef.length ? h("button", { onclick: () => dialog("Remove a filter", h("div", { class: "stack" }, filtersDef.map((f, i) => h("div", { class: "spread" }, h("span", null, f.label), h("button", { class: "sm danger", onclick: async (e) => { filtersDef.splice(i, 1); await saveTiles(); e.target.closest("dialog").close(); drawBar(); drawTiles(); } }, "Remove")))), []) }, "Remove filter") : null,
    h("button", { class: "primary", onclick: () => go("#/dashboards/" + id) }, "Done")) : h("div", { class: "row" },
    h("button", { class: "primary", onclick: () => go(`#/brief/${id}`) }, "Executive brief"),
    h("button", { class: "ai-btn", onclick: () => openSide("insights") }, "✦ Insights"),
    can("analyst") ? h("button", { class: "ai-btn", onclick: () => openSide("ask") }, "Ask AI") : null,
    viewsMenu(d, viewState, applyView),
    h("button", { onclick: () => drawTiles() }, "Refresh"),
    liveBtn,
    h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/dashboards/${id}/export`, { filters: activeFilters() })) }, "Excel"),
    h("button", { onclick: () => window.print() }, "Print / PDF"),
    h("button", { onclick: tv }, "TV mode"),
    can("analyst") ? h("button", { onclick: subscribe }, "Subscribe") : null,
    can("analyst") ? h("button", { onclick: () => shareDialog(d, activeFilters().filter((f) => f.op !== "between")) }, "Share") : null,
    can("analyst") ? h("button", { onclick: () => go(`#/dashboards/${id}/edit`) }, "Edit") : null,
    can("analyst") ? h("button", { class: "danger", onclick: () => confirmBox(`Delete dashboard ${d.name}?`, "Delete", async () => { await DEL(`/api/dashboards/${id}`); go("#/dashboards"); }) }, "Delete") : null);
  drawBar();
  drawTiles();
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/dashboards", class: "small noprint" }, "← Dashboards"), h("h1", null, d.name), d.description ? h("span", { class: "muted small" }, d.description) : null),
    h("div", { class: "noprint" }, settingsBtn)),
  bar, tiles.length ? wrap : h("div", { class: "empty" }, "No tiles yet.", can("analyst") ? h("button", { class: "primary", onclick: () => (edit ? addTile() : go(`#/dashboards/${id}/edit`)) }, "Add a tile") : null));
  if (d.refresh_seconds > 0) T.timers.push(setInterval(() => { if (!document.hidden) drawTiles(); }, Math.max(5, d.refresh_seconds) * 1000));
  if (live) startLive();
}

function textTileDialog(t, onSave) {
  const ta = h("textarea", { id: "tt-text", rows: 8 });
  ta.value = t.text || "";
  dialog("Text tile", h("div", { class: "stack" }, field("Text (Markdown: # heading, **bold**, - list, [link](https://…))", ta)), [{ label: "Save", run: async () => { t.text = ta.value; await onSave(); } }]);
}

async function shareDialog(d, current) {
  const list = h("div");
  const drawList = async () => {
    const links = await GET(`/api/dashboards/${d.id}/shares`).catch(() => []);
    list.replaceChildren(links.length ? table([
      { label: "Link", render: (x) => h("span", { class: "mono small" }, x.prefix + "…") }, { label: "Locked to", render: (x) => (x.filters || []).map((f) => `${f.field}: ${[].concat(f.value).join(", ")}`).join("; ") || "everything you can see" },
      { label: "Expires", render: (x) => new Date(x.expires_at).toLocaleDateString() }, { label: "Views", num: true, key: "views" },
      { label: "", render: (x) => x.revoked ? chip("revoked", "bad") : h("button", { class: "sm danger", onclick: async () => { await DEL(`/api/shares/${x.id}`); drawList(); } }, "Revoke") }], links)
      : h("p", { class: "small muted" }, "No links yet."));
  };
  const days = h("select", { id: "sh-days" }, [[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "1 year"]].map(([v, l]) => h("option", { value: v, selected: v === 30 }, l)));
  const lock = h("input", { type: "checkbox", id: "sh-lock", checked: current.length > 0, disabled: !current.length });
  const note = h("input", { id: "sh-note", placeholder: "Board deck, partner portal…" });
  const out = h("div");
  dialog("Share outside Tessera", h("div", { class: "stack" },
    h("div", { class: "notice warn small" }, "Anyone with the link sees this dashboard read-only with your access, narrowed by any locked filters. Links expire and can be revoked; turning sharing off revokes all of them."),
    h("div", { class: "form" }, field("Expires after", days), field("Note", note)),
    h("label", { class: "row small" }, lock, current.length ? `Lock to the current filters (${current.map((f) => `${f.field}: ${[].concat(f.value).join(", ")}`).join("; ")})` : "Pick slicer values on the dashboard first to lock a link to them"),
    h("button", { class: "primary", onclick: async (e) => {
      const r = await act(e.target, () => POST(`/api/dashboards/${d.id}/share`, { expires_days: +days.value, note: note.value, filters: lock.checked ? current : [] }));
      const a = h("input", { readonly: true, value: r.url }), b = h("textarea", { readonly: true, rows: 3 }); b.value = r.iframe;
      out.replaceChildren(h("div", { class: "stack" }, h("div", { class: "notice" }, r.note), field("Link", a), h("button", { class: "sm", onclick: () => copyText(r.url, a) }, "Copy link"),
        field("Embed code", b), h("button", { class: "sm", onclick: () => copyText(r.iframe, b) }, "Copy embed code")));
      drawList();
    } }, "Create link"), out, h("h3", null, "Existing links"), list), []);
  drawList();
}
