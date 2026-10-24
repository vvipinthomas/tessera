"""Shared building blocks for every connector: the error type, the base class, URL and path guards, and turning
files and JSON records into Arrow tables. Kept apart from connectors.py so the lake, streaming and application modules
can use them without importing connectors.py (which imports those modules in turn)."""
from __future__ import annotations

import io
import ipaddress
import json
import math
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq

from .errors import UserError


class ConnectorError(UserError):
    pass


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
                    df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
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
