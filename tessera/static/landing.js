"use strict";
(function () {
  /* Quality mosaic: 12 generated columns × 20 row-slices, each tile coloured by its rule failure rate. */
  function rng(seed) { let s = seed >>> 0; return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  const r = rng(42), N = 2000, SL = 20, per = N / SL;
  const cols = [
    { name: "order_id", p: 0.0012 }, { name: "customer_id", p: 0 }, { name: "order_date", p: 0.0008 }, { name: "status", p: 0.0015 },
    { name: "channel", p: 0 }, { name: "category", p: 0 }, { name: "quantity", p: 0.0004 }, { name: "unit_price", p: 0 },
    { name: "order_total", p: 0.002 }, { name: "email", p: 0.004 }, { name: "province", p: 0.0006 }, { name: "card_number", p: 0 },
  ];
  const fails = cols.map((c) => { const a = new Array(SL).fill(0); for (let i = 0; i < N; i++) { let p = c.p; if (c.name === "email" && i > 1200 && i < 1500) p = 0.2; if (r() < p) a[Math.floor(i / per)]++; } return a; });
  const cv = document.getElementById("mosaic");
  function draw() {
    if (!cv) return;
    const cs = getComputedStyle(document.documentElement), col = (k) => cs.getPropertyValue(k).trim();
    const ctx = cv.getContext("2d"), W = cv.width, H = cv.height, gap = 4;
    const tw = (W - gap * (cols.length - 1)) / cols.length, th = (H - gap * (SL - 1)) / SL;
    ctx.clearRect(0, 0, W, H);
    for (let c = 0; c < cols.length; c++) for (let s = 0; s < SL; s++) {
      const rate = fails[c][s] / per;
      ctx.fillStyle = rate === 0 ? col("--ok") : rate < 0.03 ? col("--warn") : col("--bad");
      ctx.globalAlpha = rate === 0 ? 0.32 + 0.24 * (((c * 7 + s * 3) % 5) / 4) : 1;
      ctx.fillRect(c * (tw + gap), s * (th + gap), tw, th);
    }
    ctx.globalAlpha = 1;
  }
  draw();
  const flagged = fails.reduce((a, f) => a + f.reduce((x, y) => x + y, 0), 0);
  const cap = document.getElementById("mosaic-cap");
  if (cap) cap.textContent = `${cols.length} columns · ${N.toLocaleString()} rows · ${flagged} failing values · email feed breaks mid-batch`;
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", draw);
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  /* Connector list: live from this server, or the embedded copy when the page is viewed standalone. */
  function renderConnectors(cat) {
    const box = document.getElementById("conn-list");
    if (!box || !cat) return;
    const items = [
      ...cat.types.map((t) => [t.label, t.status === "verified" ? "verified" : "driver", t.status === "verified"]),
      ...(cat.apps || []).map((a) => [a.label.split(":")[0], a.changes_only ? "app · changes only" : "app", true]),
      ...cat.via_generic.map((v) => [v.name, "via " + v.via, false]),
    ];
    box.replaceChildren(...items.map(([name, tag, ok]) => {
      const d = document.createElement("div");
      const n = document.createElement("span"); n.textContent = name;
      const c = document.createElement("span"); c.className = "chip" + (ok ? " ok" : ""); c.textContent = tag;
      d.append(n, c);
      return d;
    }));
    const t = document.getElementById("conn-title");
    if (t) t.textContent = `${items.length} sources and integrations`;
  }
  if (window.TESSERA_CONNECTORS) renderConnectors(window.TESSERA_CONNECTORS);
  else fetch("/api/connectors").then((x) => (x.ok ? x.json() : null)).then(renderConnectors).catch(() => {});
})();
