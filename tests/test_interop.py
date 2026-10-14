"""Working with other platforms: deliver published data out, OpenLineage events, dbt manifest import."""
import json
import sqlite3

import httpx
import pyarrow.parquet as pq
import pytest
from conftest import PG_URL, Client, do_setup, make_app
from test_pipelines import wait_run


@pytest.fixture
def env(settings):
    events, mode = [], {"collector": "up"}

    def handler(req):
        if "lineage" in str(req.url):
            if mode["collector"] == "down":
                return httpx.Response(503)
            events.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": json.loads(req.content)})
            return httpx.Response(201)
        return httpx.Response(200, json={"ok": True})
    app = make_app(settings, http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    c = Client(app)
    do_setup(c, settings)
    (settings.uploads_dir / "o.csv").write_text("id,amount,email,region\n1,10,a@x.ca,west\n2,30,b@y.ca,east\n3,5,c@z.ca,west\n")
    files = c.post("/api/connections", json={"type": "file", "name": "files", "config": {}}).json()
    return {"c": c, "app": app, "events": events, "mode": mode, "files": files["id"], "settings": settings}


def base_steps(files):
    return [
        {"id": "load", "type": "ingest", "connection_id": files, "source": {"path": "o.csv"}, "target": "raw.o"},
        {"id": "clean", "type": "transform", "depends_on": ["load"], "sql": "select * from raw.o", "target": "curated.o"},
        {"id": "scan", "type": "pii_scan", "depends_on": ["clean"], "dataset": "curated.o"},
        {"id": "pub", "type": "publish", "depends_on": ["scan"], "dataset": "curated.o"},
    ]


def run(c, steps, name="p"):
    p = c.post("/api/pipelines", json={"name": name, "steps": steps})
    assert p.status_code == 200, p.text
    return wait_run(c, c.post(f"/api/pipelines/{p.json()['id']}/run").json()["run_id"]), p.json()


def test_deliver_to_sqlite_delta_parquet(env):
    c, s = env["c"], env["settings"]
    sqlite3.connect(s.uploads_dir / "mart.db").close()  # a destination database that already exists
    lite = c.post("/api/connections", json={"type": "sqlite", "name": "mart", "config": {"path": "mart.db"}}).json()
    delta = c.post("/api/connections", json={"type": "delta", "name": "lake", "config": {"base_path": "lake"}}).json()
    steps = base_steps(env["files"]) + [
        {"id": "to_db", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": lite["id"], "table": "orders"},
        {"id": "to_lake", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": delta["id"], "table": "gold/orders"},
        {"id": "to_files", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": env["files"],
         "table": "out/orders", "format": "parquet"},
    ]
    r, p = run(c, steps)
    assert r["status"] == "success", json.dumps(r["steps"])[:1500]
    with sqlite3.connect(s.uploads_dir / "mart.db") as db:
        rows = db.execute("select id, email from orders order by id").fetchall()
    assert [x[0] for x in rows] == [1, 2, 3]
    assert all("@" not in (x[1] or "") for x in rows), "published (masked) data only"
    from deltalake import DeltaTable
    assert DeltaTable(str(s.uploads_dir / "lake" / "gold" / "orders")).to_pyarrow_table().num_rows == 3
    assert pq.read_table(s.uploads_dir / "out" / "orders.parquet").num_rows == 3
    # append mode on a second run doubles the database rows; replace keeps three in the lake
    steps[4]["mode"] = "append"
    c.put(f"/api/pipelines/{p['id']}", json={"steps": steps})
    assert wait_run(c, c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"])["status"] == "success"
    with sqlite3.connect(s.uploads_dir / "mart.db") as db:
        assert db.execute("select count(*) from orders").fetchone()[0] == 6
    assert DeltaTable(str(s.uploads_dir / "lake" / "gold" / "orders")).to_pyarrow_table().num_rows == 3
    lin = json.dumps(c.get("/api/lineage").json())
    assert "dest:mart/orders" in lin and "dest:lake/gold/orders" in lin


def test_deliver_refuses_unpublished_and_bad_targets(env):
    c = env["c"]
    sqlite3.connect(env["settings"].uploads_dir / "mart.db").close()
    lite = c.post("/api/connections", json={"type": "sqlite", "name": "mart", "config": {"path": "mart.db"}}).json()
    r, _ = run(c, base_steps(env["files"]) + [
        {"id": "leak", "type": "deliver", "depends_on": ["pub"], "dataset": "curated.o", "connection_id": lite["id"], "table": "x"}], "leak")
    assert r["status"] == "failed" and "Only published data" in json.dumps(r["steps"])
    r, _ = run(c, base_steps(env["files"]) + [
        {"id": "bad", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": env["files"], "table": "../../etc/x"}], "trav")
    assert r["status"] == "failed" and "relative path" in json.dumps(r["steps"])
    r, _ = run(c, base_steps(env["files"]) + [
        {"id": "bad", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": lite["id"], "table": "x; drop table y"}], "inj")
    assert r["status"] == "failed" and "Destination table" in json.dumps(r["steps"])


@pytest.mark.skipif(not PG_URL, reason="set TESSERA_TEST_PG for live PostgreSQL")
def test_deliver_to_postgres(env):
    import psycopg
    c = env["c"]
    kv = dict(x.split("=") for x in PG_URL.split())
    pg = c.post("/api/connections", json={"type": "postgres", "name": "pg", "config": {"host": kv["host"], "port": int(kv["port"]), "database": kv["dbname"],
                                                                                       "username": kv["user"], "sslmode": "disable"}}).json()
    with psycopg.connect(PG_URL, autocommit=True) as db:
        db.execute("create schema if not exists tessera_out")
    r, _ = run(c, base_steps(env["files"]) + [
        {"id": "to_pg", "type": "deliver", "depends_on": ["pub"], "dataset": "serving.o", "connection_id": pg["id"], "table": "tessera_out.orders"}], "pg")
    assert r["status"] == "success", json.dumps(r["steps"])[:1500]
    with psycopg.connect(PG_URL) as db:
        assert db.execute("select count(*) from tessera_out.orders").fetchone()[0] == 3
        db.execute("drop schema tessera_out cascade")


def test_openlineage_events(env):
    c, events = env["c"], env["events"]
    r = c.put("/api/admin/openlineage", json={"enabled": True, "url": "https://127.0.0.1:5000", "namespace": "acme.tessera", "api_key": "k-123"})
    assert r.status_code == 200 and r.json()["api_key_set"] and "k-123" not in r.text
    assert c.post("/api/admin/openlineage/test").json()["ok"]
    events.clear()
    r, p = run(c, base_steps(env["files"]))
    assert r["status"] == "success"
    kinds = [e["body"]["eventType"] for e in events]
    assert kinds == ["START", "COMPLETE"]
    start, done = events[0]["body"], events[1]["body"]
    assert events[0]["url"].endswith("/api/v1/lineage") and events[0]["auth"] == "Bearer k-123"
    assert start["run"]["runId"] == done["run"]["runId"] and start["job"] == done["job"] or start["job"]["name"] == "p"
    assert done["job"]["namespace"] == "acme.tessera"
    outs = {o["name"]: o for o in done["outputs"]}
    ins = {i["name"] for i in done["inputs"]}
    assert {"raw.o", "curated.o", "serving.o"} <= set(outs) and any("o.csv" in i for i in ins)
    assert {f["name"] for f in outs["serving.o"]["facets"]["schema"]["fields"]} >= {"id", "amount", "email"}
    for k in ("eventTime", "producer", "schemaURL"):
        assert done[k]
    # a failing run sends FAIL with the error; a collector outage never fails a run
    events.clear()
    r, _ = run(c, [{"id": "bad", "type": "transform", "sql": "select * from raw.nope", "target": "curated.z"}], "bad")
    assert r["status"] == "failed" and [e["body"]["eventType"] for e in events] == ["START", "FAIL"]
    assert "errorMessage" in events[1]["body"]["run"]["facets"]
    env["mode"]["collector"] = "down"
    r, _ = run(c, base_steps(env["files"]), "still works")
    assert r["status"] == "success" and "OpenLineage event not delivered" in json.dumps(r["log"])
    # guards
    assert c.put("/api/admin/openlineage", json={"url": "http://169.254.169.254/x"}).status_code == 400
    assert c.put("/api/admin/openlineage", json={"url": "ftp://x"}).status_code == 400


def manifest():
    return {"metadata": {"dbt_version": "1.9.0"}, "nodes": {
        "model.shop.stg_orders": {"resource_type": "model", "name": "o", "alias": "o", "description": "One row per order.",
                                  "columns": {"amount": {"name": "amount", "description": "Order total in CAD"}}, "tags": ["finance"],
                                  "meta": {"owner": "data-team@acme.ca"}, "depends_on": {"nodes": ["source.shop.raw.o"]}},
        "model.shop.not_here": {"resource_type": "model", "name": "missing_model", "depends_on": {"nodes": []}},
        "test.shop.not_null_o_id": {"resource_type": "test", "test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}},
                                    "depends_on": {"nodes": ["model.shop.stg_orders"]}, "config": {"severity": "error"}},
        "test.shop.accepted_region": {"resource_type": "test", "test_metadata": {"name": "accepted_values", "kwargs": {"column_name": "region", "values": ["west", "east"]}},
                                      "depends_on": {"nodes": ["model.shop.stg_orders"]}, "config": {"severity": "warn"}},
        "test.shop.custom": {"resource_type": "test", "test_metadata": {"name": "expression_is_true"}, "depends_on": {"nodes": ["model.shop.stg_orders"]}},
    }, "sources": {"source.shop.raw.o": {"resource_type": "source", "name": "o", "identifier": "o", "description": "Raw order file"}}}


def test_dbt_import(env):
    c = env["c"]
    run(c, base_steps(env["files"]))
    r = c.post("/api/integrations/dbt", files={"file": ("manifest.json", json.dumps(manifest()).encode(), "application/json")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["matched"] == 2 and out["unmatched"] == ["missing_model"] and out["rules_created"] == 2 and out["dbt_version"] == "1.9.0"
    ds = {f"{d['schema_name']}.{d['name']}": d for d in c.get("/api/datasets").json()}
    d = c.get(f"/api/datasets/{ds['serving.o']['id']}").json()
    assert d["description"] == "One row per order." and d["owner"] == "data-team@acme.ca" and "dbt" in d["tags"]
    assert next(x for x in d["columns"] if x["name"] == "amount")["description"] == "Order total in CAD"
    rules = c.get(f"/api/datasets/{ds['serving.o']['id']}/rules").json()
    kinds = {(x["kind"], x["column_name"], x["severity"]) for x in rules}
    assert ("not_null", "id", "error") in kinds and ("allowed_values", "region", "warn") in kinds
    # importing twice does not duplicate rules
    again = c.post("/api/integrations/dbt", files={"file": ("manifest.json", json.dumps(manifest()).encode(), "application/json")}).json()
    assert again["rules_created"] == 0
    assert c.post("/api/integrations/dbt", files={"file": ("m.json", b"not json", "application/json")}).status_code == 400
    assert c.post("/api/integrations/dbt", files={"file": ("m.json", b"{}", "application/json")}).status_code == 400
    info = c.get("/api/integrations").json()
    assert info["odata_url"].endswith("/odata") and "postgres" in info["destinations"]
