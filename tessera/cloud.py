"""Durable state for hosts whose disk does not survive a restart (Cloudflare Containers, Fargate, Cloud Run, Fly
machines without volumes, Kubernetes pods without a persistent volume).

`tessera cloud-run` does three things around the normal server:

1. On start, if the data folder is empty, it restores the newest backup from an S3-compatible bucket
   (Cloudflare R2, Amazon S3, MinIO, ...).
2. While running, it writes a consistent backup to the bucket every TESSERA_CLOUD_BACKUP_MINUTES (default 5) when
   anything changed, and right after pipeline runs finish.
3. On shutdown (SIGTERM), after pipelines and streams stop, it writes a final backup before exiting.

So a restart, a sleep or a move to another machine loses at most the changes made since the last backup.
The master key is not put in the bucket: set TESSERA_MASTER_KEY from your platform's secret store.

Settings:
  TESSERA_CLOUD_BUCKET             bucket name (required to enable)
  TESSERA_CLOUD_ENDPOINT           S3 endpoint, for R2: https://<account id>.r2.cloudflarestorage.com
  TESSERA_CLOUD_ACCESS_KEY_ID      access key (R2 API token's key id)
  TESSERA_CLOUD_SECRET_ACCESS_KEY  secret key
  TESSERA_CLOUD_PREFIX             key prefix, default "tessera/"
  TESSERA_CLOUD_BACKUP_MINUTES     default 5
  TESSERA_CLOUD_KEEP               backups to keep, default 48
  TESSERA_CLOUD_RESTORE_KEY        restore this backup instead of the latest, once (point-in-time recovery)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .errors import UserError

log = logging.getLogger("tessera.cloud")


class CloudStoreError(UserError):
    pass


def config_from_env(env=None) -> dict | None:
    env = env if env is not None else os.environ
    bucket = (env.get("TESSERA_CLOUD_BUCKET") or "").strip()
    if not bucket:
        return None
    prefix = (env.get("TESSERA_CLOUD_PREFIX") or "tessera/").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return {"bucket": bucket, "endpoint": (env.get("TESSERA_CLOUD_ENDPOINT") or "").strip() or None,
            "access_key_id": env.get("TESSERA_CLOUD_ACCESS_KEY_ID") or None,
            "secret_access_key": env.get("TESSERA_CLOUD_SECRET_ACCESS_KEY") or None,
            "region": env.get("TESSERA_CLOUD_REGION") or "auto", "prefix": prefix,
            "minutes": max(1.0, float(env.get("TESSERA_CLOUD_BACKUP_MINUTES") or 5)),
            "keep": max(2, int(env.get("TESSERA_CLOUD_KEEP") or 48))}


class CloudStore:
    """Backups in an S3-compatible bucket. `client` may be injected (tests use moto)."""

    def __init__(self, cfg: dict, client=None):
        self.cfg = cfg
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise CloudStoreError("Install boto3 for cloud backups: pip install 'tessera[s3]'") from None
            kw = {"region_name": cfg["region"], "config": Config(retries={"max_attempts": 5, "mode": "standard"})}
            if cfg["endpoint"]:
                kw["endpoint_url"] = cfg["endpoint"]
            if cfg["access_key_id"]:
                kw["aws_access_key_id"] = cfg["access_key_id"]
                kw["aws_secret_access_key"] = cfg["secret_access_key"]
            client = boto3.client("s3", **kw)
        self.s3 = client
        self.bucket, self.prefix = cfg["bucket"], cfg["prefix"]

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def latest(self) -> dict | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.key("backups/latest.json"))
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception as e:  # noqa: BLE001
            if "NoSuchKey" in str(e) or "404" in str(e):
                return None
            raise CloudStoreError(f"Cannot read the backup index: {e}") from None
        return json.loads(obj["Body"].read())

    def upload(self, archive: Path, manifest: dict) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"backups/tessera-{stamp}.tar.gz"
        self.s3.upload_file(str(archive), self.bucket, self.key(name))  # multipart for large archives
        pointer = {"key": name, "created_at": manifest["created_at"], "version": manifest["version"],
                   "bytes": archive.stat().st_size, "files": manifest["files"]}
        self.s3.put_object(Bucket=self.bucket, Key=self.key("backups/latest.json"), Body=json.dumps(pointer).encode(),
                           ContentType="application/json")
        self.prune()
        return name

    def download_latest(self, dest: Path, override: str | None = None) -> dict | None:
        """The newest backup, or `override` (a key such as backups/tessera-2026...tar.gz) once, for point-in-time restores."""
        if override:
            marker = self.key(f"backups/restored/{override.rsplit('/', 1)[-1]}.done")
            try:
                self.s3.head_object(Bucket=self.bucket, Key=marker)
                log.warning("TESSERA_CLOUD_RESTORE_KEY %s was already restored once; using the latest backup. Remove the setting.", override)
            except Exception:  # noqa: BLE001  (no marker: this is the first start with the override)
                self.s3.download_file(self.bucket, self.key(override), str(dest))
                self.s3.put_object(Bucket=self.bucket, Key=marker, Body=b"restored")
                return {"key": override, "override": True}
        ptr = self.latest()
        if not ptr:
            return None
        self.s3.download_file(self.bucket, self.key(ptr["key"]), str(dest))
        return ptr

    def list_backups(self) -> list[dict]:
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.key("backups/tessera-")}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [{"key": o["Key"], "size": o["Size"]} for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        return sorted(out, key=lambda o: o["key"])

    def prune(self) -> int:
        items = self.list_backups()
        old = items[: max(0, len(items) - self.cfg["keep"])]
        for o in old:
            self.s3.delete_object(Bucket=self.bucket, Key=o["key"])
        return len(old)


def restore_if_empty(store: CloudStore, data_dir: Path, override: str | None = None) -> dict | None:
    """Fresh disk + a backup in the bucket = restore it. An existing install is never overwritten."""
    from .enterprise import restore
    if (data_dir / "tessera.db").exists():
        log.info("Data folder already holds an install; not restoring.")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "latest.tar.gz"
        ptr = store.download_latest(archive, override)
        if not ptr:
            log.info("No backup in the bucket yet; starting a new install.")
            return None
        manifest = restore(archive, data_dir, force=False)
        log.warning("Restored backup %s from %s%s", ptr["key"], manifest["created_at"], " (point-in-time restore)" if ptr.get("override") else "")
        return {**manifest, "key": ptr["key"], "override": bool(ptr.get("override"))}


class Backupper:
    """Backs up when something changed: the warehouse data version, the metadata database, or uploads/exports."""

    def __init__(self, plat, store: CloudStore, minutes: float):
        self.plat, self.store, self.interval = plat, store, minutes * 60
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_fingerprint = None
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self.count = 0
        self.thread: threading.Thread | None = None

    def fingerprint(self):
        s = self.plat.settings
        parts = [getattr(self.plat.wh, "version", 0)]
        for p in (s.meta_path, Path(str(s.meta_path) + "-wal"), s.warehouse_path):
            try:
                st = p.stat()
                parts.append((st.st_size, st.st_mtime_ns))
            except FileNotFoundError:
                parts.append(None)
        for sub in (s.uploads_dir, s.exports_dir):
            try:
                parts.append(max((f.stat().st_mtime_ns for f in sub.rglob("*") if f.is_file()), default=0))
            except FileNotFoundError:
                parts.append(None)
        return tuple(parts)

    def run_once(self, force: bool = False) -> str | None:
        from .enterprise import backup
        with self.lock:
            fp = self.fingerprint()
            if not force and fp == self.last_fingerprint:
                return None
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    archive = Path(tmp) / "backup.tar.gz"
                    manifest = backup(self.plat, archive, include_key=False)
                    name = self.store.upload(archive, manifest)
                self.last_fingerprint = self.fingerprint()  # after the checkpoint, so it is not counted as a change
                self.last_ok, self.last_error = datetime.now(timezone.utc).isoformat(timespec="seconds"), None
                self.count += 1
                log.info("Cloud backup written: %s", name)
                return name
            except Exception as e:  # noqa: BLE001  (a failed upload must never take the server down)
                self.last_error = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
                log.error("Cloud backup failed: %s", self.last_error)
                return None

    def start(self):
        def loop():
            while not self.stop_event.wait(self.interval):
                self.run_once()
        self.thread = threading.Thread(target=loop, name="tessera-cloud-backup", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def status(self) -> dict:
        return {"enabled": True, "bucket": self.store.bucket, "prefix": self.store.prefix, "interval_minutes": self.interval / 60,
                "last_backup_at": self.last_ok, "last_error": self.last_error, "backups_this_run": self.count}


def attach(app, store: CloudStore, minutes: float) -> Backupper:
    """Wire periodic, after-run and shutdown backups into a created app."""
    plat = app.state.platform
    b = Backupper(plat, store, minutes)
    app.state.cloud = b
    b.last_fingerprint = b.fingerprint()  # what was just restored (or empty) is already safe
    reporter = app.state.reporter  # the orchestrator calls reporter.on_run_finished after every run
    previous = reporter.on_run_finished

    def after_run(*args, **kw):
        try:
            return previous(*args, **kw)
        finally:
            threading.Thread(target=b.run_once, name="tessera-cloud-backup-run", daemon=True).start()

    reporter.on_run_finished = after_run

    def final():
        b.stop()
        log.warning("Shutting down: writing the final cloud backup")
        b.run_once(force=True)

    app.state.shutdown_hooks.append(final)
    b.start()
    return b


def run(settings, host: str, port: int) -> None:
    """Entry point for `tessera cloud-run`."""
    import uvicorn

    from .app import create_app
    cfg = config_from_env()
    if not cfg:
        raise SystemExit("Set TESSERA_CLOUD_BUCKET (and the endpoint and keys) for cloud-run, or use `tessera serve`.")
    if not os.environ.get("TESSERA_MASTER_KEY") and not (settings.data_dir / "master.key").exists():
        raise SystemExit("Set TESSERA_MASTER_KEY from your secret store: cloud backups do not contain the key, so without "
                         "it a restored install could not read its saved connection passwords.")
    store = CloudStore(cfg)
    restored = None
    for attempt in range(1, 6):
        try:
            restored = restore_if_empty(store, settings.data_dir, (os.environ.get("TESSERA_CLOUD_RESTORE_KEY") or "").strip() or None)
            break
        except CloudStoreError as e:
            if attempt == 5:
                raise SystemExit(f"Cannot reach the backup bucket: {e}") from None
            log.warning("Backup bucket not reachable yet (%s); retrying", e)
            time.sleep(3 * attempt)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    b = attach(app, store, cfg["minutes"])
    if restored and restored.get("override"):
        b.run_once(force=True)  # the restored point in time becomes the latest backup straight away
    log.warning("Tessera cloud mode: backups to %s/%s every %g min, on pipeline completion and on shutdown",
                cfg["bucket"], cfg["prefix"], cfg["minutes"])
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*",
                timeout_graceful_shutdown=60)
