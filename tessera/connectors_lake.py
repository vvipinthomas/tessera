"""Apache Iceberg tables and SFTP file drops."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import importlib.util
import io
import json
import posixpath
import stat as statmod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa

from .connectors_base import Connector, ConnectorError, _after, _within, concat, guard_url, read_bytes_or_path

F = lambda key, label, **kw: {"key": key, "label": label, **kw}  # noqa: E731

TYPES = {
    "iceberg": {"label": "Apache Iceberg tables", "category": "Lakehouse", "verified": True, "driver": "pyiceberg", "pip": "pyiceberg",
                "description": "Iceberg tables through a REST catalog (Polaris, Unity Catalog, Nessie, Tabular, Snowflake Open Catalog, "
                               "Lakekeeper), AWS Glue, Hive, or a SQL catalog. Snapshot time travel, filter pushdown, incremental loads.",
                "fields": [F("catalog_type", "Catalog", type="select", options=["rest", "sql", "glue", "hive"]),
                           F("uri", "Catalog URI", placeholder="https://catalog.example.com/api/catalog · postgresql+psycopg://…"),
                           F("warehouse", "Warehouse", placeholder="s3://lake/warehouse · catalog name for REST"),
                           F("catalog_name", "Catalog name", placeholder="Needed for SQL catalogs shared with Spark or PyIceberg"),
                           F("credential", "Token or client_id:secret", secret=True),
                           F("properties", "Extra properties (JSON)", type="textarea", placeholder='{"s3.region": "ca-central-1"}')],
                "source_hint": {"table": "sales.orders", "row_filter": "amount > 0", "snapshot_id": None, "cursor_column": "updated_at"}},
    "sftp": {"label": "SFTP", "category": "Files", "verified": True, "driver": "paramiko", "pip": "paramiko",
             "description": "CSV, JSON, Parquet and Excel drops on an SFTP server. Pin the host key; incremental loads skip files that have not changed.",
             "fields": [F("host", "Host", required=True), F("port", "Port", type="number", placeholder="22"), F("username", "User", required=True),
                        F("password", "Password", secret=True), F("private_key", "Private key (PEM or OpenSSH)", secret=True, type="textarea"),
                        F("host_key", "Host key fingerprint", placeholder="SHA256:… (shown by Test when empty)"),
                        F("base_path", "Folder", placeholder="/outbound")],
             "source_hint": {"path": "daily/*.csv", "format": "auto", "cursor_column": "_source_modified"}},
}


def _props(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
    except ValueError:
        raise ConnectorError("Extra properties must be a JSON object.") from None
    if not isinstance(v, dict):
        raise ConnectorError("Extra properties must be a JSON object.")
    return v


class IcebergConnector(Connector):
    catalog_factory = None  # tests may inject

    def catalog(self):
        if type(self).catalog_factory:
            return type(self).catalog_factory(self.config, self.secrets)
        if importlib.util.find_spec("pyiceberg") is None:
            raise ConnectorError("Driver not installed. Run: pip install pyiceberg")
        from pyiceberg.catalog import load_catalog
        c = self.config
        kind = c.get("catalog_type") or "rest"
        props = {"type": kind, **{k: str(v) for k, v in _props(c.get("properties")).items()}}
        uri, wh = (c.get("uri") or "").strip(), (c.get("warehouse") or "").strip()
        if kind == "rest":
            if not uri:
                raise ConnectorError("A REST catalog needs its URI.")
            guard_url(uri, self.settings.block_private_http)
        if kind == "sql":
            if not uri:
                raise ConnectorError("A SQL catalog needs a database URI.")
            if uri.startswith("sqlite"):
                self._local(uri.split(":///", 1)[-1], "catalog database")
        if wh.startswith("file://") or (wh and "://" not in wh and kind != "rest"):
            self._local(wh.removeprefix("file://"), "warehouse")
        if uri:
            props["uri"] = uri
        if wh:
            props["warehouse"] = wh
        cred = self.secrets.get("credential")
        if cred:
            props["credential" if ":" in cred else "token"] = cred
        try:
            return load_catalog((c.get("catalog_name") or "tessera").strip(), **props)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Cannot open the catalog: {str(e).splitlines()[0][:300]}") from None

    def _local(self, path: str, what: str) -> None:
        p = Path(path)
        if not p.is_absolute():
            p = self.settings.uploads_dir / p
        if not _within(p, self.settings.allowed_import_roots()):
            raise ConnectorError(f"The {what} must be in the uploads folder or a folder in TESSERA_IMPORT_DIRS.")

    def test(self) -> str:
        cat = self.catalog()
        try:
            ns = cat.list_namespaces()
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Catalog reachable but listing failed: {str(e).splitlines()[0][:300]}") from None
        return f"Catalog reachable, {len(ns)} namespace(s)."

    def browse(self) -> list[dict]:
        cat = self.catalog()
        out = []
        for ns in cat.list_namespaces()[:100]:
            for t in cat.list_tables(ns)[:500]:
                out.append({"name": ".".join(t)})
        return out[:1000]

    def read(self, source: dict, watermark=None) -> pa.Table:
        name = (source.get("table") or "").strip()
        if not name or "." not in name:
            raise ConnectorError("Give the table as namespace.table")
        cat = self.catalog()
        try:
            t = cat.load_table(name)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Table {name} not found: {str(e).splitlines()[0][:200]}") from None
        from pyiceberg.expressions import AlwaysTrue, And, GreaterThan
        flt = AlwaysTrue()
        if source.get("row_filter"):
            from pyiceberg.expressions.parser import parse
            try:
                flt = parse(str(source["row_filter"]))
            except Exception as e:  # noqa: BLE001
                raise ConnectorError(f"Row filter not understood: {str(e).splitlines()[0][:200]}") from None
        cur = source.get("cursor_column")
        if cur and watermark is not None:
            wm = watermark.isoformat() if hasattr(watermark, "isoformat") else watermark
            flt = And(flt, GreaterThan(cur, wm)) if not isinstance(flt, AlwaysTrue) else GreaterThan(cur, wm)
        kw = {"row_filter": flt}
        if source.get("columns"):
            kw["selected_fields"] = tuple(source["columns"])
        if source.get("snapshot_id") not in (None, ""):
            kw["snapshot_id"] = int(source["snapshot_id"])
        try:
            tbl = t.scan(**kw).to_arrow()
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).splitlines()[0][:300]) from None
        return _after(tbl, cur, watermark)  # exact filter again: pushdown is a pruning hint for some types

    def snapshots(self, name: str) -> list[dict]:
        t = self.catalog().load_table(name)
        return [{"snapshot_id": s.snapshot_id, "at": datetime.fromtimestamp(s.timestamp_ms / 1000, tz=timezone.utc).isoformat()}
                for s in t.snapshots()]


def fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SftpConnector(Connector):
    def _connect(self):
        if importlib.util.find_spec("paramiko") is None:
            raise ConnectorError("Driver not installed. Run: pip install paramiko")
        import paramiko
        c, s = self.config, self.secrets
        host, port = c.get("host"), int(c.get("port") or 22)
        if not host:
            raise ConnectorError("Host is required.")
        guard_url(f"http://{host}", self.settings.block_private_http)
        try:
            tr = paramiko.Transport((host, port))
            tr.banner_timeout = tr.auth_timeout = 15
            tr.start_client(timeout=15)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Cannot reach {host}:{port}: {str(e).splitlines()[0][:200]}") from None
        fp = fingerprint(tr.get_remote_server_key())
        pinned = (c.get("host_key") or "").strip()
        if pinned and pinned.rstrip("=") != fp:
            tr.close()
            raise ConnectorError(f"Host key mismatch. Expected {pinned}, server sent {fp}. Refusing to connect.")
        try:
            if s.get("private_key"):
                key = None
                for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                    try:
                        key = cls.from_private_key(io.StringIO(s["private_key"]), password=s.get("password") or None)
                        break
                    except Exception:  # noqa: BLE001  # nosec B112 - try the next key type
                        continue
                if key is None:
                    raise ConnectorError("The private key could not be read (PEM or OpenSSH format).")
                tr.auth_publickey(c["username"], key)
            else:
                tr.auth_password(c["username"], s.get("password") or "")
        except ConnectorError:
            tr.close()
            raise
        except Exception as e:  # noqa: BLE001
            tr.close()
            raise ConnectorError(f"Sign-in failed: {str(e).splitlines()[0][:200]}") from None
        return tr, paramiko.SFTPClient.from_transport(tr), fp, bool(pinned)

    def _base(self) -> str:
        b = (self.config.get("base_path") or "/").strip() or "/"
        return posixpath.normpath(b)

    def _walk(self, sftp, pattern: str) -> list[dict]:
        if ".." in pattern.split("/"):
            raise ConnectorError("Paths cannot contain '..'")
        base, out = self._base(), []
        stack = [base]
        while stack and len(out) < 10_000:
            d = stack.pop()
            try:
                entries = sftp.listdir_attr(d)
            except OSError as e:
                raise ConnectorError(f"Cannot list {d}: {e}") from None
            for e in entries:
                full = posixpath.join(d, e.filename)
                rel = posixpath.relpath(full, base)
                if statmod.S_ISDIR(e.st_mode or 0):
                    if rel.count("/") < 6:
                        stack.append(full)
                elif fnmatch.fnmatch(rel, pattern):
                    out.append({"name": rel, "size": e.st_size, "modified": datetime.fromtimestamp(e.st_mtime or 0, tz=timezone.utc)})
        return sorted(out, key=lambda x: x["name"])

    def test(self) -> str:
        tr, sftp, fp, pinned = self._connect()
        try:
            n = len(self._walk(sftp, "*"))
        finally:
            tr.close()
        return f"Signed in, {n} file(s) under {self._base()}." + ("" if pinned else f" Host key not pinned: {fp}. Paste it into Host key to pin it.")

    def browse(self) -> list[dict]:
        tr, sftp, _, _ = self._connect()
        try:
            return [{"name": f["name"], "size": f["size"]} for f in self._walk(sftp, "*")][:500]
        finally:
            tr.close()

    def read(self, source: dict, watermark=None) -> pa.Table:
        cur = source.get("cursor_column")
        tr, sftp, _, _ = self._connect()
        tables, unchanged = [], 0
        try:
            for f in self._walk(sftp, source.get("path") or "*"):
                if cur == "_source_modified" and watermark is not None:
                    wm = watermark if isinstance(watermark, datetime) else datetime.fromisoformat(str(watermark))
                    wm = wm if wm.tzinfo else wm.replace(tzinfo=timezone.utc)
                    if f["modified"] <= wm:
                        unchanged += 1
                        continue  # unchanged since the last load: not even downloaded
                with sftp.open(posixpath.join(self._base(), f["name"]), "rb") as fh:
                    fh.prefetch()
                    body = fh.read()
                t = read_bytes_or_path(body, source.get("format", "auto"), f["name"])
                if source.get("file_columns", True):
                    n = t.num_rows
                    t = t.append_column("_source_file", pa.array([f["name"]] * n, pa.string()))
                    t = t.append_column("_source_modified", pa.array([f["modified"].replace(tzinfo=None)] * n, pa.timestamp("us")))
                tables.append(t)
        finally:
            tr.close()
        if not tables and unchanged:
            return pa.table({})  # nothing new since the last load
        out = concat(tables)
        return out if cur == "_source_modified" else _after(out, cur, watermark)


CLASSES = {"iceberg": IcebergConnector, "sftp": SftpConnector}
_ = urlparse
