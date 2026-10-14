"""Every route, called without credentials and with the lowest role, must refuse unless it is deliberately public."""
import re

import pytest
from conftest import Client, add_user

PUBLIC = {
    ("GET", "/"), ("GET", "/app"), ("GET", "/api/health"), ("GET", "/api/ready"), ("GET", "/api/setup/status"), ("POST", "/api/setup"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("GET", "/api/auth/oidc/login"), ("GET", "/api/auth/oidc/callback"),
    ("GET", "/api/connectors"), ("GET", "/embed/{token}"), ("GET", "/api/embed/{token}"), ("POST", "/api/embed/{token}/tiles/{i}"),
    ("GET", "/api/auth/session"), ("GET", "/favicon.ico"),
    ("GET", "/api/openapi.json"),  # the API description (no data); the interactive docs need nothing else
}
# deliberately open to any signed-in user (viewer)
VIEWER_OK_PREFIXES = ("/api/auth/", "/api/dashboards", "/api/queries", "/api/models", "/api/datasets", "/api/catalog", "/api/overview", "/api/search",
                      "/api/notifications", "/api/glossary", "/api/workspaces", "/api/agents", "/api/demo", "/api/settings/fiscal", "/api/sessions",
                      "/api/lineage", "/api/tokens", "/api/ask", "/api/me", "/api/connectors",
                      # static reference lists: rule kinds, masking policies, pipeline step catalogue
                      "/api/quality/kinds", "/api/pii/policies", "/api/pipelines/meta")


def routes(app):
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods - {"HEAD", "OPTIONS"}:
            yield m, r.path


def fill(path):
    return re.sub(r"\{[^}]+\}", lambda m: "tsh_" + "x" * 30 if m.group(0) == "{token}" else "1", path)


def test_no_route_is_open_by_accident(admin, app):
    anon = Client(app)
    leaks = []
    for m, p in sorted(set(routes(app))):
        if (m, p) in PUBLIC or p.startswith(("/static", "/openapi", "/docs", "/redoc", "/api/docs")):
            continue
        r = anon.request(m, fill(p), json={}, headers={"x-tessera-csrf": "1"})
        if r.status_code not in (401, 403, 404, 405):
            leaks.append(f"{m} {p} -> {r.status_code}")
    assert not leaks, "\n".join(leaks)


def test_viewer_is_refused_on_privileged_routes(admin, app):
    v = add_user(admin, app, "matrix.viewer@acme.ca", "viewer")
    privileged = [(m, p) for m, p in sorted(set(routes(app))) if p.startswith("/api/") and (m, p) not in PUBLIC and not p.startswith(VIEWER_OK_PREFIXES)
                  and not p.startswith(("/api/embed", "/api/docs"))]
    assert len(privileged) > 60
    allowed = []
    for m, p in privileged:
        r = v.request(m, fill(p), json={}, headers={"x-tessera-csrf": "1"})
        if r.status_code < 400:
            allowed.append(f"{m} {p} -> {r.status_code}")
    assert not allowed, "\n".join(allowed)


@pytest.mark.parametrize("path", ["/api/admin/scim", "/api/admin/mcp", "/api/admin/sharing", "/api/admin/retention", "/api/users", "/api/audit", "/metrics"])
def test_admin_only(admin, app, path):
    a = add_user(admin, app, f"eng{abs(hash(path)) % 9999}@acme.ca", "engineer")
    assert a.get(path).status_code == 403
    assert admin.get(path).status_code == 200


def test_csrf_required_for_cookie_writes(admin, app):
    r = admin.post("/api/queries", csrf=False, json={"name": "x", "sql": "select 1"})
    assert r.status_code == 403
