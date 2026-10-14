import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import PG_URL

from tessera import connectors
from tessera.connectors import ConnectorError


def make_conn(admin, ctype, name, config):
    r = admin.post("/api/connections", json={"type": ctype, "name": name, "config": config})
    assert r.status_code == 200, r.text
    return r.json()


def test_file_formats(admin, settings):
    up = settings.uploads_dir
    (up / "a.csv").write_text("id,name,amount\n1,Ann,10.5\n2,\"Bo, Jr\",20\n")
    (up / "b.tsv").write_text("id\tname\n1\tx\n")
    (up / "c.json").write_text(json.dumps({"data": [{"id": 1, "addr": {"city": "Surrey"}, "tags": ["a"]}, {"id": 2, "addr": {"city": "Delta"}}]}))
    (up / "d.ndjson").write_text('{"id":1,"v":"x"}\n{"id":2,"v":"y"}\n')
    pq.write_table(pa.table({"id": [1, 2, 3]}), up / "e.parquet")
    import pandas as pd
    pd.DataFrame({"id": [1, 2], "label": ["p", "q"]}).to_excel(up / "f.xlsx", index=False)
    c = make_conn(admin, "file", "uploads", {})
    assert admin.post(f"/api/connections/{c['id']}/test").json()["ok"]
    names = {x["name"] for x in admin.get(f"/api/connections/{c['id']}/browse").json()}
    assert {"a.csv", "b.tsv", "c.json", "d.ndjson", "e.parquet", "f.xlsx"} <= names
    expect = {"a.csv": 2, "b.tsv": 1, "c.json": 2, "d.ndjson": 2, "e.parquet": 3, "f.xlsx": 2}
    for f, n in expect.items():
        tgt = "raw.t_" + f.replace(".", "_")
        r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"path": f}, "target": tgt})
        assert r.status_code == 200, (f, r.text)
        assert r.json()["row_count"] == n
    cols = admin.post("/api/sql", json={"sql": "select * from raw.t_c_json order by id"}).json()
    assert "addr_city" in cols["columns"] and cols["rows"][0][cols["columns"].index("addr_city")] == "Surrey"
    assert admin.post("/api/sql", json={"sql": "select name from raw.t_a_csv where id=2"}).json()["rows"] == [["Bo, Jr"]]


def test_file_connector_confined_to_allowed_folders(admin, settings):
    c = make_conn(admin, "file", "etc", {"base_path": "/etc"})
    r = admin.post(f"/api/connections/{c['id']}/test").json()
    assert not r["ok"] and "not allowed" in r["message"]
    up = make_conn(admin, "file", "up", {})
    r = admin.post(f"/api/connections/{up['id']}/ingest", json={"source": {"path": "../../etc/passwd"}, "target": "raw.x"})
    assert r.status_code == 400


def test_upload_and_load(admin):
    r = admin.post("/api/uploads", files={"file": ("sales.csv", b"region,total\nwest,10\neast,5\n", "text/csv")}, data={"target": "raw.sales"})
    assert r.status_code == 200, r.text
    assert r.json()["dataset"]["rows"] == 2
    bad = admin.post("/api/uploads", files={"file": ("evil.sh", b"rm -rf /", "text/plain")})
    assert bad.status_code == 400


def test_secrets_encrypted_and_never_returned(admin, app):
    c = make_conn(admin, "rest", "api", {"base_url": "http://127.0.0.1:1", "auth": "bearer", "token": "SUPER-SECRET-TOKEN"})
    assert "SUPER-SECRET-TOKEN" not in json.dumps(c)
    assert c["secret_fields_set"] == ["token"]
    assert "SUPER-SECRET-TOKEN" not in json.dumps(admin.get("/api/connections").json())
    raw = sqlite3.connect(app.state.settings.meta_path).execute("select config, secrets from connections").fetchall()
    assert "SUPER-SECRET-TOKEN" not in str(raw)


def test_sqlite_connector(admin, settings):
    db = settings.uploads_dir / "shop.db"
    con = sqlite3.connect(db)
    con.execute("create table orders(id integer, updated_at text, total real)")
    con.executemany("insert into orders values (?,?,?)", [(1, "2026-01-01", 5.0), (2, "2026-01-02", 7.5)])
    con.commit()
    c = make_conn(admin, "sqlite", "shop", {"path": str(db)})
    assert admin.post(f"/api/connections/{c['id']}/test").json()["ok"]
    assert {"name": "orders"} in admin.get(f"/api/connections/{c['id']}/browse").json()
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "orders", "cursor_column": "updated_at"}, "target": "raw.orders", "mode": "incremental"})
    assert r.json()["rows_loaded"] == 2 and r.json()["watermark"] == "2026-01-02"
    con.execute("insert into orders values (3,'2026-01-03',1.0)")
    con.commit()
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "orders", "cursor_column": "updated_at"}, "target": "raw.orders", "mode": "incremental"})
    assert r.json()["rows_loaded"] == 1 and r.json()["row_count"] == 3
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "orders; drop table orders"}, "target": "raw.bad"})
    assert r.status_code == 400


@pytest.mark.skipif(not PG_URL, reason="set TESSERA_TEST_PG to a libpq connection string to run PostgreSQL tests")
def test_postgres_connector_live(admin):
    import psycopg
    kv = dict(p.split("=") for p in PG_URL.split())
    with psycopg.connect(PG_URL, autocommit=True) as con:
        con.execute("drop schema if exists tessera_test cascade")
        con.execute("create schema tessera_test")
        con.execute("create table tessera_test.invoices(id serial primary key, customer text, amount numeric(10,2), updated_at timestamptz default now())")
        con.execute("insert into tessera_test.invoices(customer, amount, updated_at) values ('a', 10.50, '2026-01-01'), ('b', 20.25, '2026-01-02')")
    c = make_conn(admin, "postgres", "pg", {"host": kv["host"], "port": kv.get("port", 5432), "database": kv.get("dbname", "postgres"),
                                            "username": kv.get("user", "postgres"), "password": kv.get("password", "")})
    t = admin.post(f"/api/connections/{c['id']}/test").json()
    assert t["ok"], t
    assert {"name": "tessera_test.invoices"} in admin.get(f"/api/connections/{c['id']}/browse").json()
    src = {"table": "tessera_test.invoices", "cursor_column": "updated_at"}
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": src, "target": "raw.invoices", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 2
    with psycopg.connect(PG_URL, autocommit=True) as con:
        con.execute("insert into tessera_test.invoices(customer, amount, updated_at) values ('c', 5, '2026-02-01')")
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": src, "target": "raw.invoices", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 1 and r["row_count"] == 3
    total = admin.post("/api/sql", json={"sql": "select sum(amount) from raw.invoices"}).json()["rows"][0][0]
    assert abs(total - 35.75) < 1e-9
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"query": "select customer, amount*2 as dbl from tessera_test.invoices where amount > 6"}, "target": "raw.big"}).json()
    assert r["row_count"] == 2
    bad = make_conn(admin, "postgres", "pg-bad", {"host": kv["host"], "port": kv.get("port", 5432), "database": "nope_db", "username": "postgres"})
    assert not admin.post(f"/api/connections/{bad['id']}/test").json()["ok"]


class Api(BaseHTTPRequestHandler):
    seen_auth = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        Api.seen_auth.append(self.headers.get("Authorization"))
        if u.path == "/v1/paged":
            page = int(q.get("page", ["1"])[0])
            items = [{"id": i, "user": {"name": f"u{i}"}} for i in range((page - 1) * 2, min(page * 2, 5))]
            body = {"data": items}
        elif u.path == "/v1/linked":
            n = int(q.get("n", ["0"])[0])
            body = {"results": [{"id": n}], "next": f"/v1/linked?n={n + 1}" if n < 3 else None}
        elif u.path == "/v1/cursor":
            c = q.get("cursor", [None])[0]
            body = {"items": [{"id": 1}, {"id": 2}], "next_cursor": "abc"} if c is None else {"items": [{"id": 3}], "next_cursor": None}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def api_server():
    srv = HTTPServer(("127.0.0.1", 0), Api)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_rest_connector_pagination(admin, api_server):
    c = make_conn(admin, "rest", "api", {"base_url": api_server, "auth": "bearer", "token": "tok123"})
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"target": "raw.paged", "source": {"path": "/v1/paged", "records_path": "data",
                                                                                                     "pagination": {"type": "page", "page_param": "page"}}})
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 5
    assert "Bearer tok123" in Api.seen_auth
    cols = admin.post("/api/sql", json={"sql": "select * from raw.paged"}).json()["columns"]
    assert "user_name" in cols
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"target": "raw.linked", "source": {"path": "/v1/linked", "records_path": "results",
                                                                                                      "pagination": {"type": "next_url", "next_path": "next"}}})
    assert r.json()["row_count"] == 4
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"target": "raw.cur", "source": {"path": "/v1/cursor", "records_path": "items",
                                                                                                   "pagination": {"type": "cursor", "cursor_param": "cursor", "cursor_path": "next_cursor"}}})
    assert r.json()["row_count"] == 3


def test_rest_blocks_cloud_metadata(settings):
    cx = connectors.make("rest", {"base_url": "http://169.254.169.254/latest"}, {}, settings)
    with pytest.raises(ConnectorError, match="metadata"):
        cx.read({"path": "/meta-data"})


def test_s3_connector_with_moto(admin, settings, monkeypatch):
    moto = pytest.importorskip("moto")
    import io

    import boto3
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="lake")
        for i in range(3):
            buf = io.BytesIO()
            pq.write_table(pa.table({"id": [i * 10 + j for j in range(4)], "part": [i] * 4}), buf)
            s3.put_object(Bucket="lake", Key=f"events/2026/part-{i}.parquet", Body=buf.getvalue())
        s3.put_object(Bucket="lake", Key="events/readme.txt", Body=b"ignore me")
        c = make_conn(admin, "s3", "lake", {"bucket": "lake", "region": "us-east-1", "access_key_id": "testing", "secret_access_key": "testing"})
        assert admin.post(f"/api/connections/{c['id']}/test").json()["ok"]
        r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"path": "events/2026/*.parquet"}, "target": "raw.events"})
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 12


def test_driver_missing_message(admin):
    c = make_conn(admin, "snowflake", "snow", {"account": "x", "database": "d", "username": "u", "password": "p"})
    r = admin.post(f"/api/connections/{c['id']}/test").json()
    assert not r["ok"] and "pip install" in r["message"]


def test_catalog_marks_verified_honestly(admin):
    types = {t["type"]: t for t in admin.get("/api/connectors").json()["types"]}
    assert types["postgres"]["status"] == "verified"
    assert types["snowflake"]["status"] == "driver"
    assert types["rest"]["status"] == "verified"
