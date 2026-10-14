"""Semantic models: governed business measures and dimensions compiled to safe SQL.

A model has one base table, optional left joins to lookup tables, named dimensions and named measures.
People query it by picking fields; they never write SQL. Every identifier in the generated SQL comes from
the saved model definition and every filter value is a bound parameter.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from .warehouse import IDENT, QueryError, qi, split_fqn

AGGS = {"sum": "sum({c})", "avg": "avg({c})", "count": "count({c})", "count_distinct": "count(DISTINCT {c})",
        "min": "min({c})", "max": "max({c})", "count_rows": "count(*)", "median": "median({c})"}
GRAINS = ("day", "week", "month", "quarter", "year")
FORMATS = ("number", "integer", "currency", "percent", "text")
OPS = {"=", "!=", "in", "not_in", ">", ">=", "<", "<=", "between", "contains", "is_null", "not_null"}
EXPR_FORBIDDEN = re.compile(r";|--|/\*|\bselect\b|\bfrom\b|\bread_\w+|\battach\b|\bcopy\b|\binstall\b|\bload\b", re.I)


def _col_ref(ref: str, aliases: set[str]) -> str:
    parts = ref.split(".")
    if len(parts) == 1:
        alias, col = "b", parts[0]
    elif len(parts) == 2:
        alias, col = parts
    else:
        raise QueryError(f"Column reference '{ref}' must be 'column' or 'alias.column'.")
    if alias not in aliases:
        raise QueryError(f"Unknown table alias '{alias}' in '{ref}'.")
    return f"{qi(alias)}.{qi(col)}"


def validate_model(m: dict, wh) -> dict:
    """Check a model definition against the warehouse and normalise it."""
    name = (m.get("name") or "").strip()
    if not name:
        raise QueryError("Give the model a name.")
    schema, table = split_fqn(m.get("base") or "", "serving")
    if not wh.exists(schema, table):
        raise QueryError(f"Base table {schema}.{table} does not exist.")
    cols = {"b": {c["name"] for c in wh.columns(schema, table)}}
    joins = []
    for j in m.get("joins") or []:
        alias = j.get("alias") or ""
        if not IDENT.match(alias) or alias == "b" or alias in cols:
            raise QueryError(f"Join alias '{alias}' must be a unique identifier other than 'b'.")
        js, jt = split_fqn(j.get("table") or "", "serving")
        if not wh.exists(js, jt):
            raise QueryError(f"Joined table {js}.{jt} does not exist.")
        if js != schema and "serving" in (js, schema):
            raise QueryError("Joined tables must be in the same zone as the base table (all serving, or all raw/curated).")
        cols[alias] = {c["name"] for c in wh.columns(js, jt)}
        if j.get("left_key") not in cols["b"] or j.get("right_key") not in cols[alias]:
            raise QueryError(f"Join keys for '{alias}' must be columns of the base and joined tables.")
        joins.append({"table": f"{js}.{jt}", "alias": alias, "left_key": j["left_key"], "right_key": j["right_key"]})
    names: set[str] = set()

    def check_ref(ref: str):
        parts = ref.split(".")
        alias, col = ("b", parts[0]) if len(parts) == 1 else (parts[0], parts[-1])
        if alias not in cols or col not in cols[alias]:
            raise QueryError(f"Column '{ref}' does not exist in the model's tables.")

    dims = []
    for d in m.get("dimensions") or []:
        key = d.get("name") or ""
        if not IDENT.match(key) or key in names:
            raise QueryError(f"Dimension name '{key}' must be a unique identifier.")
        names.add(key)
        check_ref(d.get("column") or "")
        typ = d.get("type") or "category"
        if typ not in ("category", "time", "number"):
            raise QueryError(f"Dimension '{key}' type must be category, time or number.")
        dims.append({"name": key, "label": d.get("label") or key.replace("_", " ").capitalize(), "column": d["column"], "type": typ,
                     "description": d.get("description", "")})
    meas = []
    for x in m.get("measures") or []:
        key = x.get("name") or ""
        if not IDENT.match(key) or key in names:
            raise QueryError(f"Measure name '{key}' must be a unique identifier.")
        names.add(key)
        agg = x.get("agg") or "sum"
        if agg == "custom":
            expr = (x.get("expr") or "").strip()
            if not expr or EXPR_FORBIDDEN.search(expr):
                raise QueryError(f"Measure '{key}': custom expressions are single SQL aggregate expressions without subqueries or comments.")
        elif agg not in AGGS:
            raise QueryError(f"Measure '{key}': unknown aggregation '{agg}'.")
        elif agg != "count_rows":
            check_ref(x.get("column") or "")
        fmt = x.get("format") or "number"
        if fmt not in FORMATS:
            raise QueryError(f"Measure '{key}': unknown format '{fmt}'.")
        meas.append({"name": key, "label": x.get("label") or key.replace("_", " ").capitalize(), "agg": agg, "column": x.get("column"),
                     "expr": x.get("expr"), "format": fmt, "decimals": int(x.get("decimals", 0 if fmt == "integer" else 2)),
                     "description": x.get("description", ""), "direction": "down" if x.get("direction") == "down" else "up",
                     "additive_over_time": bool(x.get("additive_over_time"))})
    if not meas:
        raise QueryError("Add at least one measure.")
    out = {"name": name, "description": m.get("description", ""), "base": f"{schema}.{table}", "joins": joins, "dimensions": dims, "measures": meas}
    # compile every measure once so broken custom expressions are caught at save time
    sql, params, _ = compile_query(out, {"measures": [x["name"] for x in meas], "limit": 1})
    wh.validate_select(sql, restricted=False)
    try:
        wh.internal(f"SELECT * FROM ({sql}) LIMIT 0", params)
    except Exception as e:  # noqa: BLE001
        raise QueryError(f"Model does not compile: {str(e).splitlines()[0]}") from None
    return out


def _from_clause(model: dict) -> tuple[str, set[str]]:
    schema, table = model["base"].split(".")
    sql = f"{qi(schema)}.{qi(table)} AS b"
    aliases = {"b"}
    for j in model.get("joins") or []:
        js, jt = j["table"].split(".")
        sql += f" LEFT JOIN {qi(js)}.{qi(jt)} AS {qi(j['alias'])} ON b.{qi(j['left_key'])} = {qi(j['alias'])}.{qi(j['right_key'])}"
        aliases.add(j["alias"])
    return sql, aliases


def _measure_sql(m: dict, aliases: set[str]) -> str:
    if m["agg"] == "custom":
        return f"({m['expr']})"
    if m["agg"] == "count_rows":
        return "count(*)"
    return AGGS[m["agg"]].format(c=_col_ref(m["column"], aliases))


def _dim_sql(d: dict, aliases: set[str], grain: str | None) -> str:
    c = _col_ref(d["column"], aliases)
    if d["type"] == "time":
        g = grain or "month"
        if g not in GRAINS:
            raise QueryError(f"Time grain must be one of {', '.join(GRAINS)}.")
        return f"CAST(date_trunc('{g}', TRY_CAST({c} AS TIMESTAMP)) AS DATE)"
    return c


def compile_query(model: dict, spec: dict) -> tuple[str, list, list[dict]]:
    """Compile a field-picking spec into SQL. Returns (sql, params, field metadata)."""
    dims = {d["name"]: d for d in model["dimensions"]}
    meas = {m["name"]: m for m in model["measures"]}
    want_d = list(dict.fromkeys(spec.get("dimensions") or []))
    want_m = list(dict.fromkeys(spec.get("measures") or []))
    if not want_m and not want_d:
        raise QueryError("Pick at least one measure or dimension.")
    for n in want_d:
        if n not in dims:
            raise QueryError(f"Unknown dimension '{n}'.")
    for n in want_m:
        if n not in meas:
            raise QueryError(f"Unknown measure '{n}'.")
    grains = spec.get("grains") or {}
    frm, aliases = _from_clause(model)
    select, fields, params, where, having = [], [], [], [], []
    for n in want_d:
        select.append(f"{_dim_sql(dims[n], aliases, grains.get(n))} AS {qi(n)}")
        fields.append({"name": n, "label": dims[n]["label"], "kind": "dimension", "type": dims[n]["type"], "grain": grains.get(n) if dims[n]["type"] == "time" else None})
    for n in want_m:
        select.append(f"{_measure_sql(meas[n], aliases)} AS {qi(n)}")
        fields.append({"name": n, "label": meas[n]["label"], "kind": "measure", "format": meas[n]["format"], "decimals": meas[n]["decimals"]})
    for f in spec.get("filters") or []:
        field, op = f.get("field"), f.get("op", "=")
        if op not in OPS:
            raise QueryError(f"Unknown filter operator '{op}'.")
        if field in dims:
            expr, target = _col_ref(dims[field]["column"], aliases), where
            if dims[field]["type"] == "time":
                expr = f"TRY_CAST({expr} AS TIMESTAMP)"
        elif field in meas:
            expr, target = _measure_sql(meas[field], aliases), having
        else:
            raise QueryError(f"Unknown filter field '{field}'.")
        v = f.get("value")
        if op in ("is_null", "not_null"):
            target.append(f"{expr} IS {'NOT ' if op == 'not_null' else ''}NULL")
        elif op in ("in", "not_in"):
            vals = v if isinstance(v, list) else [v]
            if not vals:
                continue
            target.append(f"CAST({expr} AS VARCHAR) {'NOT ' if op == 'not_in' else ''}IN ({', '.join('?' for _ in vals)})")
            params.extend(str(x) for x in vals)
        elif op == "between":
            if not isinstance(v, list) or len(v) != 2:
                raise QueryError("'between' needs two values.")
            lo, hi = v
            if lo not in (None, ""):
                target.append(f"{expr} >= ?")
                params.append(_coerce(lo, field in dims and dims[field]["type"] == "time"))
            if hi not in (None, ""):
                target.append(f"{expr} {'<' if field in dims and dims[field]['type'] == 'time' else '<='} ?")
                params.append(_coerce(hi, field in dims and dims[field]["type"] == "time", end=True))
        elif op == "contains":
            target.append(f"CAST({expr} AS VARCHAR) ILIKE ?")
            params.append(f"%{v}%")
        else:
            is_time = field in dims and dims[field]["type"] == "time"
            num = isinstance(v, (int, float)) and not isinstance(v, bool)
            lhs = expr if (num or is_time) else f"CAST({expr} AS VARCHAR)"
            target.append(f"{lhs} {op} ?")
            params.append(_coerce(v, is_time) if is_time else (v if num else str(v)))
    sql = f"SELECT {', '.join(select)} FROM {frm}"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    if want_d and want_m:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(want_d)))
    elif want_d:
        sql += " GROUP BY ALL"
    if having:
        sql += " HAVING " + " AND ".join(f"({h})" for h in having)
    order = []
    for s in spec.get("sort") or []:
        if s.get("field") in want_d + want_m:
            order.append(f"{qi(s['field'])} {'DESC' if s.get('dir') == 'desc' else 'ASC'}")
    if not order:
        tdims = [n for n in want_d if dims[n]["type"] == "time"]
        if tdims:
            order = [f"{qi(tdims[0])} ASC"]
        elif want_m and want_d:
            order = [f"{qi(want_m[0])} DESC NULLS LAST"]
    if order:
        sql += " ORDER BY " + ", ".join(order)
    limit = min(int(spec.get("limit") or 5000), 50_000)
    sql += f" LIMIT {limit}"
    return sql, params, fields


def _coerce(v: Any, is_time: bool, end: bool = False):
    if not is_time:
        return v
    s = str(v)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise QueryError(f"'{s}' is not a date (use YYYY-MM-DD).") from None
    if end and len(s) == 10:
        d = d + timedelta(days=1)
    return d


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def previous_period(spec: dict, model: dict) -> dict | None:
    """Shift a time 'between' filter back by its own length, for period-over-period comparison."""
    dims = {d["name"]: d for d in model["dimensions"]}
    out = []
    shifted = False
    for f in spec.get("filters") or []:
        if not shifted and f.get("op") == "between" and dims.get(f.get("field"), {}).get("type") == "time":
            lo, hi = f["value"]
            if lo and hi:
                a, b = date.fromisoformat(str(lo)[:10]), date.fromisoformat(str(hi)[:10])
                if a.day == 1 and (b + timedelta(days=1)).day == 1:
                    months = (b.year - a.year) * 12 + b.month - a.month + 1
                    pa = _add_months(a, -months)
                    pb = _add_months(a, 0) - timedelta(days=1)
                    out.append({**f, "value": [pa.isoformat(), pb.isoformat()]})
                else:
                    span = (b - a) + timedelta(days=1)
                    out.append({**f, "value": [(a - span).isoformat(), (b - span).isoformat()]})
                shifted = True
                continue
        out.append(f)
    return {**spec, "filters": out} if shifted else None


def suggest_model(wh, schema: str, table: str) -> dict:
    """Draft a model from a table: text and date columns become dimensions, numbers become measures."""
    dims, meas = [], [{"name": "row_count", "label": "Rows", "agg": "count_rows", "format": "integer"}]
    for c in wh.columns(schema, table):
        n, t = c["name"], c["type"].upper()
        key = re.sub(r"[^A-Za-z0-9_]", "_", n).lower()
        if not IDENT.match(key):
            key = "f_" + key
        if "DATE" in t or "TIMESTAMP" in t:
            dims.append({"name": key, "column": n, "type": "time"})
        elif any(x in t for x in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "REAL", "NUMERIC", "HUGEINT")):
            if key.endswith("_id") or key == "id":
                dims.append({"name": key, "column": n, "type": "category"})
            else:
                fmt = "currency" if re.search(r"price|total|amount|revenue|cost|sales|value", key) else "number"
                meas.append({"name": f"total_{key}", "label": f"Total {n.replace('_', ' ')}", "agg": "sum", "column": n, "format": fmt})
                meas.append({"name": f"avg_{key}", "label": f"Average {n.replace('_', ' ')}", "agg": "avg", "column": n, "format": fmt})
        else:
            dims.append({"name": key, "column": n, "type": "category"})
    ids = [d for d in dims if d["name"].endswith("_id")]
    if ids:
        meas.append({"name": f"distinct_{ids[0]['name']}", "label": f"Distinct {ids[0]['column'].replace('_', ' ')}", "agg": "count_distinct",
                     "column": ids[0]["column"], "format": "integer"})
    return {"name": table.replace("_", " ").title(), "base": f"{schema}.{table}", "joins": [], "dimensions": dims, "measures": meas}
