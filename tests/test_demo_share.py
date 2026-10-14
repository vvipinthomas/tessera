"""Demo mode on/off cycle, share links and embedding, and full-year forecasts for measures that add up over time."""
import pytest
from conftest import Client, add_user


def test_demo_cycle_removes_exactly_the_demo(admin, app, settings):
    assert admin.get("/api/demo").json()["enabled"] is False
    mine = admin.post("/api/queries", json={"name": "Mine", "sql": "select 1 as x"}).json()
    r = admin.post("/api/demo")
    assert r.status_code == 200, r.text
    st = admin.get("/api/demo").json()
    assert st["enabled"] and st["counts"]["semantic_models"] == 1 and "serving.orders_enriched" in st["tables"]
    assert admin.post("/api/demo").status_code == 409
    mid = admin.get("/api/models").json()[0]["id"]
    q = admin.post("/api/queries", json={"name": "Built on demo", "kind": "model", "model_id": mid, "spec": {"measures": ["revenue"]}, "viz": {"type": "kpi"}}).json()
    admin.post("/api/dashboards", json={"name": "My demo board", "tiles": [{"query_id": q["id"], "w": 6}]})
    plan = admin.get("/api/demo/plan").json()
    assert plan["user_built"] == {"queries": 1, "dashboards": 1}
    out = admin.delete("/api/demo").json()
    assert out["tables_dropped"] >= 5 and out["deleted"]["semantic_models"] == 1
    assert admin.get("/api/models").json() == []
    assert [x["name"] for x in admin.get("/api/queries").json()] == ["Mine"], "things not built on demo data stay"
    assert admin.get("/api/dashboards").json() == []
    assert admin.get("/api/connections").json() == []
    assert admin.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).status_code == 400
    assert not (settings.uploads_dir / "sample").exists()
    assert admin.get("/api/users").json() and admin.get("/api/demo").json()["enabled"] is False
    assert admin.delete("/api/demo").status_code == 409
    # and it can come back
    assert admin.post("/api/demo").status_code == 200
    assert admin.get("/api/demo").json()["enabled"]


def test_demo_needs_admin(admin, app):
    v = add_user(admin, app, "demo.viewer@acme.ca", "analyst")
    assert v.post("/api/demo").status_code == 403 and v.delete("/api/demo").status_code == 403
    assert v.get("/api/demo").status_code == 200


# ---------------------------------------------------------------- sharing
@pytest.fixture
def shared(sample_env):
    c = sample_env["client"]
    yield c, sample_env["app"]
    c.put("/api/admin/sharing", json={"enabled": False})


def test_sharing_is_off_by_default_and_locked_links_narrow_data(shared):
    c, app = shared
    mid = next(m["id"] for m in c.get("/api/models").json() if m["name"] == "Sales")
    assert c.post("/api/dashboards/1/share", json={}).status_code == 403
    assert c.put("/api/admin/sharing", json={"enabled": True, "embed_origins": ["javascript:alert(1)"]}).status_code == 400
    c.put("/api/admin/sharing", json={"enabled": True, "embed_origins": ["https://intranet.acme.ca"]})
    r = c.post("/api/dashboards/1/share", json={"expires_days": 7, "filters": [{"model_id": mid, "field": "province", "op": "in", "value": ["BC"]}]})
    assert r.status_code == 200, r.text
    tok = r.json()["url"].rsplit("/", 1)[1]
    anon = Client(app)
    page = anon.get(f"/embed/{tok}")
    assert page.status_code == 200 and "frame-ancestors 'self' https://intranet.acme.ca" in page.headers["content-security-policy"]
    assert "x-frame-options" not in {k.lower() for k in page.headers}
    assert anon.get("/app").headers["x-frame-options"] == "DENY"
    meta = anon.get(f"/api/embed/{tok}").json()
    assert meta["tiles"] and all(t["type"] in ("text", "query") for t in meta["tiles"])
    prov_tile = next(t for t in meta["tiles"] if t.get("title") == "Revenue by province")
    rows = anon.post(f"/api/embed/{tok}/tiles/{prov_tile['i']}").json()["result"]["rows"]
    assert [r[0] for r in rows] == ["BC"]
    assert anon.get("/api/dashboards").status_code == 401, "a share token is not a session"
    links = c.get("/api/dashboards/1/shares").json()
    assert links[0]["views"] >= 1 and "token" not in links[0]
    assert c.delete(f"/api/shares/{links[0]['id']}").status_code == 200
    assert anon.get(f"/api/embed/{tok}").status_code == 404


def test_unlocked_link_withholds_nothing_but_sql_tiles_only_when_locked(shared):
    c, app = shared
    c.put("/api/admin/sharing", json={"enabled": True})
    sq = c.post("/api/queries", json={"name": "SQL tile", "sql": "select province, count(*) n from serving.orders_enriched group by 1", "viz": {"type": "bar"}}).json()
    d = c.post("/api/dashboards", json={"name": "Mixed", "tiles": [{"query_id": sq["id"], "w": 6}, {"query_id": 2, "w": 6}]}).json()
    mid = next(m["id"] for m in c.get("/api/models").json() if m["name"] == "Sales")
    open_tok = c.post(f"/api/dashboards/{d['id']}/share", json={}).json()["url"].rsplit("/", 1)[1]
    locked_tok = c.post(f"/api/dashboards/{d['id']}/share", json={"filters": [{"model_id": mid, "field": "province", "op": "in", "value": ["AB"]}]}).json()["url"].rsplit("/", 1)[1]
    anon = Client(app)
    assert len(anon.get(f"/api/embed/{open_tok}").json()["tiles"]) == 2
    locked = anon.get(f"/api/embed/{locked_tok}").json()["tiles"]
    assert len(locked) == 1, "a SQL tile cannot be narrowed by a locked filter, so it is withheld"
    assert anon.post(f"/api/embed/{locked_tok}/tiles/0").status_code == 404


def test_links_die_with_owner_access_and_when_sharing_is_off(shared):
    c, app = shared
    c.put("/api/admin/sharing", json={"enabled": True})
    a = add_user(c, app, "sharer@acme.ca", "analyst")
    tok = a.post("/api/dashboards/1/share", json={}).json()["url"].rsplit("/", 1)[1]
    anon = Client(app)
    assert anon.get(f"/api/embed/{tok}").status_code == 200
    uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "sharer@acme.ca")
    c.patch(f"/api/users/{uid}", json={"active": False})
    assert anon.get(f"/api/embed/{tok}").status_code == 404
    c.patch(f"/api/users/{uid}", json={"active": True})
    assert anon.get(f"/api/embed/{tok}").status_code == 200
    c.put("/api/admin/sharing", json={"enabled": False})
    assert anon.get(f"/api/embed/{tok}").status_code == 404
    c.put("/api/admin/sharing", json={"enabled": True})
    assert anon.get(f"/api/embed/{tok}").status_code == 404, "turning sharing off revoked existing links"
    v = add_user(c, app, "noshare@acme.ca", "viewer")
    assert v.post("/api/dashboards/1/share", json={}).status_code == 403
    assert anon.get("/embed/tsh_notarealtokenatallxxxxxxxx").status_code == 404
    assert c.post("/api/dashboards/1/share", json={"expires_days": 999}).status_code == 400


def test_orders_get_a_full_year_forecast(sample_env):
    b = sample_env["client"].post("/api/dashboards/1/brief", json={}).json()
    orders = next(m for s in b["sections"] for m in s["measures"] if m["measure"] == "orders")
    assert orders["additive"] is True and orders["landing"]["value"] > orders["landing"]["actual_to_date"] > 0
