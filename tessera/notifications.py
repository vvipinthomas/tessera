"""In-app notifications (the bell). Kept in their own module so the platform, pipelines and reports can all notify
people without importing each other."""
from __future__ import annotations

from typing import Any

from .store import now


def notify(p: Any, user_id: int, title: str, body: str = "", link: str | None = None, level: str = "info") -> None:
    """p is anything with a .store (the Platform)."""
    p.store.insert("notifications", {"user_id": user_id, "ts": now(), "level": level, "title": title[:200], "body": body[:2000], "link": link})


def notify_admins(p: Any, title: str, body: str = "", link: str | None = None, level: str = "info") -> None:
    for u in p.store.all("SELECT id FROM users WHERE role='admin' AND active=1"):
        notify(p, u["id"], title, body, link, level)
