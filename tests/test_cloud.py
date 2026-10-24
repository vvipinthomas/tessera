"""Cloud mode: backups to an S3-compatible bucket (moto standing in for Cloudflare R2), restore onto a fresh disk,
final backup on shutdown, after-run backups, proxy-aware client addresses and a preset setup code."""
import base64
import json
import os
import time

import pytest
from conftest import ADMIN, Client, do_setup

from tessera import cloud
from tessera.app import create_app
from tessera.config import Settings

KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture
def bucket(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    m = moto.mock_aws()
    m.start()
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    s3.create_bucket(Bucket="tessera-data")
    monkeypatch.setenv("TESSERA_MASTER_KEY", KEY)
    cfg = cloud.config_from_env({"TESSERA_CLOUD_BUCKET": "tessera-data", "TESSERA_CLOUD_KEEP": "3"})
    yield cloud.CloudStore(cfg, client=s3), s3
    m.stop()


def settings_for(tmp_path, name):
    return Settings.from_env(data_dir=tmp_path / name, retry_base_seconds=0.01, scheduler_interval=3600)


def test_config_from_env():
    assert cloud.config_from_env({}) is None
    c = cloud.config_from_env({"TESSERA_CLOUD_BUCKET": "b", "TESSERA_CLOUD_PREFIX": "acme", "TESSERA_CLOUD_BACKUP_MINUTES": "0.1",
                               "TESSERA_CLOUD_ENDPOINT": "https://abc.r2.cloudflarestorage.com"})
    assert c["prefix"] == "acme/" and c["minutes"] == 1.0 and c["region"] == "auto" and c["endpoint"] == "https://abc.r2.cloudflarestorage.com"


def test_backup_restore_roundtrip_on_a_fresh_disk(tmp_path, bucket):
    store, s3 = bucket
    s1 = settings_for(tmp_path, "first")
    app = create_app(s1, start_scheduler=False)
    with Client(app) as c:
        do_setup(c, s1)
        (s1.uploads_dir / "o.csv").write_text("id,amount\n1,10\n2,20\n")
        conn = c.post("/api/connections", json={"type": "postgres", "name": "pg", "config": {"host": "db", "database": "x", "username": "u", "password": "s3cr3tpw"}}).json()
        files = c.post("/api/connections", json={"type": "file", "name": "files", "config": {}}).json()
        assert c.post(f"/api/connections/{files['id']}/ingest", json={"source": {"path": "o.csv"}, "target": "raw.o"}).status_code == 200
        b = cloud.attach(app, store, 5)
        b.last_fingerprint = None
        assert b.run_once() is not None
        assert b.run_once() is None, "nothing changed, nothing uploaded"
        c.post(f"/api/connections/{files['id']}/ingest", json={"source": {"path": "o.csv"}, "target": "raw.o2"})
        assert b.run_once() is not None, "a change is backed up"
        assert c.get("/api/admin/cloud").json()["last_backup_at"]
    # shutting down wrote a final backup too
    assert len(store.list_backups()) == 3
    ptr = json.loads(s3.get_object(Bucket="tessera-data", Key="tessera/backups/latest.json")["Body"].read())
    assert "master.key" not in ptr["files"], "the key stays in the secret store, not the bucket"

    # a brand-new disk restores everything
    s2 = settings_for(tmp_path, "second")
    m = cloud.restore_if_empty(store, s2.data_dir)
    assert m and m["key"] == ptr["key"]
    app2 = create_app(s2, start_scheduler=False)
    with Client(app2) as c2:
        assert c2.post("/api/auth/login", json=ADMIN).status_code == 200
        rows = c2.post("/api/sql", json={"sql": "select count(*) from raw.o2"}).json()["rows"]
        assert rows == [[2]]
        # stored secrets still decrypt because the master key came from the environment
        assert app2.state.platform.vault.decrypt(app2.state.platform.connection(conn["id"])["secrets"])["password"] == "s3cr3tpw"
    # an existing install is never overwritten
    assert cloud.restore_if_empty(store, s2.data_dir) is None


def test_retention_and_after_run_backup(tmp_path, bucket):
    store, _ = bucket
    s = settings_for(tmp_path, "ret")
    app = create_app(s, start_scheduler=False)
    with Client(app) as c:
        do_setup(c, s)
        b = cloud.attach(app, store, 5)
        for _ in range(5):
            b.run_once(force=True)
        assert len(store.list_backups()) == 3, "keeps TESSERA_CLOUD_KEEP archives"
        before = b.count
        p = c.post("/api/pipelines", json={"name": "p", "steps": [{"id": "w", "type": "transform", "sql": "select 1 as x", "target": "curated.x"}]}).json()
        rid = c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"]
        for _ in range(100):
            if c.get(f"/api/runs/{rid}").json()["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        for _ in range(100):
            if b.count > before:
                break
            time.sleep(0.05)
        assert b.count > before, "a finished pipeline run triggers a backup"
        assert c.post("/api/admin/cloud/backup").json()["ok"]


def test_backup_failure_does_not_break_the_server(tmp_path, bucket):
    store, _ = bucket
    s = settings_for(tmp_path, "fail")
    app = create_app(s, start_scheduler=False)
    with Client(app) as c:
        do_setup(c, s)
        store.bucket = "no-such-bucket"
        b = cloud.attach(app, store, 5)
        assert b.run_once(force=True) is None and b.last_error
        assert c.get("/api/ready").status_code == 200
        r = c.post("/api/admin/cloud/backup")
        assert r.status_code == 502 and "Backup failed" in r.json()["detail"]


def test_cloud_status_when_not_configured(admin):
    assert admin.get("/api/admin/cloud").json() == {"enabled": False}
    assert admin.post("/api/admin/cloud/backup").status_code == 400


def test_client_ip_behind_a_trusted_proxy(monkeypatch):
    from tessera.auth import client_ip

    class Req:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.2"}

        class client:
            host = "10.0.0.2"
    assert client_ip(Req) == "10.0.0.2"
    monkeypatch.setenv("TESSERA_TRUST_PROXY_HEADERS", "true")
    assert client_ip(Req) == "203.0.113.9"


def test_preset_setup_code(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERA_SETUP_CODE", "cloudflare-2026")
    s = settings_for(tmp_path, "preset")
    app = create_app(s, start_scheduler=False)
    with Client(app) as c:
        bad = c.post("/api/setup", json={"setup_code": "wrong-code", "email": "a@b.ca", "name": "A", "password": ADMIN["password"]})
        assert bad.status_code == 403
        ok = c.post("/api/setup", json={"setup_code": "cloudflare-2026", "email": "a@b.ca", "name": "A", "password": ADMIN["password"]})
        assert ok.status_code == 200, ok.text
    monkeypatch.setenv("TESSERA_SETUP_CODE", "short")
    with pytest.raises(SystemExit):
        create_app(settings_for(tmp_path, "short"), start_scheduler=False)
    _ = os


def test_point_in_time_restore_happens_once(tmp_path, bucket):
    store, _ = bucket
    s = settings_for(tmp_path, "pit")
    app = create_app(s, start_scheduler=False)
    with Client(app) as c:
        do_setup(c, s)
        b = cloud.attach(app, store, 5)
        first = b.run_once(force=True)
        c.post("/api/users", json={"email": "later@acme.ca", "role": "viewer", "password": "another strong pass 7", "name": "Later"})
        b.run_once(force=True)
    s2 = settings_for(tmp_path, "pit2")
    m = cloud.restore_if_empty(store, s2.data_dir, override=first)
    assert m["override"] and m["key"] == first
    app2 = create_app(s2, start_scheduler=False)
    with Client(app2) as c2:
        c2.post("/api/auth/login", json=ADMIN)
        assert "later@acme.ca" not in [u["email"] for u in c2.get("/api/users").json()], "restored to the earlier point"
    # a second start with the same setting ignores it and takes the latest
    m3 = cloud.restore_if_empty(store, settings_for(tmp_path, "pit3").data_dir, override=first)
    assert not m3["override"]
