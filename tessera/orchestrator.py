"""Pipeline orchestrator: DAG validation, execution with retries, cron scheduling, webhooks."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter

from .errors import UserError
from .store import now, parse_ts
from .warehouse import IDENT, QueryError, split_fqn

if TYPE_CHECKING:
    from .platform import Platform

log = logging.getLogger("tessera.orchestrator")

STEP_TYPES = {
    "ingest": {"label": "Ingest from a connection", "required": ["connection_id", "source", "target"]},
    "transform": {"label": "Transform with SQL", "required": ["sql", "target"]},
    "quality": {"label": "Run quality rules", "required": ["dataset"]},
    "pii_scan": {"label": "Scan for personal data", "required": ["dataset"]},
    "publish": {"label": "Publish masked copy to serving", "required": ["dataset"]},
    "export": {"label": "Export Parquet or CSV with datacard", "required": ["dataset"]},
    "deliver": {"label": "Deliver published data to a database, warehouse or lake", "required": ["dataset", "connection_id"]},
    "webhook": {"label": "Call a webhook", "required": ["url"]},
    "run_pipeline": {"label": "Trigger another pipeline", "required": ["pipeline_id"]},
}
ACTIVE = ("queued", "running")


class PipelineError(UserError):
    pass


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TESSERA_TIMEZONE", "UTC"))


def next_run(cron: str, after: datetime | None = None) -> str:
    base = (after or datetime.now(timezone.utc)).astimezone(tz())
    return croniter(cron, base).get_next(datetime).astimezone(timezone.utc).isoformat(timespec="seconds")


def validate(steps: list[dict], schedule: str | None = None) -> list[str]:
    """Validate a pipeline definition and return the execution order."""
    if not isinstance(steps, list) or not steps:
        raise PipelineError("Add at least one step.")
    ids = {}
    for i, s in enumerate(steps):
        sid = s.get("id")
        if not sid or not IDENT.match(sid):
            raise PipelineError(f"Step {i + 1} needs an id made of letters, digits and underscores.")
        if sid in ids:
            raise PipelineError(f"Duplicate step id '{sid}'.")
        t = s.get("type")
        if t not in STEP_TYPES:
            raise PipelineError(f"Step '{sid}' has unknown type '{t}'.")
        for k in STEP_TYPES[t]["required"]:
            if s.get(k) in (None, "", {}):
                raise PipelineError(f"Step '{sid}' ({t}) needs '{k}'.")
        if t in ("transform", "ingest"):
            try:
                schema, _ = split_fqn(s["target"], "curated" if t == "transform" else "raw")
            except QueryError as e:
                raise PipelineError(f"Step '{sid}': {e}") from None
            if schema == "serving":
                raise PipelineError(f"Step '{sid}': serving is written only by publish steps.")
        if t in ("quality", "pii_scan", "publish", "export"):
            try:
                split_fqn(s["dataset"], "raw")
            except QueryError as e:
                raise PipelineError(f"Step '{sid}': {e}") from None
        if t == "webhook" and not str(s["url"]).startswith(("http://", "https://")):
            raise PipelineError(f"Step '{sid}': webhook URL must start with http:// or https://")
        r = s.get("retries", 0)
        if not isinstance(r, int) or not 0 <= r <= 5:
            raise PipelineError(f"Step '{sid}': retries must be 0 to 5.")
        ids[sid] = s
    for s in steps:
        for dep in s.get("depends_on") or []:
            if dep not in ids:
                raise PipelineError(f"Step '{s['id']}' depends on unknown step '{dep}'.")
    indeg = {sid: len(ids[sid].get("depends_on") or []) for sid in ids}
    order, ready = [], [s["id"] for s in steps if indeg[s["id"]] == 0]
    while ready:
        n = ready.pop(0)
        order.append(n)
        for s in steps:
            if n in (s.get("depends_on") or []):
                indeg[s["id"]] -= 1
                if indeg[s["id"]] == 0:
                    ready.append(s["id"])
    if len(order) != len(steps):
        raise PipelineError("Steps form a cycle. Check depends_on.")
    if schedule:
        if not croniter.is_valid(schedule):
            raise PipelineError(f"'{schedule}' is not a valid cron expression (minute hour day month weekday).")
    return order


class Orchestrator:
    def __init__(self, platform: Platform, http_client_factory=None):
        self.p = platform
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("TESSERA_WORKERS", "2")), thread_name_prefix="tessera-run")
        self.cancelled: set[int] = set()
        self._stop = threading.Event()
        self._sched: threading.Thread | None = None
        self.http_client_factory = http_client_factory
        self.lock = threading.Lock()
        self.reporter = None  # set by the app; evaluates alerts and subscriptions on each tick
        for r in self.p.store.all("SELECT id FROM pipeline_runs WHERE status IN ('queued','running')"):
            self.p.store.update("pipeline_runs", r["id"], {"status": "failed", "error": "Interrupted by a restart", "finished_at": now()})

    def _http(self):
        import httpx
        return self.http_client_factory() if self.http_client_factory else httpx.Client(timeout=15)

    # ----------------------------------------------------------- scheduling
    def start(self):
        if self._sched is None:
            self._sched = threading.Thread(target=self._loop, name="tessera-scheduler", daemon=True)
            self._sched.start()

    def stop(self):
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self._stop.wait(self.p.settings.scheduler_interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            if self.reporter:
                try:
                    self.reporter.tick()
                except Exception:  # noqa: BLE001
                    log.exception("reporting tick failed")

    def tick(self, at: datetime | None = None) -> list[int]:
        at = at or datetime.now(timezone.utc)
        started = []
        for pl in self.p.store.all("SELECT * FROM pipelines WHERE enabled=1 AND schedule IS NOT NULL AND schedule<>''"):
            due = parse_ts(pl["next_run_at"])
            if due is None:
                self.p.store.update("pipelines", pl["id"], {"next_run_at": next_run(pl["schedule"], at)})
                continue
            if due <= at:
                self.p.store.update("pipelines", pl["id"], {"next_run_at": next_run(pl["schedule"], at)})
                try:
                    started.append(self.trigger(pl["id"], "schedule", "scheduler"))
                except PipelineError as e:
                    log.info("skipped scheduled run of %s: %s", pl["name"], e)
        return started

    # ----------------------------------------------------------- triggering
    def trigger(self, pipeline_id: int, trigger: str, who: str, wait: bool = False) -> int:
        with self.lock:
            pl = self.p.store.one("SELECT * FROM pipelines WHERE id=?", (pipeline_id,))
            if not pl:
                raise PipelineError("Pipeline not found")
            if self.p.store.one("SELECT id FROM pipeline_runs WHERE pipeline_id=? AND status IN ('queued','running')", (pipeline_id,)):
                raise PipelineError(f"Pipeline '{pl['name']}' is already running.")
            run_id = self.p.store.insert("pipeline_runs", {"pipeline_id": pipeline_id, "status": "queued", "trigger": trigger,
                                                           "triggered_by": who, "created_at": now(), "steps": "{}", "log": "[]"})
        fut = self.pool.submit(self._execute, run_id)
        if wait:
            fut.result()
        return run_id

    def cancel(self, run_id: int) -> None:
        self.cancelled.add(run_id)

    # ----------------------------------------------------------- execution
    def _execute(self, run_id: int) -> None:
        store = self.p.store
        run = store.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        pl = store.one("SELECT * FROM pipelines WHERE id=?", (run["pipeline_id"],))
        logs: list[dict] = []
        states: dict[str, dict] = {}

        def emit(level, step, msg):
            logs.append({"ts": now(), "level": level, "step": step, "msg": msg})
            store.update("pipeline_runs", run_id, {"log": json.dumps(logs), "steps": json.dumps(states)})

        store.update("pipeline_runs", run_id, {"status": "running", "started_at": now()})
        steps = {s["id"]: s for s in pl["steps"]}
        try:
            order = validate(pl["steps"])
        except PipelineError as e:
            store.update("pipeline_runs", run_id, {"status": "failed", "error": str(e), "finished_at": now()})
            return
        for sid in order:
            states[sid] = {"status": "pending"}
        emit("info", None, f"Run started ({run['trigger']}) with {len(order)} step(s)")
        self._lineage_event("START", run_id, pl, emit)
        for sid in order:
            s = steps[sid]
            if run_id in self.cancelled:
                states[sid] = {"status": "cancelled"}
                continue
            blocked = [d for d in s.get("depends_on") or [] if states[d]["status"] != "success"]
            if blocked:
                states[sid] = {"status": "skipped", "reason": f"upstream {', '.join(blocked)} did not succeed"}
                emit("warn", sid, f"Skipped because {', '.join(blocked)} did not succeed")
                continue
            attempts = int(s.get("retries", 0)) + 1
            for attempt in range(1, attempts + 1):
                t0 = time.perf_counter()
                states[sid] = {"status": "running", "attempt": attempt, "started_at": now()}
                emit("info", sid, f"{STEP_TYPES[s['type']]['label']} (attempt {attempt}/{attempts})")
                try:
                    out = self._run_step(s, pl, run_id)
                    states[sid] = {"status": "success", "attempt": attempt, "seconds": round(time.perf_counter() - t0, 3), "output": out}
                    emit("info", sid, f"Succeeded in {time.perf_counter() - t0:.2f}s: {json.dumps(out, default=str)[:400]}")
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e).split("\n")[0] or e.__class__.__name__
                    states[sid] = {"status": "failed", "attempt": attempt, "error": msg, "seconds": round(time.perf_counter() - t0, 3)}
                    emit("error", sid, f"Failed: {msg}")
                    if not isinstance(e, (QueryError, PipelineError)):
                        log.debug("step failure: %s", traceback.format_exc())
                    if attempt < attempts and run_id not in self.cancelled:
                        delay = self.p.settings.retry_base_seconds * (2 ** (attempt - 1))
                        emit("info", sid, f"Retrying in {delay:g}s")
                        time.sleep(delay)
        failed = [k for k, v in states.items() if v["status"] == "failed"]
        status = "cancelled" if run_id in self.cancelled else ("failed" if failed else "success")
        if status == "success" and any(v["status"] == "skipped" for v in states.values()):
            status = "failed"
        emit("info" if status == "success" else "error", None, f"Run {status}")
        store.update("pipeline_runs", run_id, {"status": status, "finished_at": now(),
                                               "error": f"Failed steps: {', '.join(failed)}" if failed else None})
        self.cancelled.discard(run_id)
        self._lineage_event("COMPLETE" if status == "success" else "ABORT" if status == "cancelled" else "FAIL", run_id, pl, emit,
                            f"Failed steps: {', '.join(failed)}" if failed else None)
        if self.reporter:
            try:
                self.reporter.on_run_finished(pl, run_id, status, run.get("triggered_by"))
            except Exception:  # noqa: BLE001
                log.exception("run notifications failed")
        if pl.get("webhook_url"):
            try:
                with self._http() as cl:
                    cl.post(pl["webhook_url"], json={"event": "pipeline_run.finished", "pipeline": pl["name"], "pipeline_id": pl["id"],
                                                     "run_id": run_id, "status": status, "steps": {k: v["status"] for k, v in states.items()}})
                emit("info", None, "Completion webhook sent")
            except Exception as e:  # noqa: BLE001
                emit("warn", None, f"Completion webhook failed: {e}")

    def _lineage_event(self, kind: str, run_id: int, pl: dict, emit, error: str | None = None) -> None:
        """OpenLineage is best effort: a collector being down never fails a run, but the run log says so."""
        from . import interop
        try:
            ev = interop.ol_event(self.p, kind, run_id, pl, error)
            if ev:
                with self._http() as cl:
                    interop.ol_send(self.p, cl, ev)
                if kind != "START":
                    emit("info", None, f"OpenLineage {kind} event sent")
        except Exception as e:  # noqa: BLE001
            emit("warn", None, f"OpenLineage event not delivered: {str(e).splitlines()[0][:200]}")

    def _dataset_id(self, fqn: str) -> int:
        schema, name = split_fqn(fqn, "raw")
        d = self.p.dataset(schema, name)
        if not d:
            if self.p.wh.exists(schema, name):
                d = self.p.dataset(schema, name, create=True)
                self.p.refresh_columns(d)
            else:
                raise PipelineError(f"Dataset {schema}.{name} does not exist yet.")
        return d["id"]

    def _run_step(self, s: dict, pl: dict, run_id: int):
        t = s["type"]
        if t == "ingest":
            return self.p.ingest(int(s["connection_id"]), s["source"], s["target"], s.get("mode", "replace"), pl["id"])
        if t == "transform":
            return self.p.transform(s["sql"], s["target"], pl["id"])
        if t == "quality":
            res = self.p.run_quality(self._dataset_id(s["dataset"]), run_id)
            fail_on = s.get("fail_on", "error")
            summary = {k: res[k] for k in ("dataset", "score", "rules", "failed_errors", "failed_warnings")}
            if (fail_on == "error" and res["failed_errors"]) or (fail_on == "warn" and (res["failed_errors"] or res["failed_warnings"])):
                bad = [r["description"] for r in res["results"] if not r["passed"]][:5]
                raise PipelineError(f"Quality gate failed ({res['failed_errors']} error, {res['failed_warnings']} warning rules): {'; '.join(bad)}")
            return summary
        if t == "pii_scan":
            f = self.p.scan_pii(self._dataset_id(s["dataset"]))
            return {"sensitive_columns": [x["column"] for x in f if x["pii"]]}
        if t == "publish":
            return self.p.publish(self._dataset_id(s["dataset"]), pl["id"])
        if t == "export":
            return self.p.export(self._dataset_id(s["dataset"]), s.get("format", "parquet"))
        if t == "deliver":
            from . import interop
            return interop.deliver(self.p, s, pl["id"])
        if t == "webhook":
            with self._http() as cl:
                r = cl.post(s["url"], json={"event": "pipeline_step", "pipeline": pl["name"], "run_id": run_id, "step": s["id"],
                                            "payload": s.get("payload") or {}})
                if r.status_code >= 400:
                    raise PipelineError(f"Webhook returned HTTP {r.status_code}")
                return {"status_code": r.status_code}
        if t == "run_pipeline":
            if int(s["pipeline_id"]) == pl["id"]:
                raise PipelineError("A pipeline cannot trigger itself.")
            rid = self.trigger(int(s["pipeline_id"]), "pipeline", f"pipeline:{pl['name']}")
            return {"triggered_run": rid}
        raise PipelineError(f"Unknown step type {t}")
