"""Refusals that coverage showed were never exercised: sign-in throttling, deactivated accounts and token owners,
malformed credentials, the SQL guard's edge cases, share-link permissions, OData limits, password rules and the
master key check."""
import base64

import pytest
from conftest import Client, add_user
from fastapi import HTTPException

from tessera.auth import IPThrottle
from tessera.security import Vault, password_problem
from tessera.warehouse import AccessDenied, QueryError, Warehouse, split_fqn


def test_sign_in_throttle_per_address():
    t = IPThrottle(limit=3, window=600)
    for _ in range(3):
        t.check("203.0.113.9")
    with pytest.raises(HTTPException) as e:
        t.check("203.0.113.9")
    assert e.value.status_code == 429
    t.check("198.51.100.1")  # other addresses are unaffected
    t.hits["203.0.113.9"] = type(t.hits["203.0.113.9"])([0.0, 0.0, 0.0])  # old attempts age out of the window
    t.check("203.0.113.9")


def test_deactivated_people_and_their_tokens_are_shut_out(admin, app):
    c = add_user(admin, app, "leaver@acme.ca", "analyst")
    tok = c.post("/api/tokens", json={"name": "script", "role": "analyst"}).json()["token"]
    bearer = Client(app)
    assert bearer.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["email"] == "leaver@acme.ca")
    assert admin.patch(f"/api/users/{uid}", json={"active": False}).status_code == 200
    assert c.get("/api/auth/me").status_code == 401, "an open session stops working"
    assert bearer.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401, "so do their tokens"
    # backstop: even if a session survived (deactivated directly in the database), it is refused
    c2 = add_user(admin, app, "backstop@acme.ca", "viewer")
    app.state.platform.store.exec("UPDATE users SET active=0 WHERE email='backstop@acme.ca'")
    assert c2.get("/api/auth/me").status_code == 401


def test_odata_refuses_malformed_basic_auth(sample_env):
    anon = Client(sample_env["app"])
    for header in ("Basic !!!not-base64!!!", "Basic " + base64.b64encode(b"no-colon").decode()):
        r = anon.get("/odata/", headers={"Authorization": header})
        assert r.status_code == 401, (header, r.text)


def test_odata_limits(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    tok = c.post("/api/tokens", json={"name": "limits", "role": "viewer"}).json()["token"]
    od = Client(app)
    h = {"Authorization": f"Bearer {tok}"}
    assert od.get("/odata/orders_enriched?$top=abc", headers=h).status_code == 400
    assert od.get("/odata/orders_enriched?$select=nope", headers=h).status_code == 400
    assert od.get("/odata/orders_enriched?$filter=" + "province eq 'BC' and " * 400 + "true", headers=h).status_code == 400
    assert od.get("/odata/orders_enriched?$orderby=1;drop", headers=h).status_code == 400
    assert od.get("/odata/no_such_set", headers=h).status_code == 404


def test_sql_guard_edges(admin, tmp_path):
    assert admin.post("/api/sql", json={"sql": "   ;  "}).status_code == 400
    r = admin.post("/api/sql", json={"sql": "SELECT 'x' AS pragma"})
    assert r.status_code == 403 and "PRAGMA" in r.text
    wh = Warehouse(tmp_path / "w.duckdb")
    with pytest.raises(AccessDenied, match="only SELECT"):
        wh.apply_rls("UPDATE serving.t SET x = 1 WHERE", {"t": "true"}, wh.cursor())
    for bad in ("nosuchschema.t", "a.b.c", "raw.bad name"):
        with pytest.raises(QueryError):
            split_fqn(bad)


def test_share_link_permissions(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    c.put("/api/admin/sharing", json={"enabled": True})
    try:
        tok = c.post("/api/tokens", json={"name": "share", "role": "admin"}).json()["token"]
        r = Client(app).post("/api/dashboards/1/share", json={"expires_days": 7}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, "links are created in the app, not with API tokens"
        assert c.post("/api/dashboards/1/share", json={"filters": "BC"}).status_code == 400
        link = c.post("/api/dashboards/1/share", json={"expires_days": 7}).json()
        other = add_user(c, app, "colleague@acme.ca", "analyst")
        r = other.delete(f"/api/shares/{link['id']}")
        assert r.status_code == 403, "only the creator or an admin can revoke"
        r = c.delete("/api/shares/999999")
        assert r.status_code == 404
    finally:
        c.put("/api/admin/sharing", json={"enabled": False})


def test_password_rules_and_master_key(tmp_path, monkeypatch):
    assert password_problem("short") and "12" in password_problem("short")
    assert "email name" in password_problem("maria.lopez-secure-2026", "maria.lopez@acme.ca")
    assert "repetitive" in password_problem("aaaaaaaaaaaabbbb")
    assert password_problem("correct horse battery 9", "admin@acme.ca") is None
    monkeypatch.setenv("TESSERA_MASTER_KEY", base64.b64encode(b"too short").decode())
    with pytest.raises(ValueError, match="32 bytes"):
        Vault(tmp_path)
