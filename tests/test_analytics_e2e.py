"""End to end on the sample workspace: pipeline, catalog, dashboards, readiness, role scoping."""
import json

from conftest import Client


def test_sample_pipeline_succeeded(sample_env):
    c = sample_env["client"]
    p = c.get("/api/pipelines").json()[0]
    assert p["name"] == "Retail daily load" and p["last_run"]["status"] == "success"
    run = c.get(f"/api/runs/{p['last_run']['id']}").json()
    assert set(run["steps"]) >= {"load_orders", "enrich", "quality_gate", "publish_orders"}


def test_dashboard_tiles_run_for_viewer(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    c.post("/api/users", json={"email": "viewer@acme.ca", "role": "viewer", "password": "another strong pass 7"})
    v = Client(app)
    v.post("/api/auth/login", json={"email": "viewer@acme.ca", "password": "another strong pass 7"})
    d = v.get("/api/dashboards").json()[0]
    assert d["name"] == "Sales overview"
    for t in [t for t in d["tiles"] if t.get("query_id")]:
        r = v.post(f"/api/queries/{t['query_id']}/run")
        assert r.status_code == 200, r.text
        assert r.json()["result"]["row_count"] >= 1
    # viewers only see published datasets
    names = {f"{x['schema_name']}.{x['name']}" for x in v.get("/api/datasets").json()}
    assert names == {"serving.orders_enriched", "serving.customers"}
    raw_id = next(x["id"] for x in c.get("/api/datasets").json() if x["schema_name"] == "raw")
    assert v.get(f"/api/datasets/{raw_id}/preview").status_code == 403


def test_saved_query_on_raw_denied_for_analyst(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    q = c.post("/api/queries", json={"name": "raw peek", "sql": "select * from raw.customers limit 5", "viz": {"type": "table"}}).json()
    c.post("/api/users", json={"email": "an2@acme.ca", "role": "analyst", "password": "another strong pass 7"})
    a = Client(app)
    a.post("/api/auth/login", json={"email": "an2@acme.ca", "password": "another strong pass 7"})
    assert a.post(f"/api/queries/{q['id']}/run").status_code == 403
    assert a.post("/api/queries", json={"name": "x", "sql": "select * from raw.orders"}).status_code == 403
    ok = a.post("/api/queries", json={"name": "mine", "sql": "select count(*) n from serving.customers", "viz": {"type": "kpi", "y": "n"}})
    assert ok.status_code == 200
    dash = a.post("/api/dashboards", json={"name": "Mine", "tiles": [{"query_id": ok.json()["id"], "w": 4}]}).json()
    assert a.put(f"/api/dashboards/{dash['id']}", json={"tiles": [{"query_id": 99999}]}).status_code == 400


def test_masked_values_in_serving(sample_env):
    c = sample_env["client"]
    rows = c.post("/api/sql", json={"sql": "select full_name, email, sin, phone, card from (select full_name, email, sin, phone, NULL as card from serving.customers) limit 20"}).json()["rows"]
    for name, email, sin, phone, _ in rows:
        assert name == "[REDACTED]"
        assert email is None or "@" not in email
        assert sin.startswith("*****")
    raw = c.post("/api/sql", json={"sql": "select count(*) from raw.customers where email like '%@%'"}).json()["rows"][0][0]
    assert raw > 0


def test_readiness_and_datacard(sample_env):
    c = sample_env["client"]
    ds = {f"{d['schema_name']}.{d['name']}": d for d in c.get("/api/datasets").json()}
    oe = ds["curated.orders_enriched"]
    r = c.get(f"/api/datasets/{oe['id']}/readiness").json()
    keys = {x["key"] for x in r["checks"]}
    assert keys == {"quality", "completeness", "documentation", "security", "freshness", "lineage", "uniqueness"}
    assert sum(x["weight"] for x in r["checks"]) == 100
    assert 0 < r["score"] <= 100
    assert ds["raw.orders"]["readiness"] < oe["readiness"], "documented, tested, published data should score higher"
    card = c.get(f"/api/datasets/{oe['id']}/datacard").json()
    assert card["columns"] and card["lineage"]["edges"] and card["quality"]["rules"] >= 5
    json.dumps(card)


def test_overview(sample_env):
    o = sample_env["client"].get("/api/overview").json()
    assert o["datasets"] == 6 and o["pipelines"] == 1 and o["rules"] >= 10 and o["published"] == 2
    assert o["recent_runs"] and o["readiness"]


def test_rule_suggestions_and_quality_run(sample_env):
    c = sample_env["client"]
    did = next(d["id"] for d in c.get("/api/datasets").json() if d["schema_name"] == "raw" and d["name"] == "orders")
    sugg = c.post(f"/api/datasets/{did}/rules/suggest").json()
    assert any(s["kind"] == "unique" and s["column_name"] == "order_id" for s in sugg)
    for s in sugg[:4]:
        assert c.post(f"/api/datasets/{did}/rules", json=s).status_code == 200
    res = c.post(f"/api/datasets/{did}/quality/run").json()
    assert res["rules"] == 4 and res["score"] is not None


def test_openapi_docs(sample_env):
    c = sample_env["client"]
    spec = c.get("/api/openapi.json").json()
    assert "/api/pipelines/{pid}/run" in spec["paths"]
