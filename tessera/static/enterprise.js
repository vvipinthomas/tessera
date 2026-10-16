"use strict";
/* Enterprise admin: workspaces, SCIM provisioning, MCP server, operations (retention, audit export, sessions, backup). */

route("workspaces", async (main, id) => {
  const list = await GET("/api/workspaces");
  if (id) return workspaceView(main, list.find((w) => w.id === +id));
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("span", { class: "eyebrow" }, "Collaborate"), h("h1", null, "Workspaces")),
    can("analyst") ? h("button", { class: "primary", onclick: () => {
      const n = h("input", { id: "ws-name", placeholder: "Finance" }), dsc = h("input", { id: "ws-desc" });
      const vis = h("select", { id: "ws-vis" }, h("option", { value: "private" }, "Private: members only"), h("option", { value: "org" }, "Organization: everyone can view"));
      dialog("New workspace", h("div", { class: "form" }, field("Name", n), field("Description", dsc), field("Visibility", vis)),
        [{ label: "Create", run: async () => { const w = await POST("/api/workspaces", { name: n.value, description: dsc.value, visibility: vis.value }); go("#/workspaces/" + w.id); } }]);
    } }, "New workspace") : null),
  h("p", { class: "small muted" }, "Workspaces group dashboards, reports, notebooks and semantic models by team. Data access (roles, row-level security) still applies inside every workspace."),
  h("div", { class: "grid2" }, list.map((w) => h("button", { class: "card agentcard", onclick: () => go("#/workspaces/" + w.id) },
    h("div", { class: "spread" }, h("b", null, w.name), h("div", { class: "row" }, chip(w.visibility === "org" ? "organization" : "private", w.visibility === "org" ? "info" : ""), chip(w.my_role || "—"))),
    h("span", { class: "small muted" }, w.description || ""),
    h("span", { class: "small muted" }, `${w.counts.dashboard} dashboards · ${w.counts.query} reports · ${w.counts.notebook} notebooks · ${w.counts.model} models · ${w.members} members`)))));
});

async function workspaceView(main, w) {
  if (!w) { main.replaceChildren(h("div", { class: "notice bad" }, "Workspace not found.")); return; }
  const isAdmin = w.my_role === "admin";
  const [members, dashes, queries] = await Promise.all([GET(`/api/workspaces/${w.id}/members`), GET(`/api/dashboards?workspace=${w.id}`), GET(`/api/queries?workspace=${w.id}`)]);
  const addMember = () => {
    const e = h("input", { id: "wm-email", type: "email", placeholder: "name@company.com" });
    const r = h("select", { id: "wm-role" }, ["viewer", "contributor", "admin"].map((x) => h("option", null, x)));
    dialog("Add member", h("div", { class: "form" }, field("Email", e), field("Workspace role", r, "viewer reads · contributor creates and edits · admin also manages members")),
      [{ label: "Add", run: async () => { await PUT(`/api/workspaces/${w.id}/members`, { email: e.value, role: r.value }); toast("Member added"); render(); } }]);
  };
  main.replaceChildren(h("div", { class: "pagehead" }, h("div", null, h("a", { href: "#/workspaces", class: "small" }, "← Workspaces"), h("h1", null, w.name), h("span", { class: "muted small" }, w.description)),
    isAdmin ? h("div", { class: "row" }, h("button", { onclick: addMember }, "Add member"),
      w.id !== 1 ? h("button", { class: "danger", onclick: () => confirmBox(`Delete workspace ${w.name}? It must be empty.`, "Delete", async () => { await DEL(`/api/workspaces/${w.id}`); go("#/workspaces"); }) }, "Delete") : null) : null),
  h("div", { class: "grid2" },
    h("div", { class: "card" }, h("h3", null, "Dashboards"), dashes.length ? h("ul", null, dashes.map((d) => h("li", null, h("a", { href: "#/dashboards/" + d.id }, d.name)))) : h("p", { class: "small muted" }, "None yet."),
      h("h3", null, "Reports"), queries.length ? h("ul", null, queries.slice(0, 30).map((q) => h("li", null, h("a", { href: "#/reports/" + q.id }, q.name)))) : h("p", { class: "small muted" }, "None yet.")),
    h("div", { class: "card flat" }, h("h3", { style: { padding: "12px 12px 0" } }, "Members"), members.length ? table([
      { label: "Member", render: (m) => h("div", null, h("b", null, m.name), h("div", { class: "small muted" }, m.email)) },
      { label: "Role", render: (m) => { if (!isAdmin) return m.role; const s = h("select", { "aria-label": "Role for " + m.email }, ["viewer", "contributor", "admin"].map((x) => h("option", { selected: x === m.role }, x))); s.addEventListener("change", () => act(null, () => PUT(`/api/workspaces/${w.id}/members`, { user_id: m.user_id, role: s.value }), "Role updated")); return s; } },
      { label: "Source", render: (m) => chip(m.source, m.source === "scim" ? "info" : "") },
      { label: "", render: (m) => isAdmin ? h("button", { class: "sm danger", onclick: () => act(null, () => PUT(`/api/workspaces/${w.id}/members`, { user_id: m.user_id, role: null }), "Removed").then(render) }, "Remove") : null },
    ], members) : h("p", { class: "small muted", style: { padding: "12px" } }, w.visibility === "org" ? "Everyone in the organization can view this workspace; analysts can contribute." : "No members."))));
}

/* ---------------- admin: provisioning (SCIM) ---------------- */
async function adminProvisioning(el) {
  const [c, wss] = await Promise.all([GET("/api/admin/scim"), GET("/api/workspaces")]);
  const role = h("select", { id: "scim-role" }, ["viewer", "analyst", "engineer"].map((x) => h("option", { selected: c.default_role === x }, x)));
  const rows = Object.entries(c.group_map || {}).map(([g, r]) => ({ g, ...r }));
  const box = h("div", { class: "stack" });
  const draw = () => box.replaceChildren(rows.map((r, i) => h("div", { class: "row mapping" },
    h("input", { "aria-label": "IdP group name", value: r.g, placeholder: "IdP group display name", oninput: (e) => (r.g = e.target.value) }),
    h("select", { "aria-label": "Tessera role", onchange: (e) => (r.role = e.target.value || undefined) }, h("option", { value: "" }, "(role unchanged)"), ["viewer", "analyst", "engineer", "admin"].map((x) => h("option", { selected: r.role === x }, x))),
    h("select", { "aria-label": "Workspace", onchange: (e) => (r.workspace = e.target.value || undefined) }, h("option", { value: "" }, "(no workspace)"), wss.map((w) => h("option", { selected: r.workspace === w.name }, w.name))),
    h("select", { "aria-label": "Workspace role", onchange: (e) => (r.workspace_role = e.target.value) }, ["viewer", "contributor", "admin"].map((x) => h("option", { selected: (r.workspace_role || "viewer") === x }, x))),
    h("button", { class: "sm danger", onclick: () => { rows.splice(i, 1); draw(); } }, "Remove"))),
  h("button", { class: "sm", onclick: () => { rows.push({ g: "" }); draw(); } }, "+ Add group mapping"));
  draw();
  el.replaceChildren(h("div", { class: "card" }, h("h3", null, "SCIM 2.0 provisioning"),
    h("p", { class: "small muted" }, "Let Microsoft Entra ID, Okta or another identity provider create, update and deactivate users and sync group membership. Deactivating a user ends their sessions and revokes their API tokens."),
    h("div", { class: "grid3" }, kpiMini("Status", c.enabled ? "Enabled" : "Not set up"), kpiMini("Provisioned users", fmt(c.users)), kpiMini("Groups", fmt(c.groups))),
    h("div", { class: "form" }, field("Tenant URL", h("input", { readonly: true, value: c.base_url, id: "scim-url" })), field("Default role for new users", role)),
    h("div", { class: "row" }, h("button", { onclick: async (e) => {
      const t = await act(e.target, () => POST("/api/admin/scim/token"));
      const b = h("input", { readonly: true, value: t.token });
      dialog("SCIM token", h("div", { class: "stack" }, h("div", { class: "notice warn" }, t.note), b, h("button", { onclick: () => copyText(t.token, b) }, "Copy")), []);
    } }, c.enabled ? "Rotate token" : "Generate token"))),
  h("div", { class: "card" }, h("h3", null, "Group mappings"), h("p", { class: "small muted" }, "A user gets the highest role among their groups. Workspace memberships created here are marked 'scim' and follow the identity provider; manual memberships are left alone."), box,
    h("div", null, h("button", { class: "primary", onclick: (e) => act(e.target, () => PUT("/api/admin/scim", { default_role: role.value,
      group_map: Object.fromEntries(rows.filter((r) => r.g.trim()).map((r) => [r.g.trim(), { role: r.role, workspace: r.workspace, workspace_role: r.workspace ? (r.workspace_role || "viewer") : undefined }])) }), "Provisioning settings saved") }, "Save"))));
}

/* ---------------- admin: MCP server ---------------- */
async function adminMCP(el) {
  const c = await GET("/api/admin/mcp");
  const en = h("input", { type: "checkbox", id: "mcp-on", checked: c.enabled });
  const wr = h("input", { type: "checkbox", id: "mcp-writes", checked: c.allow_writes });
  const save = () => act(null, () => PUT("/api/admin/mcp", { enabled: en.checked, allow_writes: wr.checked }), "Saved").then(render);
  en.addEventListener("change", save); wr.addEventListener("change", save);
  const http = `claude mcp add --transport http tessera ${c.url} --header "Authorization: Bearer YOUR_TOKEN"`;
  const desktop = JSON.stringify({ mcpServers: { tessera: { command: "tessera", args: ["mcp", "--url", c.url.replace(/\/mcp$/, "")], env: { TESSERA_TOKEN: "YOUR_TOKEN" } } } }, null, 2);
  const snippet = (title, text) => { const pre = h("pre", { class: "log" }, text); return h("div", { class: "stack" }, h("div", { class: "spread" }, h("b", { class: "small" }, title), h("button", { class: "sm", onclick: () => copyText(text) }, "Copy")), pre); };
  el.replaceChildren(h("div", { class: "card" }, h("h3", null, "Model Context Protocol server"),
    h("p", { class: "small muted" }, "Connect Claude Desktop, Claude Code, IDEs or your own agents to Tessera's governed data. Each client uses an API token, so it gets that token's role, row-level security, workspace visibility and the PII firewall. Every tool call is audited."),
    h("div", { class: "grid3" }, kpiMini("Status", c.enabled ? "On" : "Off"), kpiMini("Writes", c.allow_writes ? "Allowed" : "Read-only"), kpiMini("Tool calls, 7 days", fmt(c.calls_7d))),
    h("label", { class: "row" }, en, h("b", null, "Enable the MCP endpoint")),
    h("label", { class: "row" }, wr, h("b", null, "Allow write tools"), h("span", { class: "small muted" }, " create quality rules, run checks and edit descriptions (engineer tokens only)"))),
  h("div", { class: "card" }, h("h3", null, "Connect a client"), h("p", { class: "small muted" }, "Create a token under API tokens (pick the lowest role that does the job), then:"),
    snippet("Claude Code (HTTP)", http), snippet("Claude Desktop (stdio bridge, needs Tessera installed locally)", desktop),
    h("p", { class: "small muted" }, "Browser-based clients on another origin must be allowed with TESSERA_MCP_ALLOWED_ORIGINS.")),
  h("div", { class: "card flat" }, table([{ label: "Token role", key: "role" }, { label: "Tools available", render: (r) => h("span", { class: "small mono" }, r.tools.join(" · ")) }],
    Object.entries(c.tools).map(([role, tools]) => ({ role, tools })))));
}

/* ---------------- admin: operations ---------------- */
async function adminOps(el) {
  const [ret, sess, shr, cloud] = await Promise.all([GET("/api/admin/retention"), GET("/api/sessions?all=true"), GET("/api/admin/sharing"), GET("/api/admin/cloud").catch(() => ({ enabled: false }))]);
  const cloudCard = cloud.enabled ? h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", null, "Cloud backups"),
      cloud.last_error ? chip("failing", "bad") : cloud.last_backup_at ? chip("protected", "ok") : chip("waiting for first backup", "warn")),
    h("p", { class: "small muted" }, `This server keeps its data in ${cloud.bucket}/${cloud.prefix}. A backup is written every ${cloud.interval_minutes} min when anything changed, after each pipeline run, and when the server stops; a new server restores the latest one automatically.`),
    cloud.last_error ? h("div", { class: "notice bad small" }, cloud.last_error) : null,
    h("div", { class: "row" }, h("span", { class: "small" }, "Last backup ", h("b", null, cloud.last_backup_at ? ago(cloud.last_backup_at) : "not yet")),
      h("button", { onclick: async (e) => { await act(e.target, () => POST("/api/admin/cloud/backup"), "Backed up"); render(); } }, "Back up now"))) : null;
  const shOn = h("input", { type: "checkbox", id: "share-on", checked: shr.enabled });
  const shOrig = h("input", { id: "share-origins", value: shr.embed_origins.join(", "), placeholder: "https://intranet.example.com, https://portal.example.com" });
  const sharingCard = h("div", { class: "card" }, h("h3", null, "Sharing and embedding"),
    h("p", { class: "small muted" }, `Let analysts create expiring, read-only links to dashboards and embed them in other sites. ${shr.active_links} active link(s). Turning this off revokes every link.`),
    h("label", { class: "row" }, shOn, h("b", null, "Allow share links")),
    field("Sites allowed to embed dashboards (frame-ancestors)", shOrig, "Comma-separated origins. Empty = links open only as full pages."),
    h("div", null, h("button", { onclick: (e) => act(e.target, () => PUT("/api/admin/sharing", { enabled: shOn.checked, embed_origins: shOrig.value.split(",").map((x) => x.trim()).filter(Boolean) }), "Sharing settings saved") }, "Save")));
  const days = h("input", { id: "ret-days", type: "number", min: 30, value: ret.audit_days ?? "", placeholder: "keep forever", style: { maxWidth: "160px" } });
  const since = h("input", { type: "date", id: "ax-since" }), until = h("input", { type: "date", id: "ax-until" });
  const key = h("input", { type: "checkbox", id: "bk-key" });
  el.replaceChildren(
    cloudCard,
    h("div", { class: "card" }, h("h3", null, "Backup"), h("p", { class: "small muted" }, "Download a consistent snapshot of metadata, the warehouse and uploaded files, with checksums. Restore with ", h("code", null, "tessera restore <file>"), " on a stopped server."),
      h("label", { class: "row small" }, key, "Include the encryption key (store the file somewhere secure)"),
      h("div", null, h("button", { class: "primary", onclick: (e) => act(e.target, () => downloadPost("/api/admin/backup", { include_key: key.checked })) }, "Download backup"))),
    h("div", { class: "card" }, h("h3", null, "Audit export and retention"),
      h("div", { class: "row" }, field("From", since), field("Until", until),
        h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/audit/export?format=csv&since=${since.value}&until=${until.value}`, undefined, "tessera-audit.csv")) }, "Export CSV"),
        h("button", { onclick: (e) => act(e.target, () => downloadPost(`/api/audit/export?format=json&since=${since.value}&until=${until.value}`, undefined, "tessera-audit.ndjson")) }, "Export JSON lines")),
      h("div", { class: "row", style: { alignItems: "end" } }, field("Keep audit records for (days)", days, "At least 30. Empty keeps everything."),
        h("button", { onclick: (e) => act(e.target, () => PUT("/api/admin/retention", { audit_days: days.value || null }), "Retention saved") }, "Save"))),
    h("div", { class: "card flat" }, h("h3", { style: { padding: "12px 12px 0" } }, "Active sessions"), table([
      { label: "User", key: "email" }, { label: "Signed in", render: (x) => ago(x.created_at) }, { label: "IP", render: (x) => h("span", { class: "mono small" }, x.ip || "") },
      { label: "Browser", render: (x) => h("span", { class: "small muted" }, (x.user_agent || "").slice(0, 60)) },
      { label: "", render: (x) => x.current ? chip("this session", "info") : h("button", { class: "sm danger", onclick: () => act(null, () => DEL(`/api/sessions/${x.id}`), "Signed out").then(render) }, "Sign out") }], sess)),
    sharingCard,
    h("div", { class: "card" }, h("h3", null, "Monitoring endpoints"), h("p", { class: "small" }, h("code", null, "/api/health"), " liveness · ", h("code", null, "/api/ready"), " readiness · ", h("code", null, "/metrics"), " Prometheus (admin session or TESSERA_METRICS_TOKEN) · JSON access logs with request IDs (TESSERA_LOG_JSON=1).")));
}

/* Admin → Integrations: BI tools (OData), lineage collectors (OpenLineage), dbt, orchestrators. */
async function adminIntegrations(el) {
  const [info, ol] = await Promise.all([GET("/api/integrations"), GET("/api/admin/openlineage")]);
  const odata = h("input", { readonly: true, value: info.odata_url, class: "mono", "aria-label": "OData feed URL" });
  const olOn = h("input", { type: "checkbox", id: "ol-on", checked: ol.enabled });
  const olUrl = h("input", { id: "ol-url", value: ol.url, placeholder: "http://marquez:5000 · https://datahub.example.com/openapi/openlineage" });
  const olNs = h("input", { id: "ol-ns", value: ol.namespace });
  const olKey = h("input", { id: "ol-key", type: "password", placeholder: ol.api_key_set ? "set (leave empty to keep)" : "optional bearer token", autocomplete: "off" });
  const dbtFile = h("input", { type: "file", id: "dbt-file", accept: ".json,application/json" });
  const dbtOut = h("div");
  const steps = (items) => h("ol", { class: "small steps" }, items.map((x) => h("li", null, x)));
  el.replaceChildren(
    h("div", { class: "card" }, h("h3", null, "Power BI, Excel and Tableau"),
      h("p", { class: "small muted" }, "A read-only OData v4 feed of every published dataset and saved report the signed-in token can see. Masking and row-level security apply, exactly as in Tessera."),
      h("div", { class: "row" }, odata, h("button", { onclick: () => copyText(odata.value, odata) }, "Copy")),
      h("div", { class: "grid3" },
        h("div", null, h("b", null, "Power BI Desktop"), steps(["Get data → OData feed", "Paste the URL above", "Basic: any user name, an API token as the password", "Pick tables and load"])),
        h("div", null, h("b", null, "Excel"), steps(["Data → Get Data → From Other Sources → From OData Feed", "Paste the URL", "Basic: API token as the password"])),
        h("div", null, h("b", null, "Tableau"), steps(["Connect → To a Server → OData", "Paste the URL", "Username and password: any name, API token"]))),
      h("p", { class: "small muted" }, "Create the token under API tokens with the viewer or analyst role. For scheduled refresh in the Power BI service, the URL must be reachable from Microsoft's cloud or through an on-premises data gateway.")),
    h("div", { class: "card" }, h("h3", null, "OpenLineage"),
      h("p", { class: "small muted" }, "Send a START and COMPLETE (or FAIL) event for every pipeline run to Marquez, DataHub, OpenMetadata, Atlan, Microsoft Purview or any OpenLineage collector. Outputs carry their column schema. A collector outage never fails a run."),
      h("label", { class: "row small" }, olOn, "Send lineage events"),
      h("div", { class: "form" }, field("Collector URL", olUrl, "Tessera posts to <URL>/api/v1/lineage unless the URL already ends in /lineage."), field("Namespace", olNs), field("API key (encrypted)", olKey)),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: (e) => act(e.target, () => PUT("/api/admin/openlineage", { enabled: olOn.checked, url: olUrl.value.trim(), namespace: olNs.value.trim(), api_key: olKey.value || undefined }), "Saved") }, "Save"),
        h("button", { onclick: async (e) => { const r = await act(e.target, () => POST("/api/admin/openlineage/test")); toast(r.message); } }, "Send test event"))),
    h("div", { class: "card" }, h("h3", null, "dbt"),
      h("p", { class: "small muted" }, "Upload target/manifest.json from a dbt project. Tessera matches models and sources to datasets by table name and brings over descriptions, column docs, owners, tags, not_null / unique / accepted_values tests as quality rules, and the model graph as lineage."),
      h("div", { class: "row" }, dbtFile, h("button", { class: "primary", onclick: async (e) => {
        if (!dbtFile.files[0]) return toast("Choose manifest.json first", "bad");
        const fd = new FormData(); fd.append("file", dbtFile.files[0]);
        const r = await act(e.target, () => api("POST", "/api/integrations/dbt", fd));
        dbtOut.replaceChildren(h("div", { class: "notice" }, `Matched ${r.matched} dataset(s), created ${r.rules_created} quality rule(s) and ${r.lineage_edges} lineage link(s)` + (r.dbt_version ? ` (dbt ${r.dbt_version}).` : "."),
          r.unmatched.length ? h("div", { class: "small" }, "Not in Tessera yet: ", r.unmatched.slice(0, 20).join(", "), r.unmatched.length > 20 ? "…" : "") : null));
      } }, "Import")), dbtOut),
    h("div", { class: "card" }, h("h3", null, "Orchestrators and agents"),
      h("p", { class: "small muted" }, "Airflow, Dagster, Prefect, Azure Data Factory and CI jobs trigger pipelines with an engineer token. AI agents and IDEs connect through MCP."),
      h("pre", { class: "mono small codeblock" }, `curl -X POST ${location.origin}/api/pipelines/<id>/run -H "Authorization: Bearer <token>"\ncurl ${location.origin}/api/runs/<run_id> -H "Authorization: Bearer <token>"`),
      h("div", { class: "row" }, h("a", { class: "btn", href: "#/admin/mcp" }, "MCP server"), h("a", { class: "btn", href: "/api/docs", target: "_blank", rel: "noopener" }, "REST API reference"))));
}
