"""Visual transforms, push API, notebooks, glossary and governance, search, monitoring and deployment bundles."""
import json

import pytest
from conftest import Client, add_user, do_setup, make_app

from tessera.transforms import compile_steps
from tessera.warehouse import QueryError


def test_transform_compiler_rejects_bad_input():
    with pytest.raises(QueryError):
        compile_steps("raw.x", [{"op": "derive", "name": "n", "expr": "(select 1)"}])
    with pytest.raises(QueryError):
        compile_steps("raw.x", [{"op": "explode"}])
    with pytest.raises(QueryError):
        compile_steps("serving.x; drop", [])
    sql = compile_steps("raw.t", [{"op": "filter", "column": "name", "operator": "=", "value": "O'Brien"}])
    assert "'O''Brien'" in sql


def test_transform_preview_and_run(sample_env):
    c = sample_env["client"]
    steps = [{"op": "trim", "column": "status"}, {"op": "lower", "column": "status"},
             {"op": "filter", "column": "status", "operator": "in", "value": "paid, shipped"},
             {"op": "dedupe", "columns": ["order_id"]},
             {"op": "derive", "name": "big_order", "expr": "order_total > 200"},
             {"op": "join", "table": "raw.customers", "left_key": "customer_id", "right_key": "customer_id", "columns": ["province"]},
             {"op": "rename", "column": "province", "to": "region"},
             {"op": "cast", "column": "quantity", "to": "decimal"},
             {"op": "drop", "columns": ["card_number"]}]
    r = c.post("/api/transforms/preview", json={"source": "raw.orders", "steps": steps}).json()
    cols = r["result"]["columns"]
    assert "region" in cols and "big_order" in cols and "card_number" not in cols and "province" not in cols
    assert r["result"]["row_count"] == 100
    out = c.post("/api/transforms/run", json={"source": "raw.orders", "steps": steps, "target": "curated.orders_clean"}).json()
    dups = c.post("/api/sql", json={"sql": "select count(*) - count(distinct order_id) from curated.orders_clean"}).json()["rows"][0][0]
    assert out["row_count"] > 4000 and dups == 0
    assert {"raw.orders", "raw.customers"} <= set(out["inputs"])
    agg = c.post("/api/transforms/preview", json={"source": "curated.orders_clean", "steps": [
        {"op": "aggregate", "group_by": ["region"], "aggs": [{"fn": "sum", "column": "order_total", "as": "total"}, {"fn": "count"}]},
        {"op": "sort", "column": "total", "desc": True}, {"op": "limit", "n": 3}]}).json()
    assert agg["result"]["columns"] == ["region", "total", "count_rows"] and agg["result"]["row_count"] == 3


def test_push_api_with_schema_evolution(admin, app):
    tok = admin.post("/api/tokens", json={"name": "stream", "role": "engineer"}).json()["token"]
    bot = Client(app)
    h = {"Authorization": f"Bearer {tok}"}
    r = bot.post("/api/push/raw.clicks", json={"records": [{"user": "u1", "page": "/a"}, {"user": "u2", "page": "/b", "meta": {"ref": "ad"}}]}, headers=h, csrf=False)
    assert r.status_code == 200 and r.json()["row_count"] == 2
    r = bot.post("/api/push/raw.clicks", json={"records": [{"user": "u3", "page": "/c", "device": "ios"}]}, headers=h, csrf=False)
    assert r.json()["row_count"] == 3
    cols = admin.post("/api/sql", json={"sql": "select * from raw.clicks order by _received_at, \"user\""}).json()
    assert {"device", "meta_ref", "_received_at"} <= set(cols["columns"])
    assert bot.post("/api/push/serving.clicks", json={"records": [{"a": 1}]}, headers=h, csrf=False).status_code == 400
    assert bot.post("/api/push/raw.clicks", json={"records": []}, headers=h, csrf=False).status_code == 400
    analyst_tok = admin.post("/api/tokens", json={"name": "ro", "role": "analyst"}).json()["token"]
    assert bot.post("/api/push/raw.clicks", json={"records": [{"a": 1}]}, headers={"Authorization": f"Bearer {analyst_tok}"}, csrf=False).status_code == 403


def test_notebooks(admin, app):
    nb = admin.post("/api/notebooks", json={"name": "Investigation"}).json()
    assert len(nb["cells"]) == 2
    cells = nb["cells"] + [{"id": "c3", "type": "sql", "source": "select 2", "viz": {"type": "kpi"}}]
    assert admin.put(f"/api/notebooks/{nb['id']}", json={"cells": cells}).json()["cells"][2]["viz"] == {"type": "kpi"}
    assert admin.put(f"/api/notebooks/{nb['id']}", json={"cells": [{"type": "python", "source": "import os"}]}).status_code == 400
    other = add_user(admin, app, "nb2@acme.ca", "analyst")
    assert other.get(f"/api/notebooks/{nb['id']}").status_code == 200
    r = other.delete(f"/api/notebooks/{nb['id']}")
    assert r.status_code == 403


def test_glossary_governance_and_search(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    terms = {g["term"] for g in c.get("/api/glossary").json()}
    assert {"Revenue", "Refund rate"} <= terms
    assert c.post("/api/glossary", json={"term": "Customer", "definition": "A person with at least one order", "columns": ["raw.customers.customer_id"]}).status_code == 200
    assert c.post("/api/glossary", json={"term": "customer", "definition": "dup"}).status_code == 409
    assert c.post("/api/glossary", json={"term": "Bad", "definition": "x", "columns": ["nope"]}).status_code == 400
    did = next(d["id"] for d in c.get("/api/datasets").json() if d["name"] == "orders" and d["schema_name"] == "raw")
    eng = add_user(c, app, "gov-eng@acme.ca", "engineer")
    assert eng.patch(f"/api/datasets/{did}/governance", json={"endorsement": "certified"}).status_code == 403
    assert eng.patch(f"/api/datasets/{did}/governance", json={"endorsement": "promoted", "domain": "Sales"}).json()["domain"] == "Sales"
    hits = c.get("/api/search?q=refund").json()
    kinds = {h["kind"] for h in hits}
    assert {"model", "term", "notebook"} <= kinds
    viewer = add_user(c, app, "search-v@acme.ca", "viewer")
    vh = viewer.get("/api/search?q=orders").json()
    assert not any(h["kind"] == "dataset" and h["title"].startswith(("raw.", "curated.")) for h in vh)
    assert not any(h["kind"] == "notebook" for h in viewer.get("/api/search?q=refund").json())


def test_monitor(sample_env):
    c = sample_env["client"]
    c.post("/api/sql", json={"sql": "select count(*) from range(1000000)"})
    m = c.get("/api/monitor").json()
    assert m["pipelines"][0]["runs"] >= 2 and m["pipelines"][0]["success_rate"] is not None
    assert m["slow_queries"] and m["slow_queries"][0]["ms"] is not None
    assert any(s["table"] == "serving.orders_enriched" for s in m["storage"])


def test_bundle_roundtrip_into_fresh_install(sample_env, tmp_path):
    c = sample_env["client"]
    b = json.loads(c.get("/api/bundle/export").content)
    assert "secrets" not in json.dumps(b["connections"]) and b["connections"][0]["name"] == "Sample retail files"
    from tessera.config import Settings
    s2 = Settings.from_env(data_dir=tmp_path / "prod", retry_base_seconds=0.01)
    app2 = make_app(s2)
    prod = Client(app2)
    do_setup(prod, s2)
    from tessera import sample
    (s2.uploads_dir / "sample").mkdir()
    sample.generate(s2.uploads_dir / "sample")
    assert b["connections"][0]["config"]["base_path"] == "sample", "upload paths are exported relative"
    r = prod.post("/api/bundle/import", json={"bundle": b, "mode": "skip"}).json()
    assert r["report"]["pipelines"]["created"] == 1 and r["report"]["dashboards"]["created"] == len(b["dashboards"])
    assert r["report"]["models"]["created"] == 1 and r["report"]["queries"]["created"] == len(b["queries"])
    pl = prod.get("/api/pipelines").json()[0]
    conn = prod.get("/api/connections").json()[0]
    assert all(st["connection_id"] == conn["id"] for st in pl["steps"] if st["type"] == "ingest")
    run = app2.state.orch.trigger(pl["id"], "manual", "admin@acme.ca", wait=True)
    rr = prod.get(f"/api/runs/{run}").json()
    assert rr["status"] == "success", rr["log"][-4:]
    d = prod.get("/api/dashboards").json()[0]
    for t in d["tiles"]:
        if t.get("query_id"):
            assert prod.post(f"/api/queries/{t['query_id']}/run").status_code == 200
    again = prod.post("/api/bundle/import", json={"bundle": b, "mode": "skip"}).json()
    assert again["report"]["pipelines"]["skipped"] == 1 and again["report"]["queries"]["created"] == 0
    assert prod.post("/api/bundle/import", json={"bundle": {"x": 1}}).status_code == 400


def test_store_migration_adds_columns(tmp_path):
    import sqlite3

    from tessera.store import Store
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE queries(id INTEGER PRIMARY KEY, name TEXT NOT NULL, sql TEXT NOT NULL, viz TEXT NOT NULL DEFAULT '{}', created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    con.execute("INSERT INTO queries(name, sql, created_at, updated_at) VALUES ('old', 'select 1', 'x', 'x')")
    con.commit()
    con.close()
    s = Store(p)
    q = s.one("SELECT * FROM queries")
    assert q["kind"] == "sql" and "model_id" in q
