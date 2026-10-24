"""HTTP endpoints for automated insights, explain-change, decomposition trees and dashboard Q&A."""
from __future__ import annotations

import json
from datetime import date

from fastapi import Body, Depends, HTTPException, Request

from . import agents, insights
from . import workspaces as ws
from .errors import public_message
from .security import restricted
from .store import local_today, now
from .warehouse import QueryError


def register(app, plat, user, need, audit):
    store = plat.store
    viewer, analyst = need("viewer"), need("analyst")

    def visible_model(u, mid):
        m = plat.model(int(mid))
        ws.require(store, u, m.get("workspace_id") or 1, "viewer")
        if restricted(u["role"]) and not m["base"].startswith("serving."):
            raise HTTPException(403, "This model is built on unpublished data.")
        return m

    import copy
    import time as _time
    from collections import OrderedDict
    memo_store: "OrderedDict[str, tuple[float, object]]" = OrderedDict()

    def memo(kind, u, args, fn):
        """Cache heavy analyses per user and data version; any warehouse write invalidates them (wh.version changes)."""
        key = json.dumps([kind, u["id"], u["role"], plat.wh.version, local_today().isoformat(), args], default=str, sort_keys=True)
        hit = memo_store.get(key)
        if hit and _time.monotonic() - hit[0] < 600:
            memo_store.move_to_end(key)
            return copy.deepcopy(hit[1])
        out = fn()
        memo_store[key] = (_time.monotonic(), copy.deepcopy(out))
        while len(memo_store) > 128:
            memo_store.popitem(last=False)
        return out
    app.state.analysis_memo = memo_store

    def _today(b):
        return date.fromisoformat(b["today"]) if b.get("today") else None

    @app.post("/api/models/{mid}/insights")
    def model_insights(mid: int, b: dict = Body(default={}), u: dict = Depends(viewer)):
        m = visible_model(u, mid)
        return insights.model_insights(plat, u, m, b.get("measures"), b.get("grain") or "month", b.get("filters"),
                                       horizon=max(1, min(int(b.get("horizon") or 3), 12)), today=_today(b))

    @app.post("/api/models/{mid}/explain")
    def explain(mid: int, b: dict = Body(...), u: dict = Depends(viewer)):
        m = visible_model(u, mid)
        if not b.get("measure"):
            raise HTTPException(400, "Pick a measure.")
        return insights.explain_change(plat, u, m, b["measure"], b.get("grain") or "month", b.get("filters"),
                                       b.get("current"), b.get("baseline"), b.get("dimensions"), top=int(b.get("top") or 5))

    @app.post("/api/models/{mid}/decompose")
    def decompose(mid: int, b: dict = Body(...), u: dict = Depends(viewer)):
        m = visible_model(u, mid)
        if not b.get("measure"):
            raise HTTPException(400, "Pick a measure.")
        if b.get("mode", "high") not in ("high", "low"):
            raise HTTPException(400, "mode is high or low.")
        return insights.decompose(plat, u, m, b["measure"], b.get("path"), b.get("dimension"), b.get("mode", "high"), b.get("filters"))

    # ------------------------------------------------------------ dashboards
    def dashboard_scope(u, did):
        d = ws.item(store, u, "dashboard", did)
        scope: dict[int, list[str]] = {}
        for t in d.get("tiles") or []:
            q = store.one("SELECT * FROM queries WHERE id=?", (t.get("query_id"),)) if t.get("query_id") else None
            if not q or q.get("kind") != "model" or ws.role_in(store, u, q.get("workspace_id") or 1) is None:
                continue
            for mn in (q.get("spec") or {}).get("measures") or []:
                scope.setdefault(q["model_id"], [])
                if mn not in scope[q["model_id"]]:
                    scope[q["model_id"]].append(mn)
        return d, scope

    def dash_filters(filters, mid):
        return [{k: v for k, v in f.items() if k != "model_id"} for f in filters or [] if f.get("model_id") in (None, mid)]

    @app.post("/api/dashboards/{did}/insights")
    def dashboard_insights(did: int, request: Request, b: dict = Body(default={}), u: dict = Depends(viewer)):
        d, scope = dashboard_scope(u, did)
        facts, models, errors = [], [], []
        for mid, meas in list(scope.items())[:3]:
            try:
                m = visible_model(u, mid)
                dims = {x["name"] for x in m["dimensions"]}
                flt = [f for f in dash_filters(b.get("filters"), mid) if f.get("field") in dims]
                r = memo("insights", u, [mid, meas[:5], b.get("grain"), flt, b.get("today")],
                         lambda: insights.model_insights(plat, u, m, meas[:5], b.get("grain") or "month", flt, today=_today(b)))  # noqa: B023 (memo calls it immediately)
            except (QueryError, HTTPException) as e:
                errors.append(str(getattr(e, "detail", e)))
                continue
            models.append({"model_id": mid, "model": m["name"], "series": r["series"]})
            for f in r["facts"]:
                facts.append({**f, "model_id": mid})
        for i, f in enumerate(facts):
            f["id"] = f"F{i + 1}"
        out = {"dashboard": d["name"], "facts": facts, "models": models, "errors": errors,
               "methods": insights.__doc__.split("Methods", 1)[1].strip()}
        if b.get("narrative") and facts:
            prov = app.state.provider_for(b.get("provider_id"))
            try:
                out["narrative"] = insights.narrative(prov, facts)
            except agents.AgentError as e:
                out["narrative_error"] = public_message(e)
            audit(u, "insights.narrative", d["name"], {"facts": len(facts)}, request)
        return out

    @app.post("/api/dashboards/{did}/ask")
    def dashboard_ask(did: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        d, scope = dashboard_scope(u, did)
        msg = str(b.get("message") or "").strip()[:8000]
        if not msg:
            raise HTTPException(400, "Ask a question.")
        sid = b.get("session_id")
        sess = store.one("SELECT * FROM agent_sessions WHERE id=? AND user_id=?", (sid, u["id"])) if sid else None
        if sid and not sess:
            raise HTTPException(404, "Conversation not found")
        ctx_lines = [f"The user is looking at the dashboard '{d['name']}'."]
        for mid, meas in scope.items():
            m = store.one("SELECT name FROM semantic_models WHERE id=?", (mid,))
            if m:
                ctx_lines.append(f"It shows measures {', '.join(meas)} from semantic model id {mid} ('{m['name']}').")
        if b.get("filters"):
            ctx_lines.append("Active dashboard filters (apply them to your queries unless the user says otherwise): " + json.dumps(b["filters"])[:1500])
        ctx_lines.append("Use find_insights and explain_change for 'why' and 'what changed' questions, query_model for specific numbers.")
        prov = app.state.provider_for(b.get("provider_id") or (sess["provider_id"] if sess else None))
        res = agents.chat(plat, prov, u, "analyst", sess["messages"] if sess else [], msg,
                          allow_raw_pii=bool(store.get_setting("ai_allow_raw_pii", False)), extra_context=" ".join(ctx_lines))
        if sess:
            store.update("agent_sessions", sess["id"], {"messages": json.dumps(res["messages"]), "tokens_in": sess["tokens_in"] + res["tokens_in"],
                                                        "tokens_out": sess["tokens_out"] + res["tokens_out"], "updated_at": now()})
            sid = sess["id"]
        else:
            sid = store.insert("agent_sessions", {"user_id": u["id"], "agent": "analyst", "provider_id": b.get("provider_id"), "title": f"{d['name']}: {msg[:60]}",
                                                  "messages": json.dumps(res["messages"]), "tokens_in": res["tokens_in"], "tokens_out": res["tokens_out"],
                                                  "created_at": now(), "updated_at": now()})
        audit(u, "agent.chat", "dashboard", {"dashboard": did, "session": sid, "tools": [t["tool"] for t in res["trace"]]}, request)
        return {"session_id": sid, "reply": res["reply"], "trace": res["trace"], "artifacts": res["artifacts"], "grounding": res["grounding"]}

    # ------------------------------------------------------------ executive brief, targets, fiscal year
    from . import brief as brief_mod
    engineer = need("engineer")
    admin = need("admin")

    @app.post("/api/models/{mid}/brief")
    def model_brief(mid: int, b: dict = Body(default={}), u: dict = Depends(viewer)):
        m = visible_model(u, mid)
        meas = b.get("measures") or [x["name"] for x in m["measures"]][:5]
        return brief_mod.brief(plat, u, m, meas, b.get("grain") or "month", b.get("filters"), _today(b), brief_mod.targets_for(store, mid),
                               int(store.get_setting("fiscal_year_start_month", 1) or 1))

    @app.post("/api/dashboards/{did}/brief")
    def dash_brief(did: int, request: Request, b: dict = Body(default={}), u: dict = Depends(viewer)):
        d = ws.item(store, u, "dashboard", did)
        targets_ver = store.scalar("SELECT coalesce(max(updated_at), '') || count(*) FROM kpi_targets")
        fy = store.get_setting("fiscal_year_start_month", 1)
        out = memo("brief", u, [did, d.get("tiles"), b.get("filters"), b.get("grain"), b.get("today"), targets_ver, fy],
                   lambda: brief_mod.dashboard_brief(plat, u, d, b.get("filters"), b.get("grain") or "month", _today(b)))
        if b.get("narrative") and out["headlines"]:
            try:
                out["narrative"] = insights.narrative(app.state.provider_for(b.get("provider_id")), out["headlines"])
            except agents.AgentError as e:
                out["narrative_error"] = public_message(e)
            audit(u, "brief.narrative", d["name"], {"headlines": len(out["headlines"])}, request)
        return out

    @app.get("/api/models/{mid}/targets")
    def list_targets(mid: int, u: dict = Depends(viewer)):
        visible_model(u, mid)
        return store.all("SELECT * FROM kpi_targets WHERE model_id=? ORDER BY measure, period", (mid,))

    @app.put("/api/models/{mid}/targets")
    def set_target(mid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        m = visible_model(u, mid)
        ws.require(store, u, m.get("workspace_id") or 1, "contributor")
        if b.get("measure") not in {x["name"] for x in m["measures"]}:
            raise HTTPException(400, "Unknown measure.")
        period = str(b.get("period") or "").strip()
        import re as _re
        if not _re.fullmatch(r"(FY)?\d{4}", period):
            raise HTTPException(400, "Period is a year such as 2026 (or FY2027 when the fiscal year does not start in January).")
        try:
            value = float(b.get("value"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Target value must be a number.") from None
        store.exec("INSERT INTO kpi_targets(model_id, measure, period, value, note, created_by, updated_at) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(model_id, measure, period) DO UPDATE SET value=excluded.value, note=excluded.note, created_by=excluded.created_by, updated_at=excluded.updated_at",
                   (mid, b["measure"], period, value, str(b.get("note") or "")[:300], u["email"], now()))
        audit(u, "target.set", f"{m['name']}.{b['measure']}", {"period": period, "value": value}, request)
        return store.one("SELECT * FROM kpi_targets WHERE model_id=? AND measure=? AND period=?", (mid, b["measure"], period))

    @app.delete("/api/models/{mid}/targets/{tid}")
    def delete_target(mid: int, tid: int, request: Request, u: dict = Depends(analyst)):
        m = visible_model(u, mid)
        ws.require(store, u, m.get("workspace_id") or 1, "contributor")
        store.exec("DELETE FROM kpi_targets WHERE id=? AND model_id=?", (tid, mid))
        audit(u, "target.deleted", str(tid), None, request)
        return {"ok": True}

    @app.get("/api/settings/fiscal")
    def get_fiscal(u: dict = Depends(viewer)):
        return {"fiscal_year_start_month": int(store.get_setting("fiscal_year_start_month", 1) or 1)}

    @app.put("/api/settings/fiscal")
    def put_fiscal(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        mth = int(b.get("fiscal_year_start_month") or 0)
        if not 1 <= mth <= 12:
            raise HTTPException(400, "Month is 1 to 12.")
        store.set_setting("fiscal_year_start_month", mth)
        audit(u, "fiscal.updated", "", {"month": mth}, request)
        return {"fiscal_year_start_month": mth}

    _ = engineer

    # ------------------------------------------------------------ natural-language questions (no LLM)
    from . import nlq

    def nl_context(u, m, with_values=True):
        syn: dict[str, list[str]] = {}
        gl = store.all("SELECT term, synonyms FROM glossary")
        for item in m["measures"] + m["dimensions"]:
            for g in gl:
                if g["term"].lower() in (item["label"].lower(), item["name"].replace("_", " ").lower()):
                    syn.setdefault(item["name"], []).extend(g["synonyms"] or [])
        vals = {}
        for d in m["dimensions"]:
            if d["type"] != "category" or not with_values:
                continue
            try:
                res = plat.run_model(u, m, {"dimensions": [d["name"]], "limit": 300})
                vals[d["name"]] = [r[0] for r in res["rows"] if r[0] is not None]
            except QueryError:
                pass  # a dimension the person cannot read simply offers no values
        return vals, syn

    def answer(u, m, question, run=True, today=None):
        vals, syn = nl_context(u, m)
        p = nlq.parse(question, m, vals, syn, today)
        p["model_id"], p["model"] = m["id"], m["name"]
        if run:
            res = plat.run_model(u, m, p["spec"], compare=p["compare"])
            if p["viz"].get("forecast"):
                res["analysis"] = insights.analyze_result(m, res, forecast=p["viz"]["forecast"], anomalies=True)
            p["result"] = res
        return p

    @app.post("/api/models/{mid}/ask")
    def ask_model(mid: int, b: dict = Body(...), u: dict = Depends(viewer)):
        m = visible_model(u, mid)
        q = str(b.get("question") or "").strip()[:500]
        if not q:
            raise HTTPException(400, "Ask a question.")
        return answer(u, m, q, b.get("run", True), _today(b))

    @app.post("/api/ask")
    def ask_any(request: Request, b: dict = Body(...), u: dict = Depends(viewer)):
        q = str(b.get("question") or "").strip()[:500]
        if not q:
            raise HTTPException(400, "Ask a question.")
        rows = ws.filter_rows(store, u, store.all("SELECT id FROM semantic_models"))
        best, best_score = None, -1
        for r in rows:
            try:
                m = visible_model(u, r["id"])
            except HTTPException:
                continue
            _, syn = nl_context(u, m, with_values=False)
            sc = nlq.score(q, m, syn) + (1 if m.get("endorsement") == "certified" else 0)
            if sc > best_score:
                best, best_score = m, sc
        if not best:
            raise HTTPException(404, "No semantic model you can use. Ask an engineer to create one.")
        out = answer(u, best, q, True, _today(b))
        audit(u, "ask", best["name"], {"question": q, "understood": out["understood"]}, request)
        return out
