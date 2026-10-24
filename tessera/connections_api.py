"""Connection helpers beyond the basic CRUD: application templates (SaaS), bulk copy of whole databases and
schema drift."""
from __future__ import annotations

import json

from fastapi import Body, Depends, HTTPException, Request

from . import connectors, saas
from .mirror import MirrorError, Mirrors, mirror_target
from .orchestrator import PipelineError, next_run, validate
from .store import now
from .warehouse import safe_name

# Column names that usually mean "this row last changed at", best first.
CHANGE_COLUMNS = ["updated_at", "updatedat", "modified_at", "last_modified", "lastmodified", "last_modified_date",
                  "lastmodifieddate", "modified", "modifieddate", "modified_date", "date_modified", "updated", "changed_at",
                  "last_updated", "lastupdated", "systemmodstamp", "row_version", "rowversion"]
TIME_TYPES = ("timestamp", "datetime", "date", "time", "rowversion", "int", "bigint", "integer", "numeric")


def change_column(cols: list[dict]) -> str | None:
    """The column that tells which rows changed, if the table has an obvious one."""
    by = {c["name"].lower(): c for c in cols}
    for cand in CHANGE_COLUMNS:
        c = by.get(cand)
        if c and any(t in c["type"] for t in TIME_TYPES):
            return c["name"]
    return None


def plan_table(info: dict | None, mode: str) -> tuple[str, dict]:
    """Choose the load mode and source extras for one SQL table: merge on the primary key when the table has a change
    column, append new rows when it has a change column but no key, otherwise a full refresh."""
    if mode not in ("auto", "replace", "incremental", "merge"):
        raise HTTPException(400, "Mode must be auto, replace, incremental or merge.")
    if not info or mode == "replace":
        return "replace", {}
    cur, pk = change_column(info["columns"]), info["primary_key"]
    if mode == "auto":
        mode = "merge" if cur and pk else "incremental" if cur else "replace"
    if mode == "merge":
        if not pk:
            raise HTTPException(400, "Merge needs tables with a primary key.")
        return "merge", {"key": pk} | ({"cursor_column": cur} if cur else {})
    if mode == "incremental":
        if not cur:
            return "replace", {}
        return "incremental", {"cursor_column": cur}
    return "replace", {}


def register(app, plat, user, need, audit, orch):
    store = plat.store
    engineer = need("engineer")
    mirrors = app.state.mirrors = Mirrors(plat, float(__import__("os").environ.get("TESSERA_MIRROR_SECONDS", "5")))

    def mirror_out(m: dict) -> dict:
        c = store.one("SELECT name FROM connections WHERE id=?", (m["connection_id"],))
        return {**m, "connection": c["name"] if c else None,
                "targets": [f"raw.{mirror_target(m['prefix'], t)}" for t in m["tables"]]}

    @app.get("/api/mirrors")
    def list_mirrors(u: dict = Depends(engineer)):
        return [mirror_out(m) for m in mirrors.rows()]

    @app.post("/api/connections/{cid}/mirror")
    def create_mirror(cid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        conn = plat.connection(cid)
        if conn["type"] != "postgres":
            raise HTTPException(400, "Mirroring works with PostgreSQL connections. Use Copy many tables for other sources.")
        tables = list(dict.fromkeys(str(t).strip() for t in (b.get("tables") or []) if str(t).strip()))
        if not tables:
            raise HTTPException(400, "Choose at least one table to mirror.")
        busy = {t for m in mirrors.rows() if m["connection_id"] == cid for t in m["tables"]} & set(tables)
        if busy:
            raise HTTPException(409, f"Already mirrored: {', '.join(sorted(busy))}.")
        prefix = safe_name(b["prefix"]) + "_" if (b.get("prefix") or "").strip() else ""
        try:
            m = mirrors.create(cid, tables, prefix, u["email"])
        except MirrorError as e:
            raise HTTPException(400, str(e)) from None
        audit(u, "mirror.created", conn["name"], {"tables": tables}, request)
        return mirror_out(m)

    @app.post("/api/mirrors/{mid}/sync")
    def sync_mirror(mid: int, u: dict = Depends(engineer)):
        try:
            applied = mirrors.sync(mid)
        except MirrorError as e:
            raise HTTPException(502 if mirrors.get(mid) else 404, str(e)) from None
        return {"applied": applied, "mirror": mirror_out(mirrors.get(mid))}

    @app.post("/api/mirrors/{mid}/pause")
    def pause_mirror(mid: int, request: Request, b: dict = Body(default={}), u: dict = Depends(engineer)):
        if not mirrors.get(mid):
            raise HTTPException(404, "Mirror not found.")
        mirrors.pause(mid, bool(b.get("paused", True)))
        audit(u, "mirror.paused" if b.get("paused", True) else "mirror.resumed", str(mid), None, request)
        return mirror_out(mirrors.get(mid))

    @app.delete("/api/mirrors/{mid}")
    def delete_mirror(mid: int, request: Request, u: dict = Depends(engineer)):
        try:
            mirrors.delete(mid)
        except MirrorError as e:
            raise HTTPException(502, f"Could not drop the replication slot: {e}") from None
        audit(u, "mirror.deleted", str(mid), None, request)
        return {"ok": True}

    @app.get("/api/connectors/apps")
    def apps(u: dict = Depends(engineer)):
        return saas.catalog()

    @app.post("/api/connections/from-app")
    def from_app(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        key = (b.get("app") or "").strip()
        name = (b.get("name") or saas.TEMPLATES.get(key, {}).get("label") or "").strip()[:80]
        if not name:
            raise HTTPException(400, "Give the connection a name.")
        if store.one("SELECT id FROM connections WHERE name=?", (name,)):
            raise HTTPException(409, "A connection with that name exists.")
        try:
            cfg, sec = saas.build(key, b.get("values") or {})
        except connectors.ConnectorError as e:
            raise HTTPException(400, str(e)) from None
        cid = store.insert("connections", {"name": name, "type": "rest", "config": json.dumps(cfg),
                                           "secrets": plat.vault.encrypt(sec) if sec else None,
                                           "created_by": u["id"], "created_at": now(), "updated_at": now()})
        audit(u, "connection.created", name, {"type": "rest", "app": key}, request)
        return plat.public_connection(plat.connection(cid))

    @app.post("/api/connections/{cid}/copy")
    def bulk_copy(cid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        """Copy many tables or objects at once: one pipeline with a load step per table, each loaded the smartest way
        the table allows (merge on the key, new rows only, or a full refresh)."""
        conn = plat.connection(cid)
        names = [str(n).strip() for n in (b.get("objects") or b.get("tables") or []) if str(n).strip()]
        names = list(dict.fromkeys(names))
        if not names:
            raise HTTPException(400, "Choose at least one table to copy.")
        if len(names) > 200:
            raise HTTPException(400, "Copy at most 200 tables at a time.")
        ctype, cfg = conn["type"], conn["config"] or {}
        meta = connectors.CONNECTOR_TYPES.get(ctype, {})
        if meta.get("stream"):
            raise HTTPException(400, "Streams are read continuously; create a stream instead of a copy.")
        prefix = safe_name(b.get("prefix") or "") + "_" if (b.get("prefix") or "").strip() else ""
        mode = b.get("mode") or "auto"
        schedule = (b.get("schedule") or "").strip() or None
        try:
            cx = plat.connector(cid)
        except connectors.ConnectorError as e:
            raise HTTPException(400, str(e)) from None
        sql = isinstance(cx, connectors.SQLConnector)
        info = {}
        if sql and mode != "replace":
            try:
                info = cx.describe(names)
            except connectors.ConnectorError as e:
                raise HTTPException(400, f"Could not read the table list: {e}") from None
        steps, used, summary = [], set(), []
        for n in names:
            base = safe_name(n.split(".", 1)[1] if sql and n.count(".") == 1 and n.split(".")[0] in ("public", "dbo", "main") else n)
            target, i = prefix + base, 2
            while target in used:
                target, i = f"{prefix}{base}_{i}", i + 1
            used.add(target)
            if ctype == "rest":
                cur, key = saas.default_cursor(cfg, n), saas.default_key(cfg, n)
                m = "replace" if mode == "replace" or not cur else "merge" if key else "incremental"
                source = {"object": n} | ({"key": key, "cursor_column": cur} if m == "merge" else {"cursor_column": cur} if m == "incremental" else {})
            elif ctype in ("file", "s3"):
                source, m = {"path": n}, "replace"
            elif sql:
                m, extra = plan_table(info.get(n), mode)
                source = {"table": n} | extra
            else:
                source, m = {"table": n}, "replace"
            steps.append({"id": ("load_" + target)[:60], "type": "ingest", "connection_id": cid, "source": source,
                          "target": f"raw.{target}", "mode": m})
            summary.append({"source": n, "target": f"raw.{target}", "mode": m, "key": source.get("key"), "cursor": source.get("cursor_column")})
        try:
            validate(steps, schedule)
        except PipelineError as e:
            raise HTTPException(400, str(e)) from None
        base_name = (b.get("name") or f"Copy from {conn['name']}").strip()[:100]
        name, i = base_name, 2
        while store.one("SELECT id FROM pipelines WHERE name=?", (name,)):
            name, i = f"{base_name} ({i})", i + 1
        pid = store.insert("pipelines", {"name": name, "description": f"Copies {len(steps)} table(s) from {conn['name']}.",
                                         "steps": json.dumps(steps), "schedule": schedule, "enabled": 1,
                                         "next_run_at": next_run(schedule) if schedule else None, "webhook_url": None,
                                         "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "pipeline.created", name, {"copy_from": conn["name"], "tables": len(steps)}, request)
        run_id = orch.trigger(pid, "manual", u["email"]) if b.get("run", True) else None
        return {"pipeline": {"id": pid, "name": name, "schedule": schedule}, "steps": len(steps), "tables": summary, "run_id": run_id}

    @app.get("/api/datasets/{did}/schema-changes")
    def schema_changes(did: int, u: dict = Depends(need("analyst"))):
        plat.dataset_by_id(did)
        return plat.schema_changes(did)

    return None
