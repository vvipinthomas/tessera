"""Data quality rules. Every rule compiles to SQL and runs inside the warehouse, so checks scale with DuckDB."""
from __future__ import annotations

import re
from typing import Any

from .warehouse import QueryError, Warehouse, jsonable, qi

KINDS = {
    "not_null": {"label": "is never empty", "column": True, "params": []},
    "unique": {"label": "has no duplicate values", "column": True, "params": []},
    "range": {"label": "is within a numeric range", "column": True, "params": ["min", "max"]},
    "regex": {"label": "matches a pattern", "column": True, "params": ["pattern"]},
    "allowed_values": {"label": "is one of a set of values", "column": True, "params": ["values"]},
    "not_future": {"label": "is not a future date", "column": True, "params": []},
    "freshness": {"label": "has data newer than N hours", "column": True, "params": ["hours"]},
    "row_count": {"label": "table has at least N rows", "column": False, "params": ["min"]},
    "custom_sql": {"label": "custom SQL returns no rows", "column": False, "params": ["sql"]},
}

PATTERNS = {
    "email": r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$",
    "ca_postal_code": r"^[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d$",
    "us_zip": r"^\d{5}(-\d{4})?$",
    "phone_na": r"^(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$",
    "iso_date": r"^\d{4}-\d{2}-\d{2}$",
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}


def describe(rule: dict) -> str:
    k, p, c = rule["kind"], rule.get("params") or {}, rule.get("column_name")
    if k == "range":
        return f"{c} is between {p.get('min', '-inf')} and {p.get('max', 'inf')}"
    if k == "regex":
        return f"{c} matches {p.get('label') or p.get('pattern')}"
    if k == "allowed_values":
        vals = p.get("values") or []
        return f"{c} is one of {', '.join(map(str, vals[:8]))}{'…' if len(vals) > 8 else ''}"
    if k == "freshness":
        return f"{c} has data newer than {p.get('hours')} hours"
    if k == "row_count":
        return f"table has at least {p.get('min')} rows"
    if k == "custom_sql":
        return p.get("label") or "custom SQL check returns no rows"
    return f"{c} {KINDS[k]['label']}"


def validate_rule(rule: dict, columns: list[str]) -> None:
    k = rule.get("kind")
    if k not in KINDS:
        raise QueryError(f"Unknown rule kind '{k}'")
    if KINDS[k]["column"] and rule.get("column_name") not in columns:
        raise QueryError(f"Column '{rule.get('column_name')}' does not exist")
    p = rule.get("params") or {}
    if k == "regex":
        try:
            re.compile(p.get("pattern", ""))
        except re.error as e:
            raise QueryError(f"Invalid pattern: {e}") from None
        if not p.get("pattern"):
            raise QueryError("Pattern is required")
    if k == "allowed_values" and not p.get("values"):
        raise QueryError("List at least one allowed value")
    if k == "range" and p.get("min") is None and p.get("max") is None:
        raise QueryError("Set a minimum, a maximum, or both")
    if k in ("freshness",) and not p.get("hours"):
        raise QueryError("Hours is required")
    if k == "row_count" and p.get("min") is None:
        raise QueryError("Minimum row count is required")
    if rule.get("severity", "error") not in ("error", "warn"):
        raise QueryError("Severity must be error or warn")


def _failing_sql(rule: dict, table: str) -> tuple[str, list, str]:
    """Return (sql counting failing rows, params, sql selecting sample failing rows)."""
    k, p = rule["kind"], rule.get("params") or {}
    col = qi(rule["column_name"]) if rule.get("column_name") else None
    v = f"CAST({col} AS VARCHAR)" if col else None
    if k == "not_null":
        where, params = f"{col} IS NULL OR trim({v}) = ''", []
    elif k == "range":
        conds, params = [], []
        if p.get("min") is not None:
            conds.append(f"TRY_CAST({col} AS DOUBLE) < ?")
            params.append(float(p["min"]))
        if p.get("max") is not None:
            conds.append(f"TRY_CAST({col} AS DOUBLE) > ?")
            params.append(float(p["max"]))
        where = f"{col} IS NOT NULL AND trim({v}) <> '' AND (TRY_CAST({col} AS DOUBLE) IS NULL OR {' OR '.join(conds)})"
    elif k == "regex":
        where, params = f"{col} IS NOT NULL AND trim({v}) <> '' AND NOT regexp_full_match(trim({v}), ?)", [p["pattern"]]
    elif k == "allowed_values":
        vals = [str(x) for x in p["values"]]
        where = f"{col} IS NOT NULL AND trim({v}) <> '' AND {v} NOT IN ({','.join('?' for _ in vals)})"
        params = vals
    elif k == "not_future":
        where, params = f"TRY_CAST({col} AS TIMESTAMP) > now()::TIMESTAMP", []
    elif k == "unique":
        cnt = (f"SELECT COALESCE(sum(c - 1), 0) FROM (SELECT count(*) c FROM {table} WHERE {col} IS NOT NULL "
               f"GROUP BY {col} HAVING count(*) > 1)")
        sample = (f"SELECT {col} AS value, count(*) AS occurrences FROM {table} WHERE {col} IS NOT NULL "
                  f"GROUP BY {col} HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 5")
        return cnt, [], sample
    elif k == "freshness":
        hrs = float(p["hours"])
        cnt = (f"SELECT CASE WHEN max(TRY_CAST({col} AS TIMESTAMP)) IS NULL OR max(TRY_CAST({col} AS TIMESTAMP)) "
               f"< now()::TIMESTAMP - to_microseconds(CAST(? * 3600000000 AS BIGINT)) THEN 1 ELSE 0 END FROM {table}")
        sample = f"SELECT max(TRY_CAST({col} AS TIMESTAMP)) AS latest FROM {table}"
        return cnt, [hrs], sample
    elif k == "row_count":
        return f"SELECT CASE WHEN count(*) < ? THEN 1 ELSE 0 END FROM {table}", [int(p["min"])], f"SELECT count(*) AS row_count FROM {table}"
    elif k == "custom_sql":
        inner = p["sql"].strip().rstrip(";")
        return f"SELECT count(*) FROM ({inner})", [], f"SELECT * FROM ({inner}) LIMIT 5"
    else:
        raise QueryError(f"Unknown rule kind {k}")
    return f"SELECT count(*) FROM {table} WHERE {where}", params, f"SELECT * FROM {table} WHERE {where} LIMIT 5"


def run_rule(wh: Warehouse, schema: str, name: str, rule: dict) -> dict:
    table = f"{qi(schema)}.{qi(name)}"
    total = wh.row_count(schema, name)
    try:
        if rule["kind"] == "custom_sql":
            wh.validate_select(rule["params"]["sql"], restricted=False)
        cnt_sql, params, sample_sql = _failing_sql(rule, table)
        failing = int(wh.internal(cnt_sql, params)[0][0] or 0)
        sample = None
        if failing:
            cols, rows = wh.internal_desc(sample_sql, params)
            sample = {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows]}
        return {"passed": failing == 0, "failing": failing, "total": total, "sample": sample, "error": None}
    except Exception as e:  # noqa: BLE001 - a broken rule should be reported, not crash the run
        return {"passed": False, "failing": 0, "total": total, "sample": None, "error": str(e).split("\n")[0]}


def suggest(wh: Warehouse, schema: str, name: str, existing: list[dict] | None = None) -> list[dict]:
    """Suggest rules from the data itself. Nothing is applied until a person (or permitted agent) accepts."""
    existing_keys = {(r["kind"], r.get("column_name")) for r in existing or []}
    summary = wh.summarize(schema, name)
    total = wh.row_count(schema, name)
    out: list[dict] = []
    table = f"{qi(schema)}.{qi(name)}"

    def add(kind, col, params=None, severity="error", why=""):
        if (kind, col) in existing_keys:
            return
        out.append({"kind": kind, "column_name": col, "params": params or {}, "severity": severity, "reason": why})

    if total:
        add("row_count", None, {"min": max(1, int(total * 0.5))}, "warn", f"Table has {total} rows today; alert if it halves.")
    for s in summary:
        col, ctype = s["column_name"], str(s["column_type"]).upper()
        nullp = float(s.get("null_percentage") or 0)
        approx = int(s.get("approx_unique") or 0)
        lname = col.lower()
        if nullp == 0:
            add("not_null", col, why="No empty values today.")
        elif nullp < 5:
            add("not_null", col, severity="warn", why=f"{nullp:.1f}% empty today.")
        if (lname == "id" or lname.endswith("_id") or lname.endswith("key") or "uuid" in lname) and total:
            distinct = wh.internal(f"SELECT count(DISTINCT {qi(col)}) FROM {table}")[0][0]
            if distinct >= total * 0.9:
                add("unique", col, why=f"Identifier column; {distinct} distinct values in {total} rows.")
        if any(t in ctype for t in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "NUMERIC", "REAL")):
            try:
                neg = wh.internal(f"SELECT count(*) FILTER (WHERE {qi(col)} < 0), count(*) FROM {table}")[0]
                if neg[1] and neg[0] / neg[1] < 0.02:
                    add("range", col, {"min": 0}, "error", f"{neg[0]} negative value(s); values are otherwise non-negative.")
            except Exception:  # noqa: BLE001
                pass
        if "DATE" in ctype or "TIMESTAMP" in ctype:
            add("not_future", col, why="Date column; future dates usually mean entry errors.")
        if "VARCHAR" in ctype:
            sample = [r[0] for r in wh.internal(
                f"SELECT trim(CAST({qi(col)} AS VARCHAR)) FROM {table} WHERE {qi(col)} IS NOT NULL AND trim(CAST({qi(col)} AS VARCHAR))<>'' LIMIT 500")]
            if sample:
                for label, pat in PATTERNS.items():
                    hit = sum(1 for v in sample if re.fullmatch(pat, v)) / len(sample)
                    if hit >= 0.85:
                        add("regex", col, {"pattern": pat, "label": label}, "error" if hit > 0.97 else "warn",
                            f"{hit:.0%} of sampled values look like {label}.")
                        break
                else:
                    if 1 < approx <= 15 and total >= 50:
                        vals = [r[0] for r in wh.internal(
                            f"SELECT CAST({qi(col)} AS VARCHAR) v, count(*) c FROM {table} WHERE {qi(col)} IS NOT NULL "
                            f"GROUP BY 1 HAVING count(*) >= ? ORDER BY 2 DESC", [max(2, int(total * 0.02))])]
                        if len(vals) >= 2:
                            add("allowed_values", col, {"values": vals}, "warn",
                                f"Low-cardinality column; {len(vals)} values cover at least 2% of rows each.")
    return out


def score(results: list[dict]) -> float | None:
    """Mean pass rate across rules, weighting each rule equally."""
    vals = []
    for r in results:
        if r.get("error"):
            vals.append(0.0)
        elif r["kind"] in ("freshness", "row_count", "custom_sql"):
            vals.append(1.0 if r["passed"] else 0.0)
        else:
            vals.append(1 - (r["failing"] / r["total"]) if r["total"] else 1.0)
    return round(sum(vals) / len(vals), 4) if vals else None


_: Any = None
