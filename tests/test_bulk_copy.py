"""Bulk copy of whole databases, merge (upsert) loads and schema drift tracking."""
import sqlite3
import time

import pyarrow as pa
import pytest

from tessera.connections_api import change_column, plan_table


def wait_run(c, rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def sql(c, q):
    r = c.post("/api/sql", json={"sql": q})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


@pytest.fixture
def shop(settings):
    path = settings.uploads_dir / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("""
        create table customers(id integer primary key, name text, updated_at timestamp);
        create table events(seq integer, kind text, modified timestamp);
        create table countries(code text primary key, name text);
        create table "Order Lines"(id integer primary key, qty integer);
        insert into customers values (1,'Ann','2026-01-01 10:00:00'),(2,'Bo','2026-01-01 10:00:00');
        insert into events values (1,'a','2026-01-01 10:00:00'),(2,'b','2026-01-02 10:00:00');
        insert into countries values ('CA','Canada'),('US','United States');
        insert into "Order Lines" values (1, 3);
    """)
    db.commit()
    yield db
    db.close()


def test_detection_rules():
    assert change_column([{"name": "Id", "type": "integer"}, {"name": "LastModifiedDate", "type": "datetime"}]) == "LastModifiedDate"
    assert change_column([{"name": "updated_at", "type": "text"}]) is None, "a text column is not trusted as a change marker"
    assert plan_table({"columns": [{"name": "updated_at", "type": "timestamp"}], "primary_key": ["id"]}, "auto") == \
        ("merge", {"key": ["id"], "cursor_column": "updated_at"})
    assert plan_table({"columns": [{"name": "modified", "type": "datetime"}], "primary_key": []}, "auto") == ("incremental", {"cursor_column": "modified"})
    assert plan_table({"columns": [{"name": "x", "type": "text"}], "primary_key": ["x"]}, "auto") == ("replace", {})
    assert plan_table(None, "auto") == ("replace", {})


def test_copy_a_whole_database_then_keep_it_in_sync(admin, settings, shop):
    conn = admin.post("/api/connections", json={"type": "sqlite", "name": "shop", "config": {"path": "shop.db"}}).json()
    tables = [t["name"] for t in admin.get(f"/api/connections/{conn['id']}/browse").json()]
    assert set(tables) == {"customers", "events", "countries", "Order Lines"}
    r = admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": tables, "prefix": "Shop", "schedule": "0 * * * *"})
    assert r.status_code == 200, r.text
    body = r.json()
    plan = {t["source"]: t for t in body["tables"]}
    assert plan["customers"]["mode"] == "merge" and plan["customers"]["key"] == ["id"] and plan["customers"]["cursor"] == "updated_at"
    assert plan["events"]["mode"] == "incremental" and plan["countries"]["mode"] == "replace"
    assert plan["Order Lines"]["target"] == "raw.shop_order_lines"
    assert body["pipeline"]["schedule"] == "0 * * * *" and body["steps"] == 4
    assert next(p for p in admin.get("/api/pipelines").json() if p["id"] == body["pipeline"]["id"])["next_run_at"]
    first = wait_run(admin, body["run_id"])
    assert first["status"] == "success", (first.get("error"), first.get("log"))
    assert sql(admin, "select count(*) from raw.shop_customers") == [[2]]

    # the source changes: one update, one new row, one new event
    shop.executescript("""
        update customers set name='Ann B', updated_at='2026-02-01 10:00:00' where id=1;
        insert into customers values (3,'Cy','2026-02-01 10:00:00');
        insert into events values (3,'c','2026-02-01 10:00:00');
        update countries set name='Canada!' where code='CA';
    """)
    shop.commit()
    rid = admin.post(f"/api/pipelines/{body['pipeline']['id']}/run").json()["run_id"]
    assert wait_run(admin, rid)["status"] == "success"
    assert sql(admin, "select id, name from raw.shop_customers order by id") == [[1, "Ann B"], [2, "Bo"], [3, "Cy"]], "updated in place, no duplicates"
    assert sql(admin, "select count(*) from raw.shop_events") == [[3]]
    assert sql(admin, "select name from raw.shop_countries where code='CA'") == [["Canada!"]]

    # a second copy of the same tables gets its own pipeline name
    again = admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": ["countries"], "run": False, "mode": "replace"}).json()
    assert again["pipeline"]["name"] == "Copy from shop (2)" and again["run_id"] is None
    third = admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": ["countries"], "run": False}).json()
    assert third["pipeline"]["name"] == "Copy from shop (3)"


def test_copy_rejects_bad_requests(admin, shop):
    conn = admin.post("/api/connections", json={"type": "sqlite", "name": "shop", "config": {"path": "shop.db"}}).json()
    assert admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": []}).status_code == 400
    assert admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": ["countries"], "mode": "weird"}).status_code == 400
    assert admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": ["events"], "mode": "merge"}).status_code == 400
    assert admin.post(f"/api/connections/{conn['id']}/copy", json={"tables": ["countries"], "schedule": "not a cron"}).status_code == 400


def test_copy_files_and_app_objects(admin, settings):
    (settings.uploads_dir / "a.csv").write_text("id\n1\n")
    (settings.uploads_dir / "b.csv").write_text("id\n2\n")
    files = admin.post("/api/connections", json={"type": "file", "name": "files", "config": {}}).json()
    r = admin.post(f"/api/connections/{files['id']}/copy", json={"tables": ["a.csv", "b.csv"]}).json()
    assert [t["target"] for t in r["tables"]] == ["raw.a_csv", "raw.b_csv"]
    assert wait_run(admin, r["run_id"])["status"] == "success"
    app = admin.post("/api/connections/from-app", json={"app": "stripe", "name": "Stripe", "values": {"token": "rk_test_1"}}).json()
    r = admin.post(f"/api/connections/{app['id']}/copy", json={"objects": ["customers", "invoices"], "prefix": "stripe", "run": False, "mode": "auto"}).json()
    assert {t["mode"] for t in r["tables"]} == {"merge"} and r["tables"][0]["target"] == "raw.stripe_customers"
    assert r["tables"][0]["key"] == ["id"] and r["tables"][0]["cursor"] == "created"


def test_merge_loads_and_schema_drift(admin, settings, app):
    plat = app.state.platform
    up = settings.uploads_dir
    files = admin.post("/api/connections", json={"type": "file", "name": "files", "config": {}}).json()

    def load(text, mode="merge", key="id"):
        (up / "o.csv").write_text(text)
        r = admin.post(f"/api/connections/{files['id']}/ingest", json={"source": {"path": "o.csv", "key": key}, "target": "raw.o", "mode": mode})
        assert r.status_code == 200, r.text
        return r.json()

    load("id,amount,note\n1,10,a\n2,20,b\n")
    assert admin.post(f"/api/connections/{files['id']}/ingest", json={"source": {"path": "o.csv"}, "target": "raw.o", "mode": "merge"}).status_code == 400
    # a downstream table, so the drift notice can name the impact
    admin.post("/api/pipelines", json={"name": "down", "steps": [{"id": "t", "type": "transform", "sql": "select * from raw.o", "target": "curated.o2"}]})
    plat.transform("select id from raw.o", "curated.o_ids")
    load("id,amount,region\n2,25.5,west\n3,30,east\n")
    assert sql(admin, "select id, amount, region from raw.o order by id") == [[1, 10.0, None], [2, 25.5, "west"], [3, 30.0, "east"]]
    did = plat.dataset("raw", "o")["id"]
    changes = {(c["change"], c["column_name"]) for c in admin.get(f"/api/datasets/{did}/schema-changes").json()}
    assert changes == {("added", "region"), ("removed", "note"), ("retyped", "amount")}
    notes = admin.get("/api/notifications").json()
    n = next(x for x in (notes if isinstance(notes, list) else notes["items"]) if "Schema changed in raw.o" in x["title"])
    assert "curated.o_ids" in n["body"] and n["level"] == "warn"
    # a load with the same columns records nothing new
    before = len(admin.get(f"/api/datasets/{did}/schema-changes").json())
    load("id,amount,region\n4,1.5,x\n")
    assert len(admin.get(f"/api/datasets/{did}/schema-changes").json()) == before
    # viewers cannot read raw schema history
    assert admin.get("/api/datasets/999999/schema-changes").status_code == 404


def test_warehouse_merge_with_deletes(app):
    wh = app.state.platform.wh
    wh.load_arrow(pa.table({"k": [1, 2, 2], "v": ["a", "b", "b2"]}), "raw", "m", "merge", ["k"])
    wh.load_arrow(pa.table({"k": [2, 3, 1], "v": ["B", "c", None], "gone": [False, False, True]}), "raw", "m", "merge", ["k"], "gone")
    cur = wh.cursor()
    assert cur.execute("select k, v from raw.m order by k").fetchall() == [(2, "B"), (3, "c")]
    assert "gone" not in [r[0] for r in cur.execute("describe raw.m").fetchall()]
    with pytest.raises(Exception, match="Key column"):
        wh.load_arrow(pa.table({"x": [1]}), "raw", "m", "merge", ["k"])
