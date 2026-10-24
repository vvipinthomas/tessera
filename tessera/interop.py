"""Working alongside other platforms.

deliver      - a pipeline step that writes a published (serving) dataset to another system: any SQL database or
               warehouse Tessera can connect to (PostgreSQL, Snowflake, BigQuery, Databricks, SQL Server / Fabric
               Warehouse, ...), a Delta Lake table (local, S3, ADLS / OneLake, GCS), or Parquet/CSV files on S3 or disk.
               Only published data leaves: masking has already been applied.
openlineage  - emits OpenLineage run events (START, COMPLETE, FAIL) for every pipeline run to Marquez, DataHub,
               OpenMetadata, Atlan, Microsoft Purview (through its OpenLineage endpoint) or any compatible collector.
dbt          - imports a dbt manifest.json: model and column descriptions, owners and tags, tests as quality rules,
               and the model dependency graph as lineage, matched to Tessera datasets by table name.
"""
from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from fastapi import Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import connectors
from .connectors import ConnectorError, _within
from .errors import UserError
from .store import now
from .warehouse import QueryError, arrow_table, qi, split_fqn

SQL_TYPES = {k for k, t in connectors.CONNECTOR_TYPES.items() if k not in ("file", "rest", "s3", "mongodb", "elasticsearch", "delta")
             and not t.get("stream") and k not in ("iceberg", "sftp")}
TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}(\.[A-Za-z_][A-Za-z0-9_$]{0,127}){0,2}$")
PATHLIKE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./=-]{0,255}$")


class DeliveryError(UserError):
    pass


# ---------------------------------------------------------------- deliver
def read_published(plat, dataset: str) -> tuple[str, str, pa.Table]:
    schema, name = split_fqn(dataset, "serving")
    if schema != "serving":
        raise DeliveryError("Only published data can be delivered to other systems. Publish it to serving first; "
                            "publishing applies the masking policies.")
    if not plat.wh.exists(schema, name):
        raise DeliveryError(f"serving.{name} does not exist. Run its publish step first.")
    cur = plat.wh.cursor()
    try:
        return schema, name, arrow_table(cur.sql(f"SELECT * FROM {qi(schema)}.{qi(name)}"))
    finally:
        cur.close()


def deliver(plat, step: dict, pipeline_id: int | None = None) -> dict:
    schema, name, table = read_published(plat, step.get("dataset") or "")
    if not isinstance(table, pa.Table):
        table = table.read_all()
    conn = plat.connection(int(step.get("connection_id") or 0))
    cx = plat.connector(conn["id"])
    mode = step.get("mode") or "replace"
    if mode not in ("replace", "append"):
        raise DeliveryError("Mode must be replace or append.")
    target = (step.get("table") or name).strip()
    if conn["type"] in SQL_TYPES:
        dest = _to_sql(cx, table, target, mode)
    elif conn["type"] == "delta":
        dest = _to_delta(cx, table, target, mode)
    elif conn["type"] in ("file", "s3"):
        dest = _to_files(cx, conn["type"], table, target, step.get("format") or "parquet")
    else:
        raise DeliveryError(f"{connectors.CONNECTOR_TYPES[conn['type']]['label']} cannot be a destination. "
                            "Use a database, warehouse, Delta Lake, S3 or Files connection.")
    plat.add_lineage(f"{schema}.{name}", f"dest:{conn['name']}/{dest}", "deliver", pipeline_id)
    return {"dataset": f"{schema}.{name}", "destination": f"{conn['name']}/{dest}", "rows": table.num_rows, "mode": mode}


def _to_sql(cx, table: pa.Table, target: str, mode: str) -> str:
    if not TARGET.match(target):
        raise DeliveryError("Destination table must be name, schema.name or database.schema.name.")
    parts = target.split(".")
    tname, tschema = parts[-1], (parts[-2] if len(parts) > 1 else None)
    df = table.to_pandas()
    for c in df.columns:  # nested values are written as JSON text, which every database accepts
        if df[c].dtype == object and any(isinstance(v, (dict, list)) for v in df[c].head(50)):
            df[c] = df[c].map(lambda v: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
    eng = cx.engine()
    try:
        with eng.begin() as con:
            df.to_sql(tname, con, schema=tschema, if_exists="replace" if mode == "replace" else "append",
                      index=False, chunksize=5000, method="multi")
    except Exception as e:  # noqa: BLE001 - driver specific
        raise DeliveryError(f"Write failed: {str(e).splitlines()[0][:300]}") from None
    finally:
        eng.dispose()
    return target


def _to_delta(cx, table: pa.Table, target: str, mode: str) -> str:
    if not PATHLIKE.match(target) or ".." in target.split("/"):
        raise DeliveryError("Delta destination must be a relative folder such as gold/orders.")
    try:
        from deltalake import write_deltalake
    except ImportError:
        raise DeliveryError("Install deltalake to write Delta tables: pip install deltalake") from None
    base, opts = cx._base()
    uri = f"{base}/{target.strip('/')}"
    try:
        write_deltalake(uri, table, mode="overwrite" if mode == "replace" else "append",
                        schema_mode="overwrite" if mode == "replace" else "merge", storage_options=opts or None)
    except Exception as e:  # noqa: BLE001
        raise DeliveryError(f"Delta write failed: {str(e).splitlines()[0][:300]}") from None
    return target


def _to_files(cx, ctype: str, table: pa.Table, target: str, fmt: str) -> str:
    if fmt not in ("parquet", "csv"):
        raise DeliveryError("File format must be parquet or csv.")
    if not PATHLIKE.match(target) or ".." in target.split("/"):
        raise DeliveryError("File destination must be a relative path such as exports/orders.")
    key = target if target.endswith("." + fmt) else f"{target}.{fmt}"
    buf = io.BytesIO()
    if fmt == "parquet":
        pq.write_table(table, buf)
    else:
        pacsv.write_csv(table, buf)
    if ctype == "s3":
        try:
            cx.client().put_object(Bucket=cx.config["bucket"], Key=key, Body=buf.getvalue())
        except Exception as e:  # noqa: BLE001
            raise DeliveryError(f"S3 write failed: {str(e).splitlines()[0][:300]}") from None
        return key
    root = cx.root()
    path = (root / key).resolve()
    if not _within(path, [root.resolve()]):
        raise DeliveryError("That path leaves the connection's folder.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return key


# ---------------------------------------------------------------- OpenLineage
PRODUCER = "https://github.com/vvipinthomas/tessera"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"


def ol_settings(plat) -> dict:
    s = plat.store.get_setting("openlineage")
    s = json.loads(s) if isinstance(s, str) else (s or {})
    return s


def ol_dataset(plat, fqn: str, namespace: str, with_schema: bool = False) -> dict:
    if fqn.startswith(("source:", "upload:", "stream:", "dest:", "push:")):
        kind, rest = fqn.split(":", 1)
        return {"namespace": f"{namespace}.{kind}", "name": rest}
    ds = {"namespace": namespace, "name": fqn}
    if with_schema and "." in fqn:
        sch, nm = fqn.split(".", 1)
        d = plat.dataset(sch, nm)
        if d and d.get("columns"):
            ds["facets"] = {"schema": {"_producer": PRODUCER, "_schemaURL": "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json",
                                       "fields": [{"name": c["name"], "type": c.get("type"), "description": c.get("description") or None}
                                                  for c in d["columns"]]}}
    return ds


def ol_event(plat, event_type: str, run_id: int, pipeline: dict, error: str | None = None) -> dict | None:
    cfg = ol_settings(plat)
    if not cfg.get("enabled") or not cfg.get("url"):
        return None
    ns = cfg.get("namespace") or "tessera"
    ev = {"eventType": event_type, "eventTime": datetime.now(timezone.utc).isoformat(),
          "run": {"runId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"tessera:{ns}:run:{run_id}"))},
          "job": {"namespace": ns, "name": pipeline["name"],
                  "facets": {"documentation": {"_producer": PRODUCER, "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/DocumentationJobFacet.json",
                                               "description": pipeline.get("description") or ""}}},
          "inputs": [], "outputs": [], "producer": PRODUCER, "schemaURL": SCHEMA_URL}
    if event_type in ("COMPLETE", "FAIL"):
        edges = plat.store.all("SELECT src, dst FROM lineage WHERE pipeline_id=?", (pipeline["id"],))
        dsts = {e["dst"] for e in edges}
        srcs = {e["src"] for e in edges} - dsts
        ev["inputs"] = [ol_dataset(plat, s, ns) for s in sorted(srcs)]
        ev["outputs"] = [ol_dataset(plat, d, ns, with_schema=True) for d in sorted(dsts)]
    if error:
        ev["run"]["facets"] = {"errorMessage": {"_producer": PRODUCER, "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json",
                                                "message": error[:2000], "programmingLanguage": "SQL"}}
    return ev


def ol_send(plat, http_client, event: dict) -> None:
    cfg = ol_settings(plat)
    url = cfg["url"].rstrip("/")
    if not url.endswith("/lineage"):
        url += "/api/v1/lineage"
    connectors.guard_url(url, plat.settings.block_private_http)
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key_enc"):
        key = plat.vault.decrypt(cfg["api_key_enc"]).get("api_key")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    r = http_client.post(url, content=json.dumps(event, default=str), headers=headers)
    if r.status_code >= 400:
        raise ConnectorError(f"OpenLineage endpoint returned HTTP {r.status_code}")


# ---------------------------------------------------------------- dbt
DBT_TESTS = {"not_null": "not_null", "unique": "unique", "accepted_values": "allowed_values"}


def import_dbt(plat, manifest: dict, user: dict, apply_tests: bool = True) -> dict:
    """Match dbt models, seeds and sources to Tessera datasets by table name, then bring over documentation,
    tests and lineage. Nothing is created for models that have no matching dataset; they are reported instead."""
    if not isinstance(manifest, dict) or "nodes" not in manifest:
        raise DeliveryError("That is not a dbt manifest.json (no 'nodes').")
    by_name: dict[str, list[dict]] = {}
    for d in plat.store.all("SELECT * FROM datasets"):
        by_name.setdefault(d["name"].lower(), []).append(d)

    def match(node: dict) -> dict | None:
        names = [node.get("alias"), node.get("identifier"), node.get("name")]
        for n in names:
            if n and n.lower() in by_name:
                cands = by_name[n.lower()]
                # dbt sources are landed data (raw first); models are built data (published first)
                order = {"raw": 0, "curated": 1, "serving": 2} if node.get("resource_type") == "source" else {"serving": 0, "curated": 1, "raw": 2}
                return sorted(cands, key=lambda d: order.get(d["schema_name"], 3))[0]
        return None

    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
    matched, unmatched, rules, edges = {}, [], 0, 0
    for uid, n in nodes.items():
        if n.get("resource_type") not in ("model", "seed", "snapshot", "source"):
            continue
        d = match(n)
        if not d:
            unmatched.append(n.get("name"))
            continue
        matched[uid] = d
        cols = {c["name"]: c for c in (d.get("columns") or [])}
        for cname, c in (n.get("columns") or {}).items():
            if cname in cols and c.get("description"):
                cols[cname]["description"] = c["description"]
        meta = n.get("meta") or {}
        upd = {"columns": json.dumps(list(cols.values()))}
        if n.get("description"):
            upd["description"] = n["description"]
        if meta.get("owner") or (n.get("config") or {}).get("meta", {}).get("owner"):
            upd["owner"] = meta.get("owner") or n["config"]["meta"]["owner"]
        tags = sorted(set((d.get("tags") or []) + list(n.get("tags") or []) + ["dbt"]))
        upd["tags"] = json.dumps(tags)
        plat.store.update("datasets", d["id"], upd)
    for uid, n in nodes.items():
        if uid not in matched:
            continue
        for dep in (n.get("depends_on") or {}).get("nodes", []):
            if dep in matched:
                a, b = matched[dep], matched[uid]
                plat.add_lineage(f"{a['schema_name']}.{a['name']}", f"{b['schema_name']}.{b['name']}", "dbt", None)
                edges += 1
    if apply_tests:
        for uid, n in manifest.get("nodes", {}).items():
            if n.get("resource_type") != "test":
                continue
            tm = n.get("test_metadata") or {}
            kind = DBT_TESTS.get(tm.get("name"))
            parent = next((p for p in (n.get("depends_on") or {}).get("nodes", []) if p in matched), None)
            col = (tm.get("kwargs") or {}).get("column_name") or n.get("column_name")
            if not kind or not parent or not col:
                continue
            d = matched[parent]
            params = {"values": (tm.get("kwargs") or {}).get("values") or []} if kind == "allowed_values" else {}
            exists = plat.store.one("SELECT id FROM rules WHERE dataset_id=? AND kind=? AND column_name=?", (d["id"], kind, col))
            if exists:
                continue
            sev = "warn" if ((n.get("config") or {}).get("severity") or "error").lower() == "warn" else "error"
            plat.store.insert("rules", {"dataset_id": d["id"], "column_name": col, "kind": kind, "params": json.dumps(params),
                                        "severity": sev, "enabled": 1, "description": f"dbt test {tm.get('name')} on {col}",
                                        "created_by": user["email"], "created_at": now()})
            rules += 1
    return {"matched": len(matched), "unmatched": sorted(x for x in unmatched if x)[:200], "rules_created": rules, "lineage_edges": edges,
            "dbt_version": (manifest.get("metadata") or {}).get("dbt_version")}


_ = (Path, QueryError)


# ---------------------------------------------------------------- API
def register(app, plat, user, need, audit, orch):
    admin, engineer, analyst = need("admin"), need("engineer"), need("analyst")
    app.add_exception_handler(DeliveryError, lambda r, e: JSONResponse({"detail": str(e)}, status_code=400))

    @app.get("/api/integrations")
    def integrations(request: Request, u: dict = Depends(analyst)):
        root = (plat.settings.base_url or str(request.base_url)).rstrip("/")
        ol = ol_settings(plat)
        return {"odata_url": root + "/odata", "mcp_url": root + "/mcp", "openapi_url": root + "/api/docs",
                "openlineage": {"enabled": bool(ol.get("enabled")), "url": ol.get("url"), "namespace": ol.get("namespace") or "tessera"},
                "destinations": sorted(SQL_TYPES | {"delta", "file", "s3"})}

    @app.get("/api/admin/openlineage")
    def get_ol(u: dict = Depends(admin)):
        s = ol_settings(plat)
        return {"enabled": bool(s.get("enabled")), "url": s.get("url") or "", "namespace": s.get("namespace") or "tessera",
                "api_key_set": bool(s.get("api_key_enc"))}

    @app.put("/api/admin/openlineage")
    def put_ol(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        s = ol_settings(plat)
        url = (b.get("url") if "url" in b else s.get("url") or "") or ""
        if url:
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "The OpenLineage URL must start with http:// or https://")
            try:
                connectors.guard_url(url, plat.settings.block_private_http)
            except ConnectorError as e:
                raise HTTPException(400, str(e)) from None
        ns = (b.get("namespace") or s.get("namespace") or "tessera").strip()
        if not re.match(r"^[A-Za-z0-9_.:/-]{1,100}$", ns):
            raise HTTPException(400, "Namespace may use letters, digits and _ . : / -")
        new = {"enabled": bool(b.get("enabled", s.get("enabled"))) and bool(url), "url": url, "namespace": ns,
               "api_key_enc": s.get("api_key_enc")}
        if b.get("api_key") is not None:
            new["api_key_enc"] = plat.vault.encrypt({"api_key": b["api_key"]}) if b["api_key"] else None
        plat.store.set_setting("openlineage", new)
        audit(u, "openlineage.updated", url, {"enabled": new["enabled"]}, request)
        return get_ol(u)

    @app.post("/api/admin/openlineage/test")
    def test_ol(u: dict = Depends(admin)):
        ev = ol_event(plat, "COMPLETE", 0, {"id": -1, "name": "tessera_connection_test", "description": "Connection test from Tessera"})
        if not ev:
            raise HTTPException(400, "Turn OpenLineage on and set its URL first.")
        try:
            with orch._http() as cl:
                ol_send(plat, cl, ev)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Could not deliver: {str(e).splitlines()[0][:300]}") from None
        return {"ok": True, "message": "Test event accepted."}

    @app.post("/api/integrations/dbt")
    async def dbt_import(request: Request, file: UploadFile = File(...), u: dict = Depends(engineer)):
        raw = await file.read(60 * 1024 * 1024 + 1)
        if len(raw) > 60 * 1024 * 1024:
            raise HTTPException(413, "The manifest is larger than 60 MB.")
        try:
            manifest = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "The file is not valid JSON. Upload target/manifest.json from a dbt project.") from None
        out = import_dbt(plat, manifest, u)
        audit(u, "dbt.imported", file.filename or "manifest.json", {k: out[k] for k in ("matched", "rules_created", "lineage_edges")}, request)
        return out
