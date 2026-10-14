"""Workspaces: group dashboards, reports, notebooks and semantic models by team, with per-workspace roles.

Access rules (global role still applies to data: row-level security and the serving-only rule are unchanged)
- Global admins manage every workspace.
- Members get their workspace role: viewer (read), contributor (create and edit), admin (also manage members).
- Organization-wide workspaces (visibility 'org') are readable by everyone; global analysts and above may also
  contribute there, which keeps the single shared workspace of earlier versions working as before.
- Private workspaces are invisible to non-members, including in lists, search and agent tools.
- Editing always also needs the global analyst role (engineer for semantic models).
"""
from __future__ import annotations

from fastapi import HTTPException

from .security import has_role

WS_ROLES = ("viewer", "contributor", "admin")
WS_RANK = {r: i for i, r in enumerate(WS_ROLES)}
ITEM_TABLES = {"dashboard": "dashboards", "query": "queries", "notebook": "notebooks", "model": "semantic_models"}


def role_in(store, user: dict, ws_id: int) -> str | None:
    if user["role"] == "admin":
        return "admin"
    ws = store.one("SELECT * FROM workspaces WHERE id=?", (ws_id,))
    if not ws:
        return None
    m = store.one("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", (ws_id, user["id"]))
    member = m["role"] if m else None
    if ws["visibility"] == "org":
        base = "contributor" if has_role(user["role"], "analyst") else "viewer"
        if member is None or WS_RANK[base] > WS_RANK[member]:
            return base
    return member


def visible_ids(store, user: dict) -> set[int]:
    if user["role"] == "admin":
        return {r["id"] for r in store.all("SELECT id FROM workspaces")}
    rows = store.all("SELECT id FROM workspaces WHERE visibility='org' UNION SELECT workspace_id FROM workspace_members WHERE user_id=?", (user["id"],))
    return {r["id"] for r in rows}


def require(store, user: dict, ws_id: int, need: str = "viewer") -> str:
    r = role_in(store, user, int(ws_id or 1))
    if r is None:
        raise HTTPException(404, "Not found")
    if WS_RANK[r] < WS_RANK[need]:
        raise HTTPException(403, f"You need the workspace {need} role for this.")
    if need != "viewer" and not has_role(user["role"], "analyst"):
        raise HTTPException(403, "Editing needs the analyst role or higher.")
    return r


def item(store, user: dict, kind: str, item_id: int, need: str = "viewer") -> dict:
    row = store.one(f"SELECT * FROM {ITEM_TABLES[kind]} WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "Not found")
    require(store, user, row.get("workspace_id") or 1, need)
    return row


def filter_rows(store, user: dict, rows: list[dict], ws: int | None = None) -> list[dict]:
    ids = visible_ids(store, user)
    out = [r for r in rows if (r.get("workspace_id") or 1) in ids]
    if ws:
        out = [r for r in out if (r.get("workspace_id") or 1) == int(ws)]
    return out


def target_ws(store, user: dict, ws_id) -> int:
    """Workspace for a new item; defaults to General. Caller must be able to contribute there."""
    wid = int(ws_id or 1)
    require(store, user, wid, "contributor")
    return wid
