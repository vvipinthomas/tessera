"use strict";
/* Streams: continuous ingestion from Kafka-protocol brokers, Kinesis, MQTT and HTTP push. */
const STREAM_KINDS = {
  kafka: { label: "Kafka topic", hint: "Kafka, Confluent, Redpanda, MSK, Event Hubs", key: "topic", ph: "orders" },
  kinesis: { label: "Kinesis stream", hint: "Amazon Kinesis Data Streams", key: "stream", ph: "clickstream" },
  mqtt: { label: "MQTT topic", hint: "Devices and IoT brokers. Wildcards + and # work.", key: "topic", ph: "plant/+/temperature" },
  webhook: { label: "HTTP push", hint: "Anything that can POST JSON", key: null },
};
const CONN_KIND = { kafka: "kafka", eventhubs: "kafka", kinesis: "kinesis", mqtt: "mqtt" };

function streamStatus(s) {
  const st = s.desired === "stopped" ? "stopped" : s.status;
  const k = { running: "ok", retrying: "bad", starting: "info", stopped: "" }[st] ?? "";
  return h("span", { class: "chip live-chip " + k }, st === "running" ? h("i", { class: "pulse", "aria-hidden": "true" }) : null, st);
}

function sparkBars(recent, w = 120, hgt = 28) {
  const now = Date.now() / 1000, buckets = Array(30).fill(0);
  (recent || []).forEach(([t, n]) => { const i = 29 - Math.floor((now - t) / 10); if (i >= 0 && i < 30) buckets[i] += n; });
  const max = Math.max(1, ...buckets), bw = w / 30;
  const svg = `<svg viewBox="0 0 ${w} ${hgt}" width="${w}" height="${hgt}" role="img" aria-label="Events in the last 5 minutes">` +
    buckets.map((v, i) => `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${(hgt - (v / max) * (hgt - 2)).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${((v / max) * (hgt - 2)).toFixed(1)}" rx="1"/>`).join("") + "</svg>";
  return h("span", { class: "spark", html: svg });
}

route("streams", async (main, id, sub) => {
  if (!can("analyst")) { main.replaceChildren(h("div", { class: "notice" }, "Streams are visible to analysts and engineers.")); return; }
  if (id === "new") return newStream(main, sub ? +sub : null);
  if (id) return streamView(main, +id);
  const draw = async () => {
    const list = await GET("/api/streams");
    const running = list.filter((s) => s.desired === "running").length;
    const perMin = list.reduce((a, s) => a + (s.events_per_min || 0), 0);
    const failing = list.filter((s) => s.status === "retrying").length;
    main.replaceChildren(
      h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Data"), h("h1", null, "Streams"),
        h("span", { class: "muted small" }, "Continuous ingestion into the raw zone. Positions are saved after each batch lands, so a restart resumes without loss.")),
        can("engineer") ? h("button", { class: "primary", onclick: () => go("#/streams/new") }, "New stream") : null),
      h("div", { class: "kpis" },
        kpi("Running", fmt(running), `${list.length} defined`), kpi("Events / min", fmt(perMin), "across all streams"),
        kpi("Needs attention", fmt(failing), failing ? "retrying with errors" : "all healthy", failing ? "bad" : "ok")),
      list.length ? h("div", { class: "streamgrid" }, list.map((s) => h("a", { class: "card streamcard", href: "#/streams/" + s.id },
        h("div", { class: "spread" }, h("b", null, s.name), streamStatus(s)),
        h("div", { class: "small muted" }, STREAM_KINDS[s.kind]?.label || s.kind, " → ", h("span", { class: "mono" }, s.target)),
        h("div", { class: "spread" }, sparkBars(s.meta?.recent), h("div", { class: "num-stack" }, h("b", null, fmt(s.events_per_min)), h("span", { class: "small muted" }, "per min"))),
        h("div", { class: "spread small muted" }, h("span", null, fmt(s.meta?.rows_total || 0), " rows"),
          s.lag != null ? h("span", null, "lag ", fmt(s.lag), s.kind === "kinesis" ? " ms" : "") : h("span", null, "last batch ", ago(s.meta?.last_batch_at))),
        s.status === "retrying" && s.last_error ? h("div", { class: "notice bad small" }, s.last_error) : null)))
        : h("div", { class: "empty" }, h("b", null, "No streams yet."),
          h("p", { class: "muted" }, "Connect a Kafka, Event Hubs, Kinesis or MQTT source, or create an HTTP push endpoint for webhooks and app events."),
          can("engineer") ? h("button", { class: "primary", onclick: () => go("#/streams/new") }, "Create a stream") : null));
  };
  await draw();
  T.timers.push(setInterval(() => { if (!document.hidden) safe(draw()); }, 4000));
});

function kpi(label, value, note, kind) {
  return h("div", { class: "kpi " + (kind || "") }, h("span", { class: "eyebrow" }, label), h("b", null, value), h("span", { class: "small muted" }, note));
}

async function newStream(main, connId) {
  if (!can("engineer")) { main.replaceChildren(h("div", { class: "notice" }, "Engineers create streams.")); return; }
  const conns = (await GET("/api/connections")).filter((c) => CONN_KIND[c.type]);
  const pre = conns.find((c) => c.id === connId);
  const kind = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Source kind" });
  let k = pre ? CONN_KIND[pre.type] : (conns[0] ? CONN_KIND[conns[0].type] : "webhook");
  const name = h("input", { id: "st-name", placeholder: "Orders (live)" });
  const conn = h("select", { id: "st-conn" });
  const src = h("input", { id: "st-src" });
  const target = h("input", { id: "st-target", placeholder: "raw.orders_live" });
  const format = h("select", { id: "st-format" }, h("option", { value: "json" }, "JSON (one object per event)"), h("option", { value: "text" }, "Text (stored in a value column)"));
  const start = h("select", { id: "st-start" }, h("option", { value: "earliest" }, "From the beginning (backfill)"), h("option", { value: "latest" }, "Only new events"));
  const batch = h("input", { id: "st-batch", type: "number", min: 1, value: 5000 });
  const wait = h("input", { id: "st-wait", type: "number", min: 0.2, step: 0.1, value: 2 });
  const dedupe = h("input", { id: "st-dedupe", placeholder: "event_id (optional)" });
  const retention = h("input", { id: "st-retention", type: "number", min: 1, placeholder: "keep everything" });
  const srcField = field("Topic", src), connField = field("Connection", conn);
  const advanced = h("details", { class: "advanced" }, h("summary", null, "Batching, de-duplication and retention"),
    h("div", { class: "form" }, field("Max events per batch", batch), field("Max wait per batch (seconds)", wait, "Lower is fresher; higher writes fewer, larger batches."),
      field("De-duplicate on column", dedupe, "Makes replays after a crash harmless. Keeps the latest event per key."),
      field("Keep events for (days)", retention, "Older rows are trimmed hourly.")));
  const draw = () => {
    kind.replaceChildren(Object.entries(STREAM_KINDS).map(([kk, v]) => h("button", { type: "button", role: "radio", "aria-checked": String(kk === k), class: kk === k ? "on" : "",
      onclick: () => { k = kk; draw(); } }, h("b", null, v.label), h("span", { class: "small muted" }, v.hint))));
    const opts = conns.filter((c) => CONN_KIND[c.type] === k);
    conn.replaceChildren(opts.map((c) => h("option", { value: c.id, selected: pre && c.id === pre.id }, c.name)));
    const meta = STREAM_KINDS[k];
    connField.hidden = srcField.hidden = k === "webhook";
    format.parentElement && (format.parentElement.hidden = k === "webhook");
    start.parentElement && (start.parentElement.hidden = k === "webhook" || k === "mqtt");
    srcField.querySelector("span").textContent = k === "kinesis" ? "Stream name" : "Topic";
    src.placeholder = meta.ph || "";
    need.replaceChildren(k !== "webhook" && !opts.length ? h("div", { class: "notice warn" }, `No ${meta.label.toLowerCase()} connection yet. `,
      h("a", { href: "#/connections/new" }, "Add one"), " first (Kafka, Event Hubs, Kinesis or MQTT).") : null);
  };
  const need = h("div");
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/streams", class: "small" }, "← Streams"), h("h1", null, "New stream"))),
    h("div", { class: "card" }, h("span", { class: "eyebrow" }, "Source"), kind, need,
      h("div", { class: "form" }, field("Name", name), connField, srcField, field("Land in", target, "A raw-zone table. It is created on the first batch and grows new columns as events change."),
        field("Payload", format), field("Start", start)), advanced,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async (e) => {
        const cfg = { format: format.value, start: start.value, max_batch: +batch.value || 5000, max_wait_s: +wait.value || 2 };
        if (STREAM_KINDS[k].key) cfg[STREAM_KINDS[k].key] = src.value.trim();
        if (dedupe.value.trim()) cfg.dedupe_key = dedupe.value.trim();
        if (retention.value) cfg.retention_days = +retention.value;
        const body = { name: name.value.trim(), kind: k, target: target.value.trim() || "raw." + (name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_") || "stream"), config: cfg, start: true };
        if (k !== "webhook") body.connection_id = +conn.value;
        const s = await act(e.target, () => POST("/api/streams", body));
        if (s.token) tokenDialog(s, s.token);
        go("#/streams/" + s.id);
      } }, "Create and start"), h("span", { class: "small muted" }, "You can stop, preview and reset it from its page."))));
  draw();
}

function pushSnippet(s, token) {
  const url = location.origin + `/api/streams/${s.id}/events`;
  return `curl -X POST ${url} \\\n  -H "Authorization: Bearer ${token}" \\\n  -H "Content-Type: application/json" \\\n  -d '[{"event_id":"e-1","type":"signup","plan":"pro"}]'`;
}

function tokenDialog(s, token) {
  const pre = h("textarea", { class: "mono", rows: 5, readonly: true }, pushSnippet(s, token));
  dialog("Push endpoint ready", h("div", { class: "stack" },
    h("p", null, "Send events with this token. It is shown once; rotate it from the stream page if it leaks."),
    pre, h("p", { class: "small muted" }, "Accepts one JSON object, an array, {\"events\": [...]}, or NDJSON. Up to 5 MB or 50,000 events per request. A 200 response means the events are stored."),
    h("button", { onclick: () => copyText(pre.value, pre) }, "Copy")), []);
}

async function streamView(main, id) {
  let s = await GET("/api/streams/" + id);
  const eng = can("engineer");
  const preview = h("div");
  const body = h("div");
  const draw = () => {
    const m = s.meta || {};
    body.replaceChildren(
      h("div", { class: "kpis" },
        kpi("Status", s.desired === "stopped" ? "Stopped" : s.status[0].toUpperCase() + s.status.slice(1), s.desired === "running" ? "since " + ago(s.updated_at) : "not reading", s.status === "retrying" ? "bad" : s.desired === "running" ? "ok" : ""),
        kpi("Events / min", fmt(s.events_per_min), "last batch " + ago(m.last_batch_at)),
        kpi("Rows landed", fmt(m.rows_total || 0), `${fmt(m.batches || 0)} batches`),
        s.kind === "webhook" ? kpi("Rejected", fmt(m.rejects_total || 0), "unreadable payloads") :
          kpi(s.kind === "kinesis" ? "Behind latest" : "Consumer lag", s.lag == null ? "—" : fmt(s.lag) + (s.kind === "kinesis" ? " ms" : ""), s.kind === "kafka" ? "messages not yet read" : s.kind === "mqtt" ? "broker pushes; no lag" : "", s.lag > 10000 ? "warn" : "")),
      s.status === "retrying" && s.last_error ? h("div", { class: "notice bad" }, h("b", null, "Retrying: "), s.last_error, h("div", { class: "small" }, "Tessera keeps retrying with back-off and resumes from the last saved position.")) : null,
      h("div", { class: "grid2" },
        h("div", { class: "card" }, h("h2", null, "Throughput"), h("p", { class: "small muted" }, "Events per 10 seconds, last 5 minutes"), sparkBars(m.recent, 520, 90)),
        h("div", { class: "card" }, h("h2", null, "Configuration"), table([{ label: "Setting", key: "k" }, { label: "Value", render: (r) => h("span", { class: "mono" }, r.v) }], [
          { k: "Source", v: (STREAM_KINDS[s.kind]?.label || s.kind) + (s.config.topic ? " · " + s.config.topic : s.config.stream ? " · " + s.config.stream : "") },
          { k: "Lands in", v: s.target }, { k: "Rejects go to", v: s.target + "__rejects" }, { k: "Payload", v: s.config.format },
          { k: "Batch", v: `${fmt(s.config.max_batch)} events or ${s.config.max_wait_s}s` },
          { k: "De-duplicate on", v: s.config.dedupe_key || "off (at-least-once)" }, { k: "Retention", v: s.config.retention_days ? s.config.retention_days + " days" : "keep everything" },
          ...Object.entries((s.state || {}).positions || {}).slice(0, 8).map(([p, o]) => ({ k: s.kind === "kafka" ? `Partition ${p} next offset` : `Shard ${p.slice(-6)}`, v: String(o) })),
        ]))),
      s.kind === "webhook" ? h("div", { class: "card" }, h("h2", null, "Push endpoint"), h("p", { class: "mono small" }, "POST " + location.origin + s.endpoint),
        h("p", { class: "small muted" }, `Token starts with ${s.token_prefix || "tsk_"}…. Send it as a Bearer token.`)) : null,
      h("div", { class: "row" }, h("a", { class: "btn", href: "#/sql", onclick: () => { try { sessionStorage.setItem("tessera.sql", `SELECT * FROM ${s.target} ORDER BY _ingested_at DESC LIMIT 100`); } catch {} } }, "Query latest rows"),
        h("a", { class: "btn", href: "#/catalog" }, "Open in catalog")),
      preview);
  };
  const actions = eng ? h("div", { class: "row" },
    s.desired === "running" ? h("button", { onclick: async (e) => { s = await act(e.target, () => POST(`/api/streams/${id}/stop`), "Stopped"); render(); } }, "Stop")
      : h("button", { class: "primary", onclick: async (e) => { s = await act(e.target, () => POST(`/api/streams/${id}/start`), "Started"); render(); } }, "Start"),
    s.kind !== "webhook" ? h("button", { onclick: async (e) => {
      const r = await act(e.target, () => POST(`/api/streams/${id}/preview`));
      const cols = r.rows.length ? Object.keys(r.rows[0]) : [];
      preview.replaceChildren(h("div", { class: "card" }, h("h2", null, "Preview (nothing saved, position unchanged)"),
        r.rows.length ? resultTable({ columns: cols, rows: r.rows.map((x) => cols.map((c) => x[c])) }) : h("p", { class: "muted" }, "No events at the saved position."),
        r.rejects.length ? h("div", { class: "notice warn" }, `${r.rejects.length} unreadable payload(s) would go to ${s.target}__rejects.`) : null));
    }, title: "Stop the stream first" }, "Preview") : null,
    s.kind !== "webhook" ? h("button", { onclick: () => {
      const to = h("select", null, h("option", { value: "earliest" }, "Earliest (replay everything still retained)"), h("option", { value: "latest" }, "Latest (skip the backlog)"));
      dialog("Reset position", h("div", { class: "stack" }, h("p", null, "Rows already landed stay in the table. Use de-duplication if you replay."), field("Move to", to)),
        [{ label: "Reset", run: async () => { await POST(`/api/streams/${id}/reset`, { to: to.value }); toast("Position reset"); render(); } }]);
    } }, "Reset position") : h("button", { onclick: async (e) => { const r = await act(e.target, () => POST(`/api/streams/${id}/token`)); tokenDialog(s, r.token); } }, "Rotate token"),
    h("button", { class: "danger", onclick: () => confirmBox(`Delete stream ${s.name}? The table ${s.target} is kept.`, "Delete", async () => { await DEL(`/api/streams/${id}`); go("#/streams"); }) }, "Delete")) : null;
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/streams", class: "small" }, "← Streams"),
    h("div", { class: "row" }, h("h1", null, s.name), streamStatus(s))), actions), body);
  draw();
  T.timers.push(setInterval(async () => { if (document.hidden) return; try { s = await GET("/api/streams/" + id); draw(); } catch {} }, 3000));
}
