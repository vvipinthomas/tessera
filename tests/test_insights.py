"""Insights engine: statistics, explain-change additivity, decomposition, dashboards, AI narrative and dashboard Q&A."""
import json
import math

import pytest
from conftest import add_user
from test_agents import Script, add_provider, setup_env  # noqa: F401  (fixture reuse)

from tessera import insights


# ---------------------------------------------------------------- pure statistics
def test_ols_and_holt_follow_a_clean_line():
    y = [10 + 2 * i for i in range(12)]
    fit = insights.ols(y)
    assert fit["slope"] == pytest.approx(2) and fit["r2"] == pytest.approx(1)
    fc = insights.holt(y, 3)
    assert [round(p["value"], 6) for p in fc["points"]] == pytest.approx([34, 36, 38], abs=1e-6)
    assert all(p["lower"] <= p["value"] <= p["upper"] for p in fc["points"])


def test_holt_interval_widens_with_horizon():
    y = [100, 104, 99, 107, 103, 110, 108, 115, 111, 118]
    pts = insights.holt(y, 4)["points"]
    widths = [p["upper"] - p["lower"] for p in pts]
    assert widths == sorted(widths) and widths[0] > 0


def test_robust_outliers_flags_only_the_spike():
    y = [50, 52, 49, 51, 50, 53, 48, 120, 51, 50, 52, 49]
    fit = insights.ols(y)
    out = insights.robust_outliers(y, fit["fit"])
    assert [i for i, _ in out] == [7] and out[0][1] > 0


def test_flat_series_has_no_outliers():
    assert insights.robust_outliers([5.0] * 10, [5.0] * 10) == []


@pytest.mark.parametrize("m,expected", [
    ({"agg": "sum"}, True), ({"agg": "count_rows"}, True), ({"agg": "avg"}, False), ({"agg": "count_distinct"}, False),
    ({"agg": "custom", "expr": "sum(order_total) FILTER (WHERE status IN ('paid','shipped'))"}, True),
    ({"agg": "custom", "expr": "count(*) FILTER (WHERE status = 'refunded') / count(*)"}, False),
    ({"agg": "custom", "expr": "avg(order_total) FILTER (WHERE x)"}, False),
    ({"agg": "custom", "expr": "count(DISTINCT customer_id)"}, False),
])
def test_additivity_detection(m, expected):
    assert insights.additive(m) is expected


def test_formatting():
    cur = {"format": "currency", "decimals": 0}
    assert insights.fmt(42574.3, cur) == "$42.6K" and insights.fmt(9831.29, cur) == "$9,831"
    assert insights.fmt(1_250_000, cur) == "$1.2M" or insights.fmt(1_250_000, cur) == "$1.3M"
    assert insights.fmt(0.0612, {"format": "percent", "decimals": 1}) == "6.1%"
    assert insights.fmt_delta(0.045, 0.06, {"format": "percent"}) == "+1.5 pts"
    assert insights.fmt_delta(100, 90, cur) == "-10.0%"


def test_analyze_result_skips_partial_and_future_periods():
    from datetime import date
    rows = [[f"2026-{m:02d}-01", 100 + m] for m in range(1, 11)] + [["2027-03-01", 5]]
    res = {"fields": [{"name": "d", "kind": "dimension", "type": "time", "grain": "month"}, {"name": "v", "kind": "measure"}], "rows": rows}
    a = insights.analyze_result({}, res, forecast=2, anomalies=True, today=date(2026, 10, 13))
    assert a["partial"] == "2026-10-01"
    assert [p["period"] for p in a["forecast"]] == ["2026-10-01", "2026-11-01"]
    assert a["forecast"][0]["value"] == pytest.approx(110, abs=0.5)


# ---------------------------------------------------------------- against the sample model
def _model(c):
    return next(m for m in c.get("/api/models").json() if m["name"] == "Sales")


def test_model_insights_facts(sample_env):
    c = sample_env["client"]
    m = _model(c)
    r = c.post(f"/api/models/{m['id']}/insights", json={"measures": ["revenue", "refund_rate"]})
    assert r.status_code == 200, r.text
    j = r.json()
    kinds = {(f["measure"], f["kind"]) for f in j["facts"]}
    assert ("revenue", "change") in kinds and ("revenue", "forecast") in kinds and ("revenue", "driver") in kinds
    assert all(f["id"].startswith("F") and f["text"] for f in j["facts"])
    s = j["series"]["revenue"]
    assert len(s["points"]) >= 12 and s["partial_note"]
    # the sample deliberately contains a few future-dated orders: they must not reach the series
    assert all(p["period"] <= s["forecast"]["points"][0]["period"] for p in s["points"])
    assert "future_note" in s
    # refund rate: lower is better, so a rise is reported as bad
    ch = next(f for f in j["facts"] if f["measure"] == "refund_rate" and f["kind"] == "change")
    d = ch["data"]
    assert ch["severity"] == ("bad" if d["current"] > d["previous"] else "good" if d["current"] < d["previous"] else "info")


def test_explain_change_is_additive_for_sums(sample_env):
    c = sample_env["client"]
    m = _model(c)
    j = c.post(f"/api/models/{m['id']}/explain", json={"measure": "revenue", "top": 50}).json()
    assert j["additive"] is True and j["summary"]
    for d in j["dimensions"]:
        if d["member_count"] <= 50:
            assert sum(x["change"] for x in d["members"]) == pytest.approx(j["change"], rel=1e-9, abs=1e-6)
    ratio = c.post(f"/api/models/{m['id']}/explain", json={"measure": "refund_rate"}).json()
    assert ratio["additive"] is False and "not sum" in ratio["method"]


def test_explain_change_explicit_periods(sample_env):
    c = sample_env["client"]
    m = _model(c)
    j = c.post(f"/api/models/{m['id']}/explain", json={"measure": "orders", "current": "2026-09-01", "baseline": "2025-09-01"}).json()
    assert j["current"]["label"] == "Sep 2026" and j["baseline"]["label"] == "Sep 2025"
    direct = c.post(f"/api/models/{m['id']}/query", json={"spec": {"measures": ["orders"], "filters": [
        {"field": "order_date", "op": "between", "value": ["2026-09-01", "2026-09-30"]}]}}).json()
    assert j["current"]["value"] == direct["rows"][0][0]


def test_decomposition_tree(sample_env):
    c = sample_env["client"]
    m = _model(c)
    root = c.post(f"/api/models/{m['id']}/decompose", json={"measure": "revenue"}).json()
    assert root["dimension"] and root["why"]
    assert sum(k["value"] for k in root["children"]) == pytest.approx(root["total"], rel=1e-9)
    assert sum(k["share"] for k in root["children"] if k["share"] is not None) == pytest.approx(1, abs=1e-9)
    top = root["children"][0]["member"]
    lvl2 = c.post(f"/api/models/{m['id']}/decompose", json={"measure": "revenue", "path": [{"field": root["dimension"], "value": top}],
                                                            "dimension": root["remaining"][0]}).json()
    assert lvl2["total"] == pytest.approx(root["children"][0]["value"], rel=1e-9)
    assert root["dimension"] not in lvl2["remaining"] and lvl2["dimension"] == root["remaining"][0]
    bad = c.post(f"/api/models/{m['id']}/decompose", json={"measure": "revenue", "path": [{"field": root["dimension"], "value": top}],
                                                           "dimension": root["dimension"]})
    assert bad.status_code == 400


def test_dashboard_insights_respect_filters(sample_env):
    c = sample_env["client"]
    d = next(x for x in c.get("/api/dashboards").json() if x["name"] == "Sales overview")
    all_ = c.post(f"/api/dashboards/{d['id']}/insights", json={}).json()
    on = c.post(f"/api/dashboards/{d['id']}/insights", json={"filters": [{"field": "channel", "op": "in", "value": ["web"]}]}).json()
    assert all_["facts"] and on["facts"] and not all_["errors"]
    rev = lambda j: next(f["data"]["current"] for f in j["facts"] if f["measure"] == "revenue" and f["kind"] == "change")  # noqa: E731
    assert rev(on) < rev(all_)


def test_rls_applies_to_insights(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    m = _model(c)
    r = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "province"})
    assert r.status_code == 200, r.text
    v = add_user(c, app, "bc.viewer@acme.ca", "viewer")
    uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "bc.viewer@acme.ca")
    assert c.put(f"/api/users/{uid}/attributes", json={"attributes": {"province": ["BC"]}}).status_code == 200
    try:
        root = v.post(f"/api/models/{m['id']}/decompose", json={"measure": "revenue", "dimension": "province"}).json()
        assert [k["member"] for k in root["children"]] == ["BC"]
        ex = v.post(f"/api/models/{m['id']}/explain", json={"measure": "revenue", "dimensions": ["province"]}).json()
        assert {x["member"] for d in ex["dimensions"] for x in d["members"]} <= {"BC"}
    finally:
        c.delete(f"/api/rls/{r.json()['id']}")


def test_query_tile_forecast_and_anomalies(sample_env):
    c = sample_env["client"]
    m = _model(c)
    r = c.post("/api/queries", json={"name": "Revenue forecast", "kind": "model", "model_id": m["id"],
                                     "spec": {"measures": ["revenue"], "dimensions": ["order_date"], "grains": {"order_date": "month"}},
                                     "viz": {"type": "line", "forecast": 3, "anomalies": True}})
    assert r.status_code == 200, r.text
    res = c.post(f"/api/queries/{r.json()['id']}/run", json={}).json()["result"]
    a = res["analysis"]
    assert len(a["forecast"]) == 3 and a["partial"]
    assert isinstance(a["anomalies"], list) and a["forecast_method"] and a["forecast_accuracy"]["holdout_periods"] >= 3
    assert all(set(x) >= {"period", "value", "expected", "z"} for x in a["anomalies"])
    c.delete(f"/api/queries/{r.json()['id']}")


# ---------------------------------------------------------------- AI narrative and dashboard Q&A
def test_narrative_is_grounded_in_facts(setup_env):  # noqa: F811
    app, c, holder = setup_env
    add_provider(c, "anthropic")
    holder["s"] = Script("anthropic", [("text", "- Revenue fell 3.7% last month [F1]\n- Made-up claim [F99]")])
    d = next(x for x in c.get("/api/dashboards").json() if x["name"] == "Sales overview")
    j = c.post(f"/api/dashboards/{d['id']}/insights", json={"narrative": True}).json()
    sent = holder["s"].requests[0]["body"]
    assert "tools" not in sent
    prompt = sent["messages"][0]["content"]
    assert prompt.startswith("Facts:") and "F1 (" in prompt
    assert "card_number" not in prompt and "@" not in prompt
    assert j["narrative"]["unknown_citations"] == ["F99"]


def test_dashboard_ask_uses_insight_tools(setup_env):  # noqa: F811
    app, c, holder = setup_env
    add_provider(c, "openai")
    mid = next(m["id"] for m in c.get("/api/models").json() if m["name"] == "Sales")
    holder["s"] = Script("openai", [("tool", ("explain_change", {"model_id": mid, "measure": "revenue"})), ("text", "Revenue fell mainly in paid orders.")])
    d = next(x for x in c.get("/api/dashboards").json() if x["name"] == "Sales overview")
    r = c.post(f"/api/dashboards/{d['id']}/ask", json={"message": "Why did revenue drop?", "filters": [{"field": "channel", "op": "in", "value": ["web"]}]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["trace"][0]["tool"] == "explain_change" and j["trace"][0]["ok"]
    system = holder["s"].requests[0]["body"]["messages"][0]["content"]
    assert "Sales overview" in system and "channel" in system
    tool_msg = holder["s"].requests[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["additive"] is True
    assert c.get(f"/api/agents/sessions/{j['session_id']}").status_code == 200


def test_viewer_cannot_use_dashboard_ask(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    v = add_user(c, app, "asker@acme.ca", "viewer")
    d = next(x for x in c.get("/api/dashboards").json() if x["name"] == "Sales overview")
    assert v.post(f"/api/dashboards/{d['id']}/ask", json={"message": "hi"}).status_code == 403
    assert v.post(f"/api/dashboards/{d['id']}/insights", json={}).status_code == 200
    _ = math
