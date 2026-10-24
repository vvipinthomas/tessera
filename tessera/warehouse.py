"""Analytical warehouse on DuckDB.

Security model
- The DuckDB instance is opened with enable_external_access=false, so SQL can never read or write
  files, attach databases, load extensions or reach the network. Data enters only through connectors.
- Every ad-hoc statement is parsed by DuckDB's own parser. Only a single SELECT/EXPLAIN is allowed.
- Restricted roles (analyst, viewer) may only reference tables in the `serving` schema, which holds
  published, PII-masked copies. Table references are resolved by the binder, not by regex.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import decimal
import json
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

SCHEMAS = ("raw", "curated", "serving")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SAFE_TABLE_FUNCS = {"range", "generate_series", "unnest"}
RESTRICTED_FUNCS = re.compile(r"\b(duckdb_[a-z_]+|information_schema|pg_catalog|sqlite_master|pragma_[a-z_]+|current_setting|getenv)\b", re.I)


class QueryError(Exception):
    pass


class AccessDenied(QueryError):
    pass


def arrow_table(result) -> pa.Table:
    """A query result or relation as an Arrow table, on DuckDB 1.x and 2.x (the method was renamed)."""
    for name in ("to_arrow_table", "fetch_arrow_table", "arrow"):
        fn = getattr(result, name, None)
        if fn:
            return fn()
    raise QueryError("This DuckDB version cannot return Arrow tables.")


def qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def check_ident(name: str, what: str = "name") -> str:
    if not IDENT.match(name or ""):
        raise QueryError(f"Invalid {what} '{name}'. Use letters, digits and underscores, starting with a letter.")
    return name


def split_fqn(fqn: str, default_schema: str = "raw") -> tuple[str, str]:
    parts = fqn.split(".")
    if len(parts) == 1:
        return default_schema, check_ident(parts[0], "table name")
    if len(parts) == 2:
        if parts[0] not in SCHEMAS:
            raise QueryError(f"Schema must be one of {', '.join(SCHEMAS)}")
        return parts[0], check_ident(parts[1], "table name")
    raise QueryError(f"Invalid table reference '{fqn}'")


def sql_literal(v: Any) -> str:
    """Render a Python value as a DuckDB literal (used only where parameters cannot be bound, e.g. view bodies)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise QueryError("Invalid number")
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return str(v)


CACHE_TTL = 600.0
CACHE_SIZE = 256
READONLY_INTERNAL = re.compile(r"\s*(select|with|describe|pragma|show|summarize|explain)\b", re.I)


class Warehouse:
    def __init__(self, path: Path):
        self.path = str(path)
        self.db = duckdb.connect(self.path, config={"enable_external_access": False})
        self.catalog = self.db.execute("SELECT current_database()").fetchone()[0]
        self.write_lock = threading.RLock()
        # result cache for interactive queries; any write bumps the version, so cached results are never stale
        self.version = 0
        self._cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_hits = self.cache_misses = 0
        for s in SCHEMAS:
            self.db.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")

    def cursor(self, restricted: bool = False):
        c = self.db.cursor()
        if restricted:
            c.execute("SET search_path='serving'")
        return c

    # ---------- validation ----------
    def validate_select(self, sql: str, restricted: bool, cur=None) -> None:
        sql = (sql or "").strip().rstrip(";").strip()
        if not sql:
            raise QueryError("Query is empty.")
        cur = cur or self.cursor(restricted)
        try:
            stmts = cur.extract_statements(sql)
        except duckdb.Error as e:
            raise QueryError(str(e).split("\n")[0]) from None
        if len(stmts) != 1:
            raise QueryError("Run one statement at a time.")
        st = str(stmts[0].type).split(".")[-1]
        if st not in {"SELECT", "EXPLAIN"}:
            raise AccessDenied(f"Only read-only SELECT queries are allowed here (got {st}).")
        if re.match(r"^\s*pragma\b", sql, re.I) or re.search(r"\bpragma\b", sql, re.I):
            raise AccessDenied("PRAGMA statements are not allowed.")
        if restricted:
            if RESTRICTED_FUNCS.search(sql):
                raise AccessDenied("System catalog functions are not available to your role.")
            tables, ctes, funcs = self.table_refs(sql, cur)
            bad_funcs = [f for f in funcs if f not in SAFE_TABLE_FUNCS]
            if bad_funcs:
                raise AccessDenied(f"Table function {bad_funcs[0]}() is not available to your role.")
            serving = {r[0] for r in cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='serving'").fetchall()}
            for cat, sch, tbl in tables:
                if not cat and not sch and tbl in ctes:
                    continue
                if sch:
                    ok = sch == "serving" and cat in ("", self.catalog)
                else:
                    ok = not cat and tbl in serving
                name = ".".join(x for x in (cat, sch, tbl) if x)
                if not ok:
                    raise AccessDenied(f"Your role can only query published datasets (serving schema). '{name}' is not published.")

    def table_refs(self, sql: str, cur=None) -> tuple[list[tuple[str, str, str]], set[str], list[str]]:
        """Parse (without binding) and return base-table references, CTE names and table functions."""
        cur = cur or self.cursor()
        raw = cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        tree = json.loads(raw)
        if tree.get("error"):
            raise QueryError(tree.get("error_message") or "Could not parse the query.")
        tables, ctes, funcs = [], set(), []

        def walk(n):
            if isinstance(n, dict):
                t = n.get("type")
                if t == "BASE_TABLE":
                    tables.append((n.get("catalog_name") or "", n.get("schema_name") or "", n.get("table_name") or ""))
                elif t == "TABLE_FUNCTION":
                    fn = (n.get("function") or {}).get("function_name")
                    if fn:
                        funcs.append(fn.lower())
                if isinstance(n.get("cte_map"), dict):
                    for m in n["cte_map"].get("map", []):
                        ctes.add(m.get("key"))
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(tree)
        return tables, ctes, funcs

    # ---------- row-level security ----------
    def apply_rls(self, sql: str, rls: dict[str, str], cur) -> str:
        """Rewrite references to protected serving tables so they read per-query filtered temp views.

        rls maps serving table name -> SQL predicate built by Tessera (identifiers quoted, values escaped).
        References are rewritten in DuckDB's parse tree, so qualified, unqualified and nested uses are all covered.
        """
        raw = cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        tree = json.loads(raw)
        if tree.get("error"):
            raise AccessDenied("Row-level security applies to these datasets, so only SELECT queries can run on them.")
        ctes: set[str] = set()
        used: set[str] = set()

        def collect(n):
            if isinstance(n, dict):
                if isinstance(n.get("cte_map"), dict):
                    for m in n["cte_map"].get("map", []):
                        ctes.add(m.get("key"))
                for v in n.values():
                    collect(v)
            elif isinstance(n, list):
                for v in n:
                    collect(v)

        def rewrite(n):
            if isinstance(n, dict):
                if n.get("type") == "BASE_TABLE":
                    cat, sch, tbl = n.get("catalog_name") or "", n.get("schema_name") or "", n.get("table_name") or ""
                    target = None
                    if sch == "serving" and cat in ("", self.catalog) and tbl in rls:
                        target = tbl
                    elif not cat and not sch and tbl in rls and tbl not in ctes:
                        target = tbl
                    if target:
                        n["catalog_name"], n["schema_name"], n["table_name"] = "", "", f"__rls_{target}"
                        if "qualified_name" in n:  # DuckDB 2.0 writes the name from this path, not the fields above
                            n["qualified_name"] = {**(n["qualified_name"] or {}), "path": [f"__rls_{target}"]}
                        used.add(target)
                for v in n.values():
                    rewrite(v)
            elif isinstance(n, list):
                for v in n:
                    rewrite(v)
        collect(tree)
        rewrite(tree)
        if not used:
            return sql
        for t in used:
            cur.execute(f"CREATE OR REPLACE TEMP VIEW {qi('__rls_' + t)} AS SELECT * FROM {qi(self.catalog)}.serving.{qi(t)} WHERE {rls[t]}")
        out = cur.execute("SELECT json_deserialize_sql(?)", [json.dumps(tree)]).fetchone()[0]
        # Fail closed: parse the rewritten query again and refuse it if any protected table is still read directly.
        # (DuckDB 2.0 changed how table names are written back; a silent change like that must never drop the filter.)
        tables, out_ctes, _ = self.table_refs(out, cur)
        for cat, sch, tbl in tables:
            direct = (sch == "serving" and cat in ("", self.catalog)) or (not cat and not sch and tbl not in out_ctes)
            if direct and tbl in rls:
                raise AccessDenied("Row-level security could not be applied to this query, so it was not run.")
        return out

    # ---------- queries ----------
    @contextlib.contextmanager
    def writing(self):
        """Hold the write lock and invalidate cached results before and after (a query racing the write can't cache stale rows)."""
        with self.write_lock:
            self.bump()
            try:
                yield
            finally:
                self.bump()

    def bump(self) -> None:
        with self._cache_lock:
            self.version += 1
            self._cache.clear()

    def query(self, sql: str, restricted: bool, limit: int = 10_000, timeout: float = 60.0, params=None, rls: dict | None = None) -> dict:
        cur = self.cursor(restricted)
        try:
            self.validate_select(sql, restricted, cur)
        except Exception:
            cur.close()
            raise
        key = json.dumps([sql, list(params or []), restricted, sorted((rls or {}).items()), limit, self.version], default=str)
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                cur.close()
                return {**hit[1], "cached": True, "elapsed_ms": 0.0}
        self.cache_misses += 1
        out = self._run_query(cur, sql, restricted, limit, timeout, params, rls)
        if out["row_count"] <= 20_000:
            with self._cache_lock:
                if key.endswith(f", {self.version}]"):
                    self._cache[key] = (time.monotonic(), out)
                    while len(self._cache) > CACHE_SIZE:
                        self._cache.popitem(last=False)
        return dict(out)

    def _run_query(self, cur, sql, restricted, limit, timeout, params, rls) -> dict:
        sql = sql.strip().rstrip(";")
        if restricted and rls:
            try:
                sql = self.apply_rls(sql, rls, cur)
            except QueryError:
                cur.close()
                raise
        timer = threading.Timer(timeout, cur.interrupt)
        t0 = time.perf_counter()
        timer.start()
        try:
            rel = cur.execute(sql, params or [])
            cols = [d[0] for d in rel.description] if rel.description else []
            types = [str(d[1]) for d in rel.description] if rel.description else []
            rows = rel.fetchmany(limit + 1)
        except duckdb.InterruptException:
            raise QueryError(f"Query cancelled after {timeout:.0f}s timeout.") from None
        except duckdb.Error as e:
            raise QueryError(str(e).split("\n")[0]) from None
        finally:
            timer.cancel()
            cur.close()
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {"columns": cols, "types": types, "rows": [[jsonable(v) for v in r] for r in rows],
                "row_count": len(rows), "truncated": truncated,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}

    def internal(self, sql: str, params=None) -> list[tuple]:
        """Trusted internal statement (built by Tessera with quoted identifiers and bound params)."""
        write = not READONLY_INTERNAL.match(sql)
        if write:
            self.bump()
        cur = self.cursor()
        try:
            return cur.execute(sql, params or []).fetchall()
        finally:
            cur.close()
            if write:
                self.bump()

    def internal_desc(self, sql: str, params=None) -> tuple[list[str], list[tuple]]:
        cur = self.cursor()
        try:
            rel = cur.execute(sql, params or [])
            return [d[0] for d in rel.description], rel.fetchall()
        finally:
            cur.close()

    # ---------- tables ----------
    def exists(self, schema: str, name: str) -> bool:
        return bool(self.internal("SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?", [schema, name]))

    def list_tables(self) -> list[dict]:
        rows = self.internal("SELECT schema_name, table_name, estimated_size, column_count FROM duckdb_tables() "
                             "WHERE database_name=current_database() ORDER BY 1,2")
        return [{"schema": r[0], "name": r[1], "estimated_rows": r[2], "columns": r[3]} for r in rows]

    def columns(self, schema: str, name: str) -> list[dict]:
        rows = self.internal("SELECT column_name, data_type FROM information_schema.columns "
                             "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position", [schema, name])
        return [{"name": r[0], "type": r[1]} for r in rows]

    def row_count(self, schema: str, name: str) -> int:
        return self.internal(f"SELECT count(*) FROM {qi(schema)}.{qi(name)}")[0][0]

    def load_arrow(self, table: pa.Table, schema: str, name: str, mode: str = "replace", keys: list[str] | None = None,
                   delete_flag: str | None = None) -> int:
        """Load rows. ``merge`` upserts on ``keys``: incoming rows replace stored rows with the same key, and when a key
        appears more than once in the batch the last row wins. With ``delete_flag``, a winning row whose flag column is
        true deletes the stored row instead (change data capture); the flag column itself is not stored."""
        check_ident(name, "table name")
        for k in keys or []:
            if k not in table.column_names:
                raise QueryError(f"Key column '{k}' is not in the loaded data.")
        if schema not in SCHEMAS:
            raise QueryError("Unknown schema")
        view = f"_tessera_in_{uuid.uuid4().hex[:10]}"
        with self.writing():
            cur = self.cursor()
            try:
                cur.register(view, table)
                target = f"{qi(schema)}.{qi(name)}"
                merging = mode == "merge" and bool(keys)
                if merging:  # remember arrival order so the last row for a key wins
                    cur.unregister(view)
                    table = table.append_column("_tessera_ord", pa.array(range(table.num_rows), pa.int64()))
                    cur.register(view, table)
                if mode == "replace" or (not merging and not self.exists(schema, name)):
                    cur.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {qi(view)}")
                else:
                    if not self.exists(schema, name):
                        drop = "_tessera_ord" + (f", {qi(delete_flag)}" if merging and delete_flag else "")
                        cur.execute(f"CREATE TABLE {target} AS SELECT * EXCLUDE ({drop}) FROM {qi(view)} LIMIT 0")
                    stored = {c["name"]: c["type"] for c in self.columns(schema, name)}
                    have = set(stored) | {"_tessera_ord", delete_flag}
                    incoming = cur.execute(f"DESCRIBE SELECT * FROM {qi(view)}").fetchall()
                    for col, ctype, *_ in incoming:
                        if col not in have:
                            cur.execute(f"ALTER TABLE {target} ADD COLUMN {qi(col)} {ctype}")
                        elif col in stored and stored[col] != ctype:
                            # widen instead of truncating: 25.5 arriving in an integer column makes it a double
                            try:
                                wide = cur.execute(f"SELECT typeof(x) FROM (SELECT NULL::{stored[col]} AS x UNION ALL SELECT NULL::{ctype}) LIMIT 1").fetchone()[0]
                            except duckdb.Error:
                                wide = "VARCHAR"
                            if wide != stored[col]:
                                cur.execute(f"ALTER TABLE {target} ALTER COLUMN {qi(col)} TYPE {wide}")
                    if mode == "merge" and keys:
                        on = " AND ".join(f"t.{qi(k)} IS NOT DISTINCT FROM s.{qi(k)}" for k in keys)
                        part = ", ".join(qi(k) for k in keys)
                        cur.execute(f"CREATE TEMP TABLE {qi(view + '_d')} AS SELECT * EXCLUDE (_tessera_rn) FROM "
                                    f"(SELECT *, row_number() OVER (PARTITION BY {part} ORDER BY _tessera_ord DESC) AS _tessera_rn "
                                    f"FROM {qi(view)}) WHERE _tessera_rn = 1")
                        cur.execute(f"ALTER TABLE {qi(view + '_d')} DROP COLUMN _tessera_ord")
                        cur.execute("BEGIN")
                        try:
                            cur.execute(f"DELETE FROM {target} t WHERE EXISTS (SELECT 1 FROM {qi(view + '_d')} s WHERE {on})")
                            if delete_flag:
                                cur.execute(f"INSERT INTO {target} BY NAME SELECT * EXCLUDE ({qi(delete_flag)}) FROM {qi(view + '_d')} "
                                            f"WHERE NOT coalesce({qi(delete_flag)}, false)")
                            else:
                                cur.execute(f"INSERT INTO {target} BY NAME SELECT * FROM {qi(view + '_d')}")
                            cur.execute("COMMIT")
                        except Exception:
                            cur.execute("ROLLBACK")
                            raise
                        finally:
                            cur.execute(f"DROP TABLE IF EXISTS {qi(view + '_d')}")
                    else:
                        cur.execute(f"INSERT INTO {target} BY NAME SELECT * FROM {qi(view)}")
                cur.unregister(view)
            finally:
                cur.close()
        return table.num_rows

    def create_table_as(self, select_sql: str, schema: str, name: str) -> int:
        check_ident(name, "table name")
        if schema not in ("raw", "curated"):
            raise AccessDenied("Transforms write to raw or curated. Use Publish to write to serving.")
        self.validate_select(select_sql, restricted=False)
        with self.writing():
            cur = self.cursor()
            try:
                cur.execute(f"CREATE OR REPLACE TABLE {qi(schema)}.{qi(name)} AS {select_sql.strip().rstrip(';')}")
            except duckdb.Error as e:
                raise QueryError(str(e).split("\n")[0]) from None
            finally:
                cur.close()
        return self.row_count(schema, name)

    def referenced_tables(self, sql: str) -> list[str]:
        try:
            tables, ctes, _ = self.table_refs(sql)
        except (duckdb.Error, QueryError):
            return []
        out = []
        for cat, sch, tbl in tables:
            if not sch:
                if tbl in ctes:
                    continue
                for s in SCHEMAS:
                    if self.exists(s, tbl):
                        out.append(f"{s}.{tbl}")
                        break
            elif sch in SCHEMAS:
                out.append(f"{sch}.{tbl}")
        return sorted(set(out))

    def drop(self, schema: str, name: str) -> None:
        with self.writing():
            self.internal(f"DROP TABLE IF EXISTS {qi(schema)}.{qi(name)}")

    def arrow(self, schema: str, name: str, batch_rows: int = 100_000):
        cur = self.cursor()
        rel = cur.sql(f"SELECT * FROM {qi(schema)}.{qi(name)}")  # a relation streams batches without an extra copy
        fn = getattr(rel, "to_arrow_reader", None) or rel.fetch_arrow_reader
        return fn(batch_rows)

    def summarize(self, schema: str, name: str) -> list[dict]:
        def run():
            cols, rows = self.internal_desc(f"SUMMARIZE {qi(schema)}.{qi(name)}")
            return [{k: jsonable(v) for k, v in zip(cols, r)} for r in rows]
        return [dict(x) for x in self.stat(("summarize", schema, name), run)]

    def stat(self, key: tuple, fn):
        """Memoise a read-only statistic until the next write (profiles, distinct counts for readiness scores)."""
        k = json.dumps(["stat", *key, self.version])
        with self._cache_lock:
            hit = self._cache.get(k)
        if hit:
            return hit[1]
        out = fn()
        with self._cache_lock:
            if k.endswith(f", {self.version}]"):
                self._cache[k] = (time.monotonic(), out)
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        return out
