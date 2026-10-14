"""Visual transforms: a list of point-and-click steps compiled to one DuckDB SELECT.

Each step becomes a CTE over the previous one, so the result is readable SQL that can be reviewed,
saved as a pipeline transform step, or run once into curated.
"""
from __future__ import annotations

from .semantic import EXPR_FORBIDDEN
from .warehouse import QueryError, qi, split_fqn, sql_literal

CAST_TYPES = {"text": "VARCHAR", "integer": "BIGINT", "decimal": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN"}
FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "contains", "starts_with", "is_null", "not_null", "in", "not_in"}
AGG_FNS = {"sum", "avg", "min", "max", "count", "count_distinct", "median"}
OPS = ("select", "drop", "rename", "filter", "dedupe", "cast", "derive", "fill_null", "trim", "lower", "upper", "replace_text",
       "join", "aggregate", "sort", "limit")
OP_HELP = {
    "select": "Keep only these columns", "drop": "Remove columns", "rename": "Rename a column", "filter": "Keep rows that match",
    "dedupe": "Remove duplicate rows by key", "cast": "Change a column's type", "derive": "Add a calculated column",
    "fill_null": "Replace empty values", "trim": "Trim spaces", "lower": "Lowercase text", "upper": "Uppercase text",
    "replace_text": "Find and replace text", "join": "Join another table", "aggregate": "Group and summarize", "sort": "Sort rows",
    "limit": "Keep the first N rows",
}


def _cols(xs) -> list[str]:
    if not isinstance(xs, list) or not xs:
        raise QueryError("Choose at least one column.")
    return [str(x) for x in xs]


def _expr_ok(expr: str) -> str:
    expr = (expr or "").strip()
    if not expr or EXPR_FORBIDDEN.search(expr):
        raise QueryError("Calculated columns use one SQL expression, without subqueries, comments or semicolons.")
    return expr


def compile_steps(source: str, steps: list[dict]) -> str:
    schema, table = split_fqn(source, "raw")
    ctes = [f"s0 AS (SELECT * FROM {qi(schema)}.{qi(table)})"]
    for i, st in enumerate(steps or [], start=1):
        op, prev = st.get("op"), f"s{i - 1}"
        if op not in OPS:
            raise QueryError(f"Step {i}: unknown operation '{op}'.")
        c = st.get("column")
        if op == "select":
            body = f"SELECT {', '.join(qi(x) for x in _cols(st.get('columns')))} FROM {prev}"
        elif op == "drop":
            body = f"SELECT * EXCLUDE ({', '.join(qi(x) for x in _cols(st.get('columns')))}) FROM {prev}"
        elif op == "rename":
            if not st.get("to"):
                raise QueryError(f"Step {i}: new name is required.")
            body = f"SELECT * RENAME ({qi(c)} AS {qi(st['to'])}) FROM {prev}"
        elif op == "filter":
            o, v = st.get("operator", "="), st.get("value")
            if o not in FILTER_OPS:
                raise QueryError(f"Step {i}: unknown filter '{o}'.")
            col = qi(c)
            if o == "is_null":
                cond = f"{col} IS NULL"
            elif o == "not_null":
                cond = f"{col} IS NOT NULL"
            elif o == "contains":
                cond = f"CAST({col} AS VARCHAR) ILIKE {sql_literal('%' + str(v) + '%')}"
            elif o == "starts_with":
                cond = f"starts_with(CAST({col} AS VARCHAR), {sql_literal(str(v))})"
            elif o in ("in", "not_in"):
                vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
                cond = f"CAST({col} AS VARCHAR) {'NOT ' if o == 'not_in' else ''}IN ({', '.join(sql_literal(str(x)) for x in vals) or 'NULL'})"
            else:
                num = isinstance(v, (int, float)) and not isinstance(v, bool)
                if not num:
                    try:
                        v, num = float(v), True
                    except (TypeError, ValueError):
                        pass
                cond = f"{col} {o} {sql_literal(v)}" if num else f"CAST({col} AS VARCHAR) {o} {sql_literal(str(v))}"
            body = f"SELECT * FROM {prev} WHERE {cond}"
        elif op == "dedupe":
            keys = ", ".join(qi(x) for x in _cols(st.get("columns")))
            order = f" ORDER BY {qi(st['order_by'])} {'DESC' if st.get('desc', True) else 'ASC'}" if st.get("order_by") else ""
            body = f"SELECT * FROM {prev} QUALIFY row_number() OVER (PARTITION BY {keys}{order}) = 1"
        elif op == "cast":
            t = CAST_TYPES.get(st.get("to"))
            if not t:
                raise QueryError(f"Step {i}: type must be one of {', '.join(CAST_TYPES)}.")
            body = f"SELECT * REPLACE (TRY_CAST({qi(c)} AS {t}) AS {qi(c)}) FROM {prev}"
        elif op == "derive":
            if not st.get("name"):
                raise QueryError(f"Step {i}: name the new column.")
            body = f"SELECT *, ({_expr_ok(st.get('expr'))}) AS {qi(st['name'])} FROM {prev}"
        elif op == "fill_null":
            body = f"SELECT * REPLACE (coalesce(CAST({qi(c)} AS VARCHAR), {sql_literal(str(st.get('value', '')))}) AS {qi(c)}) FROM {prev}" \
                if not isinstance(st.get("value"), (int, float)) else \
                f"SELECT * REPLACE (coalesce({qi(c)}, {sql_literal(st['value'])}) AS {qi(c)}) FROM {prev}"
        elif op in ("trim", "lower", "upper"):
            fn = {"trim": "trim", "lower": "lower", "upper": "upper"}[op]
            body = f"SELECT * REPLACE ({fn}(CAST({qi(c)} AS VARCHAR)) AS {qi(c)}) FROM {prev}"
        elif op == "replace_text":
            body = (f"SELECT * REPLACE (replace(CAST({qi(c)} AS VARCHAR), {sql_literal(str(st.get('find', '')))}, "
                    f"{sql_literal(str(st.get('replace', '')))}) AS {qi(c)}) FROM {prev}")
        elif op == "join":
            js, jt = split_fqn(st.get("table") or "", "raw")
            how = {"left": "LEFT", "inner": "INNER"}.get(st.get("how", "left"), "LEFT")
            take = st.get("columns") or []
            sel = ", ".join(f"j.{qi(x)}" for x in take) if take else "j.* EXCLUDE (" + qi(st.get("right_key")) + ")"
            body = (f"SELECT p.*, {sel} FROM {prev} AS p {how} JOIN {qi(js)}.{qi(jt)} AS j "
                    f"ON p.{qi(st.get('left_key'))} = j.{qi(st.get('right_key'))}")
        elif op == "aggregate":
            groups = [qi(x) for x in st.get("group_by") or []]
            aggs = []
            for a in st.get("aggs") or []:
                fn = a.get("fn", "sum")
                if fn not in AGG_FNS:
                    raise QueryError(f"Step {i}: unknown summary '{fn}'.")
                expr = "count(*)" if fn == "count" and not a.get("column") else (
                    f"count(DISTINCT {qi(a['column'])})" if fn == "count_distinct" else f"{fn}({qi(a['column'])})")
                aggs.append(f"{expr} AS {qi(a.get('as') or (fn + '_' + (a.get('column') or 'rows')))}")
            if not aggs:
                raise QueryError(f"Step {i}: add at least one summary.")
            body = f"SELECT {', '.join(groups + aggs)} FROM {prev}" + (f" GROUP BY {', '.join(groups)}" if groups else "")
        elif op == "sort":
            body = f"SELECT * FROM {prev} ORDER BY {qi(c)} {'DESC' if st.get('desc') else 'ASC'}"
        elif op == "limit":
            body = f"SELECT * FROM {prev} LIMIT {max(1, int(st.get('n') or 100))}"
        ctes.append(f"s{i} AS ({body})")
    return "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM s{len(steps or [])}"
