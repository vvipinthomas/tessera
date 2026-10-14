"""Enterprise operations: workspaces, SCIM provisioning (Entra- and Okta-style), metrics, readiness, rate limits,
audit export and retention, sessions, and backup/restore."""
import csv
import io
import json
import tarfile

import pytest
from conftest import ADMIN, Client, add_user, do_setup, make_app

from tessera.config import Settings

SCIM_JSON = {"Content-Type": "application/scim+json"}


@pytest.fixture
def scim(admin, app):
    tok = admin.post("/api/admin/scim/token").json()["token"]
    sc = Client(app)
    sc.headers.update({"Authorization": f"Bearer {tok}"})
    return sc


def scim_user(sc, email, **kw):
    r = sc.post("/scim/v2/Users", csrf=False, headers=SCIM_JSON, json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": email,
                                                                      "name": {"givenName": "Pat", "familyName": "Lee"}, "active": True, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- workspaces
def test_private_workspace_isolation(admin, app):
    a = add_user(admin, app, "ana@acme.ca", "analyst")
    b = add_user(admin, app, "ben@acme.ca", "analyst")
    w = a.post("/api/workspaces", json={"name": "Finance", "visibility": "private"}).json()
    q = a.post("/api/queries", json={"name": "Secret", "sql": "select 1 as x", "workspace_id": w["id"]})
    assert q.status_code == 200, q.text
    qid = q.json()["id"]
    assert all(x["name"] != "Finance" for x in b.get("/api/workspaces").json())
    assert all(x["id"] != qid for x in b.get("/api/queries").json())
    assert b.post(f"/api/queries/{qid}/run", json={}).status_code == 404
    assert b.post("/api/queries", json={"name": "x", "sql": "select 1", "workspace_id": w["id"]}).status_code == 404
    assert a.put(f"/api/workspaces/{w['id']}/members", json={"email": "ben@acme.ca", "role": "viewer"}).status_code == 200
    assert b.post(f"/api/queries/{qid}/run", json={}).status_code == 200
    assert b.delete(f"/api/queries/{qid}").status_code == 403
    assert admin.post(f"/api/queries/{qid}/run", json={}).status_code == 200, "global admins see everything"
    assert a.delete(f"/api/workspaces/{w['id']}").status_code == 409
    assert admin.put("/api/workspaces/1", json={"visibility": "private"}).status_code == 400


def test_saved_views(admin, app):
    d = admin.post("/api/dashboards", json={"name": "D", "tiles": []}).json()
    v = add_user(admin, app, "viv@acme.ca", "viewer")
    mine = v.post(f"/api/dashboards/{d['id']}/views", json={"name": "BC only", "state": {"filters": [{"field": "province", "value": ["BC"]}]}}).json()
    admin.post(f"/api/dashboards/{d['id']}/views", json={"name": "Shared", "state": {}, "shared": True})
    admin.post(f"/api/dashboards/{d['id']}/views", json={"name": "Private admin", "state": {}})
    names = {x["name"] for x in v.get(f"/api/dashboards/{d['id']}/views").json()}
    assert names == {"BC only", "Shared"}
    assert admin.delete(f"/api/dashboards/{d['id']}/views/{mine['id']}").status_code == 200


# ---------------------------------------------------------------- SCIM
def test_scim_requires_token(app, admin):
    c = Client(app)
    assert c.get("/scim/v2/Users").status_code == 401
    c.headers.update({"Authorization": "Bearer scim_wrong"})
    assert c.get("/scim/v2/Users").status_code == 401


def test_scim_entra_style_user_lifecycle(scim, admin, app):
    u = scim_user(scim, "Pat.Lee@Acme.ca", externalId="aad-123")
    assert u["userName"] == "pat.lee@acme.ca" and u["active"] and u["displayName"] == "Pat Lee"
    lst = scim.get('/scim/v2/Users?filter=userName eq "pat.lee@acme.ca"').json()
    assert lst["totalResults"] == 1 and lst["Resources"][0]["id"] == u["id"]
    assert scim.get('/scim/v2/Users?filter=userName eq "nobody@acme.ca"').json()["totalResults"] == 0
    assert scim.get('/scim/v2/Users?filter=name sw "x"').status_code == 400
    dup = scim.post("/scim/v2/Users", csrf=False, json={"userName": "pat.lee@acme.ca"})
    assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"
    # Entra sends string booleans and path-based replace operations
    r = scim.patch(f"/scim/v2/Users/{u['id']}", csrf=False, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                                                    "Operations": [{"op": "Replace", "path": "displayName", "value": "Patricia Lee"},
                                                                                   {"op": "Replace", "path": "active", "value": "False"}]})
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["displayName"] == "Patricia Lee"
    row = next(x for x in admin.get("/api/users").json() if x["email"] == "pat.lee@acme.ca")
    assert row["active"] in (0, False) and row["role"] == "viewer"
    events = [a["action"] for a in admin.get("/api/audit?action=scim.").json()]
    assert "scim.user_created" in events and "scim.user_updated" in events


def test_scim_okta_style_groups_map_roles_and_workspaces(scim, admin, app):
    admin.post("/api/workspaces", json={"name": "Finance", "visibility": "private"})
    r = admin.put("/api/admin/scim", json={"default_role": "viewer", "group_map": {
        "Data Engineers": {"role": "engineer"}, "Finance Analysts": {"role": "analyst", "workspace": "Finance", "workspace_role": "contributor"}}})
    assert r.status_code == 200, r.text
    u1 = scim_user(scim, "eng@acme.ca")
    u2 = scim_user(scim, "fin@acme.ca")
    g = scim.post("/scim/v2/Groups", csrf=False, json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "Data Engineers",
                                                       "members": [{"value": u1["id"]}]}).json()
    f = scim.post("/scim/v2/Groups", csrf=False, json={"displayName": "Finance Analysts", "members": []}).json()
    # Okta adds members with a PATCH add of a members array
    scim.patch(f"/scim/v2/Groups/{f['id']}", csrf=False, json={"Operations": [{"op": "add", "path": "members", "value": [{"value": u2["id"]}]}]})
    roles = {x["email"]: x["role"] for x in admin.get("/api/users").json()}
    assert roles["eng@acme.ca"] == "engineer" and roles["fin@acme.ca"] == "analyst"
    fin = next(w for w in admin.get("/api/workspaces").json() if w["name"] == "Finance")
    mem = {m["email"]: (m["role"], m["source"]) for m in admin.get(f"/api/workspaces/{fin['id']}/members").json()}
    assert mem["fin@acme.ca"] == ("contributor", "scim")
    # Entra-style removal by filter path
    scim.patch(f"/scim/v2/Groups/{f['id']}", csrf=False, json={"Operations": [{"op": "remove", "path": f'members[value eq "{u2["id"]}"]'}]})
    roles = {x["email"]: x["role"] for x in admin.get("/api/users").json()}
    assert roles["fin@acme.ca"] == "viewer"
    assert "fin@acme.ca" not in {m["email"] for m in admin.get(f"/api/workspaces/{fin['id']}/members").json()}
    assert scim.delete(f"/scim/v2/Groups/{g['id']}", csrf=False).status_code == 204
    assert {x["email"]: x["role"] for x in admin.get("/api/users").json()}["eng@acme.ca"] == "viewer"
    assert scim.get("/scim/v2/ServiceProviderConfig").json()["patch"]["supported"] is True


def test_scim_never_grants_admin_by_default(admin):
    assert admin.put("/api/admin/scim", json={"default_role": "admin"}).status_code == 400
    assert admin.put("/api/admin/scim", json={"group_map": {"x": {"workspace": "Nope"}}}).status_code == 400


def test_scim_delete_deactivates_and_revokes(scim, admin, app):
    u = scim_user(scim, "leaver@acme.ca")
    admin.put(f"/api/users/{next(x['id'] for x in admin.get('/api/users').json() if x['email'] == 'leaver@acme.ca')}", json={"password": "another strong pass 7"})
    assert scim.delete(f"/scim/v2/Users/{u['id']}", csrf=False).status_code == 204
    assert scim.get(f"/scim/v2/Users/{u['id']}").json()["active"] is False
    c = Client(app)
    assert c.post("/api/auth/login", json={"email": "leaver@acme.ca", "password": "another strong pass 7"}).status_code in (401, 403)


# ---------------------------------------------------------------- ops
def test_ready_metrics_and_request_id(admin, app, monkeypatch):
    r = admin.get("/api/ready")
    assert r.status_code == 200 and r.json()["ready"] and r.headers["x-request-id"]
    assert admin.get("/api/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    m = admin.get("/metrics")
    assert m.status_code == 200 and "tessera_http_requests_total" in m.text and 'tessera_build_info{version="' in m.text
    assert Client(app).get("/metrics").status_code == 401
    monkeypatch.setenv("TESSERA_METRICS_TOKEN", "prom-secret")
    assert Client(app).get("/metrics", headers={"Authorization": "Bearer prom-secret"}).status_code == 200
    assert Client(app).get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_rate_limit(settings, monkeypatch):
    monkeypatch.setenv("TESSERA_RATE_LIMIT", "5")
    app = make_app(settings)
    c = Client(app)
    codes = [c.get("/api/auth/me").status_code for _ in range(8)]
    assert 429 in codes and codes[0] != 429
    r = c.get("/api/auth/me")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/api/health").status_code == 200, "health checks are never limited"


def test_audit_export_and_retention(admin):
    r = admin.get("/api/audit/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["id", "ts", "user_email"] and any("auth.setup" in x[3] or "setup" in x[3] for x in rows[1:])
    j = admin.get("/api/audit/export?format=json")
    assert all(json.loads(line)["action"] for line in j.text.strip().splitlines())
    assert admin.put("/api/admin/retention", json={"audit_days": 7}).status_code == 400
    assert admin.put("/api/admin/retention", json={"audit_days": 30}).json()["ok"]
    assert admin.get("/api/admin/retention").json()["audit_days"] == 30


def test_retention_purges_old_audit(admin, app):
    st = app.state
    store = app.state.reporter.p.store
    store.exec("INSERT INTO audit(ts, action, target, detail) VALUES('2000-01-01T00:00:00+00:00','old.event','', '{}')")
    admin.put("/api/admin/retention", json={"audit_days": 30})
    assert store.scalar("SELECT count(*) FROM audit WHERE action='old.event'") == 0
    assert st.purge() == 0


def test_sessions_list_and_revoke(admin, app):
    other = Client(app)
    assert other.post("/api/auth/login", json=ADMIN).status_code == 200
    ss = admin.get("/api/sessions").json()
    assert len(ss) >= 2 and sum(s["current"] for s in ss) == 1
    victim = next(s for s in ss if not s["current"])
    assert admin.delete(f"/api/sessions/{victim['id']}").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert admin.delete("/api/sessions/zzz").status_code == 400


def test_backup_restore_roundtrip(sample_env, tmp_path):
    c = sample_env["client"]
    r = c.post("/api/admin/backup", json={"include_key": True})
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    arc = tmp_path / "b.tar.gz"
    arc.write_bytes(r.content)
    with tarfile.open(arc) as t:
        names = t.getnames()
        man = json.loads(t.extractfile("manifest.json").read())
    assert {"tessera.db", "warehouse.duckdb", "master.key"} <= set(man["files"]) and "imports" in names
    from tessera.enterprise import restore
    target = tmp_path / "restored"
    restore(arc, target)
    with pytest.raises(SystemExit):
        restore(arc, target)
    s2 = Settings.from_env(data_dir=target, retry_base_seconds=0.01, scheduler_interval=3600)
    app2 = make_app(s2)
    c2 = Client(app2)
    assert c2.post("/api/auth/login", json=ADMIN).status_code == 200
    orig = c.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"]
    assert c2.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"] == orig
    assert {m["name"] for m in c2.get("/api/models").json()} == {m["name"] for m in c.get("/api/models").json()}
    # connection secrets still decrypt with the restored key
    assert c2.get("/api/connections").status_code == 200


def test_backup_detects_tampering(sample_env, tmp_path):
    from tessera.enterprise import restore
    raw = sample_env["client"].post("/api/admin/backup", json={}).content
    src = tmp_path / "ok.tar.gz"
    src.write_bytes(raw)
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(src) as t, tarfile.open(bad, "w:gz") as out:
        for m in t.getmembers():
            f = t.extractfile(m) if m.isfile() else None
            if m.name == "tessera.db":
                data = f.read() + b"x"
                m.size = len(data)
                out.addfile(m, io.BytesIO(data))
            else:
                out.addfile(m, f)
    with pytest.raises(SystemExit, match="Checksum"):
        restore(bad, tmp_path / "t2")


def test_cli_backup_offline(sample_env, tmp_path, capsys):
    from tessera.cli import main
    out = tmp_path / "cli.tar.gz"
    main(["--data-dir", str(sample_env["settings"].data_dir), "backup", "--out", str(out)])
    assert out.exists() and "Backup written" in capsys.readouterr().out
    main(["--data-dir", str(tmp_path / "fresh"), "restore", str(out)])
    assert (tmp_path / "fresh" / "tessera.db").exists()
    _ = do_setup
