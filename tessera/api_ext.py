"""API for the reporting and governance layer: semantic models, exports, subscriptions, alerts, notifications,
row-level security, glossary, visual transforms, notebooks, push ingestion, monitoring, deployment bundles and search."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import Response

from . import bundle, connectors, semantic, transforms
from . import workspaces as ws
from .platform import NotFound
from .reporting import ALERT_OPS, ALERT_SOURCES, Reporter, ReportError, dashboard_results, to_csv, to_xlsx, validate_cron
from .security import has_role, restricted
from .store import now
from .warehouse import AccessDenied, QueryError, split_fqn

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EMAIL = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")


def file_response(data: bytes, name: str, fmt: str) -> Response:
    safe = re.sub(r"[^\w.\- ]+", "_", name)[:100] or "export"
    return Response(data, media_type=XLSX if fmt == "xlsx" else "text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.{fmt}"'})


def register(app, plat, orch, user, need, audit):
    store = plat.store
    reporter = Reporter(plat, orch.http_client_factory)
    orch.reporter = reporter
    app.state.reporter = reporter
    viewer, analyst, engineer, admin = need("viewer"), need("analyst"), need("engineer"), need("admin")
    app.add_exception_handler(ReportError, lambda r, e: __import__("fastapi").responses.JSONResponse({"detail": str(e)}, status_code=400))

    # ------------------------------------------------------------------ semantic models
    def visible_model(u, m, need="viewer"):
        ws.require(store, u, m.get("workspace_id") or 1, need)
        if restricted(u["role"]) and not m["base"].startswith("serving."):
            raise AccessDenied("This model is built on unpublished data. Ask an engineer to point it at a serving dataset.")
        return m

    @app.get("/api/models")
    def models(workspace: int | None = None, u: dict = Depends(viewer)):
        rows = store.all("SELECT * FROM semantic_models ORDER BY CASE endorsement WHEN 'certified' THEN 0 WHEN 'promoted' THEN 1 ELSE 2 END, name")
        rows = ws.filter_rows(store, u, rows, workspace)
        return [m for m in rows if not (restricted(u["role"]) and not m["base"].startswith("serving."))]

    @app.get("/api/models/{mid}")
    def get_model(mid: int, u: dict = Depends(viewer)):
        return visible_model(u, plat.model(mid))

    @app.post("/api/models/suggest")
    def suggest_model(b: dict = Body(...), u: dict = Depends(engineer)):
        schema, table = split_fqn(b.get("base") or "", "serving")
        if not plat.wh.exists(schema, table):
            raise NotFound("Table not found")
        return semantic.suggest_model(plat.wh, schema, table)

    @app.post("/api/models")
    def create_model(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        wid = ws.target_ws(store, u, b.get("workspace_id"))
        m = semantic.validate_model(b, plat.wh)
        if store.one("SELECT id FROM semantic_models WHERE name=?", (m["name"],)):
            raise HTTPException(409, "A model with that name exists.")
        mid = store.insert("semantic_models", {**{k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in m.items()}, "workspace_id": wid,
                                               "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "model.created", m["name"], {"measures": len(m["measures"]), "dimensions": len(m["dimensions"])}, request)
        return plat.model(mid)

    @app.put("/api/models/{mid}")
    def update_model(mid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        old = visible_model(u, plat.model(mid), "contributor")
        m = semantic.validate_model({**{k: old[k] for k in ("name", "description", "base", "joins", "dimensions", "measures")}, **b}, plat.wh)
        store.update("semantic_models", mid, {**{k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in m.items()}, "updated_at": now()})
        audit(u, "model.updated", m["name"], None, request)
        return plat.model(mid)

    @app.patch("/api/models/{mid}/endorsement")
    def endorse_model(mid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        e = b.get("endorsement", "none")
        if e not in ("none", "promoted", "certified"):
            raise HTTPException(400, "Endorsement is none, promoted or certified.")
        if e == "certified" and not has_role(u["role"], "admin"):
            raise HTTPException(403, "Only admins can certify.")
        visible_model(u, plat.model(mid), "contributor")
        store.update("semantic_models", mid, {"endorsement": e})
        audit(u, "model.endorsed", str(mid), {"endorsement": e}, request)
        return plat.model(mid)

    @app.delete("/api/models/{mid}")
    def delete_model(mid: int, request: Request, u: dict = Depends(engineer)):
        visible_model(u, plat.model(mid), "contributor")
        if store.one("SELECT id FROM queries WHERE model_id=?", (mid,)):
            raise HTTPException(409, "Saved reports use this model. Delete or change them first.")
        store.exec("DELETE FROM semantic_models WHERE id=?", (mid,))
        audit(u, "model.deleted", str(mid), None, request)
        return {"ok": True}

    @app.post("/api/models/{mid}/query")
    def query_model(mid: int, b: dict = Body(...), u: dict = Depends(viewer)):
        m = visible_model(u, plat.model(mid))
        res = plat.run_model(u, m, b.get("spec") or {}, compare=bool(b.get("compare")))
        if b.get("forecast") or b.get("anomalies"):
            from .insights import analyze_result
            res["analysis"] = analyze_result(m, res, forecast=b.get("forecast"), anomalies=bool(b.get("anomalies")))
        return res

    @app.get("/api/models/{mid}/values")
    def model_values(mid: int, field: str, q: str = "", u: dict = Depends(viewer)):
        m = visible_model(u, plat.model(mid))
        spec = {"dimensions": [field], "limit": 500, "sort": [{"field": field, "dir": "asc"}]}
        if q:
            spec["filters"] = [{"field": field, "op": "contains", "value": q}]
        res = plat.run_model(u, m, spec)
        return [r[0] for r in res["rows"] if r[0] is not None]

    @app.post("/api/models/{mid}/export")
    def export_model(mid: int, b: dict = Body(...), u: dict = Depends(viewer)):
        m = visible_model(u, plat.model(mid))
        res = plat.run_model(u, m, {**(b.get("spec") or {}), "limit": 50_000})
        fmt = b.get("format", "csv")
        audit(u, "export.model", m["name"], {"rows": res["row_count"], "format": fmt})
        return file_response(to_xlsx([(m["name"], res)]) if fmt == "xlsx" else to_csv(res), b.get("name") or m["name"], fmt)

    # ------------------------------------------------------------------ exports
    @app.post("/api/queries/{qid}/export")
    def export_query(qid: int, b: dict = Body(default={}), u: dict = Depends(viewer)):
        q = ws.item(store, u, "query", qid)
        res = plat.run_saved(u, q, {**(b or {}), "limit": 50_000})
        fmt = (b or {}).get("format", "csv")
        audit(u, "export.query", q["name"], {"rows": res["row_count"], "format": fmt})
        return file_response(to_xlsx([(q["name"], res)]) if fmt == "xlsx" else to_csv(res), q["name"], fmt)

    @app.post("/api/sql/export")
    def export_sql(b: dict = Body(...), u: dict = Depends(analyst)):
        res = plat.user_query(u, b.get("sql", ""), limit=plat.settings.max_result_rows * 10)
        fmt = b.get("format", "csv")
        audit(u, "export.sql", "", {"rows": res["row_count"], "format": fmt})
        return file_response(to_xlsx([("Query", res)]) if fmt == "xlsx" else to_csv(res), "query", fmt)

    @app.post("/api/dashboards/{did}/export")
    def export_dashboard(did: int, b: dict = Body(default={}), u: dict = Depends(viewer)):
        d = ws.item(store, u, "dashboard", did)
        results = dashboard_results(plat, u, d, (b or {}).get("filters") or [])
        audit(u, "export.dashboard", d["name"], {"tiles": len(results)})
        return file_response(to_xlsx([(t, r) for t, _, r in results if not r.get("error")], d["name"]), d["name"], "xlsx")

    # ------------------------------------------------------------------ subscriptions
    def sub_body(b, u, old=None):
        old = old or {}
        rec = b.get("recipients", old.get("recipients") or [])
        if isinstance(rec, str):
            rec = [x.strip() for x in re.split(r"[,;\s]+", rec) if x.strip()]
        if not rec or not all(EMAIL.match(r) for r in rec):
            raise HTTPException(400, "Enter one or more valid email addresses.")
        sched = b.get("schedule", old.get("schedule") or "0 8 * * 1")
        validate_cron(sched)
        did = int(b.get("dashboard_id", old.get("dashboard_id") or 0))
        if not store.one("SELECT id FROM dashboards WHERE id=?", (did,)):
            raise HTTPException(400, "Dashboard not found.")
        ws.item(store, u, "dashboard", did)
        attach = b.get("attach", old.get("attach") or "xlsx")
        if attach not in ("xlsx", "csv", "none"):
            raise HTTPException(400, "Attachment must be xlsx, csv or none.")
        from .orchestrator import next_run
        return {"name": (b.get("name") or old.get("name") or "Report")[:120], "dashboard_id": did, "recipients": json.dumps(rec), "schedule": sched,
                "attach": attach, "filters": json.dumps(b.get("filters", old.get("filters") or [])), "enabled": int(b.get("enabled", old.get("enabled", 1))),
                "include_brief": 1 if b.get("include_brief", old.get("include_brief", 0)) else 0,
                "next_run_at": next_run(sched)}

    def own(table, rid, u):
        r = store.one(f"SELECT * FROM {table} WHERE id=?", (rid,))
        if not r or (r["owner_id"] != u["id"] and u["role"] != "admin"):
            raise NotFound("Not found")
        return r

    @app.get("/api/subscriptions")
    def subscriptions(u: dict = Depends(analyst)):
        q = "SELECT s.*, d.name dashboard, u.email owner FROM subscriptions s JOIN dashboards d ON d.id=s.dashboard_id JOIN users u ON u.id=s.owner_id"
        return store.all(q + " ORDER BY s.id DESC") if u["role"] == "admin" else store.all(q + " WHERE s.owner_id=? ORDER BY s.id DESC", (u["id"],))

    @app.post("/api/subscriptions")
    def add_subscription(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        sid = store.insert("subscriptions", {**sub_body(b, u), "owner_id": u["id"], "created_at": now()})
        audit(u, "subscription.created", b.get("name", ""), {"dashboard_id": b.get("dashboard_id")}, request)
        return store.one("SELECT * FROM subscriptions WHERE id=?", (sid,))

    @app.put("/api/subscriptions/{sid}")
    def edit_subscription(sid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        old = own("subscriptions", sid, u)
        store.update("subscriptions", sid, sub_body(b, u, old))
        audit(u, "subscription.updated", old["name"], None, request)
        return store.one("SELECT * FROM subscriptions WHERE id=?", (sid,))

    @app.delete("/api/subscriptions/{sid}")
    def delete_subscription(sid: int, request: Request, u: dict = Depends(analyst)):
        own("subscriptions", sid, u)
        store.exec("DELETE FROM subscriptions WHERE id=?", (sid,))
        audit(u, "subscription.deleted", str(sid), None, request)
        return {"ok": True}

    @app.post("/api/subscriptions/{sid}/send")
    def send_subscription(sid: int, request: Request, u: dict = Depends(analyst)):
        s = own("subscriptions", sid, u)
        r = reporter.run_subscription(s)
        audit(u, "subscription.sent", s["name"], {"status": r["status"]}, request)
        if r["status"] != "sent":
            raise HTTPException(400, r.get("error", "Sending failed"))
        return r

    # ------------------------------------------------------------------ alerts & notifications
    def alert_body(b, u, old=None):
        old = old or {}
        src = b.get("source", old.get("source") or {})
        t = src.get("type")
        if t not in ALERT_SOURCES:
            raise HTTPException(400, f"Alert source must be one of {', '.join(ALERT_SOURCES)}.")
        if t == "query" and not store.one("SELECT id FROM queries WHERE id=?", (src.get("query_id"),)):
            raise HTTPException(400, "Saved query not found.")
        if t == "query":
            ws.item(store, u, "query", int(src["query_id"]))
        if t == "model":
            m = visible_model(u, plat.model(int(src.get("model_id") or 0)))
            if src.get("measure") not in {x["name"] for x in m["measures"]}:
                raise HTTPException(400, "Choose a measure of the model.")
        if t in ("quality", "freshness"):
            plat.dataset_by_id(int(src.get("dataset_id") or 0))
            if not has_role(u["role"], "engineer"):
                raise HTTPException(403, "Quality and freshness alerts need the engineer role.")
        if t == "pipeline" and not store.one("SELECT id FROM pipelines WHERE id=?", (src.get("pipeline_id"),)):
            raise HTTPException(400, "Pipeline not found.")
        cond = b.get("condition", old.get("condition") or {})
        if t != "pipeline":
            if cond.get("op") not in ALERT_OPS:
                raise HTTPException(400, "Condition operator must be one of > >= < <= = !=.")
            try:
                float(cond.get("threshold"))
            except (TypeError, ValueError):
                raise HTTPException(400, "Threshold must be a number.") from None
        ch = b.get("channels", old.get("channels") or {"inapp": True})
        if isinstance(ch.get("email"), str):
            ch["email"] = [x.strip() for x in re.split(r"[,;\s]+", ch["email"]) if x.strip()]
        if ch.get("email") and not all(EMAIL.match(e) for e in ch["email"]):
            raise HTTPException(400, "Alert email addresses are not valid.")
        if ch.get("webhook") and not str(ch["webhook"]).startswith(("http://", "https://")):
            raise HTTPException(400, "Webhook must be an http(s) URL.")
        sched = b.get("schedule", old.get("schedule") or "*/15 * * * *")
        validate_cron(sched)
        from .orchestrator import next_run
        return {"name": (b.get("name") or old.get("name") or "Alert")[:120], "source": json.dumps(src), "condition": json.dumps(cond),
                "channels": json.dumps(ch), "schedule": sched, "enabled": int(b.get("enabled", old.get("enabled", 1))), "next_run_at": next_run(sched)}

    @app.get("/api/alerts")
    def alerts(u: dict = Depends(analyst)):
        q = "SELECT a.*, u.email owner FROM alerts a JOIN users u ON u.id=a.owner_id"
        return store.all(q + " ORDER BY a.id DESC") if u["role"] == "admin" else store.all(q + " WHERE a.owner_id=? ORDER BY a.id DESC", (u["id"],))

    @app.post("/api/alerts")
    def add_alert(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        aid = store.insert("alerts", {**alert_body(b, u), "owner_id": u["id"], "created_at": now()})
        a = store.one("SELECT * FROM alerts WHERE id=?", (aid,))
        audit(u, "alert.created", a["name"], {"source": a["source"]}, request)
        reporter.evaluate(a)
        return store.one("SELECT * FROM alerts WHERE id=?", (aid,))

    @app.put("/api/alerts/{aid}")
    def edit_alert(aid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        old = own("alerts", aid, u)
        store.update("alerts", aid, alert_body(b, u, old))
        audit(u, "alert.updated", old["name"], None, request)
        return store.one("SELECT * FROM alerts WHERE id=?", (aid,))

    @app.delete("/api/alerts/{aid}")
    def delete_alert(aid: int, request: Request, u: dict = Depends(analyst)):
        own("alerts", aid, u)
        store.exec("DELETE FROM alerts WHERE id=?", (aid,))
        audit(u, "alert.deleted", str(aid), None, request)
        return {"ok": True}

    @app.post("/api/alerts/{aid}/check")
    def check_alert(aid: int, u: dict = Depends(analyst)):
        return reporter.evaluate(own("alerts", aid, u))

    @app.get("/api/notifications")
    def notifications(u: dict = Depends(viewer)):
        return {"unread": store.scalar("SELECT count(*) FROM notifications WHERE user_id=? AND read=0", (u["id"],)),
                "items": store.all("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))}

    @app.post("/api/notifications/read")
    def read_notifications(b: dict = Body(default={}), u: dict = Depends(viewer)):
        ids = (b or {}).get("ids")
        if ids:
            store.exec(f"UPDATE notifications SET read=1 WHERE user_id=? AND id IN ({','.join('?' for _ in ids)})", (u["id"], *[int(i) for i in ids]))
        else:
            store.exec("UPDATE notifications SET read=1 WHERE user_id=?", (u["id"],))
        return {"ok": True}

    # ------------------------------------------------------------------ email settings
    @app.get("/api/admin/email")
    def get_email(u: dict = Depends(admin)):
        c = store.get_setting("smtp") or {}
        return {k: v for k, v in c.items() if k != "secret_blob"} | {"password_set": bool(c.get("secret_blob"))}

    @app.put("/api/admin/email")
    def put_email(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        old = store.get_setting("smtp") or {}
        c = {k: b.get(k, old.get(k)) for k in ("host", "port", "username", "from", "starttls", "ssl")}
        c["secret_blob"] = plat.vault.encrypt({"password": b["password"]}) if b.get("password") else old.get("secret_blob")
        store.set_setting("smtp", c)
        audit(u, "email.updated", c.get("host") or "", None, request)
        return {"ok": True}

    @app.post("/api/admin/email/test")
    def test_email(b: dict = Body(...), u: dict = Depends(admin)):
        to = b.get("to") or u["email"]
        reporter.mailer.send([to], "Tessera test message", "<p>Email from Tessera is working.</p>")
        return {"ok": True, "to": to}

    # ------------------------------------------------------------------ row-level security
    @app.get("/api/rls")
    def rls(u: dict = Depends(admin)):
        return {"policies": store.all("SELECT * FROM rls_policies ORDER BY dataset, id"),
                "users": [{"id": x["id"], "email": x["email"], "role": x["role"], "attributes": x["attributes"]} for x in store.all("SELECT * FROM users ORDER BY email")]}

    @app.post("/api/rls")
    def add_rls(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        ds, col, attr = (b.get("dataset") or "").replace("serving.", ""), b.get("column_name"), (b.get("attribute") or "").strip()
        if not plat.wh.exists("serving", ds):
            raise HTTPException(400, "Row-level security applies to published (serving) datasets.")
        if col not in {c["name"] for c in plat.wh.columns("serving", ds)}:
            raise HTTPException(400, "Column not found in that dataset.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", attr):
            raise HTTPException(400, "Attribute names use letters, digits and underscores.")
        pid = store.insert("rls_policies", {"dataset": ds, "column_name": col, "attribute": attr, "description": b.get("description", ""),
                                            "created_by": u["email"], "created_at": now()})
        audit(u, "rls.created", ds, {"column": col, "attribute": attr}, request)
        return store.one("SELECT * FROM rls_policies WHERE id=?", (pid,))

    @app.delete("/api/rls/{pid}")
    def delete_rls(pid: int, request: Request, u: dict = Depends(admin)):
        store.exec("DELETE FROM rls_policies WHERE id=?", (pid,))
        audit(u, "rls.deleted", str(pid), None, request)
        return {"ok": True}

    @app.put("/api/users/{uid}/attributes")
    def set_attributes(uid: int, request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        t = store.one("SELECT * FROM users WHERE id=?", (uid,))
        if not t:
            raise NotFound("User not found")
        attrs = {}
        for k, v in (b.get("attributes") or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", k):
                raise HTTPException(400, f"Bad attribute name '{k}'.")
            vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
            attrs[k] = [str(x)[:200] for x in vals][:500]
        store.update("users", uid, {"attributes": json.dumps(attrs)})
        audit(u, "user.attributes", t["email"], attrs, request)
        return {"ok": True, "attributes": attrs}

    # ------------------------------------------------------------------ catalog governance & glossary
    @app.patch("/api/datasets/{did}/governance")
    def governance(did: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        d = plat.dataset_by_id(did)
        patch = {}
        if "endorsement" in b:
            if b["endorsement"] not in ("none", "promoted", "certified"):
                raise HTTPException(400, "Endorsement is none, promoted or certified.")
            if b["endorsement"] == "certified" and not has_role(u["role"], "admin"):
                raise HTTPException(403, "Only admins can certify datasets.")
            patch["endorsement"] = b["endorsement"]
        if "domain" in b:
            patch["domain"] = (str(b["domain"]).strip() or None) if b["domain"] else None
        store.update("datasets", did, patch)
        audit(u, "dataset.governance", f"{d['schema_name']}.{d['name']}", patch, request)
        return plat.dataset_by_id(did)

    @app.get("/api/glossary")
    def glossary(u: dict = Depends(viewer)):
        return store.all("SELECT * FROM glossary ORDER BY term")

    def term_body(b):
        if not (b.get("term") or "").strip() or not (b.get("definition") or "").strip():
            raise HTTPException(400, "A term and a definition are required.")
        cols = b.get("columns") or []
        for c in cols:
            if len(str(c).split(".")) != 3:
                raise HTTPException(400, f"Link columns as schema.table.column ('{c}').")
        syn = b.get("synonyms") or []
        if isinstance(syn, str):
            syn = [x.strip() for x in syn.split(",") if x.strip()]
        return {"term": b["term"].strip()[:120], "definition": b["definition"][:4000], "owner": b.get("owner"), "synonyms": json.dumps(syn),
                "columns": json.dumps(cols), "domain": b.get("domain"), "updated_at": now()}

    @app.post("/api/glossary")
    def add_term(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        row = term_body(b)
        if store.one("SELECT id FROM glossary WHERE term=?", (row["term"],)):
            raise HTTPException(409, "That term already exists.")
        gid = store.insert("glossary", {**row, "created_at": now()})
        audit(u, "glossary.created", row["term"], None, request)
        return store.one("SELECT * FROM glossary WHERE id=?", (gid,))

    @app.put("/api/glossary/{gid}")
    def edit_term(gid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        store.update("glossary", gid, term_body(b))
        audit(u, "glossary.updated", b.get("term", ""), None, request)
        return store.one("SELECT * FROM glossary WHERE id=?", (gid,))

    @app.delete("/api/glossary/{gid}")
    def delete_term(gid: int, request: Request, u: dict = Depends(engineer)):
        store.exec("DELETE FROM glossary WHERE id=?", (gid,))
        audit(u, "glossary.deleted", str(gid), None, request)
        return {"ok": True}

    # ------------------------------------------------------------------ visual transforms
    @app.get("/api/transforms/ops")
    def transform_ops(u: dict = Depends(engineer)):
        return {"ops": transforms.OP_HELP, "cast_types": list(transforms.CAST_TYPES), "filters": sorted(transforms.FILTER_OPS), "aggs": sorted(transforms.AGG_FNS)}

    @app.post("/api/transforms/preview")
    def transform_preview(b: dict = Body(...), u: dict = Depends(engineer)):
        sql = transforms.compile_steps(b.get("source") or "", b.get("steps") or [])
        res = plat.user_query(u, sql, limit=100)
        return {"sql": sql, "result": res}

    @app.post("/api/transforms/run")
    def transform_run(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        sql = transforms.compile_steps(b.get("source") or "", b.get("steps") or [])
        res = plat.transform(sql, b.get("target") or "")
        audit(u, "transform.run", res["dataset"], {"rows": res["row_count"], "steps": len(b.get("steps") or [])}, request)
        return {**res, "sql": sql}

    # ------------------------------------------------------------------ notebooks
    @app.get("/api/notebooks")
    def notebooks(workspace: int | None = None, u: dict = Depends(analyst)):
        return ws.filter_rows(store, u, store.all("SELECT id, name, workspace_id, created_by, created_at, updated_at, json_array_length(cells) AS cell_count FROM notebooks ORDER BY updated_at DESC"), workspace)

    @app.get("/api/notebooks/{nid}")
    def notebook(nid: int, u: dict = Depends(analyst)):
        return ws.item(store, u, "notebook", nid)

    def cells_ok(cells):
        if not isinstance(cells, list) or len(cells) > 300:
            raise HTTPException(400, "A notebook holds up to 300 cells.")
        for c in cells:
            if c.get("type") not in ("sql", "markdown"):
                raise HTTPException(400, "Cells are sql or markdown.")
            if len(str(c.get("source", ""))) > 100_000:
                raise HTTPException(400, "A cell is too large.")
        return json.dumps([{k: c.get(k) for k in ("id", "type", "source", "viz")} for c in cells])

    @app.post("/api/notebooks")
    def add_notebook(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        cells = b.get("cells") or [{"id": "c1", "type": "markdown", "source": "# Notes\nDescribe the question this notebook answers."},
                                   {"id": "c2", "type": "sql", "source": "select 1 as answer"}]
        wid = ws.target_ws(store, u, b.get("workspace_id"))
        nid = store.insert("notebooks", {"name": (b.get("name") or "Untitled notebook")[:120], "cells": cells_ok(cells), "owner_id": u["id"], "workspace_id": wid,
                                         "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "notebook.created", b.get("name", ""), None, request)
        return store.one("SELECT * FROM notebooks WHERE id=?", (nid,))

    @app.put("/api/notebooks/{nid}")
    def save_notebook(nid: int, b: dict = Body(...), u: dict = Depends(analyst)):
        n = ws.item(store, u, "notebook", nid, "contributor")
        store.update("notebooks", nid, {"name": (b.get("name") or n["name"])[:120], "cells": cells_ok(b.get("cells", n["cells"])), "updated_at": now()})
        return store.one("SELECT * FROM notebooks WHERE id=?", (nid,))

    @app.delete("/api/notebooks/{nid}")
    def delete_notebook(nid: int, request: Request, u: dict = Depends(analyst)):
        n = ws.item(store, u, "notebook", nid, "contributor")
        if n and n["owner_id"] != u["id"] and ws.role_in(store, u, n["workspace_id"]) != "admin":
            raise HTTPException(403, "Only the owner or an admin can delete this notebook.")
        store.exec("DELETE FROM notebooks WHERE id=?", (nid,))
        audit(u, "notebook.deleted", str(nid), None, request)
        return {"ok": True}

    # ------------------------------------------------------------------ push ingestion
    @app.post("/api/push/{target}")
    def push(target: str, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        schema, name = split_fqn(target, "raw")
        if schema != "raw":
            raise HTTPException(400, "Push endpoints write to the raw zone.")
        recs = b.get("records")
        if isinstance(b, list):
            recs = b
        if not isinstance(recs, list) or not recs:
            raise HTTPException(400, "Send {\"records\": [ {...}, ... ]}.")
        if len(recs) > 50_000:
            raise HTTPException(413, "Send at most 50,000 records per request.")
        if b.get("stamp", True):
            ts = now()
            recs = [{**r, "_received_at": ts} if isinstance(r, dict) else {"value": r, "_received_at": ts} for r in recs]
        tbl = connectors.normalize_records(recs)
        try:
            plat.wh.load_arrow(tbl, "raw", name, "append")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Records do not match the table: {str(e).splitlines()[0]}") from None
        d = plat.dataset("raw", name, create=True)
        store.update("datasets", d["id"], {"last_loaded_at": now(), "source": json.dumps({"push": True})})
        plat.refresh_columns(plat.dataset_by_id(d["id"]))
        plat.add_lineage("push:api", f"raw.{name}", "ingest", None)
        audit(u, "push.ingested", f"raw.{name}", {"records": len(recs)}, request)
        return {"dataset": f"raw.{name}", "accepted": len(recs), "row_count": plat.wh.row_count("raw", name)}

    # ------------------------------------------------------------------ monitoring hub
    @app.get("/api/monitor")
    def monitor(days: int = 14, u: dict = Depends(analyst)):
        since = (datetime.now(timezone.utc) - timedelta(days=min(days, 90))).isoformat()
        runs = store.all("SELECT r.id, r.pipeline_id, p.name pipeline, r.status, r.trigger, r.triggered_by, r.created_at, r.started_at, r.finished_at, r.error "
                         "FROM pipeline_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE r.created_at>=? ORDER BY r.id DESC LIMIT 500", (since,))
        for r in runs:
            if r["started_at"] and r["finished_at"]:
                r["seconds"] = round((datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds(), 3)
        per = {}
        for r in runs:
            p = per.setdefault(r["pipeline"], {"pipeline": r["pipeline"], "pipeline_id": r["pipeline_id"], "runs": 0, "failed": 0, "seconds": []})
            p["runs"] += 1
            p["failed"] += r["status"] == "failed"
            if r.get("seconds") is not None:
                p["seconds"].append(r["seconds"])
        stats = [{**{k: v for k, v in p.items() if k != "seconds"}, "success_rate": round(1 - p["failed"] / p["runs"], 4) if p["runs"] else None,
                  "avg_seconds": round(sum(p["seconds"]) / len(p["seconds"]), 3) if p["seconds"] else None} for p in per.values()]
        queries = []
        for a in store.all("SELECT ts, user_email, detail FROM audit WHERE action='sql.run' AND ts>=? ORDER BY id DESC LIMIT 2000", (since,)):
            d = a["detail"] or {}
            queries.append({"ts": a["ts"], "user": a["user_email"], "ms": d.get("ms"), "rows": d.get("rows"), "sql": (d.get("sql") or "")[:200]})
        slow = sorted([q for q in queries if q["ms"] is not None], key=lambda q: -q["ms"])[:10]
        storage = []
        if has_role(u["role"], "engineer"):
            storage = [{"table": f"{t['schema']}.{t['name']}", "rows": t["estimated_rows"], "columns": t["columns"]} for t in plat.wh.list_tables()]
        return {
            "runs": runs, "pipelines": stats, "slow_queries": slow, "query_count": len(queries),
            "alerts": store.all("SELECT id, name, state, last_value, last_checked, last_error FROM alerts ORDER BY CASE state WHEN 'firing' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, name"),
            "deliveries": store.all("SELECT * FROM deliveries WHERE ts>=? ORDER BY id DESC LIMIT 100", (since,)),
            "storage": storage, "warehouse_bytes": sum(f.stat().st_size for f in plat.settings.data_dir.glob("warehouse.duckdb*") if f.is_file()),
        }

    # ------------------------------------------------------------------ deployment bundles
    @app.get("/api/bundle/export")
    def export_bundle(request: Request, u: dict = Depends(admin)):
        b = bundle.export_bundle(plat)
        audit(u, "bundle.exported", "", {k: len(v) for k, v in b.items() if isinstance(v, list)}, request)
        return Response(json.dumps(b, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="tessera-bundle-{datetime.now(timezone.utc):%Y%m%d}.json"'})

    @app.post("/api/bundle/import")
    def import_bundle(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        mode = b.get("mode", "skip")
        if mode not in ("skip", "overwrite"):
            raise HTTPException(400, "Mode is skip or overwrite.")
        try:
            res = bundle.import_bundle(plat, b.get("bundle") or {}, mode, u["email"])
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f"Bundle could not be imported: {e}") from None
        audit(u, "bundle.imported", mode, res["report"], request)
        return res

    # ------------------------------------------------------------------ workspaces
    @app.get("/api/workspaces")
    def workspaces(u: dict = Depends(viewer)):
        vis = ws.visible_ids(store, u)
        out = []
        for w in store.all("SELECT * FROM workspaces ORDER BY id"):
            if w["id"] not in vis:
                continue
            counts = {k: store.scalar(f"SELECT count(*) FROM {t} WHERE workspace_id=?", (w["id"],)) for k, t in ws.ITEM_TABLES.items()}
            out.append({**w, "my_role": ws.role_in(store, u, w["id"]), "counts": counts,
                        "members": store.scalar("SELECT count(*) FROM workspace_members WHERE workspace_id=?", (w["id"],))})
        return out

    def ws_body(b, old=None):
        old = old or {}
        name = (b.get("name") or old.get("name") or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "Give the workspace a name (up to 80 characters).")
        vis = b.get("visibility", old.get("visibility") or "private")
        if vis not in ("org", "private"):
            raise HTTPException(400, "Visibility is org or private.")
        return {"name": name, "description": (b.get("description", old.get("description")) or "")[:500], "visibility": vis}

    @app.post("/api/workspaces")
    def add_workspace(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        row = ws_body(b)
        if store.one("SELECT id FROM workspaces WHERE name=?", (row["name"],)):
            raise HTTPException(409, "A workspace with that name exists.")
        wid = store.insert("workspaces", {**row, "created_by": u["email"], "created_at": now()})
        store.insert("workspace_members", {"workspace_id": wid, "user_id": u["id"], "role": "admin"})
        audit(u, "workspace.created", row["name"], row, request)
        return store.one("SELECT * FROM workspaces WHERE id=?", (wid,))

    @app.put("/api/workspaces/{wid}")
    def edit_workspace(wid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        ws.require(store, u, wid, "admin")
        old = store.one("SELECT * FROM workspaces WHERE id=?", (wid,))
        row = ws_body(b, old)
        if wid == 1 and row["visibility"] != "org":
            raise HTTPException(400, "The General workspace stays organization-wide.")
        store.update("workspaces", wid, row)
        audit(u, "workspace.updated", row["name"], row, request)
        return store.one("SELECT * FROM workspaces WHERE id=?", (wid,))

    @app.delete("/api/workspaces/{wid}")
    def delete_workspace(wid: int, request: Request, u: dict = Depends(analyst)):
        ws.require(store, u, wid, "admin")
        if wid == 1:
            raise HTTPException(400, "The General workspace cannot be deleted.")
        if any(store.scalar(f"SELECT count(*) FROM {t} WHERE workspace_id=?", (wid,)) for t in ws.ITEM_TABLES.values()):
            raise HTTPException(409, "Move or delete the workspace's dashboards, reports, notebooks and models first.")
        store.exec("DELETE FROM workspaces WHERE id=?", (wid,))
        audit(u, "workspace.deleted", str(wid), None, request)
        return {"ok": True}

    @app.get("/api/workspaces/{wid}/members")
    def ws_members(wid: int, u: dict = Depends(viewer)):
        ws.require(store, u, wid, "viewer")
        return store.all("SELECT m.user_id, m.role, m.source, u.email, u.name FROM workspace_members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.email", (wid,))

    @app.put("/api/workspaces/{wid}/members")
    def set_member(wid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        ws.require(store, u, wid, "admin")
        target = store.one("SELECT * FROM users WHERE email=?", ((b.get("email") or "").strip().lower(),)) if b.get("email") else store.one("SELECT * FROM users WHERE id=?", (b.get("user_id"),))
        if not target:
            raise HTTPException(404, "No user with that email. Add them under Admin → Users first, or provision them through SCIM.")
        role = b.get("role")
        if role is None:
            if target["id"] == u["id"] and u["role"] != "admin":
                raise HTTPException(400, "You cannot remove yourself; ask another workspace admin.")
            store.exec("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (wid, target["id"]))
        else:
            if role not in ws.WS_ROLES:
                raise HTTPException(400, "Role is viewer, contributor or admin.")
            store.exec("INSERT INTO workspace_members(workspace_id,user_id,role,source) VALUES(?,?,?,'manual') "
                       "ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role, source='manual'", (wid, target["id"], role))
        audit(u, "workspace.member", str(wid), {"user": target["email"], "role": role}, request)
        return {"ok": True}

    # ------------------------------------------------------------------ saved dashboard views
    @app.get("/api/dashboards/{did}/views")
    def views(did: int, u: dict = Depends(viewer)):
        ws.item(store, u, "dashboard", did)
        return store.all("SELECT v.*, u.email owner FROM dashboard_views v JOIN users u ON u.id=v.user_id WHERE dashboard_id=? AND (user_id=? OR shared=1) ORDER BY v.name",
                         (did, u["id"]))

    @app.post("/api/dashboards/{did}/views")
    def add_view(did: int, b: dict = Body(...), u: dict = Depends(viewer)):
        ws.item(store, u, "dashboard", did)
        name = (b.get("name") or "").strip()[:80]
        if not name:
            raise HTTPException(400, "Name the view.")
        state = b.get("state") or {}
        if len(json.dumps(state)) > 20000:
            raise HTTPException(400, "View state is too large.")
        vid = store.insert("dashboard_views", {"dashboard_id": did, "user_id": u["id"], "name": name, "state": json.dumps(state),
                                               "shared": 1 if b.get("shared") else 0, "created_at": now()})
        return store.one("SELECT * FROM dashboard_views WHERE id=?", (vid,))

    @app.delete("/api/dashboards/{did}/views/{vid}")
    def delete_view(did: int, vid: int, u: dict = Depends(viewer)):
        v = store.one("SELECT * FROM dashboard_views WHERE id=? AND dashboard_id=?", (vid, did))
        if not v or (v["user_id"] != u["id"] and u["role"] != "admin"):
            raise NotFound("View not found")
        store.exec("DELETE FROM dashboard_views WHERE id=?", (vid,))
        return {"ok": True}

    # ------------------------------------------------------------------ search
    @app.get("/api/search")
    def search(q: str, u: dict = Depends(viewer)):
        term = q.strip().lower()
        if len(term) < 2:
            return []
        out = []

        def hit(*texts):
            return any(term in (t or "").lower() for t in texts)
        for d in store.all("SELECT * FROM datasets"):
            if restricted(u["role"]) and d["schema_name"] != "serving":
                continue
            cols = [c["name"] for c in d["columns"] or [] if hit(c["name"], c.get("description"))]
            if hit(d["name"], d["description"], d.get("domain")) or cols:
                out.append({"kind": "dataset", "id": d["id"], "title": f"{d['schema_name']}.{d['name']}", "detail": (", ".join(cols[:5]) and f"columns: {', '.join(cols[:5])}") or d["description"][:120],
                            "link": f"#/catalog/{d['id']}", "badge": d.get("endorsement")})
        vis = ws.visible_ids(store, u)
        for m in [x for x in store.all("SELECT * FROM semantic_models") if x["workspace_id"] in vis]:
            if restricted(u["role"]) and not m["base"].startswith("serving."):
                continue
            meas = [x["label"] for x in m["measures"] + m["dimensions"] if hit(x["label"], x["name"], x.get("description"))]
            if hit(m["name"], m["description"]) or meas:
                out.append({"kind": "model", "id": m["id"], "title": m["name"], "detail": ", ".join(meas[:6]) or m["description"][:120], "link": f"#/explore/{m['id']}", "badge": m["endorsement"]})
        for d in [x for x in store.all("SELECT id, name, description, workspace_id FROM dashboards") if x["workspace_id"] in vis]:
            if hit(d["name"], d["description"]):
                out.append({"kind": "dashboard", "id": d["id"], "title": d["name"], "detail": d["description"][:120], "link": f"#/dashboards/{d['id']}"})
        for x in [y for y in store.all("SELECT id, name, workspace_id FROM queries") if y["workspace_id"] in vis]:
            if hit(x["name"]):
                out.append({"kind": "report", "id": x["id"], "title": x["name"], "detail": "", "link": f"#/reports/{x['id']}"})
        for x in store.all("SELECT id, name, description FROM pipelines"):
            if hit(x["name"], x["description"]):
                out.append({"kind": "pipeline", "id": x["id"], "title": x["name"], "detail": x["description"][:120], "link": f"#/pipelines/{x['id']}"})
        for g in store.all("SELECT * FROM glossary"):
            if hit(g["term"], g["definition"], " ".join(g["synonyms"] or [])):
                out.append({"kind": "term", "id": g["id"], "title": g["term"], "detail": g["definition"][:140], "link": "#/glossary"})
        if has_role(u["role"], "analyst"):
            for n in [y for y in store.all("SELECT id, name, workspace_id FROM notebooks") if y["workspace_id"] in vis]:
                if hit(n["name"]):
                    out.append({"kind": "notebook", "id": n["id"], "title": n["name"], "detail": "", "link": f"#/notebooks/{n['id']}"})
        order = {"model": 0, "dashboard": 1, "dataset": 2, "term": 3, "report": 4, "notebook": 5, "pipeline": 6}
        return sorted(out, key=lambda r: (order.get(r["kind"], 9), r["badge"] != "certified" if r.get("badge") else True, r["title"]))[:40]

    _ = (QueryError,)
