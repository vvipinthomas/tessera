"""Metadata store (SQLite). Holds users, connections, catalog, rules, pipelines, dashboards and audit."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
  role TEXT NOT NULL, pw_hash TEXT, sso_subject TEXT, active INTEGER NOT NULL DEFAULT 1,
  failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created_at TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT, user_agent TEXT);
CREATE TABLE IF NOT EXISTS api_tokens(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL,
  created_at TEXT NOT NULL, last_used TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS oidc_states(state TEXT PRIMARY KEY, nonce TEXT, verifier TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS connections(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, type TEXT NOT NULL, config TEXT NOT NULL,
  secrets TEXT, created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_test_ok INTEGER, last_test_msg TEXT, last_test_at TEXT);
CREATE TABLE IF NOT EXISTS datasets(
  id INTEGER PRIMARY KEY, schema_name TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
  owner TEXT DEFAULT '', tags TEXT DEFAULT '[]', columns TEXT DEFAULT '[]', row_count INTEGER,
  connection_id INTEGER, source TEXT, last_loaded_at TEXT, freshness_sla_hours REAL,
  pii_scanned_at TEXT, published_at TEXT, meta TEXT DEFAULT '{}',
  UNIQUE(schema_name, name));
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY, dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
  column_name TEXT, kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', severity TEXT NOT NULL DEFAULT 'error',
  enabled INTEGER NOT NULL DEFAULT 1, description TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_results(
  id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  run_at TEXT NOT NULL, passed INTEGER NOT NULL, failing INTEGER NOT NULL, total INTEGER NOT NULL,
  sample TEXT, error TEXT, pipeline_run_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_rr_rule ON rule_results(rule_id, id);
CREATE TABLE IF NOT EXISTS pipelines(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', steps TEXT NOT NULL,
  schedule TEXT, enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT, webhook_url TEXT,
  created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pipeline_runs(
  id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  status TEXT NOT NULL, trigger TEXT NOT NULL, triggered_by TEXT, created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, steps TEXT DEFAULT '{}', log TEXT DEFAULT '[]', error TEXT);
CREATE INDEX IF NOT EXISTS ix_runs_p ON pipeline_runs(pipeline_id, id);
CREATE TABLE IF NOT EXISTS lineage(
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, pipeline_id INTEGER,
  step TEXT, updated_at TEXT NOT NULL, UNIQUE(src, dst, kind));
CREATE TABLE IF NOT EXISTS queries(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, sql TEXT NOT NULL, viz TEXT NOT NULL DEFAULT '{}',
  created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dashboards(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', tiles TEXT NOT NULL DEFAULT '[]',
  refresh_seconds INTEGER DEFAULT 0, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_providers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, base_url TEXT, model TEXT NOT NULL,
  secrets TEXT, is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_sessions(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, agent TEXT NOT NULL,
  provider_id INTEGER, title TEXT, messages TEXT NOT NULL DEFAULT '[]', tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, user_email TEXT, action TEXT NOT NULL,
  target TEXT, detail TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(id DESC);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_models(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', base TEXT NOT NULL,
  joins TEXT NOT NULL DEFAULT '[]', dimensions TEXT NOT NULL DEFAULT '[]', measures TEXT NOT NULL DEFAULT '[]',
  endorsement TEXT DEFAULT 'none', created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rls_policies(
  id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, column_name TEXT NOT NULL, attribute TEXT NOT NULL,
  description TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, condition TEXT NOT NULL, channels TEXT NOT NULL,
  schedule TEXT NOT NULL DEFAULT '*/15 * * * *', owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'unknown', last_value TEXT, last_checked TEXT,
  last_error TEXT, next_run_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, dashboard_id INTEGER REFERENCES dashboards(id) ON DELETE CASCADE,
  recipients TEXT NOT NULL, schedule TEXT NOT NULL, attach TEXT NOT NULL DEFAULT 'xlsx', filters TEXT NOT NULL DEFAULT '[]',
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT, last_sent_at TEXT, last_status TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref_id INTEGER, ts TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, body TEXT, link TEXT, read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(user_id, read, id);
CREATE TABLE IF NOT EXISTS schema_changes(
  id INTEGER PRIMARY KEY, dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE, ts TEXT NOT NULL,
  change TEXT NOT NULL, column_name TEXT NOT NULL, old_type TEXT, new_type TEXT, pipeline_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_schema_changes ON schema_changes(dataset_id, id);
CREATE TABLE IF NOT EXISTS mirrors(
  id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE, tables TEXT NOT NULL,
  keys TEXT DEFAULT '{}', prefix TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, error TEXT, changes INTEGER NOT NULL DEFAULT 0,
  lag_bytes INTEGER, last_sync_at TEXT, created_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebooks(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, cells TEXT NOT NULL DEFAULT '[]', owner_id INTEGER, created_by TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS glossary(
  id INTEGER PRIMARY KEY, term TEXT UNIQUE NOT NULL COLLATE NOCASE, definition TEXT NOT NULL, owner TEXT,
  synonyms TEXT NOT NULL DEFAULT '[]', columns TEXT NOT NULL DEFAULT '[]', domain TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, description TEXT DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'org', created_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_members(
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual', PRIMARY KEY(workspace_id, user_id));
CREATE TABLE IF NOT EXISTS dashboard_views(
  id INTEGER PRIMARY KEY, dashboard_id INTEGER NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, state TEXT NOT NULL,
  shared INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scim_groups(
  id TEXT PRIMARY KEY, display_name TEXT UNIQUE NOT NULL COLLATE NOCASE, external_id TEXT,
  members TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kpi_targets(
  id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL REFERENCES semantic_models(id) ON DELETE CASCADE, measure TEXT NOT NULL,
  period TEXT NOT NULL, value REAL NOT NULL, note TEXT DEFAULT '', created_by TEXT, updated_at TEXT NOT NULL, UNIQUE(model_id, measure, period));
INSERT OR IGNORE INTO workspaces(id, name, description, visibility, created_by, created_at)
  VALUES (1, 'General', 'Shared with everyone in the organization', 'org', 'system', strftime('%Y-%m-%dT%H:%M:%S+00:00','now'));
"""

MIGRATIONS = [
    ("subscriptions", "include_brief", "INTEGER NOT NULL DEFAULT 0"),
    ("dashboards", "workspace_id", "INTEGER NOT NULL DEFAULT 1"),
    ("queries", "workspace_id", "INTEGER NOT NULL DEFAULT 1"),
    ("notebooks", "workspace_id", "INTEGER NOT NULL DEFAULT 1"),
    ("semantic_models", "workspace_id", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "external_id", "TEXT"),
    ("users", "scim_id", "TEXT"),
    ("queries", "kind", "TEXT NOT NULL DEFAULT 'sql'"),
    ("queries", "spec", "TEXT"),
    ("queries", "model_id", "INTEGER"),
    ("dashboards", "filters", "TEXT NOT NULL DEFAULT '[]'"),
    ("datasets", "endorsement", "TEXT NOT NULL DEFAULT 'none'"),
    ("datasets", "domain", "TEXT"),
    ("users", "attributes", "TEXT NOT NULL DEFAULT '{}'"),
]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

JSON_COLS = {"config", "tags", "columns", "meta", "params", "sample", "steps", "log", "viz", "tiles", "messages", "detail", "value", "source",
             "joins", "dimensions", "measures", "condition", "channels", "recipients", "filters", "cells", "synonyms", "spec", "attributes", "state", "members", "tables", "keys"}


def local_today():
    """Today's date in TESSERA_TIMEZONE, the same zone schedules use (not the server clock's zone)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(os.environ.get("TESSERA_TIMEZONE") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return datetime.now(zone).date()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: Path):
        self.path = str(path)
        self._local = threading.local()
        self.lock = threading.RLock()
        self._opened: list[sqlite3.Connection] = []  # every per-thread connection, so close() can release them all
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
            with self.lock:
                self._opened.append(c)
        return c

    def close(self) -> None:
        """Close every connection this store opened, from any thread (each holds the database, WAL and shm files)."""
        with self.lock:
            opened, self._opened = self._opened, []
            self._local = threading.local()
        for c in opened:
            try:
                c.close()
            except sqlite3.Error:
                pass  # already closed, or busy in a thread that is shutting down

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in list(d):
            if k in JSON_COLS and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass  # not JSON after all: return the stored text unchanged
        return d

    @staticmethod
    def _p(params: Any) -> Any:
        if isinstance(params, (list, tuple)):
            return [json.dumps(v) if isinstance(v, (dict, list)) else v for v in params]
        if isinstance(params, dict):
            return {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
        return params

    def all(self, sql: str, params: Any = ()) -> list[dict]:
        return [self._row(r) for r in self.conn().execute(sql, self._p(params)).fetchall()]

    def one(self, sql: str, params: Any = ()) -> dict | None:
        return self._row(self.conn().execute(sql, self._p(params)).fetchone())

    def scalar(self, sql: str, params: Any = ()):
        r = self.conn().execute(sql, self._p(params)).fetchone()
        return r[0] if r else None

    def exec(self, sql: str, params: Any = ()) -> int:
        with self.lock:
            c = self.conn()
            cur = c.execute(sql, self._p(params))
            c.commit()
            return cur.lastrowid

    @staticmethod
    def _idents(*names: str) -> None:
        # identifiers are interpolated into SQL; values are always bound parameters
        for n in names:
            if not _IDENT.fullmatch(n):
                raise ValueError(f"Bad identifier: {n!r}")

    def insert(self, table: str, row: dict) -> int:
        self._idents(table, *row)
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        return self.exec(f"INSERT INTO {table}({cols}) VALUES({qs})", list(row.values()))

    def update(self, table: str, id_: int, row: dict, key: str = "id") -> None:
        if not row:
            return
        self._idents(table, key, *row)
        sets = ",".join(f"{k}=?" for k in row)
        self.exec(f"UPDATE {table} SET {sets} WHERE {key}=?", [*row.values(), id_])

    # settings helpers
    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key: str, value) -> None:
        self.exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))

    def audit(self, user: dict | None, action: str, target: str = "", detail: dict | None = None, ip: str | None = None):
        self.insert("audit", {"ts": now(), "user_id": user["id"] if user else None,
                              "user_email": user["email"] if user else None, "action": action,
                              "target": target, "detail": json.dumps(detail or {}), "ip": ip})
