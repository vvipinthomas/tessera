"""AI agents: tool-using assistants that work on the catalog under the caller's permissions.

Safeguards
- Every tool runs with the requesting user's role. Analysts' agents only see published (masked) data.
- A PII firewall redacts personal data in tool results before they reach the model provider.
- Write tools (create rules, edit descriptions) are offered only to engineers and are audited.
- Charts and pipelines are returned as proposals; a person saves them.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from . import pii, quality
from .errors import UserError, public_message
from .orchestrator import PipelineError, validate
from .platform import NotFound, Platform
from .security import has_role, restricted
from .warehouse import QueryError, split_fqn

PROVIDER_KINDS = {
    "anthropic": {"label": "Anthropic Claude", "base_url": "https://api.anthropic.com", "key": True, "model_hint": "claude-sonnet-5-5"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "key": True, "model_hint": "gpt-5"},
    "azure_openai": {"label": "Azure OpenAI", "base_url": "https://YOUR-RESOURCE.openai.azure.com", "key": True, "model_hint": "deployment name"},
    "openai_compatible": {"label": "OpenAI-compatible (vLLM, Groq, OpenRouter, LM Studio)", "base_url": "http://localhost:1234/v1", "key": False, "model_hint": "model id"},
    "ollama": {"label": "Ollama (fully local)", "base_url": "http://localhost:11434/v1", "key": False, "model_hint": "llama3.1"},
    "cloudflare_workers_ai": {"label": "Cloudflare Workers AI", "base_url": "https://api.cloudflare.com/client/v4/accounts/YOUR-ACCOUNT-ID/ai/v1",
                              "key": True, "model_hint": "@cf/meta/llama-3.3-70b-instruct-fp8-fast (pick a model that supports function calling)"},
}


class AgentError(UserError):
    pass


class RetryableProviderError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _check(r: httpx.Response):
    if r.status_code in (408, 409, 429) or r.status_code >= 500:
        ra = r.headers.get("retry-after")
        try:
            ra_s = float(ra) if ra is not None else None
        except ValueError:
            ra_s = None
        raise RetryableProviderError(f"Provider error HTTP {r.status_code}: {r.text[:200]}", ra_s)
    if r.status_code >= 400:
        raise AgentError(f"Provider error HTTP {r.status_code}: {r.text[:300]}")


# ---------------------------------------------------------------- providers
class Provider:
    def __init__(self, kind: str, base_url: str, model: str, api_key: str | None, http_factory: Callable[[], httpx.Client] | None = None):
        self.kind, self.base_url, self.model, self.api_key = kind, (base_url or PROVIDER_KINDS[kind]["base_url"]).rstrip("/"), model, api_key
        self.http_factory = http_factory or (lambda: httpx.Client(timeout=120))

    retries = 3
    sleep = staticmethod(time.sleep)

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        """Call the provider, retrying rate limits, overloads and network errors with exponential backoff."""
        last = None
        for attempt in range(self.retries + 1):
            try:
                if self.kind == "anthropic":
                    return self._anthropic(system, messages, tools)
                return self._openai(system, messages, tools)
            except RetryableProviderError as e:
                last = str(e)
                wait = e.retry_after if e.retry_after is not None else 2 ** attempt
            except httpx.HTTPError as e:
                last = f"Could not reach the model provider: {e}"
                wait = 2 ** attempt
            if attempt < self.retries:
                self.sleep(min(wait, 20))
        raise AgentError(f"{last} (after {self.retries + 1} attempts)")

    def _anthropic(self, system, messages, tools) -> dict:
        msgs = []
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in m.get("tool_calls") or []]
                msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif m["role"] == "tool":
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in m["results"]]})
        body = {"model": self.model, "max_tokens": 2048, "system": system, "messages": msgs}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        with self.http_factory() as cl:
            r = cl.post(f"{self.base_url}/v1/messages", json=body,
                        headers={"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01", "content-type": "application/json"})
        _check(r)
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}} for b in data.get("content", []) if b.get("type") == "tool_use"]
        u = data.get("usage") or {}
        return {"text": text, "tool_calls": calls, "tokens_in": u.get("input_tokens", 0), "tokens_out": u.get("output_tokens", 0)}

    def _openai(self, system, messages, tools) -> dict:
        msgs: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                e: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    e["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                       for c in m["tool_calls"]]
                msgs.append(e)
            elif m["role"] == "tool":
                for r in m["results"]:
                    msgs.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        body: dict[str, Any] = {"messages": msgs}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools]
        headers = {"content-type": "application/json"}
        if self.kind == "azure_openai":
            url = f"{self.base_url}/openai/deployments/{self.model}/chat/completions?api-version=2024-10-21"
            headers["api-key"] = self.api_key or ""
        else:
            url = f"{self.base_url}/chat/completions"
            body["model"] = self.model
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        with self.http_factory() as cl:
            r = cl.post(url, json=body, headers=headers)
        _check(r)
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": c["id"], "name": c["function"]["name"], "input": args})
        u = data.get("usage") or {}
        return {"text": msg.get("content") or "", "tool_calls": calls, "tokens_in": u.get("prompt_tokens", 0), "tokens_out": u.get("completion_tokens", 0)}


# ---------------------------------------------------------------- PII firewall
def redact_value(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    for d in pii.DETECTORS:
        if d["key"] in ("postal_code",):
            continue
        if d["test"](s):
            return f"<{d['key']}>"
    return v


def redact_result(columns: list[str], rows: list[list], sensitive: set[str]) -> list[list]:
    idx = [i for i, c in enumerate(columns) if c in sensitive]
    out = []
    for r in rows:
        r = [redact_value(v) for v in r]
        for i in idx:
            if r[i] is not None:
                r[i] = "<redacted>"
        out.append(r)
    return out


# ---------------------------------------------------------------- tools
class Ctx:
    def __init__(self, platform: Platform, user: dict, allow_raw_pii: bool = False):
        self.p, self.user, self.allow_raw_pii = platform, user, allow_raw_pii
        self.restricted = restricted(user["role"])
        self.artifacts: list[dict] = []

    def dataset(self, fqn: str) -> dict:
        schema, name = split_fqn(fqn, "serving" if self.restricted else "raw")
        if self.restricted and schema != "serving":
            raise QueryError("Your role can only use published datasets (serving schema).")
        d = self.p.dataset(schema, name)
        if not d:
            raise NotFound(f"Dataset {schema}.{name} not found")
        return d

    def sensitive_columns(self) -> set[str]:
        if self.allow_raw_pii:
            return set()
        out = set()
        for d in self.p.store.all("SELECT columns FROM datasets"):
            for c in d["columns"] or []:
                if c.get("pii") and c.get("mask", "none") != "redact_never":
                    out.add(c["name"])
        return out


def t_list_datasets(ctx: Ctx, a: dict):
    rows = ctx.p.store.all("SELECT id, schema_name, name, description, row_count, last_loaded_at FROM datasets ORDER BY schema_name, name")
    if ctx.restricted:
        rows = [r for r in rows if r["schema_name"] == "serving"]
    return [{"dataset": f"{r['schema_name']}.{r['name']}", "rows": r["row_count"], "description": r["description"],
             "quality_score": ctx.p.latest_quality(r["id"])["score"], "last_loaded_at": r["last_loaded_at"]} for r in rows]


def t_describe(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    res = ctx.p.user_query(ctx.user, f'SELECT * FROM "{d["schema_name"]}"."{d["name"]}" LIMIT 5', limit=5)
    sens = {c["name"] for c in d["columns"] if c.get("pii")} if not ctx.allow_raw_pii else set()
    return {"dataset": f"{d['schema_name']}.{d['name']}", "description": d["description"], "rows": d["row_count"],
            "columns": [{k: c.get(k) for k in ("name", "type", "description", "pii", "level", "mask")} for c in d["columns"]],
            "sample": {"columns": res["columns"], "rows": redact_result(res["columns"], res["rows"], sens)}}


def t_run_sql(ctx: Ctx, a: dict):
    res = ctx.p.user_query(ctx.user, a["sql"], limit=200, timeout=30)
    rows = redact_result(res["columns"], res["rows"][:50], ctx.sensitive_columns())
    return {"columns": res["columns"], "rows": rows, "row_count": res["row_count"], "truncated_to": 50 if res["row_count"] > 50 else None}


def t_list_models(ctx: Ctx, a: dict):
    from . import workspaces as ws
    out = []
    vis = ws.visible_ids(ctx.p.store, ctx.user)
    for m in ctx.p.store.all("SELECT * FROM semantic_models ORDER BY name"):
        if m["workspace_id"] not in vis:
            continue
        if ctx.restricted and not m["base"].startswith("serving."):
            continue
        out.append({"model_id": m["id"], "name": m["name"], "description": m["description"], "endorsement": m["endorsement"],
                    "measures": [{"name": x["name"], "label": x["label"], "description": x.get("description", "")} for x in m["measures"]],
                    "dimensions": [{"name": x["name"], "label": x["label"], "type": x["type"]} for x in m["dimensions"]]})
    return out


def model_for(ctx: Ctx, mid) -> dict:
    from . import workspaces as ws
    m = ctx.p.model(int(mid))
    if ws.role_in(ctx.p.store, ctx.user, m.get("workspace_id") or 1) is None:
        raise NotFound("Semantic model not found")
    if ctx.restricted and not m["base"].startswith("serving."):
        raise QueryError("This model is built on unpublished data.")
    return m


def t_query_model(ctx: Ctx, a: dict):
    m = model_for(ctx, a["model_id"])
    spec = {k: a.get(k) for k in ("measures", "dimensions", "filters", "grains", "sort", "limit") if a.get(k) is not None}
    spec["limit"] = min(int(spec.get("limit") or 100), 200)
    res = ctx.p.run_model(ctx.user, m, spec)
    rows = redact_result(res["columns"], res["rows"][:50], ctx.sensitive_columns())
    if a.get("chart_type") and res["rows"]:
        ctx.artifacts.append({"type": "chart", "title": a.get("title") or m["name"], "kind": "model", "model_id": m["id"], "spec": spec,
                              "sql": res["sql"], "viz": {"type": a["chart_type"]}, "preview": {**res, "rows": res["rows"][:200]}})
    return {"columns": res["columns"], "fields": res["fields"], "rows": rows, "row_count": res["row_count"]}


def t_find_insights(ctx: Ctx, a: dict):
    from . import insights
    m = model_for(ctx, a["model_id"])
    r = insights.model_insights(ctx.p, ctx.user, m, a.get("measures"), a.get("grain") or "month", a.get("filters"))
    return {"facts": [{k: f[k] for k in ("id", "kind", "measure", "severity", "text")} for f in r["facts"]], "methods": r["methods"]}


def t_explain_change(ctx: Ctx, a: dict):
    from . import insights
    m = model_for(ctx, a["model_id"])
    r = insights.explain_change(ctx.p, ctx.user, m, a["measure"], a.get("grain") or "month", a.get("filters"), a.get("current"), a.get("baseline"), top=5)
    sens = ctx.sensitive_columns()
    for d in r["dimensions"]:
        col = next((x["column"] for x in m["dimensions"] if x["name"] == d["dimension"]), "")
        if not ctx.allow_raw_pii and col.split(".")[-1] in sens:
            for x in d["members"]:
                x["member"] = "<redacted>"
    return r


def t_profile(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    if ctx.restricted and d["name"] in ctx.p.rls_protected():
        raise QueryError("Row-level security applies to this dataset; whole-table statistics are not available.")
    summ = ctx.p.wh.summarize(d["schema_name"], d["name"])
    sens = {c["name"] for c in d["columns"] if c.get("pii")} if not ctx.allow_raw_pii else set()
    for s in summ:
        if s["column_name"] in sens:
            s["min"] = s["max"] = "<redacted>"
    return summ


def t_suggest_rules(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    return quality.suggest(ctx.p.wh, d["schema_name"], d["name"], ctx.p.rules(d["id"]))


def t_create_rule(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    rid = ctx.p.add_rule(d["id"], {"kind": a["kind"], "column_name": a.get("column"), "params": a.get("params") or {},
                                    "severity": a.get("severity", "error"), "description": "Created by Quality Steward agent"},
                         f"agent:{ctx.user['email']}")
    ctx.p.store.audit(ctx.user, "agent.create_rule", f"{d['schema_name']}.{d['name']}", {"rule_id": rid, **a})
    return {"created_rule_id": rid}


def t_run_quality(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    r = ctx.p.run_quality(d["id"])
    return {k: r[k] for k in ("dataset", "score", "rules", "failed_errors", "failed_warnings")} | {
        "failing": [x["description"] + f" ({x['failing']} rows)" for x in r["results"] if not x["passed"]]}


def t_readiness(ctx: Ctx, a: dict):
    return ctx.p.readiness(ctx.dataset(a["dataset"])["id"])


def t_update_description(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    if a.get("description"):
        ctx.p.store.update("datasets", d["id"], {"description": str(a["description"])[:2000]})
    changed = []
    for col, desc in (a.get("columns") or {}).items():
        try:
            ctx.p.set_column(d["id"], col, {"description": desc})
            changed.append(col)
        except NotFound:
            pass  # the column was dropped since the agent looked; nothing to describe
    ctx.p.store.audit(ctx.user, "agent.update_description", f"{d['schema_name']}.{d['name']}", {"columns": changed})
    return {"updated_table_description": bool(a.get("description")), "updated_columns": changed}


def t_propose_chart(ctx: Ctx, a: dict):
    res = ctx.p.user_query(ctx.user, a["sql"], limit=500)
    for k in ("x", "y"):
        if a.get(k) and a[k] not in res["columns"]:
            raise QueryError(f"Column '{a[k]}' is not in the query result {res['columns']}")
    art = {"type": "chart", "title": a.get("title") or "Chart", "sql": a["sql"],
           "viz": {"type": a.get("chart_type", "bar"), "x": a.get("x"), "y": a.get("y")}, "preview": {**res, "rows": res["rows"][:200]}}
    ctx.artifacts.append(art)
    return {"ok": True, "rows": res["row_count"], "note": "Chart proposed to the user. They can save it to a dashboard."}


def t_list_connections(ctx: Ctx, a: dict):
    return [{"id": c["id"], "name": c["name"], "type": c["type"], "last_test_ok": c["last_test_ok"]}
            for c in ctx.p.store.all("SELECT id, name, type, last_test_ok FROM connections ORDER BY name")]


def t_draft_pipeline(ctx: Ctx, a: dict):
    try:
        order = validate(a["steps"], a.get("schedule"))
    except PipelineError as e:
        return {"valid": False, "error": str(e)}
    ctx.artifacts.append({"type": "pipeline", "name": a["name"], "description": a.get("description", ""),
                          "steps": a["steps"], "schedule": a.get("schedule")})
    return {"valid": True, "execution_order": order, "note": "Draft shown to the user. They can save it as a pipeline."}


# ---------------------------------------------------------------- catalog, lineage and operations tools
def _visible_dataset_rows(ctx: Ctx) -> list[dict]:
    rows = ctx.p.store.all("SELECT * FROM datasets ORDER BY schema_name, name")
    return [r for r in rows if not ctx.restricted or r["schema_name"] == "serving"]


def t_search_catalog(ctx: Ctx, a: dict):
    from . import workspaces as ws
    term = str(a.get("query") or "").strip().lower()
    if len(term) < 2:
        raise ValueError("Search for at least two characters.")
    hit = lambda *xs: any(term in (x or "").lower() for x in xs)  # noqa: E731
    out = []
    for d in _visible_dataset_rows(ctx):
        cols = [c["name"] for c in d["columns"] or [] if hit(c["name"], c.get("description"))]
        if hit(d["name"], d["description"], d.get("domain")) or cols:
            out.append({"kind": "dataset", "name": f"{d['schema_name']}.{d['name']}", "description": (d["description"] or "")[:200], "matching_columns": cols[:10],
                        "endorsement": d.get("endorsement")})
    vis = ws.visible_ids(ctx.p.store, ctx.user)
    for m in ctx.p.store.all("SELECT * FROM semantic_models"):
        if m["workspace_id"] not in vis or (ctx.restricted and not m["base"].startswith("serving.")):
            continue
        fields = [f"{x['name']} ({x['label']})" for x in m["measures"] + m["dimensions"] if hit(x["name"], x["label"], x.get("description"))]
        if hit(m["name"], m["description"]) or fields:
            out.append({"kind": "model", "model_id": m["id"], "name": m["name"], "matching_fields": fields[:10], "endorsement": m["endorsement"]})
    for g in ctx.p.store.all("SELECT * FROM glossary"):
        if hit(g["term"], g["definition"], " ".join(g["synonyms"] or [])):
            out.append({"kind": "term", "term": g["term"], "definition": g["definition"][:300]})
    return out[:40]


def t_glossary(ctx: Ctx, a: dict):
    term = str(a.get("term") or "").strip()
    rows = ctx.p.store.all("SELECT * FROM glossary ORDER BY term")
    if term:
        t = term.lower()
        rows = [g for g in rows if t == g["term"].lower() or t in [x.lower() for x in g["synonyms"] or []] or t in g["term"].lower()]
    return [{"term": g["term"], "definition": g["definition"], "synonyms": g["synonyms"], "owner": g["owner"], "domain": g["domain"], "columns": g["columns"]} for g in rows[:50]]


def t_datacard(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    card = ctx.p.datacard(d["id"])
    if ctx.restricted:
        card["lineage"]["nodes"] = [n for n in card["lineage"]["nodes"] if n.startswith("serving.")]
        card["lineage"]["edges"] = [e for e in card["lineage"]["edges"] if e["src"].startswith("serving.") and e["dst"].startswith("serving.")]
    return card


def impact(ctx: Ctx, node: str) -> dict:
    """Everything downstream of a dataset: tables, semantic models, reports and dashboards."""
    from . import workspaces as ws
    lin = ctx.p.lineage(node)
    down, frontier = set(), {node}
    while frontier:
        nxt = {e["dst"] for e in lin["edges"] if e["src"] in frontier} - down - {node}
        down |= nxt
        frontier = nxt
    tables = {node} | down
    vis = ws.visible_ids(ctx.p.store, ctx.user)
    models = [m for m in ctx.p.store.all("SELECT * FROM semantic_models")
              if m["workspace_id"] in vis and (m["base"] in tables or any(j["table"] in tables for j in m["joins"] or []))]
    mids = {m["id"] for m in models}
    names = {t.split(".")[-1].lower() for t in tables}
    queries = [q for q in ctx.p.store.all("SELECT id, name, kind, model_id, sql, workspace_id FROM queries") if q["workspace_id"] in vis and
               (q["model_id"] in mids or any(re.search(rf"\b{re.escape(n)}\b", (q["sql"] or "").lower()) for n in names))]
    qids = {q["id"] for q in queries}
    dashboards = [d for d in ctx.p.store.all("SELECT id, name, tiles, workspace_id FROM dashboards")
                  if d["workspace_id"] in vis and any(t.get("query_id") in qids for t in d["tiles"] or [])]
    return {"dataset": node, "downstream_tables": sorted(down), "models": [{"id": m["id"], "name": m["name"]} for m in models],
            "reports": [{"id": q["id"], "name": q["name"]} for q in queries], "dashboards": [{"id": d["id"], "name": d["name"]} for d in dashboards]}


def t_lineage(ctx: Ctx, a: dict):
    d = ctx.dataset(a["dataset"])
    node = f"{d['schema_name']}.{d['name']}"
    lin = ctx.p.lineage(node)
    out = {"upstream": sorted({e["src"] for e in lin["edges"]} - {node}), "edges": lin["edges"], "impact": impact(ctx, node)}
    if ctx.restricted:
        out["upstream"] = [n for n in out["upstream"] if n.startswith("serving.")]
        out["edges"] = [e for e in out["edges"] if e["src"].startswith("serving.") and e["dst"].startswith("serving.")]
        out["impact"]["downstream_tables"] = [n for n in out["impact"]["downstream_tables"] if n.startswith("serving.")]
    return out


def t_data_health(ctx: Ctx, a: dict):
    """One view of what is broken right now: failed runs, failing rules, stale data and firing alerts."""
    from datetime import datetime, timedelta, timezone

    from .store import parse_ts
    days = max(1, min(int(a.get("days") or 7), 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    nowdt = datetime.now(timezone.utc)
    visible = {(r["schema_name"], r["name"]): r for r in _visible_dataset_rows(ctx)}
    failing = []
    for r in ctx.p.store.all("SELECT r.id, r.kind, r.column_name, r.severity, r.description, d.schema_name, d.name, rr.failing, rr.total, rr.error, rr.run_at "
                             "FROM rules r JOIN datasets d ON d.id=r.dataset_id JOIN rule_results rr ON rr.id=(SELECT max(id) FROM rule_results WHERE rule_id=r.id) "
                             "WHERE r.enabled=1 AND rr.passed=0 ORDER BY CASE r.severity WHEN 'error' THEN 0 ELSE 1 END, rr.failing DESC"):
        if (r["schema_name"], r["name"]) in visible:
            failing.append({"dataset": f"{r['schema_name']}.{r['name']}", "rule": r["description"] or f"{r['kind']} {r['column_name'] or ''}".strip(),
                            "severity": r["severity"], "failing_rows": r["failing"], "total_rows": r["total"], "error": r["error"], "checked_at": r["run_at"]})
    stale = []
    for (sch, nm), d in visible.items():
        if d.get("freshness_sla_hours") and d.get("last_loaded_at"):
            age = (nowdt - parse_ts(d["last_loaded_at"])).total_seconds() / 3600
            if age > d["freshness_sla_hours"]:
                stale.append({"dataset": f"{sch}.{nm}", "hours_since_load": round(age, 1), "sla_hours": d["freshness_sla_hours"]})
    out = {"window_days": days, "failing_rules": failing[:30], "stale_datasets": stale,
           "firing_alerts": ctx.p.store.all("SELECT id, name, state, last_value, last_checked, last_error FROM alerts WHERE state IN ('firing','error')")}
    if has_role(ctx.user["role"], "engineer"):
        runs = ctx.p.store.all("SELECT r.id run_id, p.name pipeline, r.status, r.created_at, r.error FROM pipeline_runs r JOIN pipelines p ON p.id=r.pipeline_id "
                               "WHERE r.created_at>=? ORDER BY r.id DESC", (since,))
        out["failed_runs"] = [x for x in runs if x["status"] == "failed"][:20]
        out["run_counts"] = {st: sum(1 for x in runs if x["status"] == st) for st in sorted({x["status"] for x in runs})}
    if has_role(ctx.user["role"], "analyst"):
        try:
            streams = ctx.p.store.all("SELECT name, kind, target, desired, status, last_error, meta FROM streams ORDER BY name")
        except Exception:  # noqa: BLE001  (streams table not created yet)
            streams = []
        out["streams"] = [{"name": x["name"], "kind": x["kind"], "target": x["target"], "state": x["status"] if x["desired"] == "running" else "stopped",
                           "error": x["last_error"] if x["status"] == "retrying" else None,
                           "last_batch_at": (x.get("meta") or {}).get("last_batch_at"), "rows_total": (x.get("meta") or {}).get("rows_total", 0)}
                          for x in streams]
        out["failing_streams"] = [x["name"] for x in out["streams"] if x["state"] == "retrying"]
    out["healthy"] = not (out["failing_rules"] or stale or out["firing_alerts"] or out.get("failed_runs") or out.get("failing_streams"))
    return out


def t_run_details(ctx: Ctx, a: dict):
    r = ctx.p.store.one("SELECT r.*, p.name pipeline, p.steps pipeline_steps FROM pipeline_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE r.id=?", (int(a["run_id"]),))
    if not r:
        raise NotFound("Run not found")
    log = r["log"] or []
    return {"run_id": r["id"], "pipeline": r["pipeline"], "status": r["status"], "trigger": r["trigger"], "error": r["error"],
            "created_at": r["created_at"], "started_at": r["started_at"], "finished_at": r["finished_at"], "steps": r["steps"],
            "pipeline_steps": [{k: st.get(k) for k in ("id", "type", "target", "dataset", "depends_on")} for st in r["pipeline_steps"] or []],
            "log_tail": log[-40:]}


def t_recent_runs(ctx: Ctx, a: dict):
    q, args = "SELECT r.id run_id, p.name pipeline, r.status, r.trigger, r.created_at, r.finished_at, r.error FROM pipeline_runs r JOIN pipelines p ON p.id=r.pipeline_id WHERE 1=1", []
    if a.get("pipeline"):
        q += " AND p.name=?"
        args.append(a["pipeline"])
    if a.get("status"):
        q += " AND r.status=?"
        args.append(a["status"])
    return ctx.p.store.all(q + " ORDER BY r.id DESC LIMIT ?", (*args, max(1, min(int(a.get("limit") or 20), 100))))


def t_decompose(ctx: Ctx, a: dict):
    from . import insights
    m = model_for(ctx, a["model_id"])
    return insights.decompose(ctx.p, ctx.user, m, a["measure"], a.get("path"), a.get("dimension"), a.get("mode", "high"), a.get("filters"))


def t_executive_brief(ctx: Ctx, a: dict):
    from . import brief
    m = model_for(ctx, a["model_id"])
    meas = a.get("measures") or [x["name"] for x in m["measures"]][:5]
    b = brief.brief(ctx.p, ctx.user, m, meas, a.get("grain") or "month", a.get("filters"), None, brief.targets_for(ctx.p.store, m["id"]),
                    int(ctx.p.store.get_setting("fiscal_year_start_month", 1) or 1))
    keep = ("label", "current", "vs_prior", "vs_last_year", "ytd", "ttm", "momentum", "next", "landing", "target", "pace", "extreme")
    return {"headlines": [{"id": h["id"], "text": h["text"], "severity": h["severity"]} for h in b["headlines"]],
            "measures": [{k: x.get(k) for k in keep} | {"forecast_method": x["forecast"]["method_label"], "forecast_accuracy": x["forecast"]["accuracy"]} for x in b["measures"]],
            "errors": b["errors"]}


def t_suggest_model(ctx: Ctx, a: dict):
    from . import semantic
    d = ctx.dataset(a["dataset"])
    draft = semantic.suggest_model(ctx.p.wh, d["schema_name"], d["name"])
    ctx.artifacts.append({"type": "model", "name": a.get("name") or draft.get("name"), "model": draft,
                          "note": "Draft semantic model. Review measures, then save it under Models."})
    return {"draft": draft, "note": "Shown to the user as a draft; nothing was saved."}


DS = {"type": "object", "properties": {"dataset": {"type": "string", "description": "schema.table, e.g. serving.orders"}}, "required": ["dataset"]}
TOOLS = {
    "list_datasets": {"description": "List datasets you can access, with row counts, descriptions and quality scores.", "schema": {"type": "object", "properties": {}}, "role": "viewer", "fn": t_list_datasets},
    "describe_dataset": {"description": "Columns, types, descriptions, sensitivity and 5 sample rows (personal data redacted).", "schema": DS, "role": "analyst", "fn": t_describe},
    "run_sql": {"description": "Run one read-only DuckDB SELECT. Returns at most 50 rows; personal data is redacted.", "schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}, "role": "analyst", "fn": t_run_sql},
    "profile_dataset": {"description": "Column statistics: min, max, distinct, null percentage, averages.", "schema": DS, "role": "analyst", "fn": t_profile},
    "suggest_rules": {"description": "Data-driven quality rule suggestions for a dataset (not yet applied).", "schema": DS, "role": "analyst", "fn": t_suggest_rules},
    "create_rule": {"description": f"Create a quality rule. kind is one of {', '.join(quality.KINDS)}. params: range{{min,max}}, regex{{pattern}}, allowed_values{{values}}, freshness{{hours}}, row_count{{min}}.",
                    "schema": {"type": "object", "properties": {"dataset": {"type": "string"}, "kind": {"type": "string"}, "column": {"type": "string"},
                                                                 "params": {"type": "object"}, "severity": {"type": "string", "enum": ["error", "warn"]}},
                               "required": ["dataset", "kind"]}, "role": "engineer", "write": True, "fn": t_create_rule},
    "run_quality": {"description": "Run all enabled quality rules on a dataset and report failures.", "schema": DS, "role": "engineer", "write": True, "fn": t_run_quality},
    "readiness_report": {"description": "AI readiness score with per-check breakdown and fixes.", "schema": DS, "role": "analyst", "fn": t_readiness},
    "update_description": {"description": "Write the table description and column descriptions (object of column -> text).",
                           "schema": {"type": "object", "properties": {"dataset": {"type": "string"}, "description": {"type": "string"}, "columns": {"type": "object"}}, "required": ["dataset"]},
                           "role": "engineer", "write": True, "fn": t_update_description},
    "propose_chart": {"description": "Propose a chart from a SELECT. chart_type: bar, line, area, kpi or table. x is the category/time column, y the numeric column.",
                      "schema": {"type": "object", "properties": {"title": {"type": "string"}, "sql": {"type": "string"}, "chart_type": {"type": "string"}, "x": {"type": "string"}, "y": {"type": "string"}}, "required": ["sql"]},
                      "role": "analyst", "fn": t_propose_chart},
    "list_models": {"description": "List governed semantic models with their business measures and dimensions. Prefer these over raw SQL for business metrics.",
                    "schema": {"type": "object", "properties": {}}, "role": "analyst", "fn": t_list_models},
    "query_model": {"description": "Query a semantic model by picking measures and dimensions. filters: [{field, op (=, !=, in, between, >, <, contains), value}]. grains: {time_dimension: day|week|month|quarter|year}. Set chart_type (bar, line, area, kpi, table, pie) to propose a chart.",
                    "schema": {"type": "object", "properties": {"model_id": {"type": "integer"}, "measures": {"type": "array", "items": {"type": "string"}},
                                                                 "dimensions": {"type": "array", "items": {"type": "string"}}, "filters": {"type": "array", "items": {"type": "object"}},
                                                                 "grains": {"type": "object"}, "limit": {"type": "integer"}, "chart_type": {"type": "string"}, "title": {"type": "string"}},
                               "required": ["model_id", "measures"]}, "role": "analyst", "fn": t_query_model},
    "find_insights": {"description": "Automated facts about semantic-model measures: latest change, drivers of that change, trend, anomalies, forecast and concentration. Use for 'how are we doing' and 'anything unusual' questions.",
                      "schema": {"type": "object", "properties": {"model_id": {"type": "integer"}, "measures": {"type": "array", "items": {"type": "string"}},
                                                                   "grain": {"type": "string"}, "filters": {"type": "array", "items": {"type": "object"}}}, "required": ["model_id"]},
                      "role": "analyst", "fn": t_find_insights},
    "explain_change": {"description": "Explain why a measure changed between two periods by splitting the change across every category dimension. Defaults to the last complete period vs the one before; current/baseline are period start dates (YYYY-MM-DD).",
                       "schema": {"type": "object", "properties": {"model_id": {"type": "integer"}, "measure": {"type": "string"}, "grain": {"type": "string"},
                                                                    "current": {"type": "string"}, "baseline": {"type": "string"}, "filters": {"type": "array", "items": {"type": "object"}}},
                                  "required": ["model_id", "measure"]}, "role": "analyst", "fn": t_explain_change},
    "search_catalog": {"description": "Search datasets, columns, semantic models and glossary terms by keyword. Start here when you do not know where data lives.",
                       "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, "role": "viewer", "fn": t_search_catalog},
    "glossary": {"description": "Business glossary: definitions, synonyms, owners and mapped columns. Pass a term, or nothing to list all.",
                 "schema": {"type": "object", "properties": {"term": {"type": "string"}}}, "role": "viewer", "fn": t_glossary},
    "get_datacard": {"description": "Full documentation card for a dataset: columns with descriptions and sensitivity, quality score, AI readiness and lineage.", "schema": DS, "role": "analyst", "fn": t_datacard},
    "lineage_impact": {"description": "Upstream sources and downstream impact of a dataset: tables, semantic models, reports and dashboards that depend on it.", "schema": DS, "role": "analyst", "fn": t_lineage},
    "data_health": {"description": "What is broken right now: failing quality rules, datasets past their freshness SLA, firing alerts and (engineers) failed pipeline runs.",
                    "schema": {"type": "object", "properties": {"days": {"type": "integer", "description": "Look-back window for runs, default 7"}}}, "role": "analyst", "fn": t_data_health},
    "recent_runs": {"description": "Recent pipeline runs, optionally filtered by pipeline name or status (queued, running, success, failed, cancelled).",
                    "schema": {"type": "object", "properties": {"pipeline": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer"}}}, "role": "engineer", "fn": t_recent_runs},
    "run_details": {"description": "A pipeline run's steps, error and the last 40 log lines.", "schema": {"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"]},
                    "role": "engineer", "fn": t_run_details},
    "decompose": {"description": "One level of a decomposition tree: split a measure by a dimension under a path of {field, value} filters. Omit dimension to let Tessera pick the split where one member stands out (mode high or low).",
                  "schema": {"type": "object", "properties": {"model_id": {"type": "integer"}, "measure": {"type": "string"}, "path": {"type": "array", "items": {"type": "object"}},
                                                               "dimension": {"type": "string"}, "mode": {"type": "string", "enum": ["high", "low"]}, "filters": {"type": "array", "items": {"type": "object"}}},
                             "required": ["model_id", "measure"]}, "role": "analyst", "fn": t_decompose},
    "executive_brief": {"description": "Leadership summary of a semantic model's measures: latest vs prior and last year, year to date, trailing twelve months, momentum, full-year forecast with range, chance of hitting targets, and trending segments. Use for 'how are we tracking', 'will we hit target', 'what will the year look like'.",
                        "schema": {"type": "object", "properties": {"model_id": {"type": "integer"}, "measures": {"type": "array", "items": {"type": "string"}},
                                                                     "grain": {"type": "string", "enum": ["month", "quarter"]}, "filters": {"type": "array", "items": {"type": "object"}}},
                                   "required": ["model_id"]}, "role": "analyst", "fn": t_executive_brief},
    "suggest_model": {"description": "Draft a semantic model (dimensions and measures) from a dataset. Returns a proposal; nothing is saved.",
                      "schema": {"type": "object", "properties": {"dataset": {"type": "string"}, "name": {"type": "string"}}, "required": ["dataset"]}, "role": "engineer", "fn": t_suggest_model},
    "list_connections": {"description": "List configured source connections (id, name, type).", "schema": {"type": "object", "properties": {}}, "role": "engineer", "fn": t_list_connections},
    "draft_pipeline": {"description": "Validate and propose a pipeline. Step types: ingest{connection_id,source{table|query|path},target,mode}, transform{sql,target}, quality{dataset,fail_on}, pii_scan{dataset}, publish{dataset}, export{dataset,format}, webhook{url}. Each step has id and optional depends_on list.",
                       "schema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "schedule": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object"}}}, "required": ["name", "steps"]},
                       "role": "engineer", "fn": t_draft_pipeline},
}

BASE_PROMPT = ("You are {label}, an agent inside Tessera, a local-first data quality, security and analytics platform. "
               "The warehouse is DuckDB with schemas raw (ingested), curated (transformed) and serving (published, PII-masked). "
               "Always inspect data with tools before answering; never invent numbers, columns or results. "
               "Quote figures exactly as tools return them. If a tool fails, explain the error plainly. "
               "Personal data in tool output is redacted as <type>; do not try to recover it. "
               "The user has the role '{role}'. Keep answers short and concrete. {goal}")
AGENTS = {
    "analyst": {"label": "Data Analyst", "goal": "Answer business questions. Use governed semantic-model measures first (list_models, query_model); fall back to SQL only when no model covers the question. Propose a chart when it helps.",
                "tools": ["list_models", "query_model", "executive_brief", "find_insights", "explain_change", "decompose", "glossary", "search_catalog", "list_datasets", "describe_dataset", "run_sql", "propose_chart"]},
    "steward": {"label": "Quality Steward", "goal": "Profile datasets, explain issues, and create sensible quality rules when asked.",
                "tools": ["data_health", "list_datasets", "describe_dataset", "profile_dataset", "suggest_rules", "create_rule", "run_quality"]},
    "engineer": {"label": "Pipeline Engineer", "goal": "Design pipelines (ingest, transform, quality gate, PII scan, publish) and return them with draft_pipeline.",
                 "tools": ["list_connections", "list_datasets", "describe_dataset", "run_sql", "draft_pipeline"]},
    "incident": {"label": "Data Incident Responder", "goal": ("Triage data problems. Start with data_health, then dig into failing runs (run_details) and rules. "
                 "Use lineage_impact to list affected models, reports and dashboards. Report: what is broken, since when, what it affects, the most likely cause "
                 "(say clearly when it is a hypothesis), and concrete next steps. Do not change anything."),
                 "tools": ["data_health", "recent_runs", "run_details", "lineage_impact", "get_datacard", "describe_dataset", "profile_dataset", "run_sql"]},
    "modeler": {"label": "Semantic Modeler", "goal": ("Help build governed semantic models. Find the right published dataset (search_catalog, get_datacard), reuse glossary definitions "
                "for measure names and descriptions, check existing models first to avoid duplicates, and propose a draft with suggest_model. Explain each measure in one line."),
                "tools": ["search_catalog", "glossary", "list_models", "list_datasets", "get_datacard", "describe_dataset", "profile_dataset", "suggest_model"]},
    "readiness": {"label": "AI Readiness Advisor", "goal": "Assess AI readiness, explain the gaps, and write missing documentation when asked.",
                  "tools": ["list_datasets", "get_datacard", "describe_dataset", "readiness_report", "glossary", "update_description"]},
}


def tools_for(agent: str, role: str) -> list[dict]:
    return [{"name": n, "description": TOOLS[n]["description"], "schema": TOOLS[n]["schema"]}
            for n in AGENTS[agent]["tools"] if has_role(role, TOOLS[n]["role"])]


def run_tool(ctx: Ctx, name: str, args: dict, allowed: set[str], max_chars: int = 12000) -> tuple[str, bool]:
    if name not in allowed:
        return json.dumps({"error": f"Tool '{name}' is not available to this agent for your role."}), False
    try:
        out = TOOLS[name]["fn"](ctx, args or {})
        s = json.dumps(out, default=str)
        if len(s) > max_chars:
            s = s[:max_chars] + "…(truncated)"
        return s, True
    except (QueryError, NotFound, KeyError, ValueError) as e:
        return json.dumps({"error": public_message(e, also=(KeyError, ValueError))}), False


def chat(platform: Platform, provider: Provider, user: dict, agent: str, history: list[dict], message: str,
         max_steps: int = 10, allow_raw_pii: bool = False, extra_context: str = "") -> dict:
    if agent not in AGENTS:
        raise AgentError("Unknown agent")
    a = AGENTS[agent]
    tools = tools_for(agent, user["role"])
    allowed = {t["name"] for t in tools}
    system = BASE_PROMPT.format(label=a["label"], role=user["role"], goal=a["goal"]) + (" " + extra_context if extra_context else "")
    ctx = Ctx(platform, user, allow_raw_pii)
    messages = list(history) + [{"role": "user", "content": message}]
    trace, tin, tout = [], 0, 0
    seen_calls: dict[str, int] = {}
    outputs: list[str] = []
    for _ in range(max_steps):
        resp = provider.complete(system, messages, tools)
        tin += resp["tokens_in"]
        tout += resp["tokens_out"]
        messages.append({"role": "assistant", "content": resp["text"], "tool_calls": resp["tool_calls"]})
        if not resp["tool_calls"]:
            return {"reply": resp["text"], "messages": messages, "trace": trace, "artifacts": ctx.artifacts, "tokens_in": tin, "tokens_out": tout,
                    "grounding": grounding(resp["text"], outputs, message)}
        results = []
        for c in resp["tool_calls"]:
            key = c["name"] + json.dumps(c["input"], sort_keys=True, default=str)
            seen_calls[key] = seen_calls.get(key, 0) + 1
            if seen_calls[key] > 2:
                out, ok = json.dumps({"error": "You already ran this exact call twice. Use the earlier result or change the arguments."}), False
            else:
                out, ok = run_tool(ctx, c["name"], c["input"], allowed)
                if ok:
                    outputs.append(out)
            results.append({"id": c["id"], "name": c["name"], "content": out})
            trace.append({"tool": c["name"], "input": c["input"], "ok": ok, "output_preview": out[:600]})
        messages.append({"role": "tool", "results": results})
    reply = "I stopped after the maximum number of steps. Here is what I found so far; ask me to continue if you need more."
    messages.append({"role": "assistant", "content": reply, "tool_calls": []})
    return {"reply": reply, "messages": messages, "trace": trace, "artifacts": ctx.artifacts, "tokens_in": tin, "tokens_out": tout,
            "grounding": grounding("", outputs, message)}


# ---------------------------------------------------------------- grounding check
_FIG = re.compile(r"(?<![\w.\-/:#])([-−+]?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%|pts?\b|[KMB]\b|k\b)?")
_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_SCALE = {"K": 1e3, "k": 1e3, "M": 1e6, "B": 1e9}


def figures(text: str) -> list[dict]:
    out = []
    for m in _FIG.finditer(text or ""):
        sign, cur, whole, frac, suf = m.groups()
        intpart = whole.replace(",", "")
        v = float(intpart + (frac or ""))
        if sign in ("-", "−"):
            v = -v
        dec = len(frac) - 1 if frac else 0
        if not suf and not cur and not frac and "," not in whole:
            n = abs(int(intpart))
            if n < 10 or 1900 <= n <= 2100:
                continue
        # skip pieces of ISO dates and times like 2026-09-01 or 10:30
        tail = text[m.end():m.end() + 1]
        if not suf and tail in ("-", ":", "/") and not cur:
            continue
        out.append({"text": m.group(0).strip(), "value": v, "decimals": dec, "suffix": (suf or "").strip()})
    return out


def grounding(reply: str, tool_outputs: list[str], user_message: str = "") -> dict:
    """Check every figure in the reply against numbers the tools actually returned.

    A figure is verified when some tool number (or a percentage, thousands/millions form, or the difference of two
    numbers from the same tool result) rounds to it at the precision shown. Unverified figures are not necessarily
    wrong, but they were not read from data in this turn and the interface flags them."""
    figs = figures(reply)
    if not figs:
        return {"figures": 0, "verified": 0, "unverified": [], "checked": True}
    asked = {f["value"] for f in figures(user_message)}
    pools = []
    for o in tool_outputs:
        nums = [float(x) for x in _NUM.findall(o)][:400]
        pools.append(nums)
    flat = [x for p in pools for x in p]
    diffs = [a - b for p in pools for a in p[:150] for b in p[:150] if a != b]

    def ok(f) -> bool:
        if f["value"] in asked:
            return True
        tol = 0.5 * 10 ** (-f["decimals"]) + 1e-9
        scale = _SCALE.get(f["suffix"], 1)
        targets = [f["value"]]
        if f["suffix"].startswith("%") or f["suffix"].startswith("pt"):
            cand = lambda x: (x * 100, x)  # noqa: E731
        else:
            cand = lambda x: (x / scale,)  # noqa: E731
        for x in flat + diffs:
            for c in cand(x):
                for t in targets:
                    if abs(abs(c) - abs(t)) <= tol:
                        return True
        return False
    bad = [f["text"] for f in figs if not ok(f)]
    return {"figures": len(figs), "verified": len(figs) - len(bad), "unverified": bad[:20], "checked": True}


_ = re
