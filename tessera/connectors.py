"""Source connectors. Each connector reads from a source and returns an Arrow table.

Connector status shown in the product is honest:
  verified  - exercised by Tessera's automated test suite against a real or protocol-faithful service
  driver    - implemented on a standard driver; works when that driver is installed, not in the test suite
"""
from __future__ import annotations

import fnmatch
import importlib.util
import io
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq

from .warehouse import QueryError, qi


class ConnectorError(Exception):
    pass


F = lambda key, label, **kw: {"key": key, "label": label, **kw}  # noqa: E731
HOSTPORT = [F("host", "Host", required=True), F("port", "Port", type="number"), F("database", "Database", required=True),
            F("username", "User", required=True), F("password", "Password", secret=True)]

CONNECTOR_TYPES: dict[str, dict] = {
    "file": {"label": "Files", "category": "Files", "verified": True,
             "description": "CSV, TSV, JSON, NDJSON, Parquet and Excel from the uploads folder or an allowed server folder.",
             "fields": [F("base_path", "Server folder", placeholder="Empty = Tessera uploads folder")],
             "source_hint": {"path": "orders*.csv", "format": "auto"}},
    "postgres": {"label": "PostgreSQL", "category": "Databases", "verified": True, "driver": "psycopg", "pip": "psycopg[binary]",
                 "dialect": "postgresql+psycopg", "default_port": 5432, "fields": HOSTPORT + [F("sslmode", "SSL mode", placeholder="prefer")]},
    "redshift": {"label": "Amazon Redshift", "category": "Warehouses", "driver": "psycopg", "pip": "psycopg[binary]",
                 "dialect": "postgresql+psycopg", "default_port": 5439, "fields": HOSTPORT + [F("sslmode", "SSL mode", placeholder="require")]},
    "cockroachdb": {"label": "CockroachDB", "category": "Databases", "driver": "psycopg", "pip": "psycopg[binary]",
                    "dialect": "postgresql+psycopg", "default_port": 26257, "fields": HOSTPORT + [F("sslmode", "SSL mode", placeholder="require")]},
    "mysql": {"label": "MySQL / MariaDB", "category": "Databases", "driver": "pymysql", "pip": "pymysql",
              "dialect": "mysql+pymysql", "default_port": 3306, "fields": HOSTPORT},
    "sqlserver": {"label": "SQL Server, Azure SQL, Synapse, Fabric Warehouse", "category": "Warehouses", "driver": "pyodbc",
                  "pip": "pyodbc (plus Microsoft ODBC Driver 18)", "dialect": "mssql+pyodbc", "default_port": 1433,
                  "fields": HOSTPORT + [F("odbc_driver", "ODBC driver", placeholder="ODBC Driver 18 for SQL Server")]},
    "oracle": {"label": "Oracle", "category": "Databases", "driver": "oracledb", "pip": "oracledb",
               "dialect": "oracle+oracledb", "default_port": 1521, "fields": HOSTPORT},
    "snowflake": {"label": "Snowflake", "category": "Warehouses", "driver": "snowflake.sqlalchemy", "pip": "snowflake-sqlalchemy",
                  "fields": [F("account", "Account", required=True), F("database", "Database", required=True), F("schema", "Schema"),
                             F("warehouse", "Warehouse"), F("role", "Role"), F("username", "User", required=True),
                             F("password", "Password", secret=True)]},
    "bigquery": {"label": "Google BigQuery", "category": "Warehouses", "driver": "sqlalchemy_bigquery", "pip": "sqlalchemy-bigquery",
                 "fields": [F("project", "Project", required=True), F("dataset", "Dataset"),
                            F("credentials_json", "Service account JSON", secret=True, type="textarea")]},
    "databricks": {"label": "Databricks SQL", "category": "Warehouses", "driver": "databricks.sqlalchemy", "pip": "databricks-sqlalchemy",
                   "fields": [F("host", "Server hostname", required=True), F("http_path", "HTTP path", required=True),
                              F("catalog", "Catalog"), F("schema", "Schema"), F("token", "Access token", secret=True)]},
    "trino": {"label": "Trino / Starburst", "category": "Warehouses", "driver": "trino", "pip": "trino[sqlalchemy]",
              "fields": [F("host", "Host", required=True), F("port", "Port", type="number"), F("catalog", "Catalog", required=True),
                         F("schema", "Schema"), F("username", "User", required=True), F("password", "Password", secret=True)]},
    "clickhouse": {"label": "ClickHouse", "category": "Warehouses", "driver": "clickhouse_sqlalchemy", "pip": "clickhouse-sqlalchemy",
                   "dialect": "clickhouse+http", "default_port": 8123, "fields": HOSTPORT},
    "sqlite": {"label": "SQLite", "category": "Databases", "verified": True,
               "fields": [F("path", "Database file", required=True, placeholder="Inside an allowed folder")]},
    "sqlalchemy": {"label": "Any SQLAlchemy database", "category": "Databases",
                   "description": "Db2, SAP HANA, Teradata, Vertica, Firebird and others through their SQLAlchemy dialect.",
                   "fields": [F("url", "SQLAlchemy URL", secret=True, required=True, placeholder="dialect+driver://user:pass@host/db")]},
    "rest": {"label": "REST / JSON API", "category": "APIs", "verified": True,
             "description": "Any JSON API with page, offset, cursor or next-link pagination. Covers Salesforce, ServiceNow, Jira, Stripe, HubSpot, Zendesk, Shopify and most SaaS APIs.",
             "fields": [F("base_url", "Base URL", required=True, placeholder="https://api.example.com/v1"),
                        F("auth", "Auth", type="select", options=["none", "bearer", "header", "basic", "query",
                                                                  "oauth2_client_credentials", "oauth2_refresh_token"]),
                        F("header_name", "Header or query name", placeholder="X-API-Key"),
                        F("username", "Basic auth user"), F("token", "Token, key or password", secret=True),
                        F("token_url", "OAuth token URL", placeholder="https://login.example.com/oauth2/token"),
                        F("client_id", "OAuth client ID"), F("client_secret", "OAuth client secret", secret=True),
                        F("scope", "OAuth scope", placeholder="https://graph.microsoft.com/.default"),
                        F("refresh_token", "OAuth refresh token", secret=True),
                        F("headers", "Extra headers (JSON)", type="textarea")],
             "source_hint": {"path": "/orders", "records_path": "data", "pagination": {"type": "page", "page_param": "page", "max_pages": 50}}},
    "s3": {"label": "Amazon S3 / MinIO / S3-compatible", "category": "Object storage", "verified": True, "driver": "boto3", "pip": "boto3",
           "fields": [F("bucket", "Bucket", required=True), F("region", "Region", placeholder="ca-central-1"),
                      F("endpoint_url", "Endpoint URL", placeholder="Only for MinIO or other S3-compatible stores"),
                      F("access_key_id", "Access key ID"), F("secret_access_key", "Secret access key", secret=True)],
           "source_hint": {"path": "exports/2026/*.parquet", "format": "auto"}},
    "mongodb": {"label": "MongoDB / Atlas / Cosmos DB (Mongo API)", "category": "Databases", "verified": True, "driver": "pymongo", "pip": "pymongo",
                "description": "Collections as tables; nested documents are flattened into columns. Optional JSON filter and incremental loads on any field.",
                "fields": [F("uri", "Connection string", required=True, secret=True, placeholder="mongodb+srv://user:pass@cluster.example.net"),
                           F("database", "Database", required=True)],
                "source_hint": {"collection": "orders", "filter": {"status": "paid"}, "cursor_column": "updated_at"}},
    "elasticsearch": {"label": "Elasticsearch / OpenSearch", "category": "Search", "verified": True,
                      "description": "Indices as tables through the REST API with scrolling, an optional query, and incremental loads on a date or number field.",
                      "fields": [F("base_url", "URL", required=True, placeholder="https://search.example.com:9200"),
                                 F("auth", "Auth", type="select", options=["none", "basic", "apikey", "bearer"]),
                                 F("username", "User (basic auth)"), F("token", "Password, API key or token", secret=True)],
                      "source_hint": {"index": "logs-*", "query": {"match": {"service": "checkout"}}, "cursor_column": "@timestamp"}},
    "delta": {"label": "Delta Lake tables", "category": "Lakehouse", "verified": True, "driver": "deltalake", "pip": "deltalake",
              "description": "Delta tables in an allowed folder or on S3, Azure Data Lake or GCS, including time travel to a version.",
              "fields": [F("base_path", "Folder or URI", placeholder="Empty = uploads folder · s3://bucket/lake · abfss://…"),
                         F("storage_options", "Storage options (JSON)", type="textarea", placeholder='{"AWS_REGION": "ca-central-1"}'),
                         F("storage_secret", "Storage key or secret (added as AWS_SECRET_ACCESS_KEY / AZURE_STORAGE_ACCOUNT_KEY)", secret=True)],
              "source_hint": {"table": "sales/orders", "version": None, "cursor_column": "updated_at"}},
}

# Destinations reachable through generic connectors. Shown in the catalog so people know the route.
VIA_GENERIC = [
    ("Workday (RaaS)", "rest"), ("GitLab", "rest"),
    ("Google Cloud Storage (S3 interoperability)", "s3"), ("Cloudflare R2", "s3"), ("Wasabi", "s3"),
    ("Cloudflare R2 Data Catalog (Iceberg)", "iceberg"), ("Delta tables on Cloudflare R2", "delta"),
    ("Microsoft Fabric Warehouse SQL endpoint", "sqlserver"), ("Azure Synapse", "sqlserver"),
    ("IBM Db2", "sqlalchemy"), ("SAP HANA", "sqlalchemy"), ("Teradata", "sqlalchemy"), ("Vertica", "sqlalchemy"),
]
ROADMAP = ["Mirroring for SQL Server, MySQL and MongoDB", "Google Pub/Sub", "Apache Pulsar", "Avro and Protobuf payloads with a schema registry"]


def catalog() -> list[dict]:
    out = []
    for key, t in CONNECTOR_TYPES.items():
        drv = t.get("driver")
        installed = True if not drv else importlib.util.find_spec(drv.split(".")[0]) is not None
        if installed and drv and "." in drv:
            try:
                installed = importlib.util.find_spec(drv) is not None
            except ModuleNotFoundError:
                installed = False
        out.append({"type": key, **{k: v for k, v in t.items() if k not in ("dialect",)},
                    "status": "verified" if t.get("verified") else "driver",
                    "driver_installed": installed})
    return out


# ---------------------------------------------------------------- helpers
def _within(path: Path, roots: list[Path]) -> bool:
    p = path.resolve()
    return any(p == r or r in p.parents for r in roots)


def normalize_records(records: list[dict]) -> pa.Table:
    """Flatten nested JSON records into an Arrow table with stable column types."""
    flat: list[dict] = []
    for rec in records:
        out: dict[str, Any] = {}

        def walk(x, prefix=""):
            for k, v in x.items():
                key = f"{prefix}_{k}" if prefix else str(k)
                if isinstance(v, dict):
                    walk(v, key)
                elif isinstance(v, list):
                    out[key] = json.dumps(v)  # noqa: B023 (called within the iteration)
                else:
                    out[key] = v  # noqa: B023
        walk(rec if isinstance(rec, dict) else {"value": rec})
        flat.append(out)
    cols: dict[str, list] = {}
    for i, r in enumerate(flat):
        for k in r:
            if k not in cols:
                cols[k] = [None] * i
        for k in cols:
            cols[k].append(r.get(k))
    arrays = {}
    for k, vals in cols.items():
        kinds = {type(v) for v in vals if v is not None}
        if len(kinds) > 1 and not kinds <= {int, float}:
            vals = [None if v is None else str(v) for v in vals]
        elif kinds == {int, float}:
            vals = [None if v is None else float(v) for v in vals]
        arrays[k] = vals
    return pa.table(arrays) if arrays else pa.table({})


def read_bytes_or_path(src: str | bytes, fmt: str, name: str) -> pa.Table:
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else "csv"
        fmt = {"txt": "csv", "jsonl": "ndjson", "xls": "xlsx", "pq": "parquet"}.get(ext, ext)
    handle = pa.BufferReader(src) if isinstance(src, bytes) else src
    try:
        if fmt in ("csv", "tsv"):
            if isinstance(src, bytes):
                head = src[:4096].decode("utf-8", "ignore")
            else:
                with open(src, "r", encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(4096)
            first = head.splitlines()[0] if head else ""
            delim = "\t" if fmt == "tsv" or first.count("\t") > first.count(",") else (";" if first.count(";") > first.count(",") else ",")
            return pacsv.read_csv(handle, parse_options=pacsv.ParseOptions(delimiter=delim, newlines_in_values=True),
                                  convert_options=pacsv.ConvertOptions(strings_can_be_null=True))
        if fmt == "parquet":
            return pq.read_table(handle)
        if fmt == "ndjson":
            return pajson.read_json(handle)
        if fmt == "json":
            data = json.loads(src if isinstance(src, bytes) else Path(src).read_bytes())
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [data])
            return normalize_records(data)
        if fmt in ("xlsx", "excel"):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(src) if isinstance(src, bytes) else src)
            for c in df.columns:
                if df[c].dtype == object:
                    df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
            df.columns = [str(c) for c in df.columns]
            return pa.Table.from_pandas(df, preserve_index=False)
    except (pa.ArrowInvalid, ValueError, json.JSONDecodeError) as e:
        raise ConnectorError(f"Could not parse {name} as {fmt}: {e}") from None
    raise ConnectorError(f"Unsupported format '{fmt}'")


def concat(tables: list[pa.Table]) -> pa.Table:
    if not tables:
        raise ConnectorError("No files matched the source pattern.")
    if len(tables) == 1:
        return tables[0]
    return pa.concat_tables(tables, promote_options="permissive")


def guard_url(url: str, block_private: bool) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ConnectorError("Only http and https URLs are allowed.")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror:
        raise ConnectorError(f"Cannot resolve host {u.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or str(ip) in ("169.254.169.254", "fd00:ec2::254"):
            raise ConnectorError("Requests to link-local and cloud metadata addresses are blocked.")
        if block_private and (ip.is_private or ip.is_loopback):
            raise ConnectorError("Requests to private network addresses are blocked by TESSERA_BLOCK_PRIVATE_HTTP.")


def dig(obj: Any, path: str | None):
    if not path:
        return obj
    if isinstance(obj, dict) and path in obj:  # keys that contain dots, such as "@odata.nextLink"
        return obj[path]
    for part in path.split("."):
        if obj is None:
            return None
        obj = obj[int(part)] if isinstance(obj, list) else obj.get(part)
    return obj


# ---------------------------------------------------------------- connectors
class Connector:
    def __init__(self, ctype: str, config: dict, secrets: dict, settings):
        self.type, self.config, self.secrets, self.settings = ctype, config or {}, secrets or {}, settings

    def test(self) -> str: raise NotImplementedError
    def browse(self) -> list[dict]: return []
    def read(self, source: dict, watermark: Any = None) -> pa.Table: raise NotImplementedError


class FileConnector(Connector):
    def root(self) -> Path:
        roots = self.settings.allowed_import_roots()
        bp = self.config.get("base_path")
        if bp and not Path(bp).is_absolute():
            root = (self.settings.uploads_dir / bp).resolve()
        else:
            root = Path(bp).resolve() if bp else self.settings.uploads_dir.resolve()
        if not _within(root, roots):
            raise ConnectorError("That folder is not allowed. Add it to TESSERA_IMPORT_DIRS or use the uploads folder.")
        if not root.is_dir():
            raise ConnectorError(f"Folder not found: {root}")
        return root

    def test(self) -> str:
        n = len(self.browse())
        return f"Folder reachable, {n} data file(s) found."

    def browse(self) -> list[dict]:
        root = self.root()
        exts = {".csv", ".tsv", ".txt", ".json", ".ndjson", ".jsonl", ".parquet", ".xlsx"}
        return [{"name": str(p.relative_to(root)), "size": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in exts][:500]

    def read(self, source: dict, watermark=None) -> pa.Table:
        root = self.root()
        pattern = source.get("path") or "*"
        if ".." in Path(pattern).parts:
            raise ConnectorError("Paths cannot contain '..'")
        files = [p for p in sorted(root.glob(pattern)) if p.is_file() and _within(p, [root])]
        return concat([read_bytes_or_path(str(p), source.get("format", "auto"), p.name) for p in files])


class SQLConnector(Connector):
    def url(self):
        from sqlalchemy.engine import URL, make_url
        t, c, s = CONNECTOR_TYPES[self.type], self.config, self.secrets
        if self.type == "sqlalchemy":
            return make_url(s.get("url") or c.get("url"))
        if self.type == "sqlite":
            raw_path = Path(c.get("path", ""))
            p = (raw_path if raw_path.is_absolute() else self.settings.uploads_dir / raw_path).resolve()
            if not _within(p, self.settings.allowed_import_roots()):
                raise ConnectorError("SQLite file must be inside the uploads folder or TESSERA_IMPORT_DIRS.")
            if not p.exists():
                raise ConnectorError(f"File not found: {p}")
            return URL.create("sqlite", database=str(p))
        if self.type == "snowflake":
            q = {k: c[k] for k in ("warehouse", "role") if c.get(k)}
            db = c["database"] + (f"/{c['schema']}" if c.get("schema") else "")
            return URL.create("snowflake", username=c.get("username"), password=s.get("password"), host=c["account"], database=db, query=q)
        if self.type == "bigquery":
            return URL.create("bigquery", host=c["project"], database=c.get("dataset"))
        if self.type == "databricks":
            q = {"http_path": c["http_path"], **{k: c[k] for k in ("catalog", "schema") if c.get(k)}}
            return URL.create("databricks", username="token", password=s.get("token"), host=c["host"], query=q)
        if self.type == "trino":
            db = c["catalog"] + (f"/{c['schema']}" if c.get("schema") else "")
            return URL.create("trino", username=c.get("username"), password=s.get("password") or None, host=c["host"],
                              port=int(c.get("port") or 8080), database=db)
        q = {}
        if c.get("sslmode"):
            q["sslmode"] = c["sslmode"]
        if self.type == "sqlserver":
            q = {"driver": c.get("odbc_driver") or "ODBC Driver 18 for SQL Server", "Encrypt": "yes"}
        return URL.create(t["dialect"], username=c.get("username"), password=s.get("password") or None,
                          host=c.get("host"), port=int(c.get("port") or t.get("default_port") or 0) or None,
                          database=c.get("database"), query=q)

    def engine(self):
        from sqlalchemy import create_engine
        drv = CONNECTOR_TYPES[self.type].get("driver")
        if drv and importlib.util.find_spec(drv.split(".")[0]) is None:
            raise ConnectorError(f"Driver not installed. Run: pip install {CONNECTOR_TYPES[self.type]['pip']}")
        kw = {}
        if CONNECTOR_TYPES[self.type].get("dialect", "").startswith("postgresql"):
            kw["connect_args"] = {"client_encoding": "utf8"}
        if self.type == "bigquery" and self.secrets.get("credentials_json"):
            kw["credentials_info"] = json.loads(self.secrets["credentials_json"])
        try:
            return create_engine(self.url(), pool_pre_ping=True, **kw)
        except Exception as e:  # noqa: BLE001 - driver specific errors
            raise ConnectorError(f"Could not create engine: {e}") from None

    def test(self) -> str:
        from sqlalchemy import text
        eng = self.engine()
        try:
            with eng.connect() as c:
                c.execute(text("SELECT 1")).fetchall()
                name = eng.dialect.name
            return f"Connected to {name}."
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).split("\n")[0]) from None
        finally:
            eng.dispose()

    def browse(self) -> list[dict]:
        from sqlalchemy import inspect
        eng = self.engine()
        try:
            insp = inspect(eng)
            out = []
            schemas = [None] if self.type in ("sqlite", "mysql") else [
                s for s in insp.get_schema_names() if s not in ("information_schema", "pg_catalog", "pg_toast", "sys", "INFORMATION_SCHEMA")]
            for sc in schemas[:20]:
                for t in insp.get_table_names(schema=sc)[:300]:
                    out.append({"name": f"{sc}.{t}" if sc else t})
            return out
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).split("\n")[0]) from None
        finally:
            eng.dispose()

    def describe(self, names: list[str]) -> dict[str, dict]:
        """Columns and primary key of each table, to plan bulk copies. Unknown tables are skipped."""
        from sqlalchemy import inspect
        eng = self.engine()
        out = {}
        try:
            insp = inspect(eng)
            for n in names:
                sc, _, name = n.rpartition(".")
                try:
                    cols = [{"name": c["name"], "type": str(c["type"]).lower()} for c in insp.get_columns(name, schema=sc or None)]
                    pk = (insp.get_pk_constraint(name, schema=sc or None) or {}).get("constrained_columns") or []
                except Exception:  # noqa: BLE001  # nosec B112 - a missing or unreadable table is reported by the copy itself
                    continue
                out[n] = {"columns": cols, "primary_key": pk}
            return out
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).split("\n")[0]) from None
        finally:
            eng.dispose()

    def read(self, source: dict, watermark=None) -> pa.Table:
        import pandas as pd
        from sqlalchemy import bindparam, column, select, table, text
        eng = self.engine()
        cursor_col = source.get("cursor_column")
        try:
            if source.get("table"):
                parts = source["table"].split(".")
                if len(parts) > 2 or not all(p.strip() and re.fullmatch(r"[\w$ #@-]{1,128}", p) for p in parts):
                    raise ConnectorError("Table must be 'table' or 'schema.table'.")
                tbl = table(parts[-1], schema=parts[0] if len(parts) == 2 else None)
                stmt = select(text("*")).select_from(tbl)
                if cursor_col:
                    col = column(cursor_col)
                    if watermark is not None:
                        stmt = stmt.where(col > bindparam("wm", watermark))
                    stmt = stmt.order_by(col)
            elif source.get("query"):
                q = source["query"].strip().rstrip(";")
                if cursor_col and watermark is not None:
                    quoted = eng.dialect.identifier_preparer.quote(cursor_col)
                    stmt = text(f"SELECT * FROM ({q}) tessera_src WHERE {quoted} > :wm ORDER BY {quoted}").bindparams(wm=watermark)
                else:
                    stmt = text(q)
            else:
                raise ConnectorError("Source needs a 'table' or a 'query'.")
            frames = []
            with eng.connect() as c:
                for df in pd.read_sql(stmt, c, chunksize=50_000):
                    frames.append(pa.Table.from_pandas(df, preserve_index=False))
            if not frames:
                return pa.table({})
            return concat(frames)
        except ConnectorError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = " ".join(str(getattr(e, "orig", None) or e.__cause__ or e).split())
            raise ConnectorError(f"Source query failed: {msg[:400]}") from None
        finally:
            eng.dispose()


def format_since(watermark, fmt: str = "iso", lookback_seconds: int = 0) -> str:
    """Turn the last loaded change time into the literal an API's filter expects, a little earlier (lookback) so
    records changed in the same second or reported in another time zone are not missed; merges remove repeats."""
    from datetime import date, datetime, timedelta, timezone
    w = watermark
    if isinstance(w, str):
        try:
            w = datetime.fromisoformat(w.replace("Z", "+00:00"))
        except ValueError:
            return w
    elif isinstance(w, (int, float)) and not isinstance(w, bool):
        w = datetime.fromtimestamp(w / 1000 if w > 1e11 else w, timezone.utc)
    if isinstance(w, date) and not isinstance(w, datetime):
        w = datetime(w.year, w.month, w.day, tzinfo=timezone.utc)
    if not isinstance(w, datetime):
        return str(watermark)
    w = (w if w.tzinfo else w.replace(tzinfo=timezone.utc)).astimezone(timezone.utc) - timedelta(seconds=lookback_seconds)
    if fmt == "jira":
        return w.strftime("%Y/%m/%d %H:%M")
    if fmt == "servicenow":
        return w.strftime("'%Y-%m-%d','%H:%M:%S'")
    return w.strftime("%Y-%m-%dT%H:%M:%SZ")


class OAuthError(ConnectorError):
    pass


class RestConnector(Connector):
    """JSON APIs. Auth: none, bearer, header, query, basic, OAuth 2 client credentials or refresh token.
    Pagination: page, offset, cursor (from a response field or the last record), next_url, link_header."""

    _token: str | None = None
    _token_exp: float = 0.0

    def oauth_token(self, force: bool = False) -> str:
        import time

        import httpx
        if self._token and not force and time.time() < self._token_exp - 60:
            return self._token
        c, s = self.config, self.secrets
        url = (c.get("token_url") or "").strip()
        if not url:
            raise OAuthError("Set the OAuth token URL.")
        guard_url(url, self.settings.block_private_http)
        if c.get("auth") == "oauth2_refresh_token":
            data = {"grant_type": "refresh_token", "refresh_token": s.get("refresh_token", "")}
        else:
            data = {"grant_type": "client_credentials"}
        if c.get("scope"):
            data["scope"] = c["scope"]
        data["client_id"] = c.get("client_id", "")
        data["client_secret"] = s.get("client_secret", "")
        factory = type(self).http_factory or (lambda: httpx.Client(timeout=30))
        with factory() as cl:
            r = cl.post(url, data=data, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            try:
                err = r.json()
                msg = err.get("error_description") or err.get("error") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            raise OAuthError(f"Sign-in to the API failed (HTTP {r.status_code}): {msg}")
        body = r.json()
        tok = body.get("access_token")
        if not tok:
            raise OAuthError("The token endpoint did not return an access_token.")
        self._token, self._token_exp = tok, time.time() + float(body.get("expires_in") or 3600)
        return tok

    http_factory = None  # tests inject a transport

    def client(self):
        import httpx
        c, s = self.config, self.secrets
        headers = {"Accept": "application/json", "User-Agent": "Tessera/1.0"}
        if c.get("headers"):
            try:
                headers.update(json.loads(c["headers"]) if isinstance(c["headers"], str) else c["headers"])
            except ValueError:
                raise ConnectorError("Extra headers must be a JSON object.") from None
        params, auth = {}, None
        mode = c.get("auth", "none")
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {s.get('token', '')}"
        elif mode == "header":
            headers[c.get("header_name") or "X-API-Key"] = s.get("token", "")
        elif mode == "query":
            params[c.get("header_name") or "api_key"] = s.get("token", "")
        elif mode == "basic":
            auth = (c.get("username", ""), s.get("token", ""))
        elif mode in ("oauth2_client_credentials", "oauth2_refresh_token"):
            headers["Authorization"] = f"Bearer {self.oauth_token()}"
        if type(self).http_factory:
            return type(self).http_factory(headers=headers, params=params, auth=auth)
        return httpx.Client(headers=headers, params=params, auth=auth, timeout=30, follow_redirects=False)

    def _get(self, cl, url, params=None):
        guard_url(url, self.settings.block_private_http)
        r = cl.get(url, params=params)
        if r.status_code == 401 and self.config.get("auth", "").startswith("oauth2"):
            cl.headers["Authorization"] = f"Bearer {self.oauth_token(force=True)}"  # expired early or revoked: sign in again once
            r = cl.get(url, params=params)
        if r.status_code >= 400:
            raise ConnectorError(f"HTTP {r.status_code} from {urlparse(url).path}: {r.text[:200]}")
        try:
            return r, r.json()
        except ValueError:
            raise ConnectorError("Response is not JSON.") from None

    def test(self) -> str:
        from . import saas
        tpl = saas.TEMPLATES.get(self.config.get("template") or "")
        with self.client() as cl:
            path = self.config.get("health_path") or (tpl or {}).get("health_path") or ""
            url = self.config["base_url"].rstrip("/") + "/" + path.lstrip("/")
            guard_url(url, self.settings.block_private_http)
            r = cl.get(url)
            if r.status_code in (401, 403):
                raise ConnectorError(f"The API refused the credentials (HTTP {r.status_code}).")
            signed = " Signed in with OAuth." if self.config.get("auth", "").startswith("oauth2") else ""
            return f"Reached {tpl['label'] if tpl else 'the API'}, HTTP {r.status_code}.{signed}"

    def browse(self) -> list[dict]:
        from . import saas
        tpl = saas.TEMPLATES.get(self.config.get("template") or "")
        if not tpl:
            return []
        return [{"name": o["name"], "label": o.get("label", o["name"]), "kind": "object"} for o in saas.objects(tpl, self.config)]

    def read(self, source: dict, watermark=None) -> pa.Table:
        if source.get("object"):
            from . import saas
            source = saas.resolve(self.config, source)
        base = self.config["base_url"].rstrip("/")
        url = base + "/" + (source.get("path") or "").lstrip("/")
        pg = source.get("pagination") or {"type": "none"}
        ptype, max_pages = pg.get("type", "none"), int(pg.get("max_pages", 100))
        params = dict(source.get("params") or {})
        if watermark is not None and source.get("cursor_param"):
            params[source["cursor_param"]] = watermark
        if watermark is not None and source.get("since_params"):  # "changed since" filters, e.g. SOQL, OData, JQL
            since = format_since(watermark, source.get("since_format", "iso"), int(source.get("lookback_seconds") or 0))
            for k, v in source["since_params"].items():
                params[k] = str(v).replace("%SINCE%", since)
        records: list = []
        with self.client() as cl:
            page = int(pg.get("start", 1))
            offset = 0
            next_url: str | None = url
            for _ in range(max_pages):
                p = dict(params)
                if ptype == "page":
                    p[pg.get("page_param", "page")] = page
                    if pg.get("size_param"):
                        p[pg["size_param"]] = pg.get("page_size", 100)
                elif ptype == "offset":
                    p[pg.get("offset_param", "offset")] = offset
                    p[pg.get("size_param", "limit")] = pg.get("page_size", 100)
                req_url = next_url if ptype in ("next_url", "link_header", "cursor") else url
                r, data = self._get(cl, req_url, p if req_url == url or ptype == "cursor" else None)
                batch = dig(data, source.get("records_path"))
                if isinstance(batch, dict):
                    batch = [batch]
                batch = batch or []
                records.extend(batch)
                if ptype == "none" or not batch:
                    break
                if pg.get("more_path") and not dig(data, pg["more_path"]):
                    break  # e.g. Stripe has_more, Zendesk meta.has_more
                if pg.get("last_path") and dig(data, pg["last_path"]):
                    break  # e.g. Jira isLast
                if ptype == "page":
                    page += 1
                elif ptype == "offset":
                    offset += len(batch)
                elif ptype == "next_url":
                    nxt = dig(data, pg.get("next_path", "next"))
                    if not nxt:
                        break
                    next_url = nxt if str(nxt).startswith("http") else base + "/" + str(nxt).lstrip("/")
                elif ptype == "link_header":
                    nxt = r.links.get("next", {}).get("url")
                    if not nxt:
                        break
                    next_url = nxt
                elif ptype == "cursor":
                    if pg.get("cursor_from") == "last_record":  # e.g. Stripe starting_after=<last id>
                        cur = batch[-1].get(pg.get("cursor_field", "id")) if isinstance(batch[-1], dict) else None
                    else:
                        cur = dig(data, pg.get("cursor_path", "next_cursor"))
                    if not cur:
                        break
                    params[pg.get("cursor_param", "cursor")] = cur
                    next_url = url
                if len(records) > 5_000_000:
                    raise ConnectorError("More than 5 million records. Narrow the source or use incremental loads.")
        return normalize_records(records)


class S3Connector(Connector):
    def client(self):
        if importlib.util.find_spec("boto3") is None:
            raise ConnectorError("Driver not installed. Run: pip install boto3")
        import boto3
        c, s = self.config, self.secrets
        kw = {"region_name": c.get("region") or None}
        if c.get("endpoint_url"):
            kw["endpoint_url"] = c["endpoint_url"]
        if c.get("access_key_id"):
            kw["aws_access_key_id"] = c["access_key_id"]
            kw["aws_secret_access_key"] = s.get("secret_access_key")
        return boto3.client("s3", **kw)

    def _keys(self, pattern: str = "*") -> list[dict]:
        cl = self.client()
        prefix = pattern.split("*")[0].split("?")[0]
        out = []
        try:
            for page in cl.get_paginator("list_objects_v2").paginate(Bucket=self.config["bucket"], Prefix=prefix):
                for o in page.get("Contents", []):
                    if fnmatch.fnmatch(o["Key"], pattern):
                        out.append({"name": o["Key"], "size": o["Size"]})
                if len(out) > 10_000:
                    break
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).split("\n")[0]) from None
        return out

    def test(self) -> str:
        return f"Bucket reachable, {len(self._keys('*'))} object(s) listed."

    def browse(self) -> list[dict]:
        return self._keys("*")[:500]

    def read(self, source: dict, watermark=None) -> pa.Table:
        cl = self.client()
        tables = []
        for k in self._keys(source.get("path") or "*"):
            body = cl.get_object(Bucket=self.config["bucket"], Key=k["name"])["Body"].read()
            tables.append(read_bytes_or_path(body, source.get("format", "auto"), k["name"]))
        return concat(tables)


def _after(table: pa.Table, column: str | None, watermark) -> pa.Table:
    """Keep rows whose cursor column is greater than the watermark (incremental loads for sources without server-side filters)."""
    if watermark is None or not column or column not in table.column_names or not table.num_rows:
        return table
    col = table.column(column)
    try:
        wm = pa.scalar(watermark, type=col.type)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
        wm = pa.scalar(str(watermark))
        col = pc.cast(col, pa.string())
    return table.filter(pc.greater(col, wm))


class MongoConnector(Connector):
    client_factory = None  # tests inject mongomock

    def db(self):
        if type(self).client_factory is None and importlib.util.find_spec("pymongo") is None:
            raise ConnectorError("Driver not installed. Run: pip install pymongo")
        factory = type(self).client_factory
        if factory is None:
            import pymongo
            factory = pymongo.MongoClient
        uri = self.secrets.get("uri") or self.config.get("uri")
        if not uri:
            raise ConnectorError("Enter a MongoDB connection string.")
        try:
            cl = factory(uri, serverSelectionTimeoutMS=8000)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Could not connect: {str(e).splitlines()[0]}") from None
        return cl[self.config["database"]]

    def test(self) -> str:
        try:
            names = self.db().list_collection_names()
        except ConnectorError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Could not connect: {str(e).splitlines()[0]}") from None
        return f"Connected, {len(names)} collection(s)."

    def browse(self) -> list[dict]:
        db = self.db()
        return [{"name": n} for n in sorted(db.list_collection_names())][:500]

    def read(self, source: dict, watermark=None) -> pa.Table:
        coll = source.get("collection") or source.get("table")
        if not coll:
            raise ConnectorError("Name the collection to read.")
        flt = source.get("filter") or {}
        if isinstance(flt, str):
            try:
                flt = json.loads(flt) if flt.strip() else {}
            except ValueError:
                raise ConnectorError("Filter must be a JSON object.") from None
        if not isinstance(flt, dict) or _has_keys(flt, {"$where", "$function", "$accumulator"}):
            raise ConnectorError("Filters must be a JSON object and cannot run server-side JavaScript ($where, $function, $accumulator).")
        cur_col = source.get("cursor_column")
        if watermark is not None and cur_col:
            flt = {"$and": [flt, {cur_col: {"$gt": watermark}}]} if flt else {cur_col: {"$gt": watermark}}
        limit = int(source.get("limit") or 0)
        try:
            cursor = self.db()[coll].find(flt, projection=source.get("projection") or None)
            if cur_col:
                cursor = cursor.sort(cur_col, 1)
            if limit:
                cursor = cursor.limit(limit)
            docs = []
            for d in cursor:
                d = dict(d)
                d["_id"] = str(d.get("_id"))
                docs.append(_jsonify(d))
                if len(docs) > 5_000_000:
                    break
        except ConnectorError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).splitlines()[0]) from None
        return normalize_records(docs)


def _has_keys(v, bad: set) -> bool:
    if isinstance(v, dict):
        return any(k in bad or _has_keys(x, bad) for k, x in v.items())
    if isinstance(v, list):
        return any(_has_keys(x, bad) for x in v)
    return False


def _jsonify(v):
    """Turn BSON types (ObjectId, Decimal128, bytes) into plain values; keep datetimes."""
    if isinstance(v, dict):
        return {k: _jsonify(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonify(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    import datetime as _d
    if isinstance(v, (_d.datetime, _d.date)):
        return v
    if hasattr(v, "to_decimal"):
        return float(v.to_decimal())
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


class ElasticConnector(Connector):
    def client(self):
        import httpx
        c, s = self.config, self.secrets
        headers, auth = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Tessera/1.0"}, None
        mode = c.get("auth", "none")
        if mode == "basic":
            auth = (c.get("username", ""), s.get("token", ""))
        elif mode == "apikey":
            headers["Authorization"] = f"ApiKey {s.get('token', '')}"
        elif mode == "bearer":
            headers["Authorization"] = f"Bearer {s.get('token', '')}"
        return httpx.Client(headers=headers, auth=auth, timeout=60, follow_redirects=False)

    def _url(self, path: str) -> str:
        url = self.config["base_url"].rstrip("/") + path
        guard_url(url, self.settings.block_private_http)
        return url

    def _call(self, cl, method, path, body=None, params=None):
        r = cl.request(method, self._url(path), json=body, params=params)
        if r.status_code >= 400:
            raise ConnectorError(f"HTTP {r.status_code} from {path.split('?')[0]}: {r.text[:200]}")
        return r.json()

    def test(self) -> str:
        with self.client() as cl:
            info = self._call(cl, "GET", "/")
        ver = (info.get("version") or {}).get("number", "?")
        return f"Connected to {(info.get('version') or {}).get('distribution', 'elasticsearch')} {ver}."

    def browse(self) -> list[dict]:
        with self.client() as cl:
            rows = self._call(cl, "GET", "/_cat/indices", params={"format": "json"})
        return [{"name": r.get("index"), "rows": int(r.get("docs.count") or 0)} for r in rows if not str(r.get("index", "")).startswith(".")][:500]

    def read(self, source: dict, watermark=None) -> pa.Table:
        index = source.get("index") or source.get("table")
        if not index or not re.fullmatch(r"[A-Za-z0-9_.*,\-]+", index):
            raise ConnectorError("Give an index name or pattern (letters, digits, - _ . * and commas).")
        q = source.get("query") or {"match_all": {}}
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except ValueError:
                raise ConnectorError("Query must be JSON (Elasticsearch query DSL).") from None
        cur_col = source.get("cursor_column")
        if watermark is not None and cur_col:
            wm = watermark.isoformat() if hasattr(watermark, "isoformat") else watermark
            q = {"bool": {"must": [q], "filter": [{"range": {cur_col: {"gt": wm}}}]}}
        size, cap = 1000, int(source.get("limit") or 1_000_000)
        docs = []
        with self.client() as cl:
            res = self._call(cl, "POST", f"/{index}/_search", {"query": q, "size": size}, params={"scroll": "2m"})
            sid = res.get("_scroll_id")
            try:
                while True:
                    hits = (res.get("hits") or {}).get("hits") or []
                    for hh in hits:
                        docs.append({"_id": hh.get("_id"), "_index": hh.get("_index"), **(hh.get("_source") or {})})
                    if not hits or len(docs) >= cap or not sid:
                        break
                    res = self._call(cl, "POST", "/_search/scroll", {"scroll": "2m", "scroll_id": sid})
                    sid = res.get("_scroll_id") or sid
            finally:
                if sid:
                    try:
                        cl.request("DELETE", self._url("/_search/scroll"), json={"scroll_id": sid})
                    except Exception:  # noqa: BLE001
                        pass
        return normalize_records(docs[:cap])


class DeltaConnector(Connector):
    def _base(self) -> tuple[str, dict]:
        bp = (self.config.get("base_path") or "").strip()
        opts = self.config.get("storage_options") or {}
        if isinstance(opts, str):
            try:
                opts = json.loads(opts) if opts.strip() else {}
            except ValueError:
                raise ConnectorError("Storage options must be a JSON object.") from None
        if self.secrets.get("storage_secret"):
            if bp.startswith("s3"):
                opts["AWS_SECRET_ACCESS_KEY"] = self.secrets["storage_secret"]
            elif bp.startswith(("abfs", "az")):
                opts["AZURE_STORAGE_ACCOUNT_KEY"] = self.secrets["storage_secret"]
        if re.match(r"^(s3|s3a|gs|abfss?|az)://", bp):
            return bp.rstrip("/"), opts
        root = (self.settings.uploads_dir / bp).resolve() if bp and not Path(bp).is_absolute() else (Path(bp).resolve() if bp else self.settings.uploads_dir.resolve())
        if not _within(root, self.settings.allowed_import_roots()):
            raise ConnectorError("That folder is not allowed. Add it to TESSERA_IMPORT_DIRS or use the uploads folder.")
        return str(root), opts

    def _table(self, name: str, version=None):
        if importlib.util.find_spec("deltalake") is None:
            raise ConnectorError("Driver not installed. Run: pip install deltalake")
        from deltalake import DeltaTable
        if ".." in Path(name).parts:
            raise ConnectorError("Paths cannot contain '..'")
        base, opts = self._base()
        uri = f"{base}/{name.strip('/')}" if name else base
        try:
            return DeltaTable(uri, version=int(version) if version not in (None, "") else None, storage_options=opts or None)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"Not a readable Delta table: {str(e).splitlines()[0]}") from None

    def test(self) -> str:
        base, _ = self._base()
        if "://" in base:
            return "Location configured. Tables are read when a load runs."
        return f"Folder reachable, {len(self.browse())} Delta table(s) found."

    def browse(self) -> list[dict]:
        base, _ = self._base()
        if "://" in base:
            return []
        root = Path(base)
        return [{"name": str(p.parent.relative_to(root)) if p.parent != root else ""} for p in sorted(root.rglob("_delta_log")) if p.is_dir()][:500]

    def read(self, source: dict, watermark=None) -> pa.Table:
        dt = self._table(source.get("table") or source.get("path") or "", source.get("version"))
        try:
            t = dt.to_pyarrow_table(columns=source.get("columns") or None)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(str(e).splitlines()[0]) from None
        return _after(t, source.get("cursor_column"), watermark)


class StreamConnector(Connector):
    """Connection object for streaming sources. Reading happens through Streams, not batch ingest."""

    def test(self) -> str:
        from . import streaming
        return streaming.connection_test(self.type, self.config, self.secrets, self.settings)

    def browse(self) -> list[dict]:
        from . import streaming
        return streaming.browse(self.type, self.config, self.secrets, self.settings)

    def read(self, source: dict, watermark=None) -> pa.Table:
        raise ConnectorError(f"{CONNECTOR_TYPES[self.type]['label']} is a streaming source. Create a stream on it "
                             "(Streams page) to land events continuously.")


def make(ctype: str, config: dict, secrets: dict, settings) -> Connector:
    if ctype not in CONNECTOR_TYPES:
        raise ConnectorError(f"Unknown connector type '{ctype}'")
    if CONNECTOR_TYPES[ctype].get("stream"):
        return StreamConnector(ctype, config, secrets, settings)
    if ctype in _LAKE_CLASSES:
        return _LAKE_CLASSES[ctype](ctype, config, secrets, settings)
    if ctype == "file":
        return FileConnector(ctype, config, secrets, settings)
    if ctype == "rest":
        return RestConnector(ctype, config, secrets, settings)
    if ctype == "s3":
        return S3Connector(ctype, config, secrets, settings)
    if ctype == "mongodb":
        return MongoConnector(ctype, config, secrets, settings)
    if ctype == "elasticsearch":
        return ElasticConnector(ctype, config, secrets, settings)
    if ctype == "delta":
        return DeltaConnector(ctype, config, secrets, settings)
    return SQLConnector(ctype, config, secrets, settings)


def split_config(ctype: str, values: dict) -> tuple[dict, dict]:
    """Separate secret fields so they are stored encrypted and never returned by the API."""
    secret_keys = {f["key"] for f in CONNECTOR_TYPES[ctype]["fields"] if f.get("secret")}
    cfg = {k: v for k, v in values.items() if k not in secret_keys and v not in (None, "")}
    sec = {k: v for k, v in values.items() if k in secret_keys and v not in (None, "")}
    return cfg, sec


_ = (qi, QueryError)

from .connectors_lake import CLASSES as _LAKE_CLASSES  # noqa: E402
from .connectors_lake import TYPES as _LAKE_TYPES  # noqa: E402
from .streaming import CONNECTOR_TYPES as _STREAM_TYPES  # noqa: E402  (streaming imports helpers from this module)

CONNECTOR_TYPES.update(_LAKE_TYPES)
CONNECTOR_TYPES.update(_STREAM_TYPES)
