"""MongoDB (mongomock), Elasticsearch/OpenSearch (local HTTP fake of the REST API) and Delta Lake (real delta-rs tables)."""
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from tessera import connectors


def make_conn(admin, ctype, name, config):
    r = admin.post("/api/connections", json={"type": ctype, "name": name, "config": config})
    assert r.status_code == 200, r.text
    return r.json()


def ingest(admin, cid, source, target, mode="replace"):
    r = admin.post(f"/api/connections/{cid}/ingest", json={"source": source, "target": target, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def sql(admin, q):
    return admin.post("/api/sql", json={"sql": q}).json()


# ---------------------------------------------------------------- MongoDB
@pytest.fixture
def mongo(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    monkeypatch.setattr(connectors.MongoConnector, "client_factory", lambda uri, **kw: client)
    db = client["shop"]
    from bson import ObjectId
    db.orders.insert_many([
        {"_id": ObjectId(), "order_id": 1, "status": "paid", "total": 12.5, "customer": {"name": "Ada", "province": "BC"}, "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"_id": ObjectId(), "order_id": 2, "status": "refunded", "total": 7.0, "customer": {"name": "Bo", "province": "AB"}, "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        {"_id": ObjectId(), "order_id": 3, "status": "paid", "total": 30.0, "customer": {"name": "Cy", "province": "ON"}, "updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc)},
    ])
    return db


def test_mongodb_load_filter_and_incremental(admin, mongo):
    c = make_conn(admin, "mongodb", "mongo", {"uri": "mongodb://user:s3cr3tpw@db.example:27017", "database": "shop"})
    conns = admin.get("/api/connections").json()
    assert all("s3cr3tpw" not in json.dumps(x) for x in conns), "the connection string holds a password and must never be returned"
    t = admin.post(f"/api/connections/{c['id']}/test").json()
    assert t["ok"] and "1 collection" in t["message"]
    assert [o["name"] for o in admin.get(f"/api/connections/{c['id']}/browse").json()] == ["orders"]
    r = ingest(admin, c["id"], {"collection": "orders"}, "raw.m_orders")
    assert r["row_count"] == 3
    cols = sql(admin, "select * from raw.m_orders")["columns"]
    assert {"_id", "order_id", "customer_name", "customer_province", "updated_at"} <= set(cols)
    paid = ingest(admin, c["id"], {"collection": "orders", "filter": {"status": "paid"}}, "raw.m_paid")
    assert paid["row_count"] == 2
    inc = ingest(admin, c["id"], {"collection": "orders", "cursor_column": "updated_at"}, "raw.m_inc", "incremental")
    assert inc["rows_loaded"] == 3
    mongo.orders.insert_one({"order_id": 4, "status": "paid", "total": 1.0, "customer": {"name": "Di"}, "updated_at": datetime(2026, 2, 1, tzinfo=timezone.utc)})
    inc2 = ingest(admin, c["id"], {"collection": "orders", "cursor_column": "updated_at"}, "raw.m_inc", "incremental")
    assert inc2["rows_loaded"] == 1 and inc2["row_count"] == 4
    bad = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"collection": "orders", "filter": {"$where": "sleep(1)"}}, "target": "raw.x"})
    assert bad.status_code == 400 and "$where" in bad.json()["detail"]
    nested = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"collection": "orders", "filter": {"$or": [{"$function": {}}]}}, "target": "raw.x"})
    assert nested.status_code == 400


# ---------------------------------------------------------------- Elasticsearch / OpenSearch
class Es(BaseHTTPRequestHandler):
    docs = [{"_id": str(i), "_index": "logs-1", "_source": {"service": "checkout" if i % 2 else "cart", "ms": i * 10, "@timestamp": f"2026-01-{i + 1:02d}T00:00:00Z",
                                                           "http": {"status": 200 if i % 3 else 500}}} for i in range(25)]
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        Es.seen.append(("GET", self.path, self.headers.get("Authorization")))
        u = urlparse(self.path)
        if u.path == "/":
            return self._send({"version": {"number": "8.15.0", "distribution": "elasticsearch"}})
        if u.path == "/_cat/indices":
            return self._send([{"index": "logs-1", "docs.count": "25"}, {"index": ".security", "docs.count": "3"}])
        self._send({"error": "nope"}, 404)

    def do_POST(self):
        b = self._body()
        Es.seen.append(("POST", self.path, b))
        u = urlparse(self.path)
        if u.path.endswith("/_search") and u.path != "/_search/scroll":
            hits = self.match(b.get("query") or {})
            Es.state = {"hits": hits, "pos": 0, "size": b.get("size", 10)}
            assert parse_qs(u.query).get("scroll") == ["2m"]
            return self.page()
        if u.path == "/_search/scroll":
            return self.page()
        self._send({}, 404)

    def do_DELETE(self):
        Es.seen.append(("DELETE", self.path, self._body()))
        self._send({"succeeded": True})

    def page(self):
        st = Es.state
        chunk = st["hits"][st["pos"]:st["pos"] + st["size"]]
        st["pos"] += st["size"]
        self._send({"_scroll_id": "sc1", "hits": {"hits": chunk}})

    def match(self, q):
        out = list(Es.docs)
        if "match" in q:
            k, v = next(iter(q["match"].items()))
            out = [d for d in out if d["_source"].get(k) == v]
        if "bool" in q:
            for f in q["bool"].get("filter", []):
                col, cond = next(iter(f["range"].items()))
                out = [d for d in out if d["_source"][col] > cond["gt"]]
        return out


@pytest.fixture(scope="module")
def es():
    srv = HTTPServer(("127.0.0.1", 0), Es)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_elasticsearch_scroll_query_and_incremental(admin, es):
    Es.seen.clear()
    c = make_conn(admin, "elasticsearch", "es", {"base_url": es, "auth": "apikey", "token": "key123"})
    assert admin.post(f"/api/connections/{c['id']}/test").json()["message"].startswith("Connected to elasticsearch 8.15.0")
    assert any(a == "ApiKey key123" for _, _, a in Es.seen if isinstance(a, str))
    assert [o["name"] for o in admin.get(f"/api/connections/{c['id']}/browse").json()] == ["logs-1"]
    r = ingest(admin, c["id"], {"index": "logs-*"}, "raw.es_logs")
    assert r["row_count"] == 25
    assert any(m == "DELETE" for m, _, _ in Es.seen), "the scroll context is cleared"
    cols = sql(admin, "select * from raw.es_logs")["columns"]
    assert {"_id", "service", "ms", "http_status"} <= set(cols)
    co = ingest(admin, c["id"], {"index": "logs-1", "query": {"match": {"service": "checkout"}}}, "raw.es_checkout")
    assert co["row_count"] == 12
    i1 = ingest(admin, c["id"], {"index": "logs-1", "cursor_column": "@timestamp"}, "raw.es_inc", "incremental")
    assert i1["rows_loaded"] == 25
    Es.docs.append({"_id": "99", "_index": "logs-1", "_source": {"service": "cart", "ms": 1, "@timestamp": "2026-02-01T00:00:00Z", "http": {"status": 200}}})
    i2 = ingest(admin, c["id"], {"index": "logs-1", "cursor_column": "@timestamp"}, "raw.es_inc", "incremental")
    assert i2["rows_loaded"] == 1 and i2["row_count"] == 26
    bad = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"index": "logs/../_cluster"}, "target": "raw.x"})
    assert bad.status_code == 400


# ---------------------------------------------------------------- Delta Lake
def test_delta_time_travel_and_incremental(admin, settings):
    deltalake = pytest.importorskip("deltalake")
    import pyarrow as pa
    lake = settings.uploads_dir / "lake" / "orders"
    lake.parent.mkdir(parents=True, exist_ok=True)
    deltalake.write_deltalake(str(lake), pa.table({"id": [1, 2], "amount": [10.0, 20.0], "updated_at": pa.array([1, 2], pa.int64())}))
    deltalake.write_deltalake(str(lake), pa.table({"id": [3], "amount": [5.0], "updated_at": pa.array([3], pa.int64())}), mode="append")
    c = make_conn(admin, "delta", "lake", {"base_path": "lake"})
    assert admin.post(f"/api/connections/{c['id']}/test").json()["ok"]
    assert [o["name"] for o in admin.get(f"/api/connections/{c['id']}/browse").json()] == ["orders"]
    assert ingest(admin, c["id"], {"table": "orders"}, "raw.d_orders")["row_count"] == 3
    assert ingest(admin, c["id"], {"table": "orders", "version": 0}, "raw.d_v0")["row_count"] == 2
    i1 = ingest(admin, c["id"], {"table": "orders", "cursor_column": "updated_at"}, "raw.d_inc", "incremental")
    assert i1["rows_loaded"] == 3
    deltalake.write_deltalake(str(lake), pa.table({"id": [4], "amount": [1.0], "updated_at": pa.array([4], pa.int64())}), mode="append")
    i2 = ingest(admin, c["id"], {"table": "orders", "cursor_column": "updated_at"}, "raw.d_inc", "incremental")
    assert i2["rows_loaded"] == 1 and i2["row_count"] == 4
    out = make_conn(admin, "delta", "outside", {"base_path": "/etc"})
    assert not admin.post(f"/api/connections/{out['id']}/test").json()["ok"]
    bad = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "../../etc"}, "target": "raw.x"})
    assert bad.status_code == 400


def test_catalog_lists_new_connectors(admin):
    types = {t["type"]: t for t in admin.get("/api/connectors").json()["types"]}
    assert {"mongodb", "elasticsearch", "delta"} <= set(types)
    assert all(types[k]["status"] == "verified" for k in ("mongodb", "elasticsearch", "delta"))
