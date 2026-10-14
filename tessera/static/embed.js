"use strict";
/* Read-only shared dashboard. Uses only the link token; no cookies or session. */
(async () => {
  const token = location.pathname.split("/").pop();
  const main = $("#main");
  const get = async (url, method = "GET") => {
    const r = await fetch(url, { method, credentials: "omit", headers: { "content-type": "application/json" } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `Request failed (${r.status})`);
    return j;
  };
  try {
    const d = await get(`/api/embed/${encodeURIComponent(token)}`);
    const grid = h("div", { class: "dash" });
    d.tiles.forEach((t) => {
      if (t.type === "text") { grid.append(h("div", { class: "tile text", style: { "--w": t.w || 12 } }, md(t.text))); return; }
      const body = h("div", { class: "tilebody" }, h("div", { class: "skeleton" }));
      const tile = h("div", { class: "tile" + (t.viz?.type === "kpi" ? " kpi-tile" : ""), style: { "--w": t.w || 6 } }, h("h3", null, t.title), body);
      if (t.h) tile.style.setProperty("--h", { s: "180px", m: "300px", l: "440px" }[t.h] || "auto");
      grid.append(tile);
      get(`/api/embed/${encodeURIComponent(token)}/tiles/${t.i}`, "POST").then((r) => body.replaceChildren(renderViz(t.viz, r.result, { wide: (t.w || 6) >= 8 && wideScreen() })))
        .catch((e) => body.replaceChildren(h("p", { class: "small", style: { color: "var(--bad)" } }, e.message)));
    });
    const locked = (d.locked_filters || []).map((f) => `${f.field} ${f.op === "not_in" ? "is not" : "is"} ${[].concat(f.value).join(", ")}`);
    main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("h1", null, d.name), d.description ? h("span", { class: "muted small" }, d.description) : null,
      locked.length ? h("div", { class: "row small" }, h("span", { class: "muted" }, "Showing:"), locked.map((l) => h("span", { class: "chip info" }, l))) : null)),
    grid, h("p", { class: "small muted embed-foot" }, "Read-only view shared from Tessera."));
  } catch (e) {
    main.replaceChildren(h("div", { class: "notice bad" }, e.message));
  }
})();
