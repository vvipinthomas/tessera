"""Enterprise operations: SCIM 2.0 provisioning, Prometheus metrics, request IDs and access logs, rate limiting,
readiness checks, audit export and retention, session management, and consistent backups."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from . import workspaces as ws
from .auth import COOKIE
from .security import RANK, ROLES, token_hash
from .store import now

access_log = logging.getLogger("tessera.access")
log = logging.getLogger("tessera")
# Structured access logs are opt-in (TESSERA_LOG_JSON=1 turns them on and uvicorn's plain access log off).
access_log.setLevel(logging.INFO if os.environ.get("TESSERA_LOG_JSON", "").lower() in ("1", "true", "yes") else logging.WARNING)
SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


# ---------------------------------------------------------------- metrics
class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: dict[tuple, int] = defaultdict(int)
        self.hist: dict[tuple, list[int]] = {}
        self.sums: dict[tuple, float] = defaultdict(float)
        self.rate_limited = 0

    def observe(self, method: str, route: str, status: int, seconds: float):
        with self.lock:
            self.requests[(method, route, str(status))] += 1
            key = (method, route)
            h = self.hist.setdefault(key, [0] * (len(BUCKETS) + 1))
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[-1] += 1
            self.sums[key] += seconds

    def render(self, extra: list[str]) -> str:
        esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        out = ["# HELP tessera_http_requests_total HTTP requests by method, route and status.", "# TYPE tessera_http_requests_total counter"]
        with self.lock:
            for (m, r, st), n in sorted(self.requests.items()):
                out.append(f'tessera_http_requests_total{{method="{m}",route="{esc(r)}",status="{st}"}} {n}')
            out += ["# HELP tessera_http_request_duration_seconds Request latency.", "# TYPE tessera_http_request_duration_seconds histogram"]
            for (m, r), h in sorted(self.hist.items()):
                for i, b in enumerate(BUCKETS):
                    out.append(f'tessera_http_request_duration_seconds_bucket{{method="{m}",route="{esc(r)}",le="{b}"}} {h[i]}')
                out.append(f'tessera_http_request_duration_seconds_bucket{{method="{m}",route="{esc(r)}",le="+Inf"}} {h[-1]}')
                out.append(f'tessera_http_request_duration_seconds_sum{{method="{m}",route="{esc(r)}"}} {self.sums[(m, r)]:.6f}')
                out.append(f'tessera_http_request_duration_seconds_count{{method="{m}",route="{esc(r)}"}} {h[-1]}')
            out += ["# TYPE tessera_rate_limited_total counter", f"tessera_rate_limited_total {self.rate_limited}"]
        return "\n".join(out + extra) + "\n"


class RateLimiter:
    """Token bucket per caller (API token, session or IP)."""

    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.cap = max(1, per_minute)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        t = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.cap, t))
            tokens = min(self.cap, tokens + (t - last) * self.rate)
            if tokens < 1:
                self.buckets[key] = (tokens, t)
                return False, (1 - tokens) / self.rate
            self.buckets[key] = (tokens - 1, t)
            if len(self.buckets) > 50_000:
                self.buckets.clear()
            return True, 0.0


def caller_key(req: Request) -> str:
    authz = req.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return "t:" + hashlib.sha256(authz[7:].strip().encode()).hexdigest()[:24]
    c = req.cookies.get(COOKIE)
    if c:
        return "s:" + hashlib.sha256(c.encode()).hexdigest()[:24]
    from .auth import client_ip
    return "ip:" + client_ip(req)


# ---------------------------------------------------------------- backups
def backup(platform, dest: Path, include_key: bool = False) -> dict:
    """Write a consistent .tar.gz of metadata, warehouse and uploads. Runs inside the server or offline."""
    data_dir = platform.settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = sqlite3.connect(str(data_dir / "tessera.db"))
        dst = sqlite3.connect(str(tmp / "tessera.db"))
        src.backup(dst)
        dst.close()
        src.close()
        if platform and platform.wh is not None:
            with platform.wh.write_lock:
                platform.wh.internal("CHECKPOINT")
                _copy(data_dir / "warehouse.duckdb", tmp / "warehouse.duckdb")
        else:
            _copy(data_dir / "warehouse.duckdb", tmp / "warehouse.duckdb")
        files = ["tessera.db", "warehouse.duckdb"]
        if include_key and (data_dir / "master.key").exists():
            _copy(data_dir / "master.key", tmp / "master.key")
            files.append("master.key")
        manifest = {"tessera_backup": 1, "version": __version__, "created_at": now(), "includes_key": "master.key" in files,
                    "files": {f: _sha256(tmp / f) for f in files}}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(tmp / "manifest.json", arcname="manifest.json")
            for f in files:
                tar.add(tmp / f, arcname=f)
            for sub in ("imports", "exports"):
                if (data_dir / sub).exists():
                    tar.add(data_dir / sub, arcname=sub)
    return manifest


def restore(archive: Path, data_dir: Path, force: bool = False) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    if (data_dir / "tessera.db").exists() and not force:
        raise SystemExit(f"{data_dir} already holds a Tessera install. Stop the server and pass --force to overwrite it.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                    raise SystemExit(f"Refusing unsafe path in backup: {m.name}")
            tar.extractall(tmp, filter="data")
        manifest = json.loads((tmp / "manifest.json").read_text())
        for f, digest in manifest["files"].items():
            if _sha256(tmp / f) != digest:
                raise SystemExit(f"Checksum mismatch for {f}; the backup is damaged.")
        for f in manifest["files"]:
            _copy(tmp / f, data_dir / f)
        for wal in data_dir.glob("warehouse.duckdb.wal"):
            wal.unlink()
        for sub in ("imports", "exports"):
            if (tmp / sub).exists():
                import shutil
                shutil.copytree(tmp / sub, data_dir / sub, dirs_exist_ok=True)
    return manifest


def _copy(a: Path, b: Path):
    with open(a, "rb") as r, open(b, "wb") as w:
        while chunk := r.read(1 << 20):
            w.write(chunk)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- SCIM helpers
def scim_error(status: int, detail: str, scim_type: str | None = None):
    body = {"schemas": [SCIM_ERROR], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status, media_type="application/scim+json")


def scim_user(u: dict, base: str = "") -> dict:
    return {"schemas": [SCIM_USER], "id": u["scim_id"], "externalId": u.get("external_id"), "userName": u["email"],
            "displayName": u["name"], "name": {"formatted": u["name"]}, "active": bool(u["active"]),
            "emails": [{"value": u["email"], "primary": True, "type": "work"}],
            "meta": {"resourceType": "User", "created": u["created_at"], "lastModified": u.get("last_login") or u["created_at"],
                     "location": f"{base}/scim/v2/Users/{u['scim_id']}"}}


def scim_group(g: dict, store, base: str = "") -> dict:
    members = []
    for sid in g["members"]:
        u = store.one("SELECT scim_id, email FROM users WHERE scim_id=?", (sid,))
        if u:
            members.append({"value": u["scim_id"], "display": u["email"]})
    return {"schemas": [SCIM_GROUP], "id": g["id"], "externalId": g.get("external_id"), "displayName": g["display_name"], "members": members,
            "meta": {"resourceType": "Group", "created": g["created_at"], "lastModified": g["updated_at"], "location": f"{base}/scim/v2/Groups/{g['id']}"}}


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")


def _filter_eq(flt: str | None, attr: str) -> str | None:
    if not flt:
        return None
    m = re.match(rf'^\s*{attr}\s+eq\s+"([^"]*)"\s*$', flt, re.I)
    if not m:
        raise HTTPException(400, f"Only '{attr} eq \"value\"' filters are supported.")
    return m.group(1)


def register(app, plat, user, need, audit):
    store, settings = plat.store, plat.settings
    viewer, admin = need("viewer"), need("admin")
    metrics = Metrics()
    app.state.metrics = metrics
    limiter = RateLimiter(int(os.environ.get("TESSERA_RATE_LIMIT", "1200")))
    app.state.limiter = limiter

    # ------------------------------------------------------------ middleware: request id, rate limit, metrics, access log
    @app.middleware("http")
    async def ops_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        rid = re.sub(r"[^A-Za-z0-9._-]", "", rid)[:64] or uuid.uuid4().hex[:16]
        path = request.url.path
        t0 = time.perf_counter()
        if path.startswith(("/api/", "/scim/", "/mcp")) and path not in ("/api/health", "/api/ready"):
            ok, wait = limiter.allow(caller_key(request))
            if not ok:
                metrics.rate_limited += 1
                return JSONResponse({"detail": "Too many requests. Slow down and retry shortly."}, status_code=429,
                                    headers={"Retry-After": str(max(1, int(wait) + 1)), "X-Request-ID": rid})
        resp = await call_next(request)
        dt = time.perf_counter() - t0
        route = getattr(request.scope.get("route"), "path", None) or ("/static" if path.startswith("/static/") else "other")
        metrics.observe(request.method, route, resp.status_code, dt)
        resp.headers["X-Request-ID"] = rid
        if path.startswith(("/api/", "/scim/", "/mcp")) and access_log.isEnabledFor(logging.INFO):
            access_log.info(json.dumps({"ts": now(), "request_id": rid, "method": request.method, "route": route, "status": resp.status_code,
                                        "ms": round(dt * 1000, 1), "ip": _ip(request)}))
        return resp

    # ------------------------------------------------------------ readiness and metrics
    @app.get("/api/ready")
    def ready():
        checks = {}
        try:
            store.scalar("SELECT 1")
            checks["metadata"] = "ok"
        except Exception:  # noqa: BLE001 - details go to the log; this endpoint needs no sign-in
            log.exception("readiness check failed: metadata")
            checks["metadata"] = "unavailable"
        try:
            plat.wh.internal("SELECT 1")
            checks["warehouse"] = "ok"
        except Exception:  # noqa: BLE001 - details go to the log; this endpoint needs no sign-in
            log.exception("readiness check failed: warehouse")
            checks["warehouse"] = "unavailable"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, "checks": checks, "version": __version__}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    def prom(request: Request):
        tok = os.environ.get("TESSERA_METRICS_TOKEN")
        authz = request.headers.get("authorization", "")
        if tok:
            if not hmac.compare_digest(authz, f"Bearer {tok}"):
                raise HTTPException(401, "Metrics token required.")
        else:
            u = user(request)
            if u["role"] != "admin":
                raise HTTPException(403, "Metrics need an admin or TESSERA_METRICS_TOKEN.")
        extra = ["# TYPE tessera_users gauge", f"tessera_users{{state=\"active\"}} {store.scalar('SELECT count(*) FROM users WHERE active=1')}",
                 "# TYPE tessera_datasets gauge", f"tessera_datasets {store.scalar('SELECT count(*) FROM datasets')}",
                 "# TYPE tessera_alerts gauge"]
        for st in ("firing", "ok", "error", "unknown"):
            extra.append(f'tessera_alerts{{state="{st}"}} {store.scalar("SELECT count(*) FROM alerts WHERE state=?", (st,))}')
        extra.append("# TYPE tessera_pipeline_runs_total counter")
        for r in store.all("SELECT status, count(*) n FROM pipeline_runs GROUP BY status"):
            extra.append(f'tessera_pipeline_runs_total{{status="{r["status"]}"}} {r["n"]}')
        size = sum(f.stat().st_size for f in settings.data_dir.glob("warehouse.duckdb*") if f.is_file())
        extra += ["# TYPE tessera_warehouse_bytes gauge", f"tessera_warehouse_bytes {size}",
                  "# TYPE tessera_build_info gauge", f'tessera_build_info{{version="{__version__}"}} 1',
                  "# TYPE tessera_query_cache_total counter", f'tessera_query_cache_total{{result="hit"}} {plat.wh.cache_hits}', f'tessera_query_cache_total{{result="miss"}} {plat.wh.cache_misses}']
        return Response(metrics.render(extra), media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------ audit export and retention
    @app.get("/api/audit/export")
    def audit_export(since: str = "", until: str = "", format: str = "csv", u: dict = Depends(admin)):
        q, args = "SELECT * FROM audit WHERE 1=1", []
        if since:
            q += " AND ts >= ?"
            args.append(since)
        if until:
            q += " AND ts < ?"
            args.append(until)
        rows = store.all(q + " ORDER BY id", args)
        audit(u, "audit.exported", format, {"rows": len(rows), "since": since, "until": until})
        if format == "json":
            body = "\n".join(json.dumps(r, default=str) for r in rows) + "\n"
            return Response(body, media_type="application/x-ndjson", headers={"Content-Disposition": 'attachment; filename="tessera-audit.ndjson"'})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "ts", "user_email", "action", "target", "detail", "ip"])
        for r in rows:
            w.writerow([r["id"], r["ts"], r["user_email"], r["action"], r["target"], json.dumps(r["detail"]), r["ip"]])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="tessera-audit.csv"'})

    @app.get("/api/admin/retention")
    def get_retention(u: dict = Depends(admin)):
        return {"audit_days": store.get_setting("audit_retention_days"), "notifications_days": store.get_setting("notification_retention_days", 90)}

    @app.put("/api/admin/retention")
    def put_retention(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        days = b.get("audit_days")
        if days not in (None, "") and (not str(days).isdigit() or int(days) < 30):
            raise HTTPException(400, "Keep audit records at least 30 days, or leave empty to keep them forever.")
        store.set_setting("audit_retention_days", int(days) if days not in (None, "") else None)
        audit(u, "retention.updated", "", {"audit_days": days}, request)
        return {"ok": True, "purged": purge()}

    def purge() -> int:
        days = store.get_setting("audit_retention_days")
        n = 0
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
            n = store.scalar("SELECT count(*) FROM audit WHERE ts < ?", (cutoff,))
            store.exec("DELETE FROM audit WHERE ts < ?", (cutoff,))
        ncut = (datetime.now(timezone.utc) - timedelta(days=int(store.get_setting("notification_retention_days", 90)))).isoformat()
        store.exec("DELETE FROM notifications WHERE ts < ? AND read=1", (ncut,))
        store.exec("DELETE FROM sessions WHERE expires_at < ?", (now(),))
        return n
    app.state.purge = purge
    if getattr(app.state, "reporter", None):
        app.state.reporter.purge = purge

    # ------------------------------------------------------------ sessions
    @app.get("/api/sessions")
    def sessions(request: Request, all: bool = False, u: dict = Depends(viewer)):
        cur = token_hash(request.cookies.get(COOKIE, ""))
        q = "SELECT s.token_hash, s.created_at, s.expires_at, s.ip, s.user_agent, u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.expires_at > ?"
        rows = store.all(q + " ORDER BY s.created_at DESC", (now(),)) if all and u["role"] == "admin" else \
            store.all(q + " AND s.user_id=? ORDER BY s.created_at DESC", (now(), u["id"]))
        return [{"id": r["token_hash"][:16], "email": r["email"], "created_at": r["created_at"], "expires_at": r["expires_at"], "ip": r["ip"],
                 "user_agent": r["user_agent"], "current": r["token_hash"] == cur} for r in rows]

    @app.delete("/api/sessions/{sid}")
    def end_session(sid: str, request: Request, u: dict = Depends(viewer)):
        if not re.fullmatch(r"[0-9a-f]{16}", sid):
            raise HTTPException(400, "Bad session id.")
        s = store.one("SELECT * FROM sessions WHERE substr(token_hash,1,16)=?", (sid,))
        if not s or (s["user_id"] != u["id"] and u["role"] != "admin"):
            raise HTTPException(404, "Session not found.")
        store.exec("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        audit(u, "session.revoked", sid, None, request)
        return {"ok": True}

    # ------------------------------------------------------------ backup
    @app.get("/api/admin/cloud")
    def cloud_status(u: dict = Depends(admin)):
        b = getattr(app.state, "cloud", None)
        return b.status() if b else {"enabled": False}

    @app.post("/api/admin/cloud/backup")
    def cloud_backup_now(request: Request, u: dict = Depends(admin)):
        b = getattr(app.state, "cloud", None)
        if not b:
            raise HTTPException(400, "Cloud backups are not configured. They run when Tessera is started with `tessera cloud-run`.")
        name = b.run_once(force=True)
        if not name:
            raise HTTPException(502, f"Backup failed: {b.last_error}")
        audit(u, "cloud.backup", name, None, request)
        return {"ok": True, "key": name, **b.status()}

    @app.post("/api/admin/backup")
    def make_backup(request: Request, b: dict = Body(default={}), u: dict = Depends(admin)):
        fd, path = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        manifest = backup(plat, Path(path), include_key=bool((b or {}).get("include_key")))
        audit(u, "backup.created", "", {"includes_key": manifest["includes_key"]}, request)

        def stream():
            try:
                with open(path, "rb") as f:
                    while chunk := f.read(1 << 20):
                        yield chunk
            finally:
                os.unlink(path)
        name = f"tessera-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz"
        return StreamingResponse(stream(), media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ SCIM settings
    @app.get("/api/admin/scim")
    def get_scim(u: dict = Depends(admin)):
        c = store.get_setting("scim") or {}
        return {"enabled": bool(c.get("token_hash")), "default_role": c.get("default_role", "viewer"), "group_map": c.get("group_map", {}),
                "base_url": (settings.base_url or f"http://localhost:{settings.port}") + "/scim/v2",
                "users": store.scalar("SELECT count(*) FROM users WHERE scim_id IS NOT NULL"), "groups": store.scalar("SELECT count(*) FROM scim_groups")}

    @app.put("/api/admin/scim")
    def put_scim(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        c = store.get_setting("scim") or {}
        if "default_role" in b:
            if b["default_role"] not in ROLES or b["default_role"] == "admin":
                raise HTTPException(400, "Default role is viewer, analyst or engineer.")
            c["default_role"] = b["default_role"]
        if "group_map" in b:
            gm = {}
            for g, rule in (b["group_map"] or {}).items():
                rule = dict(rule or {})
                if rule.get("role") and rule["role"] not in ROLES:
                    raise HTTPException(400, f"Group '{g}': unknown role.")
                if rule.get("workspace"):
                    if not store.one("SELECT id FROM workspaces WHERE name=?", (rule["workspace"],)):
                        raise HTTPException(400, f"Group '{g}': workspace '{rule['workspace']}' not found.")
                    if rule.get("workspace_role", "viewer") not in ws.WS_ROLES:
                        raise HTTPException(400, f"Group '{g}': unknown workspace role.")
                gm[str(g)] = rule
            c["group_map"] = gm
        store.set_setting("scim", c)
        resync_all()
        audit(u, "scim.updated", "", {k: v for k, v in c.items() if k != "token_hash"}, request)
        return {"ok": True}

    @app.post("/api/admin/scim/token")
    def scim_token(request: Request, u: dict = Depends(admin)):
        c = store.get_setting("scim") or {}
        tok = "scim_" + secrets.token_urlsafe(32)
        c["token_hash"] = token_hash(tok)
        store.set_setting("scim", c)
        audit(u, "scim.token_rotated", "", None, request)
        return {"token": tok, "note": "Paste this into your identity provider now. It is not shown again; generating a new one revokes it."}

    # ------------------------------------------------------------ SCIM role and workspace sync
    def resync(user_row: dict):
        c = store.get_setting("scim") or {}
        if not user_row.get("scim_id"):
            return
        gm = c.get("group_map", {})
        role = c.get("default_role", "viewer")
        wanted: dict[int, str] = {}
        for g in store.all("SELECT * FROM scim_groups"):
            if user_row["scim_id"] not in g["members"]:
                continue
            rule = gm.get(g["display_name"]) or {}
            if rule.get("role") and RANK[rule["role"]] > RANK[role]:
                role = rule["role"]
            if rule.get("workspace"):
                w = store.one("SELECT id FROM workspaces WHERE name=?", (rule["workspace"],))
                if w:
                    wr = rule.get("workspace_role", "viewer")
                    if w["id"] not in wanted or ws.WS_RANK[wr] > ws.WS_RANK[wanted[w["id"]]]:
                        wanted[w["id"]] = wr
        if user_row["role"] != role:
            store.exec("UPDATE users SET role=? WHERE id=?", (role, user_row["id"]))
            store.exec("DELETE FROM sessions WHERE user_id=?", (user_row["id"],))
        store.exec("DELETE FROM workspace_members WHERE user_id=? AND source='scim'", (user_row["id"],))
        for wid, wr in wanted.items():
            store.exec("INSERT INTO workspace_members(workspace_id,user_id,role,source) VALUES(?,?,?,'scim') "
                       "ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=CASE WHEN workspace_members.source='scim' THEN excluded.role ELSE workspace_members.role END",
                       (wid, user_row["id"], wr))

    def resync_all():
        for r in store.all("SELECT * FROM users WHERE scim_id IS NOT NULL"):
            resync(r)

    # ------------------------------------------------------------ SCIM endpoints
    def scim_auth(request: Request):
        c = store.get_setting("scim") or {}
        authz = request.headers.get("authorization", "")
        if not c.get("token_hash") or not authz.lower().startswith("bearer ") or not hmac.compare_digest(token_hash(authz[7:].strip()), c["token_hash"]):
            raise HTTPException(401, "SCIM bearer token is missing or wrong.")
        return True

    def base(request: Request) -> str:
        return settings.base_url or str(request.base_url).rstrip("/")

    def sj(body, status=200):
        return JSONResponse(body, status_code=status, media_type="application/scim+json")

    def scim_actor():
        return {"id": None, "email": "scim"}

    @app.get("/scim/v2/ServiceProviderConfig", include_in_schema=False)
    def spc(_: bool = Depends(scim_auth)):
        return sj({"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"], "patch": {"supported": True}, "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                   "filter": {"supported": True, "maxResults": 200}, "changePassword": {"supported": False}, "sort": {"supported": False}, "etag": {"supported": False},
                   "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token", "description": "Token generated under Admin → Provisioning"}]})

    @app.get("/scim/v2/ResourceTypes", include_in_schema=False)
    def rtypes(_: bool = Depends(scim_auth)):
        rts = [{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "User", "name": "User", "endpoint": "/Users", "schema": SCIM_USER},
               {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "Group", "name": "Group", "endpoint": "/Groups", "schema": SCIM_GROUP}]
        return sj({"schemas": [SCIM_LIST], "totalResults": 2, "Resources": rts, "startIndex": 1, "itemsPerPage": 2})

    @app.get("/scim/v2/Users", include_in_schema=False)
    def scim_users(request: Request, filter: str | None = None, startIndex: int = 1, count: int = 100, _: bool = Depends(scim_auth)):
        email = _filter_eq(filter, "userName")
        rows = store.all("SELECT * FROM users WHERE email=?", (email,)) if email is not None else store.all("SELECT * FROM users ORDER BY id")
        for r in rows:
            if not r.get("scim_id"):
                sid = uuid.uuid4().hex
                store.exec("UPDATE users SET scim_id=? WHERE id=?", (sid, r["id"]))
                r["scim_id"] = sid
        start, cnt = max(1, startIndex), max(0, min(count, 200))
        page = rows[start - 1:start - 1 + cnt]
        return sj({"schemas": [SCIM_LIST], "totalResults": len(rows), "startIndex": start, "itemsPerPage": len(page), "Resources": [scim_user(r, base(request)) for r in page]})

    def find_user(sid: str) -> dict:
        r = store.one("SELECT * FROM users WHERE scim_id=?", (sid,))
        if not r:
            raise HTTPException(404, "User not found.")
        return r

    def user_fields(b: dict) -> dict:
        out = {}
        email = b.get("userName") or next((e.get("value") for e in b.get("emails") or [] if e.get("primary")), None) or next((e.get("value") for e in b.get("emails") or []), None)
        if email:
            out["email"] = str(email).strip().lower()
        name = b.get("displayName") or (b.get("name") or {}).get("formatted") or " ".join(x for x in ((b.get("name") or {}).get("givenName"), (b.get("name") or {}).get("familyName")) if x)
        if name:
            out["name"] = str(name)[:120]
        if "active" in b:
            out["active"] = 1 if _bool(b["active"]) else 0
        if b.get("externalId"):
            out["external_id"] = str(b["externalId"])[:200]
        return out

    @app.post("/scim/v2/Users", include_in_schema=False)
    def scim_create_user(request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        f = user_fields(b)
        if not f.get("email") or "@" not in f["email"]:
            return scim_error(400, "userName must be an email address.", "invalidValue")
        if store.one("SELECT id FROM users WHERE email=?", (f["email"],)):
            return scim_error(409, "A user with that userName already exists.", "uniqueness")
        c = store.get_setting("scim") or {}
        sid = uuid.uuid4().hex
        uid = store.insert("users", {"email": f["email"], "name": f.get("name") or f["email"], "role": c.get("default_role", "viewer"), "pw_hash": None,
                                     "active": f.get("active", 1), "scim_id": sid, "external_id": f.get("external_id"), "created_at": now()})
        r = store.one("SELECT * FROM users WHERE id=?", (uid,))
        resync(r)
        store.audit(scim_actor(), "scim.user_created", f["email"], {"active": bool(r["active"])})
        return sj(scim_user(store.one("SELECT * FROM users WHERE id=?", (uid,)), base(request)), 201)

    @app.get("/scim/v2/Users/{sid}", include_in_schema=False)
    def scim_get_user(sid: str, request: Request, _: bool = Depends(scim_auth)):
        return sj(scim_user(find_user(sid), base(request)))

    def apply_user(r: dict, f: dict):
        if "email" in f and f["email"] != r["email"] and store.one("SELECT id FROM users WHERE email=?", (f["email"],)):
            raise HTTPException(409, "Another user already has that userName.")
        if f:
            store.update("users", r["id"], f)
        if f.get("active") == 0:
            store.exec("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            store.exec("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (r["id"],))
        store.audit(scim_actor(), "scim.user_updated", r["email"], {k: v for k, v in f.items()})

    @app.put("/scim/v2/Users/{sid}", include_in_schema=False)
    def scim_put_user(sid: str, request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        r = find_user(sid)
        apply_user(r, user_fields(b))
        return sj(scim_user(find_user(sid), base(request)))

    @app.patch("/scim/v2/Users/{sid}", include_in_schema=False)
    def scim_patch_user(sid: str, request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        r = find_user(sid)
        f: dict = {}
        for op in b.get("Operations") or []:
            kind = str(op.get("op", "")).lower()
            if kind not in ("replace", "add"):
                continue
            path, val = op.get("path"), op.get("value")
            if not path and isinstance(val, dict):
                f.update(user_fields({**val, **({"active": val["active"]} if "active" in val else {})}))
                continue
            p = (path or "").lower()
            if p == "active":
                f["active"] = 1 if _bool(val) else 0
            elif p == "username":
                f["email"] = str(val).strip().lower()
            elif p in ("displayname", "name.formatted"):
                f["name"] = str(val)[:120]
            elif p.startswith("emails"):
                v = val[0]["value"] if isinstance(val, list) and val else val
                if isinstance(v, str) and "@" in v:
                    f["email"] = v.strip().lower()
            elif p == "externalid":
                f["external_id"] = str(val)[:200]
        apply_user(r, f)
        return sj(scim_user(find_user(sid), base(request)))

    @app.delete("/scim/v2/Users/{sid}", include_in_schema=False)
    def scim_delete_user(sid: str, _: bool = Depends(scim_auth)):
        r = find_user(sid)
        apply_user(r, {"active": 0})
        store.audit(scim_actor(), "scim.user_deprovisioned", r["email"], None)
        return Response(status_code=204)

    @app.get("/scim/v2/Groups", include_in_schema=False)
    def scim_groups(request: Request, filter: str | None = None, startIndex: int = 1, count: int = 100, _: bool = Depends(scim_auth)):
        name = _filter_eq(filter, "displayName")
        rows = store.all("SELECT * FROM scim_groups WHERE display_name=?", (name,)) if name is not None else store.all("SELECT * FROM scim_groups ORDER BY display_name")
        start, cnt = max(1, startIndex), max(0, min(count, 200))
        page = rows[start - 1:start - 1 + cnt]
        return sj({"schemas": [SCIM_LIST], "totalResults": len(rows), "startIndex": start, "itemsPerPage": len(page), "Resources": [scim_group(g, store, base(request)) for g in page]})

    def find_group(gid: str) -> dict:
        g = store.one("SELECT * FROM scim_groups WHERE id=?", (gid,))
        if not g:
            raise HTTPException(404, "Group not found.")
        return g

    def set_members(g: dict, members: list[str]):
        before = set(g["members"])
        valid = [m for m in dict.fromkeys(members) if store.one("SELECT id FROM users WHERE scim_id=?", (m,))]
        store.exec("UPDATE scim_groups SET members=?, updated_at=? WHERE id=?", (json.dumps(valid), now(), g["id"]))
        for sid in before | set(valid):
            r = store.one("SELECT * FROM users WHERE scim_id=?", (sid,))
            if r:
                resync(r)

    @app.post("/scim/v2/Groups", include_in_schema=False)
    def scim_create_group(request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        name = (b.get("displayName") or "").strip()
        if not name:
            return scim_error(400, "displayName is required.", "invalidValue")
        if store.one("SELECT id FROM scim_groups WHERE display_name=?", (name,)):
            return scim_error(409, "A group with that displayName exists.", "uniqueness")
        gid = uuid.uuid4().hex
        store.insert("scim_groups", {"id": gid, "display_name": name, "external_id": b.get("externalId"), "members": "[]", "created_at": now(), "updated_at": now()})
        set_members(find_group(gid), [m.get("value") for m in b.get("members") or []])
        store.audit(scim_actor(), "scim.group_created", name, None)
        return sj(scim_group(find_group(gid), store, base(request)), 201)

    @app.get("/scim/v2/Groups/{gid}", include_in_schema=False)
    def scim_get_group(gid: str, request: Request, _: bool = Depends(scim_auth)):
        return sj(scim_group(find_group(gid), store, base(request)))

    @app.put("/scim/v2/Groups/{gid}", include_in_schema=False)
    def scim_put_group(gid: str, request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        find_group(gid)  # 404 when the group does not exist
        if b.get("displayName"):
            store.exec("UPDATE scim_groups SET display_name=? WHERE id=?", (b["displayName"], gid))
        set_members(find_group(gid), [m.get("value") for m in b.get("members") or []])
        return sj(scim_group(find_group(gid), store, base(request)))

    @app.patch("/scim/v2/Groups/{gid}", include_in_schema=False)
    def scim_patch_group(gid: str, request: Request, b: dict = Body(...), _: bool = Depends(scim_auth)):
        g = find_group(gid)
        members = list(g["members"])
        for op in b.get("Operations") or []:
            kind, path, val = str(op.get("op", "")).lower(), op.get("path") or "", op.get("value")
            if path.lower() == "displayname" or (not path and isinstance(val, dict) and "displayName" in val):
                store.exec("UPDATE scim_groups SET display_name=? WHERE id=?", (val if isinstance(val, str) else val["displayName"], gid))
                continue
            vals = [v.get("value") for v in val] if isinstance(val, list) else ([v.get("value") for v in (val or {}).get("members", [])] if isinstance(val, dict) else [])
            m = re.match(r'members\[value eq "([^"]+)"\]', path)
            if kind == "add":
                members += vals
            elif kind == "remove":
                drop = set(vals) | ({m.group(1)} if m else set())
                members = [] if path.lower() == "members" and not vals else [x for x in members if x not in drop]
            elif kind == "replace":
                members = vals
        set_members(find_group(gid), members)
        return sj(scim_group(find_group(gid), store, base(request)))

    @app.delete("/scim/v2/Groups/{gid}", include_in_schema=False)
    def scim_delete_group(gid: str, _: bool = Depends(scim_auth)):
        g = find_group(gid)
        set_members(g, [])
        store.exec("DELETE FROM scim_groups WHERE id=?", (gid,))
        for sid in g["members"]:
            r = store.one("SELECT * FROM users WHERE scim_id=?", (sid,))
            if r:
                resync(r)
        return Response(status_code=204)


def _ip(request):
    from .auth import client_ip
    return client_ip(request)
