"use strict";
/* Boot: setup, sign-in, shell. */
const ICONS = {
  overview: '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1" opacity=".6"/><rect x="1" y="9" width="6" height="6" rx="1" opacity=".6"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>',
  catalog: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><ellipse cx="8" cy="3.5" rx="6" ry="2.2"/><path d="M2 3.5v9c0 1.2 2.7 2.2 6 2.2s6-1 6-2.2v-9M2 8c0 1.2 2.7 2.2 6 2.2S14 9.2 14 8"/></svg>',
  connections: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M6 10l4-4M4.5 8.5l-1.5 1.5a2.5 2.5 0 003.5 3.5L8 12M11.5 7.5L13 6a2.5 2.5 0 00-3.5-3.5L8 4"/></svg>',
  pipelines: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="3" cy="4" r="1.8"/><circle cx="3" cy="12" r="1.8"/><circle cx="13" cy="8" r="1.8"/><path d="M4.8 4.4L11.2 7.6M4.8 11.6l6.4-3.2"/></svg>',
  sql: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 4L1.5 8 5 12M11 4l3.5 4L11 12M9.5 2.5l-3 11"/></svg>',
  dashboards: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 14V9M6 14V4M10 14V7M14 14V2"/></svg>',
  agents: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="12" height="9" rx="2"/><path d="M8 1.5V4M5.5 8.5h.01M10.5 8.5h.01"/></svg>',
  explore: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14M5 8.5V7M7 8.5V5.5M9 8.5V6.5"/></svg>',
  reports: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9 1.5V5h4M6.5 12V9.5M8.5 12V8M10.5 12v-1.5"/></svg>',
  notebooks: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="1.5" width="10.5" height="13" rx="1"/><path d="M1.5 4.5h3M1.5 8h3M1.5 11.5h3M6.5 5h4.5M6.5 8h4.5"/></svg>',
  models: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5l6 3.2v6.6L8 14.5l-6-3.2V4.7z"/><path d="M2 4.7l6 3.2 6-3.2M8 7.9v6.6"/></svg>',
  glossary: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2.5 2.5h4.5a1.5 1.5 0 011.5 1.5v10a1.5 1.5 0 00-1.5-1.5H2.5zM13.5 2.5H10A1.5 1.5 0 008.5 4v10a1.5 1.5 0 011.5-1.5h3.5z"/></svg>',
  transforms: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h8M2 8h5M2 12h8M12 2.5l2 1.5-2 1.5M12 10.5l2 1.5-2 1.5M9.5 6.5L11 8l-1.5 1.5"/></svg>',
  streams: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1.5 5c2-2 4 2 6.5 0s4.5-2 6.5 0M1.5 9c2-2 4 2 6.5 0s4.5-2 6.5 0M1.5 13c2-2 4 2 6.5 0s4.5-2 6.5 0"/></svg>',
  monitor: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1.5 8.5h3l2-5 3 9 2-4h3"/></svg>',
  alerts: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5L14.5 13.5h-13z"/><path d="M8 6v3.5M8 11.5h.01"/></svg>',
  search: '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>',
  bell: '<svg viewBox="0 0 16 16" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 11V7a4 4 0 018 0v4l1.5 1.5h-11zM6.5 14h3"/></svg>',
  workspaces: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="3" width="13" height="10" rx="1.5"/><path d="M1.5 6h13M5.5 3V1.5h5V3"/></svg>',
  admin: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>',
};
const brand = () => h("div", { class: "brand" }, h("span", { class: "tiles", "aria-hidden": "true" }, Array.from({ length: 9 }, () => h("i"))), "Tessera");

async function renderAuth() {
  const root = $("#root");
  let st = { needs_setup: false, sso: false };
  try { st = await GET("/api/setup/status"); } catch {}
  const err = h("div", { class: "notice bad", hidden: true });
  const fail = (e) => { err.textContent = e.message; err.hidden = false; };
  if (st.needs_setup) {
    const code = h("input", { id: "su-code", autocomplete: "one-time-code", placeholder: "8 characters" });
    const name = h("input", { id: "su-name", autocomplete: "name" }), email = h("input", { id: "su-email", type: "email", autocomplete: "email" });
    const pw = h("input", { id: "su-pw", type: "password", autocomplete: "new-password" });
    const sample = h("input", { type: "checkbox", id: "su-sample", checked: true });
    const form = h("form", { class: "authcard", onsubmit: async (e) => {
      e.preventDefault();
      const b = form.querySelector("button");
      b.disabled = true; b.textContent = sample.checked ? "Setting up and loading sample…" : "Setting up…";
      try { await POST("/api/setup", { setup_code: code.value, name: name.value, email: email.value, password: pw.value, load_sample: sample.checked }); await boot(); if (sample.checked) setTimeout(() => startTour({ enabled: true }), 900); }
      catch (x) { fail(x); b.disabled = false; b.textContent = "Create admin account"; }
    } }, brand(), h("h1", null, "Set up Tessera"),
    h("p", { class: "muted small" }, "Create the first administrator. The setup code is printed in the server console and saved as setup-code.txt in the data folder."),
    err, field("Setup code", code), field("Your name", name), field("Work email", email), field("Password", pw, "At least 12 characters."),
    h("label", { class: "row small" }, sample, "Start in demo mode with a sample retail workspace and a guided tour (generated data, no real people; remove it any time)"),
    h("button", { class: "primary", type: "submit" }, "Create admin account"));
    root.replaceChildren(h("div", { class: "authwrap" }, form));
    code.focus();
    return;
  }
  const email = h("input", { id: "li-email", type: "email", autocomplete: "username" });
  const pw = h("input", { id: "li-pw", type: "password", autocomplete: "current-password" });
  const form = h("form", { class: "authcard", onsubmit: async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/auth/login", { email: email.value, password: pw.value }, { quiet401: true }); await boot(); } catch (x) { fail(x); }
  } }, brand(), h("h1", null, "Sign in"), err, field("Email", email), field("Password", pw), h("button", { class: "primary", type: "submit" }, "Sign in"),
  st.sso ? h("a", { class: "btn", href: "/api/auth/oidc/login", style: { justifyContent: "center" } }, "Sign in with single sign-on") : null,
  h("p", { class: "small muted" }, "Forgot your password? An admin can set a new one, or run ", h("code", null, "tessera reset-password you@company.com"), " on the server."));
  root.replaceChildren(h("div", { class: "authwrap" }, form));
  email.focus();
}

function shell() {
  const groups = [
    [null, [["overview", "Home"]]],
    ["Analyze", [["dashboards", "Dashboards"], ["explore", "Explore", "viewer"], ["reports", "Reports"], ["notebooks", "Notebooks", "analyst"], ["sql", "SQL", "analyst"], ["agents", "AI agents", "analyst"]]],
    ["Data", [["catalog", "Catalog"], ["models", "Semantic models"], ["glossary", "Glossary"], ["connections", "Connections", "engineer"], ["streams", "Streams", "analyst"], ["pipelines", "Pipelines", "analyst"], ["transforms", "Transforms", "engineer"]]],
    ["Operate", [["monitor", "Monitor", "analyst"], ["alerts", "Alerts", "analyst"], ["workspaces", "Workspaces"], ["admin", can("admin") ? "Admin" : "API tokens"]]],
  ];
  const me = T.me;
  $("#root").replaceChildren(h("div", { class: "shell" },
    h("nav", { class: "rail", "aria-label": "Main" }, h("a", { href: "#/overview", class: "brand", style: { padding: "4px 8px 12px" } }, h("span", { class: "tiles", "aria-hidden": "true" }, Array.from({ length: 9 }, () => h("i"))), h("span", null, "Tessera")),
      groups.map(([label, items]) => [label ? h("div", { class: "railgroup" }, label) : null,
        items.filter(([, , r]) => !r || can(r)).map(([k, l]) => h("a", { href: "#/" + k, "data-r": k }, h("span", { html: ICONS[k] || ICONS.overview, style: { display: "inline-flex" }, "aria-hidden": "true" }), h("span", null, l)))]),
      h("div", { class: "me" }, h("div", { class: "who" }, h("div", null, h("b", null, me.name)), h("div", null, `${me.role} · ${me.email}`)),
        h("div", { class: "row" },
          h("button", { class: "sm pw", onclick: () => {
            const cur = h("input", { type: "password", id: "cp-cur", autocomplete: "current-password" }), nw = h("input", { type: "password", id: "cp-new", autocomplete: "new-password" });
            dialog("Change password", h("div", { class: "stack" }, me.sso ? h("p", { class: "small muted" }, "You sign in with SSO; a password is optional.") : field("Current password", cur), field("New password", nw, "At least 12 characters. Signs out your other sessions.")),
              [{ label: "Change password", run: async () => { await POST("/api/auth/password", { current_password: cur.value, new_password: nw.value }); toast("Password changed"); } }]);
          } }, "Password"),
          h("button", { class: "sm", onclick: async () => { await POST("/api/auth/logout"); T.me = null; renderAuth(); } }, "Sign out")))),
    h("div", { class: "content" }, topbar(), h("main", { class: "main", id: "main", tabindex: "-1" }))));
  pollNotifications();
  demoPill().then((el) => $("#demo-slot")?.replaceWith(el));
}

function topbar() {
  const count = h("span", { class: "badge", id: "bell-count", hidden: true });
  return h("header", { class: "topbar" },
    h("button", { class: "searchbtn", onclick: () => palette(), "aria-label": "Search everything" }, h("span", { html: ICONS.search, style: { display: "inline-flex" } }), h("span", null, "Search datasets, metrics, dashboards…"), h("kbd", null, "Ctrl K")),
    h("div", { id: "demo-slot", class: "demoslot" }),
    h("button", { class: "ghost bell", id: "bell", "aria-label": "Notifications", onclick: (e) => notificationsPanel(e.currentTarget) }, h("span", { html: ICONS.bell, style: { display: "inline-flex" } }), count));
}

async function boot() {
  try { T.me = (await api("GET", "/api/auth/session", undefined, { quiet401: true })).user; }
  catch { T.me = null; }
  if (!T.me) return renderAuth();
  shell();
  if (!location.hash) location.hash = "#/overview";
  render();
}
boot();
