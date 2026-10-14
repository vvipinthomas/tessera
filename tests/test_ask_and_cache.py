"""Plain-English questions (no AI provider) and the query result cache."""
from datetime import date

import pyarrow as pa
import pytest
from conftest import Client, add_user

from tessera import nlq

TODAY = date(2026, 10, 13)
MODEL = {"measures": [{"name": "revenue", "label": "Revenue"}, {"name": "orders", "label": "Orders"}, {"name": "units", "label": "Units sold"},
                      {"name": "refund_rate", "label": "Refund rate"}],
         "dimensions": [{"name": "order_date", "label": "Order date", "type": "time"}, {"name": "province", "label": "Province", "type": "category"},
                        {"name": "channel", "label": "Channel", "type": "category"}, {"name": "category", "label": "Product category", "type": "category"}]}
VALUES = {"province": ["BC", "AB", "ON", "QC"], "channel": ["web", "store", "mobile", "partner"], "category": ["Electronics", "Books", "Outdoor"]}
SYN = {"revenue": ["sales", "GMV"], "refund_rate": ["return rate"]}


def ask(q):
    return nlq.parse(q, MODEL, VALUES, SYN, TODAY)


def flt(p, field):
    return next((f for f in p["spec"]["filters"] if f["field"] == field), None)


def test_top_n_with_synonym_and_quarter():
    p = ask("top 5 provinces by sales last quarter")
    assert p["spec"]["measures"] == ["revenue"] and p["spec"]["dimensions"] == ["province"]
    assert p["spec"]["sort"] == [{"field": "revenue", "dir": "desc"}] and p["spec"]["limit"] == 5
    assert flt(p, "order_date")["value"] == ["2026-07-01", "2026-09-30"]
    assert p["viz"]["type"] == "bar" and not p["notes"]


def test_trend_with_value_filter_and_grain():
    p = ask("monthly revenue trend in BC")
    assert p["spec"]["dimensions"] == ["order_date"] and p["spec"]["grains"] == {"order_date": "month"}
    assert flt(p, "province") == {"field": "province", "op": "in", "value": ["BC"]}
    assert p["viz"]["type"] == "line"
    w = ask("weekly units sold for web electronics since 2026-03")
    assert w["spec"]["grains"] == {"order_date": "week"} and flt(w, "order_date")["value"] == ["2026-03-01", "2026-10-13"]
    assert flt(w, "channel")["value"] == ["web"] and flt(w, "category")["value"] == ["Electronics"]


@pytest.mark.parametrize("q,rng", [
    ("revenue this year", ["2026-01-01", "2026-10-13"]), ("revenue ytd", ["2026-01-01", "2026-10-13"]),
    ("revenue last month", ["2026-09-01", "2026-09-30"]), ("revenue last 6 months", ["2026-04-01", "2026-09-30"]),
    ("revenue in 2025", ["2025-01-01", "2025-12-31"]), ("revenue in q2 2025", ["2025-04-01", "2025-06-30"]),
    ("revenue last year", ["2025-01-01", "2025-12-31"]), ("revenue last 30 days", ["2026-09-14", "2026-10-13"]),
])
def test_time_ranges(q, rng):
    p = ask(q)
    assert flt(p, "order_date")["value"] == rng
    assert p["viz"]["type"] == "kpi" and p["compare"] is True


def test_exclusions_shares_combos_and_series():
    p = ask("orders by channel excluding partner")
    assert flt(p, "channel") == {"field": "channel", "op": "not_in", "value": ["partner"]}
    assert ask("sales share by product category in 2025")["viz"]["type"] == "donut"
    c = ask("revenue and orders by province")
    assert c["viz"] == {"type": "combo", "x": "province", "y": "revenue", "y2": "orders"}
    m = ask("revenue by month by channel")
    assert m["viz"]["type"] == "multiline" and m["spec"]["dimensions"] == ["order_date", "channel"]
    assert ask("forecast revenue by month")["viz"]["forecast"] == 3


def test_reports_what_it_did_not_understand():
    p = ask("revenue for the moon base")
    assert any("moon" in n for n in p["notes"]) and p["confidence"] < 1
    q = ask("how is it going")
    assert any(n.startswith("No measure") for n in q["notes"])


def test_ask_endpoint_runs_governed_query(sample_env):
    c = sample_env["client"]
    mid = next(m["id"] for m in c.get("/api/models").json() if m["name"] == "Sales")
    r = c.post(f"/api/models/{mid}/ask", json={"question": "top 3 provinces by sales in 2025"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["understood"].startswith("Revenue · by Province · 2025 · top 3")
    assert len(j["result"]["rows"]) == 3 and j["result"]["rows"][0][1] >= j["result"]["rows"][1][1]
    direct = c.post(f"/api/models/{mid}/query", json={"spec": {"measures": ["revenue"], "filters": [{"field": "province", "op": "=", "value": j["result"]["rows"][0][0]},
                                                                                                     {"field": "order_date", "op": "between", "value": ["2025-01-01", "2025-12-31"]}]}}).json()
    assert direct["rows"][0][0] == pytest.approx(j["result"]["rows"][0][1])
    g = c.post("/api/ask", json={"question": "monthly sales in web"}).json()
    assert g["model"] == "Sales" and g["viz"]["type"] == "line" and g["result"]["rows"]
    assert c.post("/api/ask", json={"question": " "}).status_code == 400


def test_ask_respects_rls(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    p = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "province"}).json()
    add_user(c, app, "ask.bc@acme.ca", "viewer")
    uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "ask.bc@acme.ca")
    c.put(f"/api/users/{uid}/attributes", json={"attributes": {"province": ["BC"]}})
    v = Client(app)
    v.post("/api/auth/login", json={"email": "ask.bc@acme.ca", "password": "another strong pass 7"})
    try:
        j = v.post("/api/ask", json={"question": "revenue by province"}).json()
        assert [r[0] for r in j["result"]["rows"]] == ["BC"]
        # a value the viewer cannot see is not offered as a filter either
        assert not any(f["field"] == "province" for f in v.post("/api/ask", json={"question": "revenue in ON"}).json()["spec"]["filters"])
    finally:
        c.delete(f"/api/rls/{p['id']}")


# ---------------------------------------------------------------- cache
def test_cache_hits_and_invalidates_on_write(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    wh = app.state.reporter.p.wh
    sql = "select count(*) from serving.orders_enriched"
    first = c.post("/api/sql", json={"sql": sql}).json()
    hits = wh.cache_hits
    again = c.post("/api/sql", json={"sql": sql}).json()
    assert again["rows"] == first["rows"] and wh.cache_hits == hits + 1
    wh.load_arrow(pa.table({"x": [1, 2, 3]}), "raw", "cache_probe")
    wh.internal("CREATE OR REPLACE TABLE serving.cache_probe AS SELECT * FROM raw.cache_probe")
    assert c.post("/api/sql", json={"sql": "select count(*) from serving.cache_probe"}).json()["rows"] == [[3]]
    wh.load_arrow(pa.table({"x": [4]}), "raw", "cache_probe", mode="append")
    wh.internal("CREATE OR REPLACE TABLE serving.cache_probe AS SELECT * FROM raw.cache_probe")
    assert c.post("/api/sql", json={"sql": "select count(*) from serving.cache_probe"}).json()["rows"] == [[4]], "a write must invalidate the cache"
    wh.drop("serving", "cache_probe")
    wh.drop("raw", "cache_probe")
    m = c.get("/metrics").text
    assert 'tessera_query_cache_total{result="hit"}' in m


def test_cache_is_partitioned_by_rls(sample_env):
    wh = sample_env["app"].state.reporter.p.wh
    sql = "select count(distinct province) from serving.orders_enriched"
    all_ = wh.query(sql, restricted=True)["rows"][0][0]
    bc = wh.query(sql, restricted=True, rls={"orders_enriched": "province IN ('BC')"})["rows"][0][0]
    again = wh.query(sql, restricted=True)
    assert all_ > 1 and bc == 1 and again["rows"][0][0] == all_ and again.get("cached")
