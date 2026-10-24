import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import Client, do_setup, make_app

from tessera.orchestrator import PipelineError, validate


def wait_run(c, rid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_validation():
    with pytest.raises(PipelineError, match="cycle"):
        validate([{"id": "a", "type": "webhook", "url": "http://x", "depends_on": ["b"]},
                  {"id": "b", "type": "webhook", "url": "http://x", "depends_on": ["a"]}])
    with pytest.raises(PipelineError, match="unknown step"):
        validate([{"id": "a", "type": "webhook", "url": "http://x", "depends_on": ["zz"]}])
    with pytest.raises(PipelineError, match="serving"):
        validate([{"id": "a", "type": "transform", "sql": "select 1", "target": "serving.x"}])
    with pytest.raises(PipelineError, match="cron"):
        validate([{"id": "a", "type": "webhook", "url": "http://x"}], "every tuesday")
    with pytest.raises(PipelineError, match="needs 'sql'"):
        validate([{"id": "a", "type": "transform", "target": "curated.x"}])
    assert validate([{"id": "b", "type": "webhook", "url": "http://x", "depends_on": ["a"]},
                     {"id": "a", "type": "webhook", "url": "http://x"}]) == ["a", "b"]


@pytest.fixture
def env(settings):
    calls = []
    fail_first = {"n": 0}

    def handler(req):
        calls.append((str(req.url), req.content))
        if "flaky" in str(req.url):
            fail_first["n"] += 1
            if fail_first["n"] == 1:
                return httpx.Response(503)
        if "down" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})
    app = make_app(settings, http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    c = Client(app)
    do_setup(c, settings)
    (settings.uploads_dir / "o.csv").write_text("id,amount,email\n1,10,a@x.ca\n2,-3,b@y.ca\n3,5,c@z.ca\n")
    conn = c.post("/api/connections", json={"type": "file", "name": "f", "config": {}}).json()
    return {"c": c, "app": app, "calls": calls, "conn": conn["id"]}


def steps(conn, gate="error"):
    return [
        {"id": "load", "type": "ingest", "connection_id": conn, "source": {"path": "o.csv"}, "target": "raw.o"},
        {"id": "clean", "type": "transform", "depends_on": ["load"], "sql": "select * from raw.o", "target": "curated.o"},
        {"id": "gate", "type": "quality", "depends_on": ["clean"], "dataset": "curated.o", "fail_on": gate},
        {"id": "scan", "type": "pii_scan", "depends_on": ["gate"], "dataset": "curated.o"},
        {"id": "pub", "type": "publish", "depends_on": ["scan"], "dataset": "curated.o"},
        {"id": "out", "type": "export", "depends_on": ["pub"], "dataset": "serving.o", "format": "parquet"},
    ]


def test_full_run_lineage_and_export(env):
    c = env["c"]
    p = c.post("/api/pipelines", json={"name": "p1", "steps": steps(env["conn"]), "webhook_url": "http://hooks.test/done"}).json()
    rid = c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"]
    r = wait_run(c, rid)
    assert r["status"] == "success", [x for x in r["log"] if x["level"] != "info"] or r["steps"]
    assert all(s["status"] == "success" for s in r["steps"].values())
    assert c.post("/api/sql", json={"sql": "select count(*) from serving.o"}).json()["rows"] == [[3]]
    emails = c.post("/api/sql", json={"sql": "select email from serving.o"}).json()["rows"]
    assert all("@" not in e[0] for e in emails)
    edges = {(e["src"], e["dst"]) for e in c.get("/api/lineage").json()}
    assert ("raw.o", "curated.o") in edges and ("curated.o", "serving.o") in edges and any(s.startswith("source:f") for s, _ in edges)
    exports = [e["file"] for e in c.get("/api/exports").json()]
    assert any(e.endswith(".parquet") for e in exports) and any(e.endswith(".datacard.json") for e in exports)
    assert any("hooks.test/done" in u for u, _ in env["calls"]), "completion webhook was sent"
    assert c.get(f"/api/exports/{[e for e in exports if e.endswith('.parquet')][0]}").status_code == 200
    assert c.get("/api/exports/..%2F..%2Ftessera.db").status_code == 404


def test_quality_gate_stops_downstream(env):
    c = env["c"]
    p = c.post("/api/pipelines", json={"name": "gated", "steps": steps(env["conn"])}).json()
    wait_run(c, c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"])
    did = next(d["id"] for d in c.get("/api/datasets").json() if d["schema_name"] == "curated" and d["name"] == "o")
    c.post(f"/api/datasets/{did}/rules", json={"kind": "range", "column_name": "amount", "params": {"min": 0}, "severity": "error"})
    r = wait_run(c, c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"])
    assert r["status"] == "failed"
    assert r["steps"]["gate"]["status"] == "failed" and "Quality gate failed" in r["steps"]["gate"]["error"]
    assert r["steps"]["pub"]["status"] == "skipped"
    hist = c.get(f"/api/datasets/{did}/rules").json()[0]["last_result"]
    assert hist["failing"] == 1


def test_retries(env):
    c = env["c"]
    p = c.post("/api/pipelines", json={"name": "flaky", "steps": [{"id": "hook", "type": "webhook", "url": "http://flaky.test/x", "retries": 2}]}).json()
    r = wait_run(c, c.post(f"/api/pipelines/{p['id']}/run").json()["run_id"])
    assert r["status"] == "success" and r["steps"]["hook"]["attempt"] == 2
    p2 = c.post("/api/pipelines", json={"name": "down", "steps": [{"id": "hook", "type": "webhook", "url": "http://down.test/x", "retries": 1}]}).json()
    r = wait_run(c, c.post(f"/api/pipelines/{p2['id']}/run").json()["run_id"])
    assert r["status"] == "failed" and r["steps"]["hook"]["attempt"] == 2


def test_schedule_tick(env):
    c, app = env["c"], env["app"]
    p = c.post("/api/pipelines", json={"name": "cron", "schedule": "*/5 * * * *",
                                       "steps": [{"id": "hook", "type": "webhook", "url": "http://ok.test/"}]}).json()
    assert p["next_run_at"]
    orch = app.state.orch
    assert orch.tick(datetime.now(timezone.utc)) == []
    started = orch.tick(datetime.now(timezone.utc) + timedelta(minutes=6))
    assert len(started) == 1
    r = wait_run(c, started[0])
    assert r["trigger"] == "schedule" and r["status"] == "success"


def test_api_token_trigger_for_external_orchestrators(env):
    c, app = env["c"], env["app"]
    p = c.post("/api/pipelines", json={"name": "ext", "steps": [{"id": "hook", "type": "webhook", "url": "http://ok.test/"}]}).json()
    tok = c.post("/api/tokens", json={"name": "airflow", "role": "engineer"}).json()["token"]
    bare = Client(app)
    r = bare.post(f"/api/pipelines/{p['id']}/run", headers={"Authorization": f"Bearer {tok}"}, csrf=False)
    assert r.status_code == 200
    run = wait_run(c, r.json()["run_id"])
    assert run["trigger"] == "api"


def test_chained_pipelines_and_bad_connection(env):
    c = env["c"]
    child = c.post("/api/pipelines", json={"name": "child", "steps": [{"id": "hook", "type": "webhook", "url": "http://ok.test/child"}]}).json()
    parent = c.post("/api/pipelines", json={"name": "parent", "steps": [{"id": "go", "type": "run_pipeline", "pipeline_id": child["id"]}]}).json()
    r = wait_run(c, c.post(f"/api/pipelines/{parent['id']}/run").json()["run_id"])
    assert r["status"] == "success"
    child_run = r["steps"]["go"]["output"]["triggered_run"]
    assert wait_run(c, child_run)["status"] == "success"
    bad = c.post("/api/pipelines", json={"name": "bad", "steps": [{"id": "x", "type": "ingest", "connection_id": 999, "source": {}, "target": "raw.x"}]})
    assert bad.status_code == 400


def test_engineer_only(env, settings):
    c, app = env["c"], env["app"]
    c.post("/api/users", json={"email": "ana@acme.ca", "role": "analyst", "password": "another strong pass 7"})
    a = Client(app)
    a.post("/api/auth/login", json={"email": "ana@acme.ca", "password": "another strong pass 7"})
    assert a.post("/api/pipelines", json={"name": "x", "steps": []}).status_code == 403


def test_deleting_a_pipeline_with_a_queued_run(admin, app):
    p = admin.post("/api/pipelines", json={"name": "short-lived", "steps": [{"id": "t", "type": "transform", "sql": "select 1 as x", "target": "curated.sl"}]}).json()
    store = app.state.platform.store
    from tessera.store import now
    rid = store.insert("pipeline_runs", {"pipeline_id": p["id"], "status": "queued", "trigger": "manual", "created_at": now()})
    assert admin.delete(f"/api/pipelines/{p['id']}").status_code == 200
    app.state.orch._execute(rid)  # the worker picks the run up after the delete: it must exit quietly
    assert store.one("SELECT id FROM pipeline_runs WHERE id=?", (rid,)) is None
