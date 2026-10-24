"""Mirroring PostgreSQL through logical decoding. The parser tests always run; the live tests need a PostgreSQL with
wal_level=logical (set TESSERA_TEST_PG)."""
import pytest
from conftest import PG_URL

from tessera import mirror
from tessera.mirror import TOAST, MirrorError, parse_change, parse_tuple


def test_parse_tuples_and_lines():
    t = parse_tuple("id[integer]:1 name[text]:'O''Brien says ''hi''' note[character varying]:null tags[text[]]:'{a,b}' "
                    '"Odd Col"[text]:\'x y\' big[text]:unchanged-toast-datum flag[boolean]:true')
    assert t == {"id": ("integer", "1"), "name": ("text", "O'Brien says 'hi'"), "note": ("character varying", None),
                 "tags": ("text[]", "{a,b}"), "Odd Col": ("text", "x y"), "big": ("text", TOAST), "flag": ("boolean", "true")}
    ch = parse_change("""table public."Order Lines": UPDATE: old-key: id[integer]:1 new-tuple: id[integer]:5 qty[integer]:2""")
    assert ch["table"] == "public.Order Lines" and ch["op"] == "update" and ch["old_key"] == {"id": ("integer", "1")} and ch["row"]["id"] == ("integer", "5")
    assert parse_change("BEGIN") is None and parse_change("COMMIT") is None
    assert parse_change("table s.t: TRUNCATE: (no-flags)") == {"table": "s.t", "op": "truncate"}
    assert parse_change("table s.t: DELETE: id[bigint]:7")["row"] == {"id": ("bigint", "7")}
    with pytest.raises(MirrorError, match="no primary key"):
        parse_change("table s.t: DELETE: (no-tuple-data)")
    assert parse_tuple("note[text]:'multi\nline: [x]:y'") == {"note": ("text", "multi\nline: [x]:y")}


pg = pytest.mark.skipif(not PG_URL, reason="set TESSERA_TEST_PG to a PostgreSQL with wal_level=logical")


@pytest.fixture
def pgdb(admin):
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as con:
        con.execute("set client_encoding to 'UTF8'")
        if con.execute("show wal_level").fetchone()[0] not in ("logical", b"logical"):
            pytest.skip("PostgreSQL needs wal_level=logical")
        for (s,) in con.execute("select slot_name from pg_replication_slots where slot_name like 'tessera_mirror_%'").fetchall():
            con.execute("select pg_drop_replication_slot(%s)", (s,))
        con.execute("drop schema if exists mirror_test cascade")
        con.execute("create schema mirror_test")
        con.execute("create table mirror_test.customers(id int primary key, name text, balance numeric(10,2), active boolean, "
                    "joined timestamptz, notes text)")
        con.execute("create table mirror_test.lines(order_id int, line int, sku text, primary key(order_id, line))")
        con.execute("create table mirror_test.nokey(x int)")
        con.execute("insert into mirror_test.customers values (1,'Ann',10.50,true,'2026-01-01T10:00Z',null),(2,'Bo',0,false,'2026-01-02T10:00Z','x')")
        con.execute("insert into mirror_test.lines values (1,1,'A'),(1,2,'B')")
    kv = dict(p.split("=") for p in PG_URL.split())
    c = admin.post("/api/connections", json={"type": "postgres", "name": "pg", "config": {
        "host": kv["host"], "port": kv.get("port", 5432), "database": kv.get("dbname", "postgres"),
        "username": kv.get("user", "postgres"), "password": kv.get("password", "")}}).json()
    con = psycopg.connect(PG_URL, autocommit=True)
    con.execute("set client_encoding to 'UTF8'")
    yield c, con
    for m in admin.get("/api/mirrors").json():
        admin.delete(f"/api/mirrors/{m['id']}")
    con.close()


def rows(admin, q):
    r = admin.post("/api/sql", json={"sql": q})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


@pg
def test_mirror_follows_inserts_updates_deletes_and_truncates(admin, pgdb):
    c, con = pgdb
    r = admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": ["mirror_test.customers", "mirror_test.lines"], "prefix": "crm"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["status"] == "running" and m["targets"] == ["raw.crm_mirror_test_customers", "raw.crm_mirror_test_lines"]
    assert rows(admin, "select id, name from raw.crm_mirror_test_customers order by id") == [[1, "Ann"], [2, "Bo"]]

    big = "z" * 20000  # stored out of line, so later updates that do not touch it leave it out of the change log
    con.execute("insert into mirror_test.customers values (3,'Cy',5,true,'2026-03-01T00:00Z',%s)", (big,))
    con.execute("update mirror_test.customers set name='Ann B', balance=11.25 where id=1")
    con.execute("delete from mirror_test.customers where id=2")
    con.execute("update mirror_test.customers set name='Cy 2' where id=3")
    con.execute("update mirror_test.customers set id=30 where id=3")  # a key change
    con.execute("insert into mirror_test.lines values (2,1,'C')")
    con.execute("update mirror_test.lines set sku='B2' where order_id=1 and line=2")
    s = admin.post(f"/api/mirrors/{m['id']}/sync").json()
    assert "applied" in s, s
    assert s["applied"]["mirror_test.customers"] == 5 and s["applied"]["mirror_test.lines"] == 2
    got = rows(admin, "select id, name, balance, active, length(notes) from raw.crm_mirror_test_customers order by id")
    assert got == [[1, "Ann B", 11.25, True, None], [30, "Cy 2", 5.0, True, 20000]], got
    assert rows(admin, "select order_id, line, sku from raw.crm_mirror_test_lines order by 1, 2") == [[1, 1, "A"], [1, 2, "B2"], [2, 1, "C"]]
    # nothing new: nothing applied, and the slot was advanced so the same changes do not come back
    assert admin.post(f"/api/mirrors/{m['id']}/sync").json()["applied"] == {}
    con.execute("truncate mirror_test.lines")
    con.execute("insert into mirror_test.lines values (9,1,'Z')")
    admin.post(f"/api/mirrors/{m['id']}/sync")
    assert rows(admin, "select order_id, sku from raw.crm_mirror_test_lines") == [[9, "Z"]]
    status = next(x for x in admin.get("/api/mirrors").json() if x["id"] == m["id"])
    assert status["changes"] >= 9 and status["lag_bytes"] is not None and status["connection"] == "pg"

    # the connection cannot be deleted while it is mirrored, and stopping the mirror drops the slot
    r = admin.delete(f"/api/connections/{c['id']}")
    assert r.status_code == 409
    r = admin.delete(f"/api/mirrors/{m['id']}")
    assert r.json()["ok"]
    assert con.execute("select count(*) from pg_replication_slots where slot_name=%s", (mirror.slot_name(m["id"]),)).fetchone()[0] == 0


@pg
def test_mirror_refuses_what_it_cannot_do(admin, pgdb):
    c, con = pgdb
    r = admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": ["mirror_test.nokey"]})
    assert r.status_code == 400 and "primary key" in r.json()["detail"]
    assert con.execute("select count(*) from pg_replication_slots where slot_name like 'tessera_mirror_%'").fetchone()[0] == 0, "no slot left behind"
    assert admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": []}).status_code == 400
    assert admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": ["mirror_test.customers"]}).status_code == 200
    assert admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": ["mirror_test.customers"]}).status_code == 409
    files = admin.post("/api/connections", json={"type": "file", "name": "f", "config": {}}).json()
    assert admin.post(f"/api/connections/{files['id']}/mirror", json={"tables": ["a"]}).status_code == 400


@pg
def test_background_sync(admin, pgdb, app):
    c, con = pgdb
    m = admin.post(f"/api/connections/{c['id']}/mirror", json={"tables": ["mirror_test.lines"]}).json()
    mgr = app.state.mirrors
    mgr.interval = 0.1
    mgr.resume_all()
    con.execute("insert into mirror_test.lines values (5,5,'bg')")
    import time
    for _ in range(100):
        if rows(admin, "select count(*) from raw.mirror_test_lines where sku='bg'") == [[1]]:
            break
        time.sleep(0.1)
    assert rows(admin, "select count(*) from raw.mirror_test_lines where sku='bg'") == [[1]]
    admin.post(f"/api/mirrors/{m['id']}/pause", json={"paused": True})
    assert mgr.get(m["id"])["status"] == "paused"
    mgr.shutdown()
