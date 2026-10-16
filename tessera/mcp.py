"""Model Context Protocol server, so any MCP client (Claude Desktop, Claude Code, IDEs, other agent frameworks)
can use Tessera's governed data with the same guarantees as the built-in agents.

Transport: Streamable HTTP at POST /mcp (JSON responses, stateless; no server-initiated stream), plus a stdio bridge
(`tessera mcp`) for clients that only launch local processes.

Guarantees
- Authentication is an API token (Authorization: Bearer tsr_...). Tools run as the token's user with the token's role,
  so row-level security, the serving-only rule for viewers and analysts, and workspace visibility all apply.
- The PII firewall from the built-in agents redacts personal data in every tool result.
- Write tools (create quality rules, edit descriptions, run quality checks) are hidden unless an admin turns on
  "allow writes" and the token's role permits them. Every tool call is audited.
- Origin header checks protect a locally running server from DNS-rebinding attacks by web pages.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import __version__, agents
from . import workspaces as ws
from .auth import client_ip
from .platform import NotFound
from .security import has_role
from .warehouse import QueryError

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Tessera is a governed data platform. Prefer semantic models (list_models, query_model) for business metrics; they hold certified "
                "definitions. Use find_insights, explain_change and decompose for 'why' questions, data_health and lineage_impact for "
                "'is the data OK' questions, and search_catalog when you do not know where data lives. Quote figures exactly as tools return them. "
                "Personal data is redacted as <type> and must not be reconstructed.")
TITLES = {"list_datasets": "List datasets", "describe_dataset": "Describe dataset", "run_sql": "Run read-only SQL", "profile_dataset": "Profile dataset",
          "suggest_rules": "Suggest quality rules", "create_rule": "Create quality rule", "run_quality": "Run quality checks",
          "readiness_report": "AI readiness report", "update_description": "Update descriptions", "propose_chart": "Propose chart",
          "list_models": "List semantic models", "query_model": "Query semantic model", "find_insights": "Find insights", "explain_change": "Explain a change",
          "decompose": "Decomposition tree", "search_catalog": "Search catalog", "glossary": "Business glossary", "get_datacard": "Dataset datacard",
          "lineage_impact": "Lineage and impact", "data_health": "Data health", "recent_runs": "Recent pipeline runs", "run_details": "Pipeline run details",
          "suggest_model": "Draft semantic model", "executive_brief": "Executive brief", "list_connections": "List connections", "draft_pipeline": "Draft pipeline"}
PROMPTS = {
    "leadership-update": {
        "description": "A short leadership update: where we will land this year, target likelihood, and what is driving it.",
        "arguments": [{"name": "model_id", "description": "Semantic model id (see list_models)", "required": True}],
        "text": ("Using Tessera, write a leadership update for semantic model {model_id}. Call executive_brief first. Lead with the full-year forecast and "
                 "the chance of hitting each target, then year-over-year performance, then the segments driving it, then risks. Five bullets or fewer, "
                 "figures exactly as the tool returns them, and say how accurate the forecast has been.")},
    "explain-metric-change": {
        "description": "Explain why a business metric moved, with drivers and a forecast.",
        "arguments": [{"name": "model_id", "description": "Semantic model id (see list_models)", "required": True},
                      {"name": "measure", "description": "Measure name, e.g. revenue", "required": True},
                      {"name": "grain", "description": "day, week, month, quarter or year (default month)", "required": False}],
        "text": ("Using Tessera, explain how the measure '{measure}' in semantic model {model_id} changed in the last complete {grain}. "
                 "Call explain_change first, then find_insights for trend, anomalies and forecast, and decompose to drill into the biggest driver. "
                 "Answer in five bullets or fewer, quote figures exactly as the tools return them, and say which statements are hypotheses.")},
    "triage-data-incident": {
        "description": "Find what is broken in the data platform, what it affects and what to do next.",
        "arguments": [{"name": "dataset", "description": "Optional dataset to focus on (schema.table)", "required": False}],
        "text": ("Triage the current state of the Tessera data platform{focus}. Call data_health, then run_details for failed runs and "
                 "lineage_impact for affected datasets. Report: what is broken, since when, which models, reports and dashboards are affected, "
                 "the most likely cause (marked as a hypothesis), and the next three actions. Do not change anything.")},
    "document-dataset": {
        "description": "Draft business documentation for a dataset to improve its AI readiness.",
        "arguments": [{"name": "dataset", "description": "schema.table", "required": True}],
        "text": ("Read the datacard for {dataset} with get_datacard and readiness_report, check the glossary for existing definitions, and draft a "
                 "table description and one-line descriptions for every undocumented column. Show the draft; only call update_description if I confirm.")},
}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def register(app, plat, user, need, audit):
    store = plat.store
    admin = need("admin")

    def config() -> dict:
        c = store.get_setting("mcp") or {}
        return {"enabled": c.get("enabled", True), "allow_writes": bool(c.get("allow_writes", False))}

    def tool_list(u: dict) -> list[str]:
        cfg = config()
        out = []
        for name, t in agents.TOOLS.items():
            if not has_role(u["role"], t["role"]):
                continue
            if t.get("write") and not cfg["allow_writes"]:
                continue
            out.append(name)
        return out

    def describe(name: str) -> dict:
        t = agents.TOOLS[name]
        ro = not t.get("write")
        return {"name": name, "title": TITLES.get(name, name), "description": t["description"], "inputSchema": t["schema"],
                "annotations": {"title": TITLES.get(name, name), "readOnlyHint": ro, "destructiveHint": False, "idempotentHint": ro, "openWorldHint": False}}

    # ------------------------------------------------------------ resources
    def resources(u: dict) -> list[dict]:
        ctx = agents.Ctx(plat, u)
        out = [{"uri": "tessera://catalog", "name": "catalog", "title": "Dataset catalog", "mimeType": "application/json",
                "description": "Every dataset you can access with row counts, descriptions and quality scores."},
               {"uri": "tessera://glossary", "name": "glossary", "title": "Business glossary", "mimeType": "application/json",
                "description": "Business terms, definitions and synonyms."}]
        if has_role(u["role"], "analyst"):
            for d in agents._visible_dataset_rows(ctx)[:150]:
                fqn = f"{d['schema_name']}.{d['name']}"
                out.append({"uri": f"tessera://dataset/{fqn}", "name": fqn, "title": f"Datacard: {fqn}", "mimeType": "application/json",
                            "description": (d["description"] or "")[:200]})
            for m in agents.t_list_models(ctx, {})[:50]:
                out.append({"uri": f"tessera://model/{m['model_id']}", "name": f"model-{m['model_id']}", "title": f"Semantic model: {m['name']}",
                            "mimeType": "application/json", "description": (m["description"] or "")[:200]})
        return out

    def read_resource(u: dict, uri: str) -> dict:
        ctx = agents.Ctx(plat, u, bool(store.get_setting("ai_allow_raw_pii", False)))
        if uri == "tessera://catalog":
            body = agents.t_list_datasets(ctx, {})
        elif uri == "tessera://glossary":
            body = agents.t_glossary(ctx, {})
        elif uri.startswith("tessera://dataset/"):
            if not has_role(u["role"], "analyst"):
                raise RpcError(-32002, "Datacards need the analyst role.")
            body = agents.t_datacard(ctx, {"dataset": uri.split("/", 3)[3]})
        elif uri.startswith("tessera://model/"):
            m = agents.model_for(ctx, uri.rsplit("/", 1)[1])
            body = {k: m[k] for k in ("id", "name", "description", "base", "dimensions", "measures", "endorsement")}
        else:
            raise RpcError(-32002, f"Resource not found: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(body, default=str)}]}

    # ------------------------------------------------------------ dispatcher
    def handle(u: dict, msg: dict, request: Request) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None, "error": {"code": -32600, "message": "Invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:  # notification
            return None
        try:
            result = dispatch(u, method, params, request)
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}
        except (QueryError, NotFound, KeyError, ValueError) as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e).strip("'")}}

    def dispatch(u: dict, method: str, params: dict, request: Request):
        if method == "initialize":
            want = params.get("protocolVersion")
            return {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}, "prompts": {"listChanged": False}},
                    "serverInfo": {"name": "tessera", "title": "Tessera data platform", "version": __version__}, "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [describe(n) for n in tool_list(u)]}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            allowed = set(tool_list(u))
            if name not in agents.TOOLS:
                raise RpcError(-32602, f"Unknown tool: {name}")
            if not isinstance(args, dict):
                raise RpcError(-32602, "arguments must be an object")
            ctx = agents.Ctx(plat, u, bool(store.get_setting("ai_allow_raw_pii", False)))
            t0 = time.perf_counter()
            out, ok = agents.run_tool(ctx, name, args, allowed, max_chars=60000)
            store.audit(u, "mcp.tool", name, {"ok": ok, "ms": round((time.perf_counter() - t0) * 1000), "args": {k: (str(v)[:200]) for k, v in args.items()}},
                        client_ip(request))
            try:
                structured = json.loads(out)
            except ValueError:
                structured = {"text": out}
            if not isinstance(structured, dict):
                structured = {"result": structured}
            if ctx.artifacts:
                structured["proposals"] = [{k: v for k, v in a.items() if k != "preview"} for a in ctx.artifacts]
            return {"content": [{"type": "text", "text": out}], "structuredContent": structured, "isError": not ok}
        if method == "resources/list":
            return {"resources": resources(u)}
        if method == "resources/templates/list":
            if not has_role(u["role"], "analyst"):
                return {"resourceTemplates": []}
            return {"resourceTemplates": [
                {"uriTemplate": "tessera://dataset/{schema}.{table}", "name": "datacard", "title": "Dataset datacard", "mimeType": "application/json"},
                {"uriTemplate": "tessera://model/{id}", "name": "model", "title": "Semantic model definition", "mimeType": "application/json"}]}
        if method == "resources/read":
            return read_resource(u, str(params.get("uri") or ""))
        if method == "prompts/list":
            return {"prompts": [{"name": k, "title": k.replace("-", " ").capitalize(), "description": v["description"], "arguments": v["arguments"]}
                                for k, v in PROMPTS.items()]}
        if method == "prompts/get":
            p = PROMPTS.get(params.get("name"))
            if not p:
                raise RpcError(-32602, "Unknown prompt")
            a = {k: str(v) for k, v in (params.get("arguments") or {}).items()}
            missing = [x["name"] for x in p["arguments"] if x["required"] and not a.get(x["name"])]
            if missing:
                raise RpcError(-32602, f"Missing argument(s): {', '.join(missing)}")
            vals = {"grain": a.get("grain") or "month", "focus": f" with a focus on {a['dataset']}" if a.get("dataset") else "", **a}
            text = p["text"].format_map(_Safe(vals))
            return {"description": p["description"], "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if method in ("logging/setLevel", "completion/complete"):
            return {} if method == "logging/setLevel" else {"completion": {"values": [], "hasMore": False}}
        raise RpcError(-32601, f"Method not found: {method}")

    # ------------------------------------------------------------ HTTP transport
    def check_origin(request: Request):
        origin = request.headers.get("origin")
        if not origin:
            return
        allowed = {o.strip().rstrip("/") for o in os.environ.get("TESSERA_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()}
        host = request.headers.get("host", "")
        if origin.rstrip("/") in allowed or urlparse(origin).netloc == host:
            return
        raise HTTPException(403, "Origin not allowed for MCP. Set TESSERA_MCP_ALLOWED_ORIGINS to allow it.")

    def mcp_user(request: Request) -> dict:
        check_origin(request)
        if not config()["enabled"]:
            raise HTTPException(503, "The MCP server is turned off. An admin can enable it under Admin → AI & MCP.")
        if not request.headers.get("authorization", "").lower().startswith("bearer "):
            raise HTTPException(401, "MCP needs an API token: Authorization: Bearer tsr_…", headers={"WWW-Authenticate": 'Bearer realm="tessera"'})
        return user(request)

    @app.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request):
        u = mcp_user(request)
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        if isinstance(body, list):
            out = [r for r in (handle(u, m, request) for m in body[:50]) if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        r = handle(u, body, request)
        return JSONResponse(r) if r is not None else Response(status_code=202)

    @app.get("/mcp", include_in_schema=False)
    def mcp_get(request: Request):
        mcp_user(request)
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.delete("/mcp", include_in_schema=False)
    def mcp_delete(request: Request):
        mcp_user(request)
        return Response(status_code=405, headers={"Allow": "POST"})

    # ------------------------------------------------------------ admin
    @app.get("/api/admin/mcp")
    def get_mcp(request: Request, u: dict = Depends(admin)):
        base = (plat.settings.base_url or str(request.base_url).rstrip("/"))
        return {**config(), "url": f"{base}/mcp", "tools": {r: [n for n in agents.TOOLS if has_role(r, agents.TOOLS[n]["role"]) and
                                                                 (not agents.TOOLS[n].get("write") or config()["allow_writes"])]
                                                            for r in ("viewer", "analyst", "engineer")},
                "calls_7d": store.scalar("SELECT count(*) FROM audit WHERE action='mcp.tool' AND ts >= datetime('now','-7 days')")}

    @app.put("/api/admin/mcp")
    def put_mcp(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        c = config()
        for k in ("enabled", "allow_writes"):
            if k in b:
                c[k] = bool(b[k])
        store.set_setting("mcp", c)
        audit(u, "mcp.updated", "", c, request)
        return c

    _ = ws


class _Safe(dict):
    def __missing__(self, k):
        return ""
