"""Agents against scripted Anthropic- and OpenAI-format providers (no network)."""
import json

import httpx
import pytest
from conftest import Client, do_setup, make_app


class Script:
    """Replays model turns and records what Tessera sent to the provider."""

    def __init__(self, fmt, turns):
        self.fmt, self.turns, self.requests = fmt, list(turns), []

    def handler(self, req: httpx.Request):
        body = json.loads(req.content)
        self.requests.append({"url": str(req.url), "headers": dict(req.headers), "body": body})
        kind, payload = self.turns.pop(0)
        if self.fmt == "anthropic":
            if kind == "tool":
                content = [{"type": "tool_use", "id": f"tu{len(self.requests)}", "name": payload[0], "input": payload[1]}]
            else:
                content = [{"type": "text", "text": payload}]
            return httpx.Response(200, json={"content": content, "usage": {"input_tokens": 100, "output_tokens": 20}, "stop_reason": "end_turn"})
        if kind == "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(self.requests)}", "type": "function",
                                                                       "function": {"name": payload[0], "arguments": json.dumps(payload[1])}}]}
        else:
            msg = {"role": "assistant", "content": payload}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}})


@pytest.fixture
def setup_env(settings):
    holder = {}
    app = make_app(settings, http_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: holder["s"].handler(r))))
    c = Client(app)
    do_setup(c, settings, sample=True)
    return app, c, holder


def add_provider(c, kind):
    r = c.post("/api/ai/providers", json={"name": kind, "kind": kind, "model": "test-model", "api_key": "sk-test", "is_default": True})
    assert r.status_code == 200
    return r.json()["id"]


def test_no_provider_message(settings):
    app = make_app(settings)
    c = Client(app)
    do_setup(c, settings)
    r = c.post("/api/agents/chat", json={"agent": "analyst", "message": "hi"})
    assert r.status_code == 400 and "Ollama" in r.json()["detail"]


def test_anthropic_tool_loop_and_pii_firewall(setup_env):
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    holder["s"] = Script("anthropic", [
        ("tool", ("run_sql", {"sql": "select email, card_number, order_total from curated.orders_enriched limit 3"})),
        ("text", "Here are three orders."),
    ])
    r = c.post("/api/agents/chat", json={"agent": "analyst", "message": "show me 3 orders"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["reply"] == "Here are three orders." and j["trace"][0]["tool"] == "run_sql" and j["trace"][0]["ok"]
    sent = holder["s"].requests
    assert sent[0]["url"].endswith("/v1/messages") and sent[0]["headers"]["x-api-key"] == "sk-test"
    assert {t["name"] for t in sent[0]["body"]["tools"]} == {"list_models", "query_model", "executive_brief", "find_insights", "explain_change", "decompose", "glossary", "search_catalog", "list_datasets", "describe_dataset", "run_sql", "propose_chart"}
    tool_result = json.dumps(sent[1]["body"]["messages"][-1])
    assert "tool_result" in tool_result
    assert "@" not in tool_result.replace("@x", ""), "emails must not reach the model"
    assert not any(ch.isdigit() and len(ch) >= 16 for ch in tool_result.split()), "card numbers must not reach the model"
    assert "<redacted>" in tool_result or "<email>" in tool_result
    assert j["tokens_in"] == 200
    # conversation continues with history
    holder["s"] = Script("anthropic", [("text", "Still here.")])
    r2 = c.post("/api/agents/chat", json={"agent": "analyst", "message": "thanks", "session_id": j["session_id"]}).json()
    assert r2["reply"] == "Still here."
    assert len(holder["s"].requests[0]["body"]["messages"]) == 5


def test_openai_chart_artifact(setup_env):
    app, c, holder = setup_env
    add_provider(c, "openai")
    holder["s"] = Script("openai", [
        ("tool", ("propose_chart", {"title": "By channel", "sql": "select channel, count(*) n from serving.orders_enriched group by 1",
                                    "chart_type": "bar", "x": "channel", "y": "n"})),
        ("text", "Proposed a chart."),
    ])
    j = c.post("/api/agents/chat", json={"agent": "analyst", "message": "orders by channel?"}).json()
    assert j["artifacts"][0]["type"] == "chart" and j["artifacts"][0]["viz"]["x"] == "channel"
    req = holder["s"].requests[0]
    assert req["url"].endswith("/chat/completions") and req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["messages"][0]["role"] == "system"
    assert holder["s"].requests[1]["body"]["messages"][-1]["role"] == "tool"


def test_analyst_agent_cannot_reach_raw_or_write(setup_env):
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    c.post("/api/users", json={"email": "ana@acme.ca", "role": "analyst", "password": "another strong pass 7"})
    a = Client(app)
    a.post("/api/auth/login", json={"email": "ana@acme.ca", "password": "another strong pass 7"})
    holder["s"] = Script("anthropic", [
        ("tool", ("run_sql", {"sql": "select * from raw.customers"})),
        ("tool", ("create_rule", {"dataset": "serving.orders_enriched", "kind": "not_null", "column": "order_id"})),
        ("text", "I could not access that."),
    ])
    j = a.post("/api/agents/chat", json={"agent": "steward", "message": "check raw customers"}).json()
    assert [t["ok"] for t in j["trace"]] == [False, False]
    assert "not available" in j["trace"][1]["output_preview"]
    offered = {t["name"] for t in holder["s"].requests[0]["body"]["tools"]}
    assert "create_rule" not in offered and "run_quality" not in offered


def test_engineer_agents_write_with_audit(setup_env):
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    holder["s"] = Script("anthropic", [
        ("tool", ("update_description", {"dataset": "raw.customers", "description": "Customer master from the retail sample",
                                         "columns": {"customer_id": "Unique customer key"}})),
        ("tool", ("readiness_report", {"dataset": "raw.customers"})),
        ("text", "Documented."),
    ])
    j = c.post("/api/agents/chat", json={"agent": "readiness", "message": "document customers"}).json()
    assert all(t["ok"] for t in j["trace"])
    ds = next(d for d in c.get("/api/datasets").json() if d["schema_name"] == "raw" and d["name"] == "customers")
    assert ds["description"] == "Customer master from the retail sample"
    assert any(a["action"] == "agent.update_description" for a in c.get("/api/audit").json())


def test_pipeline_draft_artifact(setup_env):
    app, c, holder = setup_env
    add_provider(c, "ollama")
    holder["s"] = Script("openai", [
        ("tool", ("draft_pipeline", {"name": "bad", "steps": [{"id": "a", "type": "transform", "sql": "select 1", "target": "serving.x"}]})),
        ("tool", ("draft_pipeline", {"name": "Nightly", "schedule": "0 2 * * *", "steps": [
            {"id": "t", "type": "transform", "sql": "select * from raw.orders", "target": "curated.o2"},
            {"id": "q", "type": "quality", "dataset": "curated.o2", "depends_on": ["t"]}]})),
        ("text", "Drafted."),
    ])
    j = c.post("/api/agents/chat", json={"agent": "engineer", "message": "build a nightly pipeline"}).json()
    assert "serving" in j["trace"][0]["output_preview"]
    assert j["artifacts"] == [{"type": "pipeline", "name": "Nightly", "description": "", "schedule": "0 2 * * *",
                               "steps": j["artifacts"][0]["steps"]}]
    assert "authorization" not in holder["s"].requests[0]["headers"] or holder["s"].requests[0]["headers"]["authorization"] == "Bearer sk-test"


def test_provider_errors_surface(setup_env):
    app, c, holder = setup_env
    add_provider(c, "anthropic")

    class Boom:
        requests = []

        def handler(self, req):
            return httpx.Response(401, json={"error": "invalid x-api-key"})
    holder["s"] = Boom()
    r = c.post("/api/agents/chat", json={"agent": "analyst", "message": "hi"})
    assert r.status_code == 502 and "401" in r.json()["detail"]


def test_provider_key_encrypted(setup_env):
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    import sqlite3
    assert "sk-test" not in str(sqlite3.connect(app.state.settings.meta_path).execute("select * from ai_providers").fetchall())
    assert "sk-test" not in json.dumps(c.get("/api/ai/providers").json())
