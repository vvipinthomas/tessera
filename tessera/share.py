"""Share and embed dashboards outside Tessera with expiring, revocable links.

Off by default: an admin turns sharing on and lists the sites allowed to frame dashboards. A link shows the dashboard
read-only with the access of the person who created it, narrowed by filters locked into the link (for example
province = BC). Every request re-checks that the creator is still active and can still open the dashboard, so
deactivating them or removing their access stops the link. Tokens are stored hashed and shown once.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from . import workspaces as ws
from .security import has_role, token_hash
from .store import now, parse_ts

ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")


def config(store) -> dict:
    c = store.get_setting("sharing") or {}
    return {"enabled": bool(c.get("enabled")), "embed_origins": list(c.get("embed_origins") or [])}


def register(app, plat, user, need, audit, static_dir):
    store = plat.store
    analyst, admin = need("analyst"), need("admin")
    store.exec("""CREATE TABLE IF NOT EXISTS share_links(
      id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, dashboard_id INTEGER NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, filters TEXT NOT NULL DEFAULT '[]', note TEXT DEFAULT '',
      expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, views INTEGER NOT NULL DEFAULT 0, last_used TEXT, created_at TEXT NOT NULL)""")

    @app.get("/api/admin/sharing")
    def get_sharing(u: dict = Depends(admin)):
        return {**config(store), "active_links": store.scalar("SELECT count(*) FROM share_links WHERE revoked=0 AND expires_at > ?", (now(),))}

    @app.put("/api/admin/sharing")
    def put_sharing(request: Request, b: dict = Body(...), u: dict = Depends(admin)):
        origins = [o.strip().rstrip("/") for o in (b.get("embed_origins") or []) if str(o).strip()]
        bad = [o for o in origins if not ORIGIN.match(o)]
        if bad:
            raise HTTPException(400, f"Not a site origin: {bad[0]} (use https://example.com)")
        c = {"enabled": bool(b.get("enabled")), "embed_origins": origins[:20]}
        store.set_setting("sharing", c)
        if not c["enabled"]:
            store.exec("UPDATE share_links SET revoked=1 WHERE revoked=0")
        audit(u, "sharing.updated", "", c, request)
        return c

    @app.post("/api/dashboards/{did}/share")
    def create_link(did: int, request: Request, b: dict = Body(default={}), u: dict = Depends(analyst)):
        if not config(store)["enabled"]:
            raise HTTPException(403, "Sharing outside Tessera is turned off. An admin can enable it under Admin → Operations.")
        if u.get("via") == "token":
            raise HTTPException(403, "Create share links from the app, not with an API token.")
        d = ws.item(store, u, "dashboard", did, "contributor")
        days = int(b.get("expires_days") or 30)
        if not 1 <= days <= 365:
            raise HTTPException(400, "Links expire after 1 to 365 days.")
        flt = b.get("filters") or []
        if not isinstance(flt, list) or not all(isinstance(f, dict) and f.get("field") for f in flt):
            raise HTTPException(400, "Locked filters must be a list of {model_id, field, op, value}.")
        tok = "tsh_" + secrets.token_urlsafe(24)
        sid = store.insert("share_links", {"token_hash": token_hash(tok), "prefix": tok[:10], "dashboard_id": did, "user_id": u["id"],
                                           "filters": __import__("json").dumps(flt), "note": str(b.get("note") or "")[:200],
                                           "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(), "created_at": now()})
        audit(u, "share.created", d["name"], {"link": sid, "days": days, "locked_filters": len(flt)}, request)
        base = plat.settings.base_url or str(request.base_url).rstrip("/")
        url = f"{base}/embed/{tok}"
        return {"id": sid, "url": url, "iframe": f'<iframe src="{url}" width="100%" height="900" style="border:0" loading="lazy" title="{d["name"]}"></iframe>',
                "note": "Copy the link now; it is not shown again. Anyone with it sees this dashboard with your access, narrowed by the locked filters."}

    @app.get("/api/dashboards/{did}/shares")
    def list_links(did: int, u: dict = Depends(analyst)):
        ws.item(store, u, "dashboard", did)
        rows = store.all("SELECT s.id, s.prefix, s.filters, s.note, s.expires_at, s.revoked, s.views, s.last_used, s.created_at, u.email FROM share_links s "
                         "JOIN users u ON u.id=s.user_id WHERE dashboard_id=? ORDER BY s.id DESC", (did,))
        return rows if u["role"] == "admin" or ws.role_in(store, u, ws.item(store, u, "dashboard", did)["workspace_id"]) == "admin" else [r for r in rows if r["email"] == u["email"]]

    @app.delete("/api/shares/{sid}")
    def revoke_link(sid: int, request: Request, u: dict = Depends(analyst)):
        s = store.one("SELECT * FROM share_links WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404, "Link not found")
        d = store.one("SELECT workspace_id FROM dashboards WHERE id=?", (s["dashboard_id"],))
        if s["user_id"] != u["id"] and u["role"] != "admin" and ws.role_in(store, u, (d or {}).get("workspace_id") or 1) != "admin":
            raise HTTPException(403, "Only the creator or an admin can revoke this link.")
        store.exec("UPDATE share_links SET revoked=1 WHERE id=?", (sid,))
        audit(u, "share.revoked", str(sid), None, request)
        return {"ok": True}

    # ------------------------------------------------------------ public, token-only endpoints
    def resolve(token: str) -> tuple[dict, dict, dict]:
        if not config(store)["enabled"] or not re.fullmatch(r"tsh_[A-Za-z0-9_\-]{20,64}", token or ""):
            raise HTTPException(404, "This link is not valid.")
        s = store.one("SELECT * FROM share_links WHERE token_hash=?", (token_hash(token),))
        if not s or s["revoked"] or parse_ts(s["expires_at"]) < datetime.now(timezone.utc):
            raise HTTPException(404, "This link has expired or was revoked.")
        owner = store.one("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],))
        d = store.one("SELECT * FROM dashboards WHERE id=?", (s["dashboard_id"],))
        if not owner or not d or not has_role(owner["role"], "viewer") or ws.role_in(store, owner, d.get("workspace_id") or 1) is None:
            raise HTTPException(404, "This link is no longer available.")
        return s, owner, d

    def tile_allowed(q: dict, locked: list) -> bool:
        """With locked filters, only show tiles the filters provably apply to: model tiles whose model has every locked field.
        SQL tiles cannot be narrowed safely, so they are withheld from filtered links."""
        if not locked:
            return True
        if q.get("kind") != "model":
            return False
        m = plat.model(q["model_id"])
        dims = {x["name"] for x in m["dimensions"]}
        return all(f.get("model_id") in (None, m["id"]) and f.get("field") in dims for f in locked)

    @app.get("/embed/{token}", include_in_schema=False)
    def embed_page(token: str):
        resolve(token)
        return FileResponse(static_dir / "embed.html")

    @app.get("/api/embed/{token}")
    def embed_meta(token: str):
        s, owner, d = resolve(token)
        store.exec("UPDATE share_links SET views=views+1, last_used=? WHERE id=?", (now(), s["id"]))
        tiles = []
        for i, t in enumerate(d.get("tiles") or []):
            if t.get("type") == "text":
                tiles.append({"i": i, "type": "text", "w": t.get("w", 12), "text": t.get("text", "")})
                continue
            q = store.one("SELECT * FROM queries WHERE id=?", (t.get("query_id"),))
            if q and ws.role_in(store, owner, q.get("workspace_id") or 1) is not None and tile_allowed(q, s["filters"] or []):
                tiles.append({"i": i, "type": "query", "w": t.get("w", 6), "h": t.get("h"), "title": t.get("title") or q["name"], "viz": q["viz"]})
        return {"name": d["name"], "description": d.get("description") or "", "tiles": tiles, "locked_filters": s["filters"], "expires_at": s["expires_at"]}

    @app.post("/api/embed/{token}/tiles/{i}")
    def embed_tile(token: str, i: int):
        s, owner, d = resolve(token)
        tiles = d.get("tiles") or []
        if not 0 <= i < len(tiles) or not tiles[i].get("query_id"):
            raise HTTPException(404, "Tile not found")
        q = store.one("SELECT * FROM queries WHERE id=?", (tiles[i]["query_id"],))
        if not q or ws.role_in(store, owner, q.get("workspace_id") or 1) is None or not tile_allowed(q, s["filters"] or []):
            raise HTTPException(404, "Tile not found")
        res = plat.run_saved(owner, q, {"filters": s["filters"] or []})
        return {"result": {k: res.get(k) for k in ("columns", "types", "rows", "fields", "previous", "row_count")}}
