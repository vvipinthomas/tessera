"""New agents (incident responder, semantic modeler) and reliability guards: retries, grounding, loop guard."""
import json

import httpx
import pytest
from test_agents import Script, add_provider, setup_env  # noqa: F401

from tessera import agents


def test_grounding_verifies_figures_against_tool_output():
    outs = ['{"rows": [[42574.33, 334]], "pct_change": -0.0372, "rate": 0.0601, "prev": 0.045}']
    g = agents.grounding("Revenue was $42.6K (down 3.7%) on 334 orders. Refund rate 6.0%, up 1.5 pts. Margin 87%.", outs)
    assert g["figures"] == 6 and g["unverified"] == ["87%"]
    assert agents.grounding("Revenue was 42,574.33 in Sep 2026 (2026-09-01).", outs)["unverified"] == []
    none = agents.grounding("Revenue grew 12% to $50K.", [])
    assert none["verified"] == 0 and len(none["unverified"]) == 2
    assert agents.grounding("Top 3 provinces in 2026.", [])["figures"] == 0
    assert agents.grounding("You asked about 250 orders.", [], "show me 250 orders")["unverified"] == []


class Flaky:
    def __init__(self, fails):
        self.fails, self.calls = list(fails), 0

    def __call__(self, req):
        self.calls += 1
        if self.fails:
            code = self.fails.pop(0)
            if code == "net":
                raise httpx.ConnectError("boom")
            return httpx.Response(code, headers={"retry-after": "0"}, text="busy")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})


def make_provider(handler):
    p = agents.Provider("anthropic", "", "m", "k", http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    p.sleep = lambda s: None
    return p


def test_provider_retries_transient_errors():
    h = Flaky([429, 529, "net"])
    assert make_provider(h).complete("s", [{"role": "user", "content": "hi"}], [])["text"] == "ok"
    assert h.calls == 4


def test_provider_gives_up_and_does_not_retry_client_errors():
    h = Flaky([503] * 10)
    with pytest.raises(agents.AgentError, match="after 4 attempts"):
        make_provider(h).complete("s", [{"role": "user", "content": "hi"}], [])
    h2 = Flaky([400])
    with pytest.raises(agents.AgentError, match="HTTP 400"):
        make_provider(h2).complete("s", [{"role": "user", "content": "hi"}], [])
    assert h2.calls == 1


def test_loop_guard_and_grounding_in_chat(setup_env):  # noqa: F811
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    same = ("tool", ("list_datasets", {}))
    holder["s"] = Script("anthropic", [same, same, same, ("text", "There are 999 datasets.")])
    r = c.post("/api/agents/chat", json={"agent": "analyst", "message": "how many datasets?"}).json()
    assert [t["ok"] for t in r["trace"]] == [True, True, False]
    assert "already ran" in r["trace"][2]["output_preview"]
    assert r["grounding"]["unverified"] == ["999"]


def test_incident_agent_reports_impact(setup_env):  # noqa: F811
    app, c, holder = setup_env
    add_provider(c, "openai")
    holder["s"] = Script("openai", [("tool", ("data_health", {})), ("tool", ("lineage_impact", {"dataset": "raw.orders"})), ("text", "Done.")])
    r = c.post("/api/agents/chat", json={"agent": "incident", "message": "what is broken?"})
    assert r.status_code == 200, r.text
    tr = r.json()["trace"]
    assert [t["tool"] for t in tr] == ["data_health", "lineage_impact"] and all(t["ok"] for t in tr)
    tool_msgs = [m for m in holder["s"].requests[-1]["body"]["messages"] if m["role"] == "tool"]
    health, lin = json.loads(tool_msgs[0]["content"]), json.loads(tool_msgs[1]["content"])
    assert "failing_rules" in health and "failed_runs" in health and isinstance(health["healthy"], bool)
    # the sample's not_future rule fails on purpose (future-dated orders)
    assert any("future" in (f["rule"] or "").lower() or f["rule"].startswith("not_future") for f in health["failing_rules"])
    imp = lin["impact"]
    assert "serving.orders_enriched" in imp["downstream_tables"]
    assert any(m["name"] == "Sales" for m in imp["models"]) and any(d["name"] == "Sales overview" for d in imp["dashboards"])


def test_modeler_agent_drafts_without_saving(setup_env):  # noqa: F811
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    before = len(c.get("/api/models").json())
    holder["s"] = Script("anthropic", [("tool", ("search_catalog", {"query": "order"})), ("tool", ("suggest_model", {"dataset": "serving.orders_enriched", "name": "Orders draft"})),
                                       ("text", "Here is a draft.")])
    r = c.post("/api/agents/chat", json={"agent": "modeler", "message": "build me an orders model"}).json()
    assert [t["ok"] for t in r["trace"]] == [True, True]
    art = r["artifacts"][0]
    assert art["type"] == "model" and art["model"]["measures"]
    assert len(c.get("/api/models").json()) == before


def test_new_tools_respect_roles():
    names = lambda agent, role: {t["name"] for t in agents.tools_for(agent, role)}  # noqa: E731
    assert "run_details" not in names("incident", "analyst") and "run_details" in names("incident", "engineer")
    assert "suggest_model" not in names("modeler", "analyst")
    assert {"glossary", "search_catalog"} <= names("analyst", "viewer")
