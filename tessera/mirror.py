"""Mirroring: keep tables in Tessera continuously in step with a PostgreSQL database by reading its change log
(logical decoding), not by re-copying. Inserts, updates, deletes and truncates arrive within seconds.

How it works
- Start: create a logical replication slot (built-in ``test_decoding`` plugin, so nothing is installed on the
  database), then copy each table once. Changes made during the copy are kept by the slot and replayed afterwards;
  replay is idempotent because rows are merged by primary key and the last change for a key wins.
- Then, every few seconds: peek the next batch of changes, apply them per table as one merge (upserts and deletes),
  and only after the write succeeded advance the slot. A crash in between replays the batch; it never loses one.
- Stop: drop the slot, so the database stops keeping log for Tessera.

Requirements on the database: ``wal_level = logical`` (on managed services: Amazon RDS/Aurora
``rds.logical_replication = 1``, Azure ``azure.replication_support = logical``, Google Cloud SQL
``cloudsql.logical_decoding = on``; Supabase and Neon have it on or as a switch), a free replication slot, and a user
with the REPLICATION attribute (or rds_replication on RDS). Every mirrored table needs a primary key.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import pyarrow as pa

from . import connectors
from .errors import UserError, public_message
from .store import now
from .warehouse import arrow_table, qi, safe_name

log = logging.getLogger("tessera.mirror")

PG_TO_DUCK = {
    "smallint": "SMALLINT", "integer": "INTEGER", "bigint": "BIGINT", "real": "FLOAT", "double precision": "DOUBLE",
    "numeric": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE", "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMPTZ", "time without time zone": "TIME", "bytea": "BLOB",
}
DELETED = "_tessera_deleted"
TOAST = object()  # an unchanged large value the change log leaves out


class MirrorError(UserError):
    pass


# ------------------------------------------------------------------ parsing the change log
def _name(s: str) -> str:
    return s[1:-1].replace('""', '"') if s.startswith('"') and s.endswith('"') else s


def split_table(s: str) -> str:
    """'public."Order Lines"' -> 'public.Order Lines'."""
    parts, cur, q = [], "", False
    for ch in s:
        if ch == '"':
            q = not q
            cur += ch
        elif ch == "." and not q:
            parts.append(_name(cur))
            cur = ""
        else:
            cur += ch
    parts.append(_name(cur))
    return ".".join(parts)


def parse_tuple(s: str) -> dict:
    """Parse ``id[integer]:1 name[text]:'O''Brien' note[text]:null`` into {column: (pg type, value)}."""
    out, i, n = {}, 0, len(s)
    while i < n:
        while i < n and s[i] == " ":
            i += 1
        if i >= n:
            break
        if s[i] == '"':
            j = i + 1
            while True:
                j = s.index('"', j)
                if j + 1 < n and s[j + 1] == '"':
                    j += 2
                    continue
                break
            col, i = _name(s[i:j + 1]), j + 1
        else:
            j = s.index("[", i)
            col, i = s[i:j], j
        end = s.index("]:", i)
        typ, i = s[i + 1:end], end + 2
        if i < n and s[i] == "'":
            j, buf = i + 1, []
            while True:
                k = s.index("'", j)
                buf.append(s[j:k])
                if k + 1 < n and s[k + 1] == "'":
                    buf.append("'")
                    j = k + 2
                    continue
                i = k + 1
                break
            val = "".join(buf)
        else:
            j = s.find(" ", i)
            j = n if j < 0 else j
            raw, i = s[i:j], j
            val = None if raw == "null" else TOAST if raw == "unchanged-toast-datum" else raw
        out[col] = (typ, val)
    return out


LINE = re.compile(r"^table (.+?): (INSERT|UPDATE|DELETE|TRUNCATE): ?(.*)$", re.S)


def parse_change(data: str) -> dict | None:
    """One test_decoding line -> {table, op, row, old_key}; None for BEGIN/COMMIT."""
    m = LINE.match(data)
    if not m:
        return None
    table, op, rest = split_table(m.group(1)), m.group(2), m.group(3)
    if op == "TRUNCATE":
        return {"table": table, "op": "truncate"}
    if rest.startswith("(no-tuple-data)"):
        raise MirrorError(f"{table} has no primary key or replica identity, so deletes cannot be mirrored.")
    old = None
    if rest.startswith("old-key: "):
        k = rest.index(" new-tuple: ")
        old, rest = parse_tuple(rest[len("old-key: "):k]), rest[k + len(" new-tuple: "):]
    return {"table": table, "op": op.lower(), "row": parse_tuple(rest), "old_key": old}


# ------------------------------------------------------------------ one mirror
def slot_name(mid: int) -> str:
    return f"tessera_mirror_{mid}"


def mirror_target(prefix: str, table: str) -> str:
    """The raw table a mirrored source table lands in (public.orders -> orders, sales.orders -> sales_orders)."""
    sc, _, t = table.rpartition(".")
    return prefix + safe_name(t if sc in ("", "public") else f"{sc}_{t}")


class Mirror:
    def __init__(self, plat, row: dict):
        self.p, self.row = plat, row
        self.id = row["id"]
        self.tables = row["tables"]
        self.keys: dict[str, list[str]] = {}

    def engine(self):
        cx = self.p.connector(self.row["connection_id"])
        if not isinstance(cx, connectors.SQLConnector) or cx.type != "postgres":
            raise MirrorError("Mirroring works with PostgreSQL connections.")
        return cx, cx.engine().execution_options(isolation_level="AUTOCOMMIT")

    def target(self, table: str) -> str:
        return mirror_target(self.row["prefix"], table)

    def check(self, cx, conn) -> None:
        from sqlalchemy import text
        lvl = conn.execute(text("show wal_level")).scalar()
        if lvl != "logical":
            raise MirrorError(f"The database has wal_level = {lvl}. Set it to logical (on managed services turn on "
                              "logical replication) and restart the database.")
        info = cx.describe(self.tables)
        for t in self.tables:
            if t not in info:
                raise MirrorError(f"Table {t} was not found.")
            if not info[t]["primary_key"]:
                raise MirrorError(f"Table {t} has no primary key. Mirroring needs one to apply updates and deletes.")
            self.keys[t] = info[t]["primary_key"]

    def start(self) -> None:
        """Create the slot, then take the first copy of every table."""
        from sqlalchemy import text
        cx, eng = self.engine()
        try:
            with eng.connect() as c:
                self.check(cx, c)
                exists = c.execute(text("select 1 from pg_replication_slots where slot_name=:s"), {"s": slot_name(self.id)}).scalar()
                if not exists:
                    c.execute(text("select pg_create_logical_replication_slot(:s, 'test_decoding')"), {"s": slot_name(self.id)})
        except MirrorError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MirrorError(_pg_error(e)) from None
        finally:
            eng.dispose()
        self.p.store.update("mirrors", self.id, {"keys": json.dumps(self.keys), "status": "copying", "error": None})
        for t in self.tables:
            self.p.ingest(self.row["connection_id"], {"table": t}, f"raw.{self.target(t)}", "replace")
        self.p.store.update("mirrors", self.id, {"status": "running", "last_sync_at": now()})

    def sync(self, batch: int = 5000) -> dict:
        """Apply the next batch of changes. Returns counts per table."""
        from sqlalchemy import text
        self.keys = self.row.get("keys") or self.keys
        cx, eng = self.engine()
        applied: dict[str, int] = {}
        try:
            with eng.connect() as c:
                rows = c.execute(text("select lsn::text, data from pg_logical_slot_peek_changes(:s, NULL, :n, 'include-xids', '0')"),
                                 {"s": slot_name(self.id), "n": batch}).fetchall()
                if not rows:
                    self._lag(c)
                    return applied
                by_table: dict[str, list[dict]] = {}
                for _, data in rows:
                    ch = parse_change(data)
                    if ch and ch["table"] in self.tables:
                        by_table.setdefault(ch["table"], []).append(ch)
                for t, changes in by_table.items():
                    applied[t] = self.apply(cx, t, changes)
                # only now that every change is stored, tell the database it can forget them
                c.execute(text("select pg_replication_slot_advance(:s, CAST(:l AS pg_lsn))"), {"s": slot_name(self.id), "l": rows[-1][0]})
                self._lag(c)
        except MirrorError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MirrorError(_pg_error(e)) from None
        finally:
            eng.dispose()
        total = sum(applied.values())
        upd = {"last_sync_at": now(), "error": None, "status": "running"}
        if total:
            upd["changes"] = (self.row.get("changes") or 0) + total
            self.row["changes"] = upd["changes"]
        self.p.store.update("mirrors", self.id, upd)
        return applied

    def _lag(self, c) -> None:
        from sqlalchemy import text
        lag = c.execute(text("select pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint from pg_replication_slots "
                             "where slot_name=:s"), {"s": slot_name(self.id)}).scalar()
        self.p.store.update("mirrors", self.id, {"lag_bytes": int(lag or 0)})

    def apply(self, cx, table: str, changes: list[dict]) -> int:
        """Merge one table's changes: upserts and deletes by key, in order, last change per key wins."""
        keys, total = self.keys[table], len(changes)
        schema, name = "raw", self.target(table)
        wh = self.p.wh
        if any(ch["op"] == "truncate" for ch in changes):
            last = max(i for i, ch in enumerate(changes) if ch["op"] == "truncate")
            if wh.exists(schema, name):
                with wh.writing():
                    cur = wh.cursor()
                    try:
                        cur.execute(f"DELETE FROM {qi(schema)}.{qi(name)}")
                    finally:
                        cur.close()
            changes = changes[last + 1:]
        records, types, refetch = [], {}, []
        for ch in changes:
            if ch["old_key"]:  # the key itself changed: remove the row under its old key
                records.append({**{k: v[1] for k, v in ch["old_key"].items()}, DELETED: True})
                types.update({k: v[0] for k, v in ch["old_key"].items()})
            row = {k: v[1] for k, v in ch["row"].items()}
            types.update({k: v[0] for k, v in ch["row"].items()})
            if any(v is TOAST for v in row.values()):
                refetch.append(tuple(row[k] for k in keys))
            records.append({**row, DELETED: ch["op"] == "delete"})
        if refetch:
            records = self._fill_toast(cx, table, keys, records)
        if not records:
            return total
        stored = {c["name"]: c["type"] for c in wh.columns(schema, name)} if wh.exists(schema, name) else {}
        cols = list(dict.fromkeys(k for r in records for k in r if k != DELETED))
        strings = pa.table({**{c: pa.array([_s(r.get(c, TOAST)) for r in records], pa.string()) for c in cols},
                            DELETED: pa.array([r[DELETED] for r in records], pa.bool_())})
        # deletes carry only the key; their other columns stay empty and are never stored
        typed = self._cast(strings, {c: stored.get(c) or PG_TO_DUCK.get(types.get(c, ""), "VARCHAR") for c in cols})
        wh.load_arrow(typed, schema, name, "merge", keys, DELETED)
        d = self.p.dataset(schema, name, create=True)
        self.p.store.update("datasets", d["id"], {"last_loaded_at": now()})
        self.p.refresh_columns(self.p.dataset_by_id(d["id"]))
        return total

    def _cast(self, t: pa.Table, types: dict) -> pa.Table:
        cur = self.p.wh.cursor()
        v = "_tessera_mirror_in"
        try:
            cur.register(v, t)
            sel = ", ".join(f"TRY_CAST({qi(c)} AS {ty}) AS {qi(c)}" for c, ty in types.items())
            return arrow_table(cur.sql(f"SELECT {sel}, {qi(DELETED)} FROM {v}"))
        finally:
            cur.unregister(v)
            cur.close()

    def _fill_toast(self, cx, table: str, keys: list[str], records: list[dict]) -> list[dict]:
        """Large unchanged values are not in the change log; read the current row from the source instead."""
        from sqlalchemy import text
        sc, _, t = table.rpartition(".")
        eng = cx.engine()
        prep = eng.dialect.identifier_preparer
        out = []
        try:
            with eng.connect() as c:
                for r in records:
                    if r[DELETED] or not any(v is TOAST for v in r.values()):
                        out.append(r)
                        continue
                    where = " AND ".join(f"{prep.quote(k)}::text = :k{i}" for i, k in enumerate(keys))
                    cur = c.execute(text(f"SELECT * FROM {prep.quote(sc or 'public')}.{prep.quote(t)} WHERE {where}"),
                                    {f"k{i}": r[k] for i, k in enumerate(keys)}).mappings().first()
                    if cur is None:
                        continue  # deleted since; the delete is later in the log
                    out.append({**r, **{k: (None if r[k] is TOAST and cur[k] is None else str(cur[k]) if r[k] is TOAST else r[k])
                                        for k in r if k != DELETED}})
        finally:
            eng.dispose()
        return out

    def drop_slot(self) -> None:
        from sqlalchemy import text
        try:
            _, eng = self.engine()
        except Exception:  # noqa: BLE001 - the connection may be gone; nothing to drop then
            return
        try:
            with eng.connect() as c:
                c.execute(text("select pg_drop_replication_slot(slot_name) from pg_replication_slots where slot_name=:s"),
                          {"s": slot_name(self.id)})
        except Exception as e:  # noqa: BLE001
            raise MirrorError(_pg_error(e)) from None
        finally:
            eng.dispose()


def _s(v):
    if v is TOAST or v is None:
        return None
    return v if isinstance(v, str) else str(v)


def _pg_error(e: Exception) -> str:
    msg = " ".join(str(getattr(e, "orig", None) or e).split())
    if "permission denied" in msg or "must be superuser or replication role" in msg or "must be superuser or have" in msg:
        return "The database user needs the REPLICATION attribute (ALTER ROLE <user> REPLICATION; on Amazon RDS: GRANT rds_replication TO <user>)."
    if "all replication slots are in use" in msg:
        return "The database has no free replication slots. Raise max_replication_slots or drop unused slots."
    return msg[:400]


# ------------------------------------------------------------------ manager
class Mirrors:
    """Runs every active mirror on a background thread, one sync pass every ``interval`` seconds."""

    def __init__(self, plat, interval: float = 5.0):
        self.p, self.interval = plat, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.enabled = False  # background syncing starts with the server's scheduler (not in tests or one-off commands)

    def rows(self) -> list[dict]:
        return self.p.store.all("SELECT * FROM mirrors ORDER BY id")

    def get(self, mid: int) -> dict | None:
        return self.p.store.one("SELECT * FROM mirrors WHERE id=?", (mid,))

    def create(self, connection_id: int, tables: list[str], prefix: str, who: str) -> dict:
        mid = self.p.store.insert("mirrors", {"connection_id": connection_id, "tables": json.dumps(tables), "prefix": prefix,
                                              "status": "starting", "created_by": who, "created_at": now(), "changes": 0})
        m = Mirror(self.p, self.get(mid))
        try:
            m.start()
        except Exception as e:
            try:
                m.drop_slot()
            except MirrorError:
                pass  # the start already failed; the original error is the one to report
            self.p.store.exec("DELETE FROM mirrors WHERE id=?", (mid,))
            raise MirrorError(public_message(e)) from None
        self.ensure_thread()
        return self.get(mid)

    def sync(self, mid: int) -> dict:
        row = self.get(mid)
        if not row:
            raise MirrorError("Mirror not found.")
        with self._lock:
            try:
                return Mirror(self.p, row).sync()
            except MirrorError as e:
                self.p.store.update("mirrors", mid, {"status": "error", "error": str(e)})
                raise

    def pause(self, mid: int, paused: bool) -> None:
        self.p.store.update("mirrors", mid, {"status": "paused" if paused else "running", "error": None})
        if not paused:
            self.ensure_thread()

    def delete(self, mid: int) -> None:
        row = self.get(mid)
        if not row:
            return
        with self._lock:
            Mirror(self.p, row).drop_slot()
            self.p.store.exec("DELETE FROM mirrors WHERE id=?", (mid,))

    def ensure_thread(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tessera-mirrors", daemon=True)
        self._thread.start()

    def resume_all(self) -> None:
        self.enabled = True
        if any(r["status"] in ("running", "error") for r in self.rows()):
            self.ensure_thread()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for r in self.rows():
                if r["status"] not in ("running", "error") or self._stop.is_set():
                    continue
                try:
                    self.sync(r["id"])
                except Exception:  # noqa: BLE001 - recorded on the mirror; retried on the next pass
                    log.warning("mirror %s sync failed", r["id"], exc_info=True)
            self._stop.wait(self.interval)

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        time.sleep(0)
