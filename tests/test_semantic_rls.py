"""Semantic models, governed queries and row-level security."""
import json

import httpx
import pytest
from conftest import add_user


@pytest.fixture(scope="module")
def env(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    mid = c.get("/api/models").json()[0]["id"]
    return {"c": c, "app": app, "mid": mid}


def bc_total(c):
    return c.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched where province = 'BC'"}).json()["rows"][0][0]


def test_model_query_compiles_and_formats(env):
    c, mid = env["c"], env["mid"]
    r = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["revenue", "orders"], "dimensions": ["order_date"], "grains": {"order_date": "quarter"}}}).json()
    assert r["fields"][1]["format"] == "currency" and r["fields"][0]["grain"] == "quarter"
    dates = [row[0] for row in r["rows"]]
    assert dates == sorted(dates) and all(d[5:7] in ("01", "04", "07", "10") for d in dates)
    total = sum(row[1] or 0 for row in r["rows"])
    one = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["revenue"]}}).json()["rows"][0][0]
    assert abs(total - one) < 0.01, "grouped totals add up to the overall measure"


def test_model_filters_are_parameters_not_sql(env):
    c, mid = env["c"], env["mid"]
    evil = "BC') OR 1=1 --"
    r = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["orders"], "filters": [{"field": "province", "op": "=", "value": evil}]}}).json()
    assert r["rows"] == [[0]]
    bad = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["revenue; drop table x"]}})
    assert bad.status_code == 400


def test_period_comparison(env):
    c, mid = env["c"], env["mid"]
    r = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["orders"], "filters": [{"field": "order_date", "op": "between", "value": ["2026-04-01", "2026-06-30"]}]},
                                              "compare": True}).json()
    direct = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["orders"], "filters": [{"field": "order_date", "op": "between", "value": ["2026-01-01", "2026-03-31"]}]}}).json()
    assert r["previous_filters"][0]["value"] == ["2026-01-01", "2026-03-31"]
    assert r["previous"] == direct["rows"]


def test_model_validation_rejects_unsafe_expressions(env):
    c = env["c"]
    for expr in ["sum(x) from raw.customers", "(select count(*) from raw.customers)", "sum(order_total); drop table t", "sum(order_total) -- hi"]:
        r = c.post("/api/models", json={"name": "bad", "base": "serving.orders_enriched", "dimensions": [], "measures": [{"name": "m", "agg": "custom", "expr": expr}]})
        assert r.status_code == 400, expr
    r = c.post("/api/models", json={"name": "bad2", "base": "serving.orders_enriched", "measures": [{"name": "m", "agg": "sum", "column": "nope"}]})
    assert r.status_code == 400


def test_model_suggest_and_create_with_join(env):
    c = env["c"]
    draft = c.post("/api/models/suggest", json={"base": "serving.orders_enriched"}).json()
    assert any(m["agg"] == "sum" for m in draft["measures"]) and any(d["type"] == "time" for d in draft["dimensions"])
    draft["name"] = "Orders with customers"
    draft["joins"] = [{"table": "serving.customers", "alias": "c", "left_key": "customer_id", "right_key": "customer_id"}]
    draft["dimensions"].append({"name": "cust_province", "column": "c.province", "type": "category"})
    r = c.post("/api/models", json=draft)
    assert r.status_code == 200, r.text
    q = c.post(f"/api/models/{r.json()['id']}/query", json={"spec": {"measures": ["row_count"], "dimensions": ["cust_province"]}}).json()
    assert sum(x[1] for x in q["rows"]) == 9000
    mixed = {**draft, "name": "mixed", "joins": [{"table": "raw.customers", "alias": "c", "left_key": "customer_id", "right_key": "customer_id"}]}
    assert c.post("/api/models", json=mixed).status_code == 400


def test_only_admin_certifies(env):
    c, app, mid = env["c"], env["app"], env["mid"]
    eng = add_user(c, app, "cert-eng@acme.ca", "engineer")
    assert eng.patch(f"/api/models/{mid}/endorsement", json={"endorsement": "certified"}).status_code == 403
    assert eng.patch(f"/api/models/{mid}/endorsement", json={"endorsement": "promoted"}).status_code == 200
    c.patch(f"/api/models/{mid}/endorsement", json={"endorsement": "certified"})


@pytest.fixture(scope="module")
def rls(env):
    c, app = env["c"], env["app"]
    p = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "region"})
    assert p.status_code == 200, p.text
    bc = add_user(c, app, "bc.analyst@acme.ca", "analyst")
    none = add_user(c, app, "no.attr@acme.ca", "analyst")
    allv = add_user(c, app, "all.regions@acme.ca", "viewer")
    users = {u["email"]: u["id"] for u in c.get("/api/users").json()}
    c.put(f"/api/users/{users['bc.analyst@acme.ca']}/attributes", json={"attributes": {"region": "BC, AB"}})
    c.put(f"/api/users/{users['all.regions@acme.ca']}/attributes", json={"attributes": {"region": ["*"]}})
    yield {"bc": bc, "none": none, "all": allv, "policy": p.json()["id"]}
    c.delete(f"/api/rls/{p.json()['id']}")


def provinces(client, sql):
    r = client.post("/api/sql", json={"sql": sql})
    assert r.status_code == 200, r.text
    return {row[0] for row in r.json()["rows"]}


@pytest.mark.parametrize("sql", [
    "select distinct province from serving.orders_enriched",
    "select distinct province from orders_enriched",
    "with x as (select * from serving.orders_enriched) select distinct province from x",
    "select distinct o.province from serving.orders_enriched o join serving.orders_enriched o2 using (order_id)",
    "select distinct province from (select province from serving.orders_enriched where order_total > 0)",
    "select province from serving.customers c where province in (select province from serving.orders_enriched) group by 1",
])
def test_rls_applies_however_the_table_is_referenced(env, rls, sql):
    got = provinces(rls["bc"], sql)
    if "serving.customers c" in sql:
        assert got <= {"BC", "AB"}
    else:
        assert got == {"BC", "AB"}


def test_rls_counts_and_defaults(env, rls):
    c = env["c"]
    total = c.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"][0][0]
    bc_ab = c.post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched where province in ('BC','AB')"}).json()["rows"][0][0]
    assert rls["bc"].post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"][0][0] == bc_ab
    assert rls["none"].post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"][0][0] == 0, "no attribute means no rows"
    assert rls["all"].post("/api/queries/1/run").status_code == 200
    assert total > bc_ab > 0
    assert bc_total(c) > 0


def test_rls_on_models_dashboards_exports_and_preview(env, rls):
    c, mid = env["c"], env["mid"]
    r = rls["bc"].post(f"/api/models/{mid}/query", json={"spec": {"measures": ["orders"], "dimensions": ["province"]}}).json()
    assert {row[0] for row in r["rows"]} == {"BC", "AB"}
    qid = next(q["id"] for q in c.get("/api/queries").json() if q["name"] == "Revenue by province")
    assert {row[0] for row in rls["bc"].post(f"/api/queries/{qid}/run").json()["result"]["rows"]} == {"BC", "AB"}
    csv = rls["bc"].post(f"/api/queries/{qid}/export", json={"format": "csv"}).text
    assert "ON" not in csv and "BC" in csv
    did = next(d["id"] for d in c.get("/api/datasets").json() if d["schema_name"] == "serving" and d["name"] == "orders_enriched")
    prev = rls["bc"].get(f"/api/datasets/{did}/preview?limit=1000").json()
    assert {r[prev["columns"].index("province")] for r in prev["rows"]} <= {"BC", "AB"}
    assert rls["bc"].get(f"/api/datasets/{did}/profile").status_code == 403
    listed = next(d for d in rls["bc"].get("/api/datasets").json() if d["id"] == did)
    assert listed["row_count"] is None and listed["rls"]
    vals = rls["bc"].get(f"/api/models/{mid}/values?field=province").json()
    assert set(vals) == {"BC", "AB"}


def test_engineers_are_not_filtered(env, rls):
    assert len(provinces(env["c"], "select distinct province from serving.orders_enriched")) == 8


def test_rls_applies_to_agents(env, rls, settings=None):
    app, c = env["app"], env["c"]
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        if len(sent) == 1:
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "run_sql", "input": {"sql": "select distinct province from serving.orders_enriched"}}],
                                             "usage": {"input_tokens": 1, "output_tokens": 1}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 1, "output_tokens": 1}})
    from tessera import agents
    orig = agents.Provider.__init__

    def patched(self, *a, **kw):
        orig(self, *a, **kw)
        self.http_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    agents.Provider.__init__ = patched
    try:
        if not c.get("/api/ai/providers").json()["providers"]:
            c.post("/api/ai/providers", json={"name": "t", "kind": "anthropic", "model": "m", "api_key": "k"})
        r = rls["bc"].post("/api/agents/chat", json={"agent": "analyst", "message": "provinces?"})
        assert r.status_code == 200, r.text
    finally:
        agents.Provider.__init__ = orig
    tool_result = json.dumps(sent[1]["messages"][-1])
    assert "BC" in tool_result and "ON" not in tool_result and "QC" not in tool_result
