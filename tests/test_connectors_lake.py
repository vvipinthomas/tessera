"""Iceberg (a real pyiceberg SQL catalog and warehouse on disk) and SFTP (a real SSH/SFTP server from asyncssh)."""
import asyncio
import socket
import threading
import time
from datetime import datetime

import pyarrow as pa
import pytest


def make_conn(admin, ctype, name, config):
    r = admin.post("/api/connections", json={"type": ctype, "name": name, "config": config})
    assert r.status_code == 200, r.text
    return r.json()


def sql(admin, q):
    r = admin.post("/api/sql", json={"sql": q})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


# ------------------------------------------------------------------ Iceberg
@pytest.fixture
def iceberg_lake(settings):
    pytest.importorskip("pyiceberg")
    from pyiceberg.catalog.sql import SqlCatalog
    root = settings.uploads_dir / "lake"
    root.mkdir(parents=True, exist_ok=True)
    cat = SqlCatalog("t", uri=f"sqlite:///{root}/catalog.db", warehouse=f"file://{root}/wh")
    cat.create_namespace("sales")
    schema = pa.schema([("order_id", pa.int64()), ("amount", pa.float64()), ("region", pa.string()), ("updated_at", pa.timestamp("us"))])
    t = cat.create_table("sales.orders", schema=schema)
    t.append(pa.table({"order_id": [1, 2, 3], "amount": [10.0, -2.0, 30.0], "region": ["west", "east", "west"],
                       "updated_at": [datetime(2026, 1, 1), datetime(2026, 1, 2), datetime(2026, 1, 3)]}, schema=schema))
    first = t.current_snapshot().snapshot_id
    t.append(pa.table({"order_id": [4], "amount": [40.0], "region": ["north"], "updated_at": [datetime(2026, 2, 1)]}, schema=schema))
    return {"root": root, "cat": cat, "schema": schema, "first": first}


def test_iceberg_read_time_travel_filter_incremental(admin, iceberg_lake):
    root = iceberg_lake["root"]
    c = make_conn(admin, "iceberg", "lake", {"catalog_type": "sql", "catalog_name": "t", "uri": f"sqlite:///{root}/catalog.db",
                                              "warehouse": f"file://{root}/wh"})
    t = admin.post(f"/api/connections/{c['id']}/test").json()
    assert t["ok"], t
    assert {"name": "sales.orders"} in admin.get(f"/api/connections/{c['id']}/browse").json()
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "sales.orders"}, "target": "raw.ice_orders"})
    assert r.status_code == 200 and r.json()["row_count"] == 4, r.text
    # time travel to the first snapshot
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "sales.orders", "snapshot_id": iceberg_lake["first"]},
                                                              "target": "raw.ice_v1"})
    assert r.json()["row_count"] == 3
    # filter pushdown
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"table": "sales.orders", "row_filter": "amount > 0 and region = 'west'"},
                                                              "target": "raw.ice_west"})
    assert r.json()["row_count"] == 2
    # incremental on updated_at
    src = {"table": "sales.orders", "cursor_column": "updated_at"}
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": src, "target": "raw.ice_inc", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 4
    tbl = iceberg_lake["cat"].load_table("sales.orders")
    tbl.append(pa.table({"order_id": [5], "amount": [5.0], "region": ["west"], "updated_at": [datetime(2026, 3, 1)]}, schema=iceberg_lake["schema"]))
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": src, "target": "raw.ice_inc", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 1 and r["row_count"] == 5


def test_iceberg_guards(admin, settings):
    c = make_conn(admin, "iceberg", "bad", {"catalog_type": "sql", "uri": "sqlite:////etc/passwd.db", "warehouse": "file:///etc"})
    r = admin.post(f"/api/connections/{c['id']}/test").json()
    assert not r["ok"] and "uploads folder" in r["message"]
    c = make_conn(admin, "iceberg", "meta", {"catalog_type": "rest", "uri": "http://169.254.169.254/latest"})
    r = admin.post(f"/api/connections/{c['id']}/test").json()
    assert not r["ok"] and "metadata" in r["message"]


# ------------------------------------------------------------------ SFTP
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def sftp_server(tmp_path):
    asyncssh = pytest.importorskip("asyncssh")
    pytest.importorskip("paramiko")
    root = tmp_path / "sftproot"
    (root / "outbound" / "daily").mkdir(parents=True)
    key = asyncssh.generate_private_key("ssh-ed25519")
    port = free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    class Server(asyncssh.SSHServer):
        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "drop" and password == "s3cr3tpw"

    class Chroot(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(root).encode())

    def run():
        asyncio.set_event_loop(loop)
        holder["srv"] = loop.run_until_complete(asyncssh.create_server(Server, "127.0.0.1", port, server_host_keys=[key],
                                                                       sftp_factory=Chroot))
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(10)
    import base64
    import hashlib
    blob = base64.b64decode(key.export_public_key("openssh").split()[1])
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    yield {"port": port, "root": root, "fp": fp}
    holder["srv"].close()
    loop.call_soon_threadsafe(loop.stop)


def test_sftp_pinned_host_key_and_incremental(admin, sftp_server):
    root = sftp_server["root"] / "outbound" / "daily"
    (root / "2026-10-01.csv").write_text("id,amount\n1,10\n2,20\n")
    (root / "2026-10-02.csv").write_text("id,amount\n3,30\n")
    (sftp_server["root"] / "outbound" / "notes.txt").write_text("ignore me")
    base = {"host": "127.0.0.1", "port": sftp_server["port"], "username": "drop", "password": "s3cr3tpw", "base_path": "/outbound"}
    c = make_conn(admin, "sftp", "partner drop", base)
    t = admin.post(f"/api/connections/{c['id']}/test").json()
    assert t["ok"] and "not pinned" in t["message"] and sftp_server["fp"] in t["message"], t
    names = {x["name"] for x in admin.get(f"/api/connections/{c['id']}/browse").json()}
    assert names == {"daily/2026-10-01.csv", "daily/2026-10-02.csv", "notes.txt"}
    # a wrong pinned key refuses to connect
    bad = make_conn(admin, "sftp", "mitm", {**base, "host_key": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    r = admin.post(f"/api/connections/{bad['id']}/test").json()
    assert not r["ok"] and "mismatch" in r["message"]
    good = make_conn(admin, "sftp", "pinned", {**base, "host_key": sftp_server["fp"]})
    assert admin.post(f"/api/connections/{good['id']}/test").json()["ok"]
    # wrong password
    wrong = make_conn(admin, "sftp", "wrong", {**base, "password": "nope"})
    assert "Sign-in failed" in admin.post(f"/api/connections/{wrong['id']}/test").json()["message"]
    # incremental by file modification time: unchanged files are not downloaded again
    src = {"path": "daily/*.csv", "cursor_column": "_source_modified"}
    r = admin.post(f"/api/connections/{good['id']}/ingest", json={"source": src, "target": "raw.drops", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 3
    r = admin.post(f"/api/connections/{good['id']}/ingest", json={"source": src, "target": "raw.drops", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 0
    time.sleep(1.1)
    (root / "2026-10-03.csv").write_text("id,amount\n4,40\n5,50\n")
    r = admin.post(f"/api/connections/{good['id']}/ingest", json={"source": src, "target": "raw.drops", "mode": "incremental"}).json()
    assert r["rows_loaded"] == 2 and r["row_count"] == 5
    files = sql(admin, "select _source_file, count(*) from raw.drops group by 1 order by 1")
    assert files == [["daily/2026-10-01.csv", 2], ["daily/2026-10-02.csv", 1], ["daily/2026-10-03.csv", 2]]
    # traversal
    r = admin.post(f"/api/connections/{good['id']}/ingest", json={"source": {"path": "../*"}, "target": "raw.x"})
    assert r.status_code == 400
