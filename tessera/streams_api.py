"""HTTP API for streams: create, start, stop, preview, reset, rotate the push token, and receive pushed events."""
from __future__ import annotations

import json

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import workspaces as ws
from .connectors import ConnectorError
from .streaming import KINDS, StreamError, StreamManager

MAX_PUSH_BYTES = 5 * 1024 * 1024


def register(app, plat, user, need, audit, kinesis_factory=None):
    mgr = StreamManager(plat, kinesis_factory=kinesis_factory)
    app.state.streams = mgr
    analyst, engineer = need("analyst"), need("engineer")
    app.add_exception_handler(StreamError, lambda r, e: JSONResponse({"detail": str(e)}, status_code=400))

    def visible(u, s, level="viewer"):
        ws.require(plat.store, u, s.get("workspace_id") or 1, level)
        return s

    def get(u, sid, level="viewer"):
        try:
            return visible(u, mgr.get(sid), level)
        except StreamError:
            raise HTTPException(404, "Stream not found") from None

    @app.get("/api/streams/kinds")
    def kinds(u: dict = Depends(analyst)):
        return [{"kind": k, "description": v} for k, v in KINDS.items()]

    @app.get("/api/streams")
    def list_streams(workspace: int | None = None, u: dict = Depends(analyst)):
        rows = ws.filter_rows(plat.store, u, plat.store.all("SELECT id, workspace_id FROM streams ORDER BY name"), workspace)
        return [mgr.status(r["id"]) for r in rows]

    @app.get("/api/streams/{sid}")
    def stream(sid: int, u: dict = Depends(analyst)):
        get(u, sid)
        return mgr.status(sid)

    @app.post("/api/streams")
    def create(request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        wid = ws.target_ws(plat.store, u, b.get("workspace_id"))
        s, token = mgr.create(b, u, wid)
        audit(u, "stream.created", s["name"], {"kind": s["kind"], "target": s["target"]}, request)
        if b.get("start"):
            s = mgr.start(s["id"])
        return {**s, **({"token": token} if token else {})}

    @app.put("/api/streams/{sid}")
    def update(sid: int, request: Request, b: dict = Body(...), u: dict = Depends(engineer)):
        get(u, sid, "contributor")
        s = mgr.update(sid, b)
        audit(u, "stream.updated", s["name"], None, request)
        return s

    @app.delete("/api/streams/{sid}")
    def delete(sid: int, request: Request, u: dict = Depends(engineer)):
        s = get(u, sid, "contributor")
        mgr.delete(sid)
        audit(u, "stream.deleted", s["name"], None, request)
        return {"ok": True}

    @app.post("/api/streams/{sid}/start")
    def start(sid: int, request: Request, u: dict = Depends(engineer)):
        s = get(u, sid, "contributor")
        audit(u, "stream.started", s["name"], None, request)
        return mgr.start(sid)

    @app.post("/api/streams/{sid}/stop")
    def stop(sid: int, request: Request, u: dict = Depends(engineer)):
        s = get(u, sid, "contributor")
        audit(u, "stream.stopped", s["name"], None, request)
        return mgr.stop(sid)

    @app.post("/api/streams/{sid}/reset")
    def reset(sid: int, request: Request, b: dict = Body(default={}), u: dict = Depends(engineer)):
        s = get(u, sid, "contributor")
        to = (b or {}).get("to", "earliest")
        if to not in ("earliest", "latest"):
            raise HTTPException(400, "Reset to earliest or latest.")
        audit(u, "stream.reset", s["name"], {"to": to}, request)
        return mgr.reset(sid, to)

    @app.post("/api/streams/{sid}/preview")
    def preview(sid: int, u: dict = Depends(engineer)):
        get(u, sid)
        try:
            return mgr.preview(sid)
        except ConnectorError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/streams/{sid}/token")
    def rotate(sid: int, request: Request, u: dict = Depends(engineer)):
        s = get(u, sid, "contributor")
        token = mgr.rotate_token(sid)
        audit(u, "stream.token_rotated", s["name"], None, request)
        return {"token": token, "endpoint": f"/api/streams/{sid}/events"}

    # Push endpoint: authenticated by the stream's own token, not a user session.
    @app.post("/api/streams/{sid}/events")
    async def events(sid: int, request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-tessera-stream-token", "")
        try:
            s = mgr.check_token(sid, token)
        except PermissionError:
            raise HTTPException(401, "Unknown stream or token.") from None
        if int(request.headers.get("content-length") or 0) > MAX_PUSH_BYTES:
            raise HTTPException(413, "Send at most 5 MB per request.")
        raw = await request.body()
        if len(raw) > MAX_PUSH_BYTES:
            raise HTTPException(413, "Send at most 5 MB per request.")
        ctype = request.headers.get("content-type", "")
        try:
            if "ndjson" in ctype or "jsonl" in ctype:
                payload = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            else:
                payload = json.loads(raw or b"null")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Send JSON (one object or an array) or NDJSON.") from None
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            payload = payload["events"]
        if payload in (None, [], {}):
            raise HTTPException(400, "No events in the request.")
        try:
            return mgr.push(s, payload)
        except StreamError as e:
            raise HTTPException(400, str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Events do not fit the table: {str(e).splitlines()[0][:300]}") from None

    return mgr
