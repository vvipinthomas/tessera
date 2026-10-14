"""MCP server: protocol, auth, role scoping, PII firewall, RLS, write gating, audit and the stdio bridge."""
import io
import json

import httpx
import pytest
from conftest import Client, add_user

PROTO = "2025-06-18"


def token(c, role=None, name="mcp"):
    r = c.post("/api/tokens", json={"name": name, **({"role": role} if role else {})})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def rpc(app, tok, method, params=None, mid=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}), **({"params": params} if params is not None else {})}
    c = Client(app)
    return c.post("/mcp", csrf=False, json=body, headers={"Authorization": f"Bearer {tok}", **(headers or {})})


def call(app, tok, name, args=None):
    r = rpc(app, tok, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture(scope="module")
def env(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    return {"c": c, "app": app, "admin": token(c), "viewer": token(c, "viewer", "viewer-mcp"), "engineer": token(c, "engineer", "eng-mcp")}


def test_initialize_and_lifecycle(env):
    r = rpc(env["app"], env["admin"], "initialize", {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    j = r.json()["result"]
    assert j["protocolVersion"] == PROTO and j["serverInfo"]["name"] == "tessera" and "tools" in j["capabilities"] and j["instructions"]
    old = rpc(env["app"], env["admin"], "initialize", {"protocolVersion": "2024-11-05"}).json()["result"]
    assert old["protocolVersion"] == "2024-11-05"
    unknown = rpc(env["app"], env["admin"], "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert unknown["protocolVersion"] == PROTO
    n = rpc(env["app"], env["admin"], "notifications/initialized", mid=None)
    assert n.status_code == 202 and n.content == b""
    assert rpc(env["app"], env["admin"], "ping").json()["result"] == {}
    assert rpc(env["app"], env["admin"], "nope/x").json()["error"]["code"] == -32601


def test_auth_and_origin(env):
    app = env["app"]
    c = Client(app)
    r = c.post("/mcp", csrf=False, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and "Bearer" in r.headers.get("www-authenticate", "")
    assert rpc(app, "tsr_wrong", "ping").status_code == 401
    assert rpc(app, env["admin"], "ping", headers={"Origin": "https://evil.example"}).status_code == 403
    assert rpc(app, env["admin"], "ping", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.get("/mcp", headers={"Authorization": f"Bearer {env['admin']}"}).status_code == 405
    bad = c.post("/mcp", csrf=False, content=b"{not json", headers={"Authorization": f"Bearer {env['admin']}", "Content-Type": "application/json"})
    assert bad.json()["error"]["code"] == -32700


def test_tools_are_scoped_by_role_and_write_setting(env):
    names = lambda tok: {t["name"] for t in rpc(env["app"], tok, "tools/list").json()["result"]["tools"]}  # noqa: E731
    v, e = names(env["viewer"]), names(env["engineer"])
    assert {"list_datasets", "search_catalog", "glossary"} <= v and "run_sql" not in v and "query_model" not in v
    assert {"run_sql", "query_model", "find_insights", "data_health", "run_details"} <= e
    assert not e & {"create_rule", "update_description", "run_quality"}, "writes are off by default"
    tools = rpc(env["app"], env["engineer"], "tools/list").json()["result"]["tools"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools) and all(t["inputSchema"]["type"] == "object" for t in tools)
    c = env["c"]
    assert c.put("/api/admin/mcp", json={"allow_writes": True}).status_code == 200
    try:
        e2 = rpc(env["app"], env["engineer"], "tools/list").json()["result"]["tools"]
        w = next(t for t in e2 if t["name"] == "create_rule")
        assert w["annotations"]["readOnlyHint"] is False
        assert "create_rule" not in names(env["viewer"])
    finally:
        c.put("/api/admin/mcp", json={"allow_writes": False})
    # a hidden tool cannot be called directly either
    res = call(env["app"], env["engineer"], "create_rule", {"dataset": "raw.orders", "kind": "not_null", "column": "order_id"})
    assert res["isError"] and "not available" in res["content"][0]["text"]


def test_tool_call_semantic_model_and_audit(env):
    app = env["app"]
    models = call(app, env["engineer"], "list_models")
    assert not models["isError"]
    sales = next(m for m in models["structuredContent"]["result"] if m["name"] == "Sales")
    res = call(app, env["engineer"], "query_model", {"model_id": sales["model_id"], "measures": ["orders"], "dimensions": ["channel"]})
    assert not res["isError"] and res["structuredContent"]["columns"] == ["channel", "orders"]
    ins = call(app, env["engineer"], "find_insights", {"model_id": sales["model_id"], "measures": ["revenue"]})
    assert any(f["kind"] == "forecast" for f in ins["structuredContent"]["facts"])
    a = env["c"].get("/api/audit?action=mcp.tool").json()
    rows = a if isinstance(a, list) else a.get("rows", a)
    assert any(r["target"] == "find_insights" for r in rows)


def test_pii_firewall_applies(env):
    res = call(env["app"], env["engineer"], "run_sql", {"sql": "SELECT c.email, o.card_number FROM raw.customers c, raw.orders o WHERE c.email LIKE '%@%' LIMIT 3"})
    txt = res["content"][0]["text"]
    assert "@" not in txt and ("<redacted>" in txt or "<email>" in txt)


def test_viewer_is_limited_to_serving(env):
    res = call(env["app"], env["viewer"], "list_datasets")
    assert {d["dataset"].split(".")[0] for d in res["structuredContent"]["result"]} == {"serving"}
    bad = call(env["app"], env["viewer"], "run_sql", {"sql": "select 1"})
    assert bad["isError"]


def test_rls_applies_through_mcp(env):
    c, app = env["c"], env["app"]
    p = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "province"}).json()
    add_user(c, app, "mcp.bc@acme.ca", "analyst")
    uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "mcp.bc@acme.ca")
    c.put(f"/api/users/{uid}/attributes", json={"attributes": {"province": ["BC"]}})
    bc = Client(app)
    bc.post("/api/auth/login", json={"email": "mcp.bc@acme.ca", "password": "another strong pass 7"})
    tok = token(bc, name="bc")
    try:
        res = call(app, tok, "run_sql", {"sql": "SELECT DISTINCT province FROM serving.orders_enriched"})
        assert res["structuredContent"]["rows"] == [["BC"]]
    finally:
        c.delete(f"/api/rls/{p['id']}")


def test_resources_and_prompts(env):
    app = env["app"]
    lst = rpc(app, env["engineer"], "resources/list").json()["result"]["resources"]
    uris = {r["uri"] for r in lst}
    assert "tessera://catalog" in uris and "tessera://dataset/serving.orders_enriched" in uris and any(u.startswith("tessera://model/") for u in uris)
    card = rpc(app, env["engineer"], "resources/read", {"uri": "tessera://dataset/serving.orders_enriched"}).json()["result"]["contents"][0]
    assert json.loads(card["text"])["name"] == "serving.orders_enriched"
    assert rpc(app, env["viewer"], "resources/read", {"uri": "tessera://dataset/serving.orders_enriched"}).json()["error"]["code"] == -32002
    assert rpc(app, env["engineer"], "resources/read", {"uri": "tessera://nope"}).json()["error"]["code"] == -32002
    ps = rpc(app, env["viewer"], "prompts/list").json()["result"]["prompts"]
    assert {p["name"] for p in ps} == {"leadership-update", "explain-metric-change", "triage-data-incident", "document-dataset"}
    g = rpc(app, env["viewer"], "prompts/get", {"name": "explain-metric-change", "arguments": {"model_id": "1", "measure": "revenue"}}).json()["result"]
    assert "revenue" in g["messages"][0]["content"]["text"] and "month" in g["messages"][0]["content"]["text"]
    assert rpc(app, env["viewer"], "prompts/get", {"name": "explain-metric-change", "arguments": {}}).json()["error"]["code"] == -32602


def test_disable_mcp(env):
    c = env["c"]
    c.put("/api/admin/mcp", json={"enabled": False})
    try:
        assert rpc(env["app"], env["admin"], "ping").status_code == 503
    finally:
        c.put("/api/admin/mcp", json={"enabled": True})
    cfg = c.get("/api/admin/mcp").json()
    assert cfg["enabled"] and cfg["url"].endswith("/mcp") and "run_sql" in cfg["tools"]["analyst"]


def test_stdio_bridge(env):
    from tessera.cli import mcp_bridge
    app = env["app"]
    tc = Client(app)

    def handler(req: httpx.Request):
        r = tc.post("/mcp", csrf=False, content=req.content, headers={"Authorization": req.headers["authorization"], "Content-Type": "application/json"})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})
    cl = httpx.Client(transport=httpx.MockTransport(handler))
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTO}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "garbage",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "glossary", "arguments": {}}}),
    ]) + "\n"
    out = io.StringIO()
    assert mcp_bridge("http://x", env["viewer"], stdin=io.StringIO(lines), stdout=out, client=cl) == 0
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [m.get("id") for m in msgs] == [1, None, 2]
    assert msgs[1]["error"]["code"] == -32700 and msgs[2]["result"]["isError"] is False
    out2 = io.StringIO()
    mcp_bridge("http://x", "tsr_bad", stdin=io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"), stdout=out2, client=cl)
    assert json.loads(out2.getvalue())["error"]["code"] == -32603
