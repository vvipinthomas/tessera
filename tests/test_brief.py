"""Executive brief: forecasting model selection and calibration, YoY/YTD exactness, full-year landing, targets,
fiscal years, row-level security, the agent tool and the emailed brief."""
import random
from datetime import date

import pytest
from conftest import Client, add_user
from test_reporting import smtp  # noqa: F401  (fixture)

from tessera import insights

SEASON = [0.82, 0.86, 0.95, 0.98, 1, 0.97, 0.95, 0.98, 1, 1.05, 1.3, 1.45]


def seasonal(n, seed, noise=0.04):
    random.seed(seed)
    return [1000 * 1.014 ** i * SEASON[i % 12] * (1 + random.gauss(0, noise)) for i in range(n)]


# ---------------------------------------------------------------- forecasting engine
def test_picks_seasonal_model_for_seasonal_data_and_is_accurate():
    y = seasonal(36, 2)
    f = insights.forecast_series(y, "month", 12)
    assert f["method"].startswith("holt_winters")
    truth = [1000 * 1.014 ** i * SEASON[i % 12] for i in range(36, 48)]
    mape = sum(abs(p["value"] - t) / t for p, t in zip(f["points"], truth)) / 12
    assert mape < 0.08
    assert f["accuracy"]["vs_naive"] > 0.3 and f["accuracy"]["holdout_periods"] == 12


def test_interval_coverage_is_close_to_nominal():
    inside = total = 0
    for seed in range(1, 31):
        y = seasonal(36, seed)
        f = insights.forecast_series(y, "month", 12)
        random.seed(seed + 1000)
        truth = [1000 * 1.014 ** i * SEASON[i % 12] * (1 + random.gauss(0, 0.04)) for i in range(36, 48)]
        inside += sum(p["lower"] <= t <= p["upper"] for p, t in zip(f["points"], truth))
        total += 12
    assert 0.85 <= inside / total <= 0.995


def test_no_seasonal_model_without_two_seasons_and_trend_for_lines():
    assert insights.forecast_series(seasonal(20, 3), "month", 3)["method"] in ("naive", "holt", "holt_damped", "seasonal_naive")
    assert "holt_winters" not in insights.candidates(20, 12)
    assert insights.forecast_series([10 + 2 * i for i in range(12)], "month", 3)["method"] == "holt"
    assert insights.forecast_series([5, 6], "month", 2)["method"] == "naive"


def test_seasonal_method_must_clearly_win():
    random.seed(9)
    noise = [100 + random.gauss(0, 10) for _ in range(40)]
    f = insights.forecast_series(noise, "month", 3)
    assert f["method"] in ("naive", "holt", "holt_damped", "seasonal_naive")
    if f["method"] == "seasonal_naive":
        scores = f["accuracy"]["all"]
        assert scores["seasonal_naive"] <= 0.9 * min(scores[c] for c in ("naive", "holt", "holt_damped"))


# ---------------------------------------------------------------- the brief against the sample
def _sales(c):
    return next(m for m in c.get("/api/models").json() if m["name"] == "Sales")


@pytest.fixture(scope="module")
def brief(sample_env):
    c = sample_env["client"]
    r = c.post("/api/dashboards/1/brief", json={})
    assert r.status_code == 200, r.text
    return r.json()


def by_measure(b, name):
    return next(m for s in b["sections"] for m in s["measures"] if m["measure"] == name)


def test_brief_structure_and_headlines(brief):
    assert brief["headlines"] and all(h["id"].startswith("H") and h["text"].endswith(".") for h in brief["headlines"])
    rev = by_measure(brief, "revenue")
    assert rev["forecast"]["method"].startswith("holt_winters"), "the sample has a holiday season"
    assert rev["landing"] and rev["landing"]["lower"] < rev["landing"]["value"] < rev["landing"]["upper"]
    assert rev["target"]["status"] in ("on_track", "at_risk", "off_track") and 0 <= rev["target"]["probability"] <= 1
    assert brief["headlines"][0]["kind"] == "landing"


def test_ytd_and_yoy_match_direct_queries(sample_env, brief):
    c = sample_env["client"]
    mid = _sales(c)["id"]
    rev = by_measure(brief, "revenue")
    cur = date.fromisoformat(rev["current"]["period"])
    q = lambda a, b, m="revenue": c.post(f"/api/models/{mid}/query", json={"spec": {"measures": [m], "filters": [  # noqa: E731
        {"field": "order_date", "op": "between", "value": [a, b]}]}}).json()["rows"][0][0]
    end = f"{cur.year}-{cur.month:02d}-{[31, 29 if cur.year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][cur.month - 1]}"
    assert rev["ytd"]["value"] == pytest.approx(q(f"{cur.year}-01-01", end))
    ly_end = f"{cur.year - 1}-{cur.month:02d}-{end[-2:]}"
    assert rev["ytd"]["last_year"] == pytest.approx(q(f"{cur.year - 1}-01-01", ly_end))
    ly_month = q(f"{cur.year - 1}-{cur.month:02d}-01", ly_end)
    assert rev["vs_last_year"]["value"] == pytest.approx(ly_month)
    # ratios are recomputed for the period, not averaged from months
    rr = by_measure(brief, "refund_rate")
    assert rr["ytd"]["value"] == pytest.approx(q(f"{cur.year}-01-01", end, "refund_rate"))
    assert rr.get("landing") is None and rr["target"]["probability"] is None and rr["target"]["basis"] == "year to date"


def test_landing_is_actual_plus_forecast(brief):
    rev = by_measure(brief, "revenue")
    L = rev["landing"]
    rest = rev["forecast"]["points"][:L["remaining_periods"]]
    assert L["value"] == pytest.approx(L["actual_to_date"] + sum(p["value"] for p in rest))
    assert L["actual_to_date"] == pytest.approx(rev["ytd"]["value"])
    cur = date.fromisoformat(rev["current"]["period"])
    assert L["remaining_periods"] == 12 - cur.month
    assert all(p["period"] > rev["current"]["period"] for p in rest)


def test_targets_crud_and_permissions(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    mid = _sales(c)["id"]
    yr = str(date.today().year)
    assert c.put(f"/api/models/{mid}/targets", json={"measure": "orders", "period": "next year", "value": 1}).status_code == 400
    assert c.put(f"/api/models/{mid}/targets", json={"measure": "nope", "period": yr, "value": 1}).status_code == 400
    t = c.put(f"/api/models/{mid}/targets", json={"measure": "units", "period": yr, "value": 1}).json()
    v = add_user(c, app, "exec.viewer@acme.ca", "viewer")
    assert v.put(f"/api/models/{mid}/targets", json={"measure": "units", "period": yr, "value": 2}).status_code == 403
    assert any(x["measure"] == "units" for x in v.get(f"/api/models/{mid}/targets").json())
    b = c.post("/api/dashboards/1/brief", json={}).json()
    assert by_measure(b, "units")["target"]["probability"] > 0.99, "a tiny target is certain"
    r = c.delete(f"/api/models/{mid}/targets/{t['id']}")
    assert r.status_code == 200
    assert by_measure(c.post("/api/dashboards/1/brief", json={}).json(), "units").get("target") is None


def test_fiscal_year(sample_env):
    c = sample_env["client"]
    assert c.put("/api/settings/fiscal", json={"fiscal_year_start_month": 13}).status_code == 400
    c.put("/api/settings/fiscal", json={"fiscal_year_start_month": 4})
    try:
        b = c.post("/api/dashboards/1/brief", json={}).json()
        rev = by_measure(b, "revenue")
        cur = date.fromisoformat(rev["current"]["period"])
        fy_start = date(cur.year if cur.month >= 4 else cur.year - 1, 4, 1)
        assert rev["landing"]["label"] == f"FY{fy_start.year + 1}"
        assert rev["landing"]["remaining_periods"] == (fy_start.year + 1 - cur.year) * 12 + 4 - cur.month - 1
        assert rev.get("target") is None, "the sample's calendar-year target does not apply to a fiscal year"
    finally:
        c.put("/api/settings/fiscal", json={"fiscal_year_start_month": 1})


def test_brief_respects_rls_and_filters(sample_env, brief):
    c, app = sample_env["client"], sample_env["app"]
    p = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "province"}).json()
    add_user(c, app, "bc.exec@acme.ca", "viewer")
    uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "bc.exec@acme.ca")
    c.put(f"/api/users/{uid}/attributes", json={"attributes": {"province": ["BC"]}})
    v = Client(app)
    v.post("/api/auth/login", json={"email": "bc.exec@acme.ca", "password": "another strong pass 7"})
    try:
        mine = by_measure(v.post("/api/dashboards/1/brief", json={}).json(), "revenue")
        assert mine["ytd"]["value"] < by_measure(brief, "revenue")["ytd"]["value"]
        for s in mine["segments"]:
            if s["dimension"] == "province":
                assert {x["member"] for x in s["growing"] + s["declining"]} <= {"BC"}
    finally:
        c.delete(f"/api/rls/{p['id']}")
    mid = _sales(c)["id"]
    web = by_measure(c.post("/api/dashboards/1/brief", json={"filters": [{"model_id": mid, "field": "channel", "op": "in", "value": ["web"]}]}).json(), "revenue")
    assert web["ytd"]["value"] < by_measure(brief, "revenue")["ytd"]["value"]


def test_quarterly_brief(sample_env):
    b = sample_env["client"].post("/api/dashboards/1/brief", json={"grain": "quarter"}).json()
    rev = by_measure(b, "revenue")
    assert rev["grain"] == "quarter" and rev["current"]["label"].startswith("Q") and rev["vs_last_year"]


def test_agent_tool_returns_headlines(sample_env):
    from tessera import agents
    from tessera.platform import Platform  # noqa: F401
    plat = sample_env["app"].state.reporter.p
    admin = plat.store.one("SELECT * FROM users WHERE role='admin'")
    out = agents.t_executive_brief(agents.Ctx(plat, admin), {"model_id": _sales(sample_env["client"])["id"], "measures": ["revenue"]})
    assert out["headlines"] and out["measures"][0]["landing"]["value"] > 0 and out["measures"][0]["forecast_method"]


def test_emailed_brief(sample_env, smtp):  # noqa: F811
    c = sample_env["client"]
    inbox, port = smtp
    c.put("/api/admin/email", json={"host": "127.0.0.1", "port": port, "from": "tessera@acme.ca", "starttls": False})
    sub = c.post("/api/subscriptions", json={"name": "Exec", "dashboard_id": 1, "recipients": "ceo@acme.ca", "schedule": "0 7 * * 1", "attach": "none", "include_brief": True}).json()
    assert sub["include_brief"] == 1
    assert c.post(f"/api/subscriptions/{sub['id']}/send").status_code == 200
    html = next(p for p in inbox.messages[-1]["msg"].walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "Executive brief" in html and "Full-year forecast" in html and "forecast to land" in html
    assert html.index("Executive brief") < html.index("<h3")
