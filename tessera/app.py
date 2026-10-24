"""HTTP API and web app."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, agents, connectors, demo, pii, quality
from . import workspaces as ws
from .auth import COOKIE, OIDC, IPThrottle, client_ip, create_session, current_user, login, public_user
from .config import Settings
from .connectors import ConnectorError
from .errors import public_message
from .orchestrator import STEP_TYPES, Orchestrator, PipelineError, next_run, validate
from .platform import NotFound, Platform
from .security import ROLE_HELP, ROLES, has_role, hash_password, new_token, password_problem, restricted, token_hash, valid_email, verify_password
from .store import now
from .warehouse import AccessDenied, QueryError

log = logging.getLogger("tessera")
STATIC = Path(__file__).parent / "static"


def body_get(b: dict, key: str, typ=str, required=True, default=None):
    v = b.get(key, default)
    if required and v in (None, ""):
        raise HTTPException(400, f"'{key}' is required.")
    if v is not None and typ is int:
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{key}' must be a number.") from None
    return v


def create_app(settings: Settings | None = None, http_factory=None, start_scheduler: bool = True,
               kinesis_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    plat = Platform(settings)
    orch = Orchestrator(plat, http_client_factory=http_factory)
    oidc = OIDC(plat.store, plat.vault, http_factory)
    throttle = IPThrottle()
    store = plat.store

    setup_code_path = settings.data_dir / "setup-code.txt"
    if not store.scalar("SELECT count(*) FROM users"):
        preset = os.environ.get("TESSERA_SETUP_CODE", "").strip()
        if preset:  # hosted installs (containers, PaaS) where reading a file or the console is awkward
            if len(preset) < 8:
                raise SystemExit("TESSERA_SETUP_CODE must be at least 8 characters.")
            setup_code_path.write_text(preset.upper())  # the form compares case-insensitively
            log.warning("First run. Use the setup code you set in TESSERA_SETUP_CODE.")
        else:
            if not setup_code_path.exists():
                setup_code_path.write_text(secrets.token_hex(4).upper())
            log.warning("First run. Setup code: %s (also in %s)", setup_code_path.read_text().strip(), setup_code_path)

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler:
            orch.start()
            app.state.streams.resume_all()
            app.state.mirrors.resume_all()
        yield
        app.state.mirrors.shutdown()
        app.state.streams.shutdown()
        orch.stop()
        for hook in app.state.shutdown_hooks:  # for example a final backup on hosted installs
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("shutdown hook failed")
        plat.close()

    app = FastAPI(title="Tessera", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.platform, app.state.orch, app.state.oidc, app.state.settings = plat, orch, oidc, settings
    app.state.shutdown_hooks = []

    # ------------------------------------------------------------------ middleware & errors
    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        embed = request.url.path.startswith("/embed/")
        ancestors = "'none'"
        if embed:
            origins = (store.get_setting("sharing") or {}).get("embed_origins") or []
            ancestors = " ".join(["'self'", *origins])
        else:
            resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/api/docs"):
            resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                       f"connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def err(code):
        return lambda req, exc: JSONResponse({"detail": str(exc).strip("'\"")}, status_code=code)
    app.add_exception_handler(AccessDenied, err(403))
    app.add_exception_handler(QueryError, err(400))
    app.add_exception_handler(NotFound, err(404))
    app.add_exception_handler(PipelineError, err(400))
    app.add_exception_handler(ConnectorError, err(400))
    app.add_exception_handler(agents.AgentError, err(502))

    # ------------------------------------------------------------------ auth deps
    def user(request: Request) -> dict:
        return current_user(request, store)

    def need(role: str):
        def dep(u: dict = Depends(user)) -> dict:
            if not has_role(u["role"], role):
                raise HTTPException(403, f"This needs the {role} role or higher.")
            return u
        return dep

    viewer, analyst, engineer, admin = need("viewer"), need("analyst"), need("engineer"), need("admin")

    def start_demo_feed(out: dict):
        # the simulated checkout feed runs only in a live server, not in tests that disable background work
        if start_scheduler and out.get("stream_id"):
            app.state.streams.start(out["stream_id"])

    def audit(u, action, target="", detail=None, req: Request | None = None):
        store.audit(u, action, target, detail, client_ip(req) if req else None)

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def landing():
        return FileResponse(STATIC / "index.html")

    @app.get("/app", include_in_schema=False)
    def webapp():
        return FileResponse(STATIC / "app.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    # ------------------------------------------------------------------ setup
    @app.get("/api/setup/status")
    def setup_status():
        return {"needs_setup": not store.scalar("SELECT count(*) FROM users"), "sso": bool(oidc.config()), "version": __version__}

    @app.post("/api/setup")
    def setup(request: Request, b: dict = Body(...)):
        if store.scalar("SELECT count(*) FROM users"):
            raise HTTPException(409, "Tessera is already set up.")
        throttle.check(client_ip(request))
        code = setup_code_path.read_text().strip() if setup_code_path.exists() else ""
        if not secrets.compare_digest(str(b.get("setup_code", "")).strip().upper(), code):
            raise HTTPException(403, "Setup code is incorrect. It is printed in the server console and saved in setup-code.txt in the data folder.")
        email, name, pw = body_get(b, "email").strip().lower(), body_get(b, "name").strip(), body_get(b, "password")
        if not valid_email(email):
            raise HTTPException(400, "Enter a valid email address.")
        if p := password_problem(pw, email):
            raise HTTPException(400, p)
        uid = store.insert("users", {"email": email, "name": name, "role": "admin", "pw_hash": hash_password(pw), "created_at": now()})
        u = store.one("SELECT * FROM users WHERE id=?", (uid,))
        audit(u, "setup.completed", email, None, request)
        setup_code_path.unlink(missing_ok=True)
        result = {"ok": True}
        if b.get("load_sample"):
            result["sample"] = demo.install(plat, orch, u)
            start_demo_feed(result["sample"])
            for a in store.all("SELECT * FROM alerts"):
                app.state.reporter.evaluate(a)
        tok = create_session(store, uid, settings.session_hours, request)
        resp = JSONResponse(result)
        resp.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=settings.secure_cookies, max_age=settings.session_hours * 3600, path="/")
        return resp

    # ------------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    def do_login(request: Request, b: dict = Body(...)):
        throttle.check(client_ip(request))
        u = login(store, body_get(b, "email"), body_get(b, "password"))
        tok = create_session(store, u["id"], settings.session_hours, request)
        audit(u, "auth.login", u["email"], None, request)
        resp = JSONResponse({"user": public_user(u)})
        resp.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=settings.secure_cookies, max_age=settings.session_hours * 3600, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        tok = request.cookies.get(COOKIE)
        if tok:
            store.exec("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/auth/session")
    def session_state(request: Request):
        try:
            u = current_user(request, store)
        except HTTPException:
            return {"user": None}
        return {"user": public_user(u), "via": u.get("via")}

    @app.get("/api/auth/me")
    def me(u: dict = Depends(user)):
        return {"user": public_user(u), "role_help": ROLE_HELP, "via": u.get("via")}

    @app.post("/api/auth/password")
    def change_password(request: Request, b: dict = Body(...), u: dict = Depends(user)):
        if u.get("pw_hash") and not verify_password(b.get("current_password", ""), u["pw_hash"]):
            raise HTTPException(400, "Current password is incorrect.")
        pw = body_get(b, "new_password")
        if p := password_problem(pw, u["email"]):
            raise HTTPException(400, p)
        store.exec("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(pw), u["id"]))
        keep = token_hash(request.cookies.get(COOKIE, ""))
        store.exec("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], keep))
        audit(u, "auth.password_changed", u["email"], None, request)
        return {"ok": True}

    @app.get("/api/auth/oidc/login", include_in_schema=False)
    def oidc_login():
        return RedirectResponse(oidc.login_url(), status_code=302)

    @app.get("/api/auth/oidc/callback", include_in_schema=False)
    def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, f"Sign-in was cancelled or refused: {error}")
        u = oidc.callback(code, state)
        tok = create_session(store, u["id"], settings.session_hours, request)
        audit(u, "auth.sso_login", u["email"], None, request)
        resp = RedirectResponse("/app", status_code=302)
        resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", secure=settings.secure_cookies, max_age=settings.session_hours * 3600, path="/")
        return resp

    # ------------------------------------------------------------------ users & tokens
    @app.get("/api/users")
    def users(u: dict = Depends(admin)):
        return [public_user(x) for x in store.all("SELECT * FROM users ORDER BY email")]

    @app.post("/api/users")
    def add_user(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        email = body_get(b, "email").strip().lower()
        role = b.get("role", "viewer")
        if role not in ROLES:
            raise HTTPException(400, "Unknown role.")
        if not valid_email(email):
            raise HTTPException(400, "Enter a valid email address.")
        if store.one("SELECT id FROM users WHERE email=?", (email,)):
            raise HTTPException(409, "A user with that email already exists.")
        pw = b.get("password") or None
        if pw and (p := password_problem(pw, email)):
            raise HTTPException(400, p)
        uid = store.insert("users", {"email": email, "name": b.get("name") or email, "role": role,
                                     "pw_hash": hash_password(pw) if pw else None, "created_at": now()})
        audit(u, "user.created", email, {"role": role}, request)
        return public_user(store.one("SELECT * FROM users WHERE id=?", (uid,)))

    @app.patch("/api/users/{uid}")
    def edit_user(uid: int, request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        t = store.one("SELECT * FROM users WHERE id=?", (uid,))
        if not t:
            raise NotFound("User not found")
        patch = {}
        if "role" in b:
            if b["role"] not in ROLES:
                raise HTTPException(400, "Unknown role.")
            if t["id"] == u["id"] and b["role"] != "admin":
                raise HTTPException(400, "You cannot remove your own admin role.")
            patch["role"] = b["role"]
        if "active" in b:
            if t["id"] == u["id"] and not b["active"]:
                raise HTTPException(400, "You cannot deactivate yourself.")
            patch["active"] = 1 if b["active"] else 0
        if "name" in b:
            patch["name"] = str(b["name"])[:120]
        if b.get("unlock"):
            patch.update({"locked_until": None, "failed_logins": 0})
        if b.get("password"):
            if p := password_problem(b["password"], t["email"]):
                raise HTTPException(400, p)
            patch["pw_hash"] = hash_password(b["password"])
        store.update("users", uid, patch)
        if "active" in patch and not patch["active"] or "pw_hash" in patch or "role" in patch:
            store.exec("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(u, "user.updated", t["email"], {k: v for k, v in patch.items() if k != "pw_hash"} | ({"credential_reset": True} if "pw_hash" in patch else {}), request)
        return public_user(store.one("SELECT * FROM users WHERE id=?", (uid,)))

    @app.get("/api/tokens")
    def tokens(u: dict = Depends(user)):
        q = "SELECT t.id, t.name, t.prefix, t.role, t.created_at, t.last_used, t.revoked, u.email FROM api_tokens t JOIN users u ON u.id=t.user_id"
        return store.all(q + " ORDER BY t.id DESC") if u["role"] == "admin" else store.all(q + " WHERE t.user_id=? ORDER BY t.id DESC", (u["id"],))

    @app.post("/api/tokens")
    def make_token(request: Request, b: dict = Body(...), u: dict = Depends(user)):
        if u.get("via") == "token":
            raise HTTPException(403, "API tokens cannot create other tokens.")
        role = b.get("role", u["role"])
        if role not in ROLES or not has_role(u["role"], role):
            raise HTTPException(400, "A token cannot have a higher role than you.")
        tok = new_token("tsr_")
        tid = store.insert("api_tokens", {"name": body_get(b, "name")[:80], "token_hash": token_hash(tok), "prefix": tok[:8], "user_id": u["id"],
                                          "role": role, "created_at": now()})
        audit(u, "token.created", b["name"], {"role": role}, request)
        return {"id": tid, "token": tok, "note": "Copy this token now. It is not shown again."}

    @app.delete("/api/tokens/{tid}")
    def revoke_token(tid: int, request: Request, u: dict = Depends(user)):
        t = store.one("SELECT * FROM api_tokens WHERE id=?", (tid,))
        if not t or (t["user_id"] != u["id"] and u["role"] != "admin"):
            raise NotFound("Token not found")
        store.exec("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,))
        audit(u, "token.revoked", t["name"], None, request)
        return {"ok": True}

    @app.get("/api/admin/sso")
    def get_sso(u: dict = Depends(admin)):
        c = store.get_setting("oidc") or {}
        return {k: v for k, v in c.items() if k != "secret_blob"} | {"client_secret_set": bool(c.get("secret_blob")),
                                                                     "suggested_redirect_uri": (settings.base_url or "http://localhost:%d" % settings.port) + "/api/auth/oidc/callback"}

    @app.put("/api/admin/sso")
    def put_sso(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        old = store.get_setting("oidc") or {}
        c = {k: b.get(k, old.get(k)) for k in ("enabled", "issuer", "client_id", "redirect_uri", "scopes", "default_role", "allowed_domains", "auto_provision", "role_groups", "button_label")}
        if c.get("enabled"):
            for k in ("issuer", "client_id", "redirect_uri"):
                if not c.get(k):
                    raise HTTPException(400, f"'{k}' is required to enable SSO.")
        if c.get("default_role") and c["default_role"] not in ROLES:
            raise HTTPException(400, "Unknown default role.")
        c["secret_blob"] = plat.vault.encrypt({"client_secret": b["client_secret"]}) if b.get("client_secret") else old.get("secret_blob")
        store.set_setting("oidc", c)
        oidc._disc.clear()
        audit(u, "sso.updated", c.get("issuer") or "", {"enabled": bool(c.get("enabled"))}, request)
        return {"ok": True}

    @app.get("/api/audit")
    def audit_log(limit: int = 200, action: str = "", u: dict = Depends(admin)):
        if action:
            return store.all("SELECT * FROM audit WHERE action LIKE ? ORDER BY id DESC LIMIT ?", (f"{action}%", min(limit, 2000)))
        return store.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 2000),))

    # ------------------------------------------------------------------ connections
    @app.get("/api/connectors")
    def connector_catalog():
        from . import saas
        return {"types": connectors.catalog(), "via_generic": [{"name": n, "via": v} for n, v in connectors.VIA_GENERIC], "roadmap": connectors.ROADMAP,
                "apps": [{"key": a["key"], "label": a["label"], "category": a["category"], "changes_only": bool(a["incremental"])} for a in saas.catalog()]}

    @app.get("/api/connections")
    def list_connections(u: dict = Depends(engineer)):
        return [plat.public_connection(c) for c in store.all("SELECT * FROM connections ORDER BY name")]

    @app.post("/api/connections")
    def create_connection(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        ctype = body_get(b, "type")
        if ctype not in connectors.CONNECTOR_TYPES:
            raise HTTPException(400, "Unknown connector type.")
        name = body_get(b, "name").strip()[:80]
        if store.one("SELECT id FROM connections WHERE name=?", (name,)):
            raise HTTPException(409, "A connection with that name exists.")
        cfg, sec = connectors.split_config(ctype, b.get("config") or {})
        for f in connectors.CONNECTOR_TYPES[ctype]["fields"]:
            if f.get("required") and not (cfg.get(f["key"]) or sec.get(f["key"])):
                raise HTTPException(400, f"'{f['label']}' is required.")
        cid = store.insert("connections", {"name": name, "type": ctype, "config": json.dumps(cfg), "secrets": plat.vault.encrypt(sec) if sec else None,
                                           "created_by": u["id"], "created_at": now(), "updated_at": now()})
        audit(u, "connection.created", name, {"type": ctype}, request)
        return plat.public_connection(plat.connection(cid))

    @app.put("/api/connections/{cid}")
    def update_connection(cid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        c = plat.connection(cid)
        cfg, sec = connectors.split_config(c["type"], b.get("config") or {})
        old_sec = plat.vault.decrypt(c["secrets"])
        old_sec.update(sec)
        store.update("connections", cid, {"name": (b.get("name") or c["name"])[:80], "config": json.dumps(cfg),
                                          "secrets": plat.vault.encrypt(old_sec) if old_sec else None, "updated_at": now()})
        audit(u, "connection.updated", c["name"], {"secrets_changed": sorted(sec)}, request)
        return plat.public_connection(plat.connection(cid))

    @app.delete("/api/connections/{cid}")
    def delete_connection(cid: int, request: Request, u: dict = Depends(engineer)):
        c = plat.connection(cid)
        if store.one("SELECT id FROM mirrors WHERE connection_id=?", (cid,)):
            raise HTTPException(409, "Stop this connection's mirrors first, so the database stops keeping change log for Tessera.")
        store.exec("DELETE FROM connections WHERE id=?", (cid,))
        audit(u, "connection.deleted", c["name"], None, request)
        return {"ok": True}

    @app.post("/api/connections/{cid}/test")
    def test_connection(cid: int, u: dict = Depends(engineer)):
        try:
            msg, ok = plat.connector(cid).test(), True
        except Exception as e:  # noqa: BLE001 - any failure is reported as a failed test
            msg, ok = public_message(e), False
        store.update("connections", cid, {"last_test_ok": int(ok), "last_test_msg": msg[:500], "last_test_at": now()})
        return {"ok": ok, "message": msg}

    @app.get("/api/connections/{cid}/browse")
    def browse_connection(cid: int, u: dict = Depends(engineer)):
        return plat.connector(cid).browse()

    @app.post("/api/connections/{cid}/ingest")
    def ingest(cid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        res = plat.ingest(cid, b.get("source") or {}, body_get(b, "target"), b.get("mode", "replace"))
        audit(u, "dataset.ingested", res["dataset"], {"rows": res["rows_loaded"], "connection_id": cid}, request)
        return res

    @app.post("/api/uploads")
    async def upload(request: Request, file: UploadFile = File(...), target: str = Form(""), u: dict = Depends(engineer)):
        name = Path(file.filename or "upload.csv").name
        if not re.fullmatch(r"[\w.\- ]{1,120}", name) or name.startswith("."):
            raise HTTPException(400, "Rename the file to letters, digits, dots, dashes or underscores.")
        if Path(name).suffix.lower() not in {".csv", ".tsv", ".txt", ".json", ".ndjson", ".jsonl", ".parquet", ".xlsx", ".db", ".sqlite"}:
            raise HTTPException(400, "Supported uploads: CSV, TSV, JSON, NDJSON, Parquet, Excel, SQLite.")
        dest = settings.uploads_dir / name
        size, limit = 0, settings.max_upload_mb * 1024 * 1024
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
                fh.write(chunk)
        audit(u, "file.uploaded", name, {"bytes": size}, request)
        out: dict[str, Any] = {"file": name, "bytes": size}
        if target:
            tbl = connectors.read_bytes_or_path(str(dest), "auto", name)
            d = plat.load_table(tbl, target, name)
            out["dataset"] = {"id": d["id"], "name": f"{d['schema_name']}.{d['name']}", "rows": d["row_count"]}
        return out

    # ------------------------------------------------------------------ catalog
    def visible(u, d):
        if restricted(u["role"]) and d["schema_name"] != "serving":
            raise AccessDenied("Your role can see published datasets only.")
        return d

    @app.get("/api/datasets")
    def datasets(u: dict = Depends(viewer)):
        plat.sync_catalog()
        rows = store.all("SELECT * FROM datasets ORDER BY CASE schema_name WHEN 'serving' THEN 0 WHEN 'curated' THEN 1 ELSE 2 END, name")
        hidden_counts = plat.rls_protected() if restricted(u["role"]) else set()
        if restricted(u["role"]):
            rows = [r for r in rows if r["schema_name"] == "serving"]
        out = []
        for r in rows:
            q = plat.latest_quality(r["id"])
            out.append({k: r[k] for k in ("id", "schema_name", "name", "description", "owner", "row_count", "last_loaded_at", "published_at", "pii_scanned_at", "endorsement", "domain")}
                       | ({"row_count": None, "rls": True} if r["name"] in hidden_counts else {"rls": r["name"] in plat.rls_protected() and r["schema_name"] == "serving"})
                       | {"columns": len(r["columns"] or []), "sensitive": sum(1 for c in r["columns"] or [] if c.get("pii")),
                          "quality": q["score"], "rules": q["rules"], "readiness": plat.readiness(r["id"])["score"]})
        return out

    @app.get("/api/datasets/{did}")
    def dataset(did: int, u: dict = Depends(viewer)):
        d = visible(u, plat.dataset_by_id(did))
        prot = d["schema_name"] == "serving" and d["name"] in plat.rls_protected()
        if prot and restricted(u["role"]):
            d["row_count"] = None
        return d | {"rls": prot, "quality": plat.latest_quality(did), "readiness": plat.readiness(did),
                    "connection": (plat.public_connection(plat.connection(d["connection_id"])) if d.get("connection_id") and not restricted(u["role"]) else None)}

    @app.patch("/api/datasets/{did}")
    def edit_dataset(did: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        d = plat.dataset_by_id(did)
        patch = {}
        for k in ("description", "owner"):
            if k in b:
                patch[k] = str(b[k])[:4000]
        if "freshness_sla_hours" in b:
            patch["freshness_sla_hours"] = float(b["freshness_sla_hours"]) if b["freshness_sla_hours"] not in (None, "") else None
        if "tags" in b:
            patch["tags"] = json.dumps([str(t)[:40] for t in b["tags"]][:20])
        store.update("datasets", did, patch)
        audit(u, "dataset.updated", f"{d['schema_name']}.{d['name']}", {"fields": list(patch)}, request)
        return plat.dataset_by_id(did)

    @app.patch("/api/datasets/{did}/columns/{col}")
    def edit_column(did: int, col: str, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        d = plat.set_column(did, col, b)
        audit(u, "column.updated", f"{d['schema_name']}.{d['name']}.{col}", b, request)
        return d

    @app.delete("/api/datasets/{did}")
    def drop_dataset(did: int, request: Request, u: dict = Depends(engineer)):
        d = plat.dataset_by_id(did)
        plat.wh.drop(d["schema_name"], d["name"])
        store.exec("DELETE FROM datasets WHERE id=?", (did,))
        audit(u, "dataset.dropped", f"{d['schema_name']}.{d['name']}", None, request)
        return {"ok": True}

    @app.get("/api/datasets/{did}/preview")
    def preview(did: int, limit: int = 100, u: dict = Depends(viewer)):
        d = visible(u, plat.dataset_by_id(did))
        return plat.user_query(u, f'SELECT * FROM "{d["schema_name"]}"."{d["name"]}"', limit=min(limit, 1000))

    @app.get("/api/datasets/{did}/profile")
    def profile(did: int, u: dict = Depends(analyst)):
        d = visible(u, plat.dataset_by_id(did))
        if restricted(u["role"]) and d["name"] in plat.rls_protected():
            raise AccessDenied("Row-level security applies to this dataset, so whole-table statistics are not available to your role.")
        return plat.wh.summarize(d["schema_name"], d["name"])

    @app.post("/api/datasets/{did}/pii-scan")
    def pii_scan(did: int, request: Request, u: dict = Depends(engineer)):
        f = plat.scan_pii(did)
        d = plat.dataset_by_id(did)
        audit(u, "dataset.pii_scanned", f"{d['schema_name']}.{d['name']}", {"sensitive": [x["column"] for x in f if x["pii"]]}, request)
        return {"findings": f, "masks": pii.MASKS, "levels": pii.LEVELS}

    @app.get("/api/pii/policies")
    def pii_policies(u: dict = Depends(viewer)):
        return {"masks": pii.MASKS, "levels": pii.LEVELS, "detectors": [{"key": d["key"], "label": d["label"], "level": d["level"], "mask": d["mask"]} for d in pii.DETECTORS]}

    @app.post("/api/datasets/{did}/publish")
    def publish(did: int, request: Request, u: dict = Depends(engineer)):
        res = plat.publish(did)
        audit(u, "dataset.published", res["dataset"], {"masked": res["masked_columns"]}, request)
        return res

    @app.post("/api/datasets/{did}/export")
    def export(did: int, request: Request, b: dict = Body(default={}), u: dict = Depends(engineer)):
        res = plat.export(did, (b or {}).get("format", "parquet"))
        audit(u, "dataset.exported", res["file"], None, request)
        return res

    @app.get("/api/exports/{name}")
    def get_export(name: str, u: dict = Depends(engineer)):
        p = (settings.exports_dir / Path(name).name).resolve()
        if p.parent != settings.exports_dir.resolve() or not p.exists():
            raise NotFound("Export not found")
        return FileResponse(p, filename=p.name)

    @app.get("/api/exports")
    def list_exports(u: dict = Depends(engineer)):
        return [{"file": p.name, "bytes": p.stat().st_size} for p in sorted(settings.exports_dir.glob("*"), key=lambda p: -p.stat().st_mtime)[:100]]

    @app.get("/api/datasets/{did}/readiness")
    def readiness(did: int, u: dict = Depends(viewer)):
        visible(u, plat.dataset_by_id(did))
        return plat.readiness(did)

    @app.get("/api/datasets/{did}/datacard")
    def datacard(did: int, u: dict = Depends(analyst)):
        visible(u, plat.dataset_by_id(did))
        return plat.datacard(did)

    @app.get("/api/datasets/{did}/lineage")
    def ds_lineage(did: int, u: dict = Depends(viewer)):
        d = visible(u, plat.dataset_by_id(did))
        return plat.lineage(f"{d['schema_name']}.{d['name']}")

    @app.get("/api/lineage")
    def all_lineage(u: dict = Depends(analyst)):
        return store.all("SELECT src, dst, kind, pipeline_id, updated_at FROM lineage ORDER BY id")

    # ------------------------------------------------------------------ quality
    @app.get("/api/quality/kinds")
    def rule_kinds(u: dict = Depends(viewer)):
        return {"kinds": quality.KINDS, "patterns": quality.PATTERNS}

    @app.get("/api/datasets/{did}/rules")
    def rules(did: int, u: dict = Depends(viewer)):
        visible(u, plat.dataset_by_id(did))
        return plat.rules(did)

    @app.post("/api/datasets/{did}/rules")
    def add_rule(did: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        rid = plat.add_rule(did, b, u["email"])
        audit(u, "rule.created", str(rid), b, request)
        return store.one("SELECT * FROM rules WHERE id=?", (rid,))

    @app.post("/api/datasets/{did}/rules/suggest")
    def suggest_rules(did: int, u: dict = Depends(analyst)):
        d = visible(u, plat.dataset_by_id(did))
        return quality.suggest(plat.wh, d["schema_name"], d["name"], plat.rules(did))

    @app.patch("/api/rules/{rid}")
    def edit_rule(rid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        r = store.one("SELECT * FROM rules WHERE id=?", (rid,))
        if not r:
            raise NotFound("Rule not found")
        patch = {}
        if "enabled" in b:
            patch["enabled"] = 1 if b["enabled"] else 0
        if "severity" in b:
            if b["severity"] not in ("error", "warn"):
                raise HTTPException(400, "Severity must be error or warn.")
            patch["severity"] = b["severity"]
        if "params" in b:
            d = plat.dataset_by_id(r["dataset_id"])
            quality.validate_rule({**r, "params": b["params"]}, [c["name"] for c in plat.wh.columns(d["schema_name"], d["name"])])
            patch["params"] = json.dumps(b["params"])
        store.update("rules", rid, patch)
        audit(u, "rule.updated", str(rid), b, request)
        return store.one("SELECT * FROM rules WHERE id=?", (rid,))

    @app.delete("/api/rules/{rid}")
    def delete_rule(rid: int, request: Request, u: dict = Depends(engineer)):
        store.exec("DELETE FROM rules WHERE id=?", (rid,))
        audit(u, "rule.deleted", str(rid), None, request)
        return {"ok": True}

    @app.get("/api/rules/{rid}/history")
    def rule_history(rid: int, u: dict = Depends(analyst)):
        return store.all("SELECT * FROM rule_results WHERE rule_id=? ORDER BY id DESC LIMIT 60", (rid,))

    @app.post("/api/datasets/{did}/quality/run")
    def run_quality(did: int, request: Request, u: dict = Depends(engineer)):
        res = plat.run_quality(did)
        audit(u, "quality.run", res["dataset"], {"score": res["score"]}, request)
        return res

    # ------------------------------------------------------------------ SQL, queries, dashboards
    @app.post("/api/sql")
    def sql(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        res = plat.user_query(u, body_get(b, "sql"), limit=min(int(b.get("limit") or settings.max_result_rows), settings.max_result_rows))
        audit(u, "sql.run", "", {"sql": b["sql"][:500], "rows": res["row_count"], "ms": res["elapsed_ms"]}, request)
        return res

    @app.get("/api/queries")
    def list_queries(workspace: int | None = None, u: dict = Depends(viewer)):
        return ws.filter_rows(store, u, store.all("SELECT * FROM queries ORDER BY name"), workspace)

    @app.post("/api/queries")
    def save_query(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        wid = ws.target_ws(store, u, b.get("workspace_id"))
        row = query_row(b, u)
        qid = store.insert("queries", {"name": body_get(b, "name")[:120], **row, "viz": json.dumps(b.get("viz") or {"type": "table"}),
                                       "workspace_id": wid, "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "query.saved", b["name"], {"workspace_id": wid}, request)
        return store.one("SELECT * FROM queries WHERE id=?", (qid,))

    @app.put("/api/queries/{qid}")
    def update_query(qid: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        q = ws.item(store, u, "query", qid, "contributor")
        row = query_row({**{"kind": q.get("kind") or "sql", "sql": q["sql"], "model_id": q.get("model_id"), "spec": q.get("spec")}, **b}, u)
        patch = {"name": b.get("name", q["name"])[:120], **row, "viz": json.dumps(b.get("viz", q["viz"])), "updated_at": now()}
        if "workspace_id" in b and int(b["workspace_id"]) != q["workspace_id"]:
            patch["workspace_id"] = ws.target_ws(store, u, b["workspace_id"])
        store.update("queries", qid, patch)
        audit(u, "query.updated", q["name"], None, request)
        return store.one("SELECT * FROM queries WHERE id=?", (qid,))

    @app.delete("/api/queries/{qid}")
    def delete_query(qid: int, request: Request, u: dict = Depends(analyst)):
        ws.item(store, u, "query", qid, "contributor")
        store.exec("DELETE FROM queries WHERE id=?", (qid,))
        audit(u, "query.deleted", str(qid), None, request)
        return {"ok": True}

    @app.post("/api/queries/{qid}/run")
    def run_query(qid: int, b: dict = Body(default={}), u: dict = Depends(viewer)):
        q = ws.item(store, u, "query", qid)
        res = plat.run_saved(u, q, b or {})
        viz = q.get("viz") or {}
        if q.get("kind") == "model" and (viz.get("forecast") or viz.get("anomalies")):
            from .insights import analyze_result
            res["analysis"] = analyze_result(plat.model(q["model_id"]), res, forecast=viz.get("forecast"), anomalies=bool(viz.get("anomalies")))
        return {"query": q, "result": res}

    def query_row(b: dict, u: dict) -> dict:
        kind = b.get("kind") or "sql"
        if kind == "model":
            m = ws.item(store, u, "model", int(body_get(b, "model_id", int)))
            spec = b.get("spec") or {}
            plat.run_model(u, plat.model(m["id"]), {**spec, "limit": 1})
            return {"kind": "model", "model_id": m["id"], "spec": json.dumps(spec), "sql": ""}
        plat.wh.validate_select(body_get(b, "sql"), restricted=restricted(u["role"]))
        return {"kind": "sql", "sql": b["sql"], "model_id": None, "spec": None}

    @app.get("/api/dashboards")
    def list_dashboards(workspace: int | None = None, u: dict = Depends(viewer)):
        return ws.filter_rows(store, u, store.all("SELECT * FROM dashboards ORDER BY name"), workspace)

    @app.get("/api/dashboards/{did}")
    def get_dashboard(did: int, u: dict = Depends(viewer)):
        d = ws.item(store, u, "dashboard", did)
        d["my_workspace_role"] = ws.role_in(store, u, d["workspace_id"])
        return d

    @app.post("/api/dashboards")
    def create_dashboard(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        wid = ws.target_ws(store, u, b.get("workspace_id"))
        tiles = b.get("tiles") or []
        did = store.insert("dashboards", {"name": body_get(b, "name")[:120], "description": b.get("description", "")[:500], "tiles": json.dumps(tiles),
                                          "filters": json.dumps(b.get("filters") or []), "workspace_id": wid,
                                          "refresh_seconds": int(b.get("refresh_seconds") or 0), "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "dashboard.created", b["name"], {"workspace_id": wid}, request)
        return store.one("SELECT * FROM dashboards WHERE id=?", (did,))

    @app.put("/api/dashboards/{did}")
    def update_dashboard(did: int, request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        d = ws.item(store, u, "dashboard", did, "contributor")
        for t in b.get("tiles", d["tiles"]):
            if t.get("type") in ("text", "insights"):
                t["text"] = str(t.get("text", ""))[:5000]
                continue
            if not store.one("SELECT id FROM queries WHERE id=?", (int(t.get("query_id", 0)),)):
                raise HTTPException(400, "A tile refers to a query that does not exist.")
            ws.item(store, u, "query", int(t["query_id"]))
        for f in b.get("filters", d.get("filters") or []):
            m = plat.model(int(f.get("model_id", 0)))
            if f.get("field") not in {x["name"] for x in m["dimensions"]}:
                raise HTTPException(400, f"Filter field '{f.get('field')}' is not a dimension of model '{m['name']}'.")
        patch = {"name": b.get("name", d["name"])[:120], "description": b.get("description", d["description"])[:500],
                 "filters": json.dumps(b.get("filters", d.get("filters") or [])),
                 "tiles": json.dumps(b.get("tiles", d["tiles"])), "refresh_seconds": int(b.get("refresh_seconds", d["refresh_seconds"]) or 0),
                 "updated_at": now()}
        if "workspace_id" in b and int(b["workspace_id"]) != d["workspace_id"]:
            patch["workspace_id"] = ws.target_ws(store, u, b["workspace_id"])
        store.update("dashboards", did, patch)
        audit(u, "dashboard.updated", d["name"], None, request)
        return store.one("SELECT * FROM dashboards WHERE id=?", (did,))

    @app.delete("/api/dashboards/{did}")
    def delete_dashboard(did: int, request: Request, u: dict = Depends(analyst)):
        ws.item(store, u, "dashboard", did, "contributor")
        store.exec("DELETE FROM dashboards WHERE id=?", (did,))
        audit(u, "dashboard.deleted", str(did), None, request)
        return {"ok": True}

    # ------------------------------------------------------------------ pipelines
    @app.get("/api/pipelines/meta")
    def pipeline_meta(u: dict = Depends(viewer)):
        return {"step_types": STEP_TYPES}

    @app.get("/api/pipelines")
    def pipelines(u: dict = Depends(analyst)):
        rows = store.all("SELECT * FROM pipelines ORDER BY name")
        for p in rows:
            p["last_run"] = store.one("SELECT id, status, trigger, created_at, finished_at FROM pipeline_runs WHERE pipeline_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
        return rows

    def _pipeline_body(b, existing=None):
        steps = b.get("steps", existing["steps"] if existing else None)
        schedule = (b.get("schedule", existing["schedule"] if existing else None) or None)
        validate(steps, schedule)
        for s in steps:
            if s["type"] == "ingest" and not store.one("SELECT id FROM connections WHERE id=?", (int(s["connection_id"]),)):
                raise PipelineError(f"Step '{s['id']}' uses connection {s['connection_id']}, which does not exist.")
        return steps, schedule

    @app.post("/api/pipelines")
    def create_pipeline(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        steps, schedule = _pipeline_body(b)
        name = body_get(b, "name")[:120]
        if store.one("SELECT id FROM pipelines WHERE name=?", (name,)):
            raise HTTPException(409, "A pipeline with that name exists.")
        pid = store.insert("pipelines", {"name": name, "description": b.get("description", ""), "steps": json.dumps(steps), "schedule": schedule,
                                         "enabled": 1 if b.get("enabled", True) else 0, "next_run_at": next_run(schedule) if schedule else None,
                                         "webhook_url": b.get("webhook_url") or None, "created_by": u["email"], "created_at": now(), "updated_at": now()})
        audit(u, "pipeline.created", name, None, request)
        return store.one("SELECT * FROM pipelines WHERE id=?", (pid,))

    @app.put("/api/pipelines/{pid}")
    def update_pipeline(pid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        p = store.one("SELECT * FROM pipelines WHERE id=?", (pid,))
        if not p:
            raise NotFound("Pipeline not found")
        steps, schedule = _pipeline_body(b, p)
        store.update("pipelines", pid, {"name": b.get("name", p["name"])[:120], "description": b.get("description", p["description"]),
                                        "steps": json.dumps(steps), "schedule": schedule, "enabled": 1 if b.get("enabled", p["enabled"]) else 0,
                                        "next_run_at": next_run(schedule) if schedule else None, "webhook_url": b.get("webhook_url", p["webhook_url"]) or None,
                                        "updated_at": now()})
        audit(u, "pipeline.updated", p["name"], None, request)
        return store.one("SELECT * FROM pipelines WHERE id=?", (pid,))

    @app.delete("/api/pipelines/{pid}")
    def delete_pipeline(pid: int, request: Request, u: dict = Depends(engineer)):
        p = store.one("SELECT * FROM pipelines WHERE id=?", (pid,))
        store.exec("DELETE FROM pipelines WHERE id=?", (pid,))
        audit(u, "pipeline.deleted", p["name"] if p else str(pid), None, request)
        return {"ok": True}

    @app.post("/api/pipelines/{pid}/run")
    def run_pipeline(pid: int, request: Request, u: dict = Depends(engineer)):
        rid = orch.trigger(pid, "api" if u.get("via") == "token" else "manual", u["email"])
        audit(u, "pipeline.triggered", str(pid), {"run_id": rid}, request)
        return {"run_id": rid}

    @app.get("/api/pipelines/{pid}/runs")
    def runs(pid: int, u: dict = Depends(analyst)):
        return store.all("SELECT id, status, trigger, triggered_by, created_at, started_at, finished_at, error FROM pipeline_runs WHERE pipeline_id=? ORDER BY id DESC LIMIT 50", (pid,))

    @app.get("/api/runs/{rid}")
    def run(rid: int, u: dict = Depends(analyst)):
        r = store.one("SELECT * FROM pipeline_runs WHERE id=?", (rid,))
        if not r:
            raise NotFound("Run not found")
        return r

    @app.post("/api/runs/{rid}/cancel")
    def cancel_run(rid: int, request: Request, u: dict = Depends(engineer)):
        orch.cancel(rid)
        audit(u, "pipeline.cancel_requested", str(rid), None, request)
        return {"ok": True, "note": "The run stops before its next step."}

    # ------------------------------------------------------------------ AI
    @app.get("/api/ai/providers")
    def providers(u: dict = Depends(analyst)):
        rows = store.all("SELECT id, name, kind, base_url, model, is_default, created_at, secrets IS NOT NULL AS key_set FROM ai_providers ORDER BY name")
        return {"providers": rows, "kinds": agents.PROVIDER_KINDS,
                "allow_raw_pii": bool(store.get_setting("ai_allow_raw_pii", False))}

    @app.post("/api/ai/providers")
    def add_provider(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        kind = body_get(b, "kind")
        if kind not in agents.PROVIDER_KINDS:
            raise HTTPException(400, "Unknown provider kind.")
        if b.get("is_default"):
            store.exec("UPDATE ai_providers SET is_default=0")
        pid = store.insert("ai_providers", {"name": body_get(b, "name")[:80], "kind": kind, "base_url": b.get("base_url") or agents.PROVIDER_KINDS[kind]["base_url"],
                                            "model": body_get(b, "model"), "secrets": plat.vault.encrypt({"api_key": b["api_key"]}) if b.get("api_key") else None,
                                            "is_default": 1 if b.get("is_default") or not store.scalar("SELECT count(*) FROM ai_providers") else 0, "created_at": now()})
        audit(u, "ai.provider_added", b["name"], {"kind": kind}, request)
        return {"id": pid}

    @app.delete("/api/ai/providers/{pid}")
    def delete_provider(pid: int, request: Request, u: dict = Depends(admin)):
        store.exec("DELETE FROM ai_providers WHERE id=?", (pid,))
        audit(u, "ai.provider_deleted", str(pid), None, request)
        return {"ok": True}

    @app.put("/api/ai/settings")
    def ai_settings(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        store.set_setting("ai_allow_raw_pii", bool(b.get("allow_raw_pii")))
        audit(u, "ai.settings", "", b, request)
        return {"ok": True}

    def provider_for(pid: int | None) -> agents.Provider:
        row = store.one("SELECT * FROM ai_providers WHERE id=?", (pid,)) if pid else store.one("SELECT * FROM ai_providers ORDER BY is_default DESC, id LIMIT 1")
        if not row:
            raise HTTPException(400, "No AI provider is configured. An admin can add one under Admin → AI providers (Ollama works fully offline).")
        return agents.Provider(row["kind"], row["base_url"], row["model"], plat.vault.decrypt(row["secrets"]).get("api_key"), http_factory)

    app.state.provider_for = provider_for

    @app.get("/api/agents")
    def agent_list(u: dict = Depends(viewer)):
        return [{"key": k, "label": a["label"], "goal": a["goal"], "tools": [t["name"] for t in agents.tools_for(k, u["role"])]} for k, a in agents.AGENTS.items()]

    @app.post("/api/agents/chat")
    def agent_chat(request: Request, b: dict = Body(...), u: dict = Depends(analyst)):
        agent = b.get("agent", "analyst")
        sid = b.get("session_id")
        sess = store.one("SELECT * FROM agent_sessions WHERE id=? AND user_id=?", (sid, u["id"])) if sid else None
        if sid and not sess:
            raise NotFound("Conversation not found")
        history = sess["messages"] if sess else []
        prov = provider_for(b.get("provider_id") or (sess["provider_id"] if sess else None))
        res = agents.chat(plat, prov, u, agent, history, body_get(b, "message")[:8000],
                          allow_raw_pii=bool(store.get_setting("ai_allow_raw_pii", False)))
        if sess:
            store.update("agent_sessions", sess["id"], {"messages": json.dumps(res["messages"]), "tokens_in": sess["tokens_in"] + res["tokens_in"],
                                                        "tokens_out": sess["tokens_out"] + res["tokens_out"], "updated_at": now()})
            sid = sess["id"]
        else:
            sid = store.insert("agent_sessions", {"user_id": u["id"], "agent": agent, "provider_id": b.get("provider_id"), "title": b["message"][:80],
                                                  "messages": json.dumps(res["messages"]), "tokens_in": res["tokens_in"], "tokens_out": res["tokens_out"],
                                                  "created_at": now(), "updated_at": now()})
        audit(u, "agent.chat", agent, {"session": sid, "tools": [t["tool"] for t in res["trace"]], "tokens": res["tokens_in"] + res["tokens_out"]}, request)
        return {"session_id": sid, "reply": res["reply"], "trace": res["trace"], "artifacts": res["artifacts"], "grounding": res["grounding"],
                "tokens_in": res["tokens_in"], "tokens_out": res["tokens_out"]}

    @app.get("/api/agents/sessions")
    def agent_sessions(u: dict = Depends(analyst)):
        return store.all("SELECT id, agent, title, tokens_in, tokens_out, created_at, updated_at FROM agent_sessions WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))

    @app.get("/api/agents/sessions/{sid}")
    def agent_session(sid: int, u: dict = Depends(analyst)):
        s = store.one("SELECT * FROM agent_sessions WHERE id=? AND user_id=?", (sid, u["id"]))
        if not s:
            raise NotFound("Conversation not found")
        return s

    @app.delete("/api/agents/sessions/{sid}")
    def delete_session(sid: int, u: dict = Depends(analyst)):
        store.exec("DELETE FROM agent_sessions WHERE id=? AND user_id=?", (sid, u["id"]))
        return {"ok": True}

    # ------------------------------------------------------------------ overview
    @app.get("/api/overview")
    def overview(u: dict = Depends(viewer)):
        plat.sync_catalog()
        o = plat.overview()
        if restricted(u["role"]):
            o["readiness"] = [r for r in o["readiness"] if r["dataset"].startswith("serving.")]
        return o

    @app.post("/api/sample")
    @app.post("/api/demo")
    def load_sample(request: Request, u: dict = Depends(admin)):
        try:
            out = demo.install(plat, orch, u)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        start_demo_feed(out)
        for a in store.all("SELECT * FROM alerts"):
            app.state.reporter.evaluate(a)
        audit(u, "demo.enabled", "", None, request)
        return out

    @app.get("/api/demo")
    def demo_status(u: dict = Depends(viewer)):
        return demo.status(store)

    @app.get("/api/demo/plan")
    def demo_plan(u: dict = Depends(admin)):
        p = demo.plan_removal(plat)
        return {"counts": {t: len(v) for t, v in p["items"].items()}, "tables": p["tables"], "user_built": p["user_built"]}

    @app.delete("/api/demo")
    def demo_remove(request: Request, u: dict = Depends(admin)):
        try:
            out = demo.remove(plat, stop_stream=lambda sid: app.state.streams.stop(sid))
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        audit(u, "demo.removed", "", out, request)
        return out

    from .api_ext import register
    register(app, plat, orch, user, need, audit)
    from .enterprise import register as register_enterprise
    register_enterprise(app, plat, user, need, audit)
    from .insights_api import register as register_insights
    register_insights(app, plat, user, need, audit)
    from .mcp import register as register_mcp
    register_mcp(app, plat, user, need, audit)
    from .share import register as register_share
    register_share(app, plat, user, need, audit, STATIC)
    from .connections_api import register as register_connections
    register_connections(app, plat, user, need, audit, orch)
    from .interop import register as register_interop
    register_interop(app, plat, user, need, audit, orch)
    from .odata import register as register_odata
    register_odata(app, plat, user, need, audit)
    from .streams_api import register as register_streams
    register_streams(app, plat, user, need, audit, kinesis_factory=kinesis_factory)
    return app
