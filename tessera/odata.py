"""Read-only OData v4 feed so Power BI, Excel, Tableau and other BI tools can read governed data directly.

What is exposed
  - every published (serving) dataset the user may read, with masking and row-level security applied
  - every saved report (SQL or semantic-model query) the user can see in their workspaces

Authentication: an API token, either as a Bearer token or as the password in Basic auth (any user name).
Power BI / Excel: Get data > OData feed > URL https://tessera.example.com/odata > Basic > token as password.

Supported query options: $select, $filter (eq ne gt ge lt le, and or not, parentheses, contains, startswith,
endswith, null), $orderby, $top, $skip, $count, and server-driven paging with @odata.nextLink.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote, urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import workspaces as ws
from .errors import UserError, public_message
from .warehouse import QueryError, qi


def escape(text: str, extra: dict | None = None) -> str:
    """Escape text for XML we generate (we never parse XML)."""
    out = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for k, v in (extra or {}).items():
        out = out.replace(k, v)
    return out


PAGE = 5000
MAX_PAGE = 50_000
NS = "Tessera"

EDM = [(r"^(TINYINT|SMALLINT|UTINYINT)", "Edm.Int16"), (r"^(INTEGER|USMALLINT)", "Edm.Int32"), (r"^(BIGINT|UINTEGER|HUGEINT|UBIGINT)", "Edm.Int64"),
       (r"^DECIMAL", "Edm.Decimal"), (r"^(DOUBLE|FLOAT|REAL)", "Edm.Double"), (r"^BOOLEAN", "Edm.Boolean"), (r"^DATE$", "Edm.Date"),
       (r"^TIMESTAMP", "Edm.DateTimeOffset"), (r"^TIME", "Edm.TimeOfDay")]


def edm(duck_type: str) -> str:
    t = duck_type.upper()
    for pat, e in EDM:
        if re.match(pat, t):
            return e
    return "Edm.String"


def safe_name(s: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return ("_" + n) if not n or n[0].isdigit() else n


# ---------------------------------------------------------------- $filter -> SQL
TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<dt>\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?)|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[mMdDfFlL]?)"
                   r"|(?P<punc>[(),])|(?P<word>[A-Za-z_][A-Za-z0-9_]*))")
CMP = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
FUNCS = {"contains": "contains({0}, {1})", "startswith": "starts_with({0}, {1})", "endswith": "ends_with({0}, {1})",
         "tolower": "lower({0})", "toupper": "upper({0})", "year": "year({0})", "month": "month({0})", "day": "day({0})", "length": "length({0})"}


class FilterError(UserError, ValueError):
    pass


def tokenize(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise FilterError(f"Cannot read $filter near: {s[pos:pos + 20]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(s) and s[pos].isspace():
            pos += 1
    return out


class FilterParser:
    """Recursive descent over OData's boolean grammar; columns are validated, values are bound parameters."""

    def __init__(self, text: str, columns: dict[str, str]):
        self.toks, self.i, self.cols, self.params = tokenize(text), 0, columns, []
        if len(self.toks) > 400:
            raise FilterError("$filter is too long.")

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else (None, None)

    def take(self, kind=None, val=None):
        t = self.peek()
        if t[0] is None or (kind and t[0] != kind) or (val and str(t[1]).lower() != val):
            raise FilterError(f"Expected {val or kind} in $filter")
        self.i += 1
        return t

    def parse(self) -> str:
        e = self.or_()
        if self.peek()[0] is not None:
            raise FilterError(f"Unexpected {self.peek()[1]!r} in $filter")
        return e

    def or_(self):
        e = self.and_()
        while self.peek() == ("word", "or") or (self.peek()[0] == "word" and self.peek()[1].lower() == "or"):
            self.i += 1
            e = f"({e} OR {self.and_()})"
        return e

    def and_(self):
        e = self.not_()
        while self.peek()[0] == "word" and self.peek()[1].lower() == "and":
            self.i += 1
            e = f"({e} AND {self.not_()})"
        return e

    def not_(self):
        if self.peek()[0] == "word" and self.peek()[1].lower() == "not":
            self.i += 1
            return f"(NOT {self.not_()})"
        return self.cmp()

    def cmp(self):
        left = self.value()
        t = self.peek()
        if t[0] == "word" and t[1].lower() in CMP:
            self.i += 1
            right = self.value()
            op = CMP[t[1].lower()]
            if right == "NULL":
                return f"({left} IS {'NOT ' if op == '<>' else ''}NULL)"
            return f"({left} {op} {right})"
        return left  # a boolean function such as contains(...)

    def value(self):
        kind, v = self.take()
        if kind == "punc" and v == "(":
            e = self.or_()
            self.take("punc", ")")
            return e
        if kind == "str":
            self.params.append(v[1:-1].replace("''", "'"))
            return "?"
        if kind == "num":
            v = v.rstrip("mMdDfFlL")
            self.params.append(float(v) if any(c in v for c in ".eE") else int(v))
            return "?"
        if kind == "dt":
            self.params.append(datetime.fromisoformat(v.replace("Z", "+00:00")) if "T" in v else date.fromisoformat(v))
            return "?"
        if kind == "word":
            lw = v.lower()
            if lw == "null":
                return "NULL"
            if lw in ("true", "false"):
                return lw.upper()
            if lw in FUNCS and self.peek() == ("punc", "("):
                self.take("punc", "(")
                args = [self.or_()]
                while self.peek() == ("punc", ","):
                    self.i += 1
                    args.append(self.or_())
                self.take("punc", ")")
                need = FUNCS[lw].count("{")
                if len(args) != need:
                    raise FilterError(f"{v}() takes {need} argument(s)")
                return FUNCS[lw].format(*args)
            if v in self.cols:
                return qi(v)
            raise FilterError(f"Unknown property '{v}'")
        raise FilterError(f"Unexpected {v!r} in $filter")


def order_by(text: str, columns: dict[str, str]) -> str:
    parts = []
    for item in text.split(","):
        bits = item.strip().split()
        if not bits or bits[0] not in columns or len(bits) > 2 or (len(bits) == 2 and bits[1].lower() not in ("asc", "desc")):
            raise FilterError(f"Cannot order by {item.strip()!r}")
        parts.append(f"{qi(bits[0])} {bits[1].upper() if len(bits) == 2 else 'ASC'}")
    return ", ".join(parts)


# ---------------------------------------------------------------- entity sets
def entity_sets(plat, user) -> dict[str, dict]:
    """name -> {kind, sql, params, title}. Only published datasets and saved reports the user can see."""
    out: dict[str, dict] = {}
    for d in plat.store.all("SELECT schema_name, name, description FROM datasets WHERE schema_name='serving' ORDER BY name"):
        if plat.wh.exists("serving", d["name"]):
            out[safe_name(d["name"])] = {"kind": "dataset", "sql": f"SELECT * FROM serving.{qi(d['name'])}", "params": [],
                                         "title": f"serving.{d['name']}", "description": d.get("description") or ""}
    rows = ws.filter_rows(plat.store, user, plat.store.all("SELECT * FROM queries ORDER BY name"))
    for q in rows:
        try:
            if q.get("kind") == "model":
                from . import semantic
                model = plat.model(q["model_id"])
                ws.require(plat.store, user, model.get("workspace_id") or 1)
                sql, params, _ = semantic.compile_query(model, dict(q.get("spec") or {}))
            else:
                sql, params = q["sql"].strip().rstrip(";"), []
        except Exception:  # noqa: BLE001  (a broken or hidden report is left out, not an error for the whole feed)  # nosec B112
            continue
        name = safe_name(f"report_{q['id']}_{q['name']}")[:120]
        out[name] = {"kind": "report", "sql": sql, "params": params, "title": q["name"], "description": q.get("description") or ""}
    return out


def shape(plat, user, es: dict) -> tuple[list[str], list[str]]:
    res = plat.user_query(user, f"SELECT * FROM ({es['sql']}) AS __t LIMIT 0", limit=1, params=es["params"])
    return res["columns"], res["types"]


def register(app, plat, user, need, audit):
    viewer = need("viewer")

    @app.middleware("http")
    async def basic_challenge(request: Request, call_next):
        # BI tools only prompt for credentials when the 401 carries a Basic challenge
        resp = await call_next(request)
        if resp.status_code == 401 and request.url.path.startswith("/odata"):
            resp.headers["WWW-Authenticate"] = 'Basic realm="Tessera OData (API token as password)"'
        return resp

    def base(request: Request) -> str:
        root = str(plat.settings.base_url or "").rstrip("/") or str(request.base_url).rstrip("/")
        return root + "/odata"

    def odata_json(body: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"OData-Version": "4.0", "Content-Type": "application/json;odata.metadata=minimal"})

    def error(code: int, msg: str):
        return odata_json({"error": {"code": str(code), "message": msg}}, code)

    @app.get("/odata")
    @app.get("/odata/")
    def service(request: Request, u: dict = Depends(viewer)):
        sets = entity_sets(plat, u)
        return odata_json({"@odata.context": base(request) + "/$metadata",
                           "value": [{"name": n, "kind": "EntitySet", "url": n, "title": s["title"]} for n, s in sets.items()]})

    @app.get("/odata/$metadata")
    def metadata(request: Request, u: dict = Depends(viewer)):
        sets = entity_sets(plat, u)
        types, container = [], []
        for n, s in sets.items():
            try:
                cols, tys = shape(plat, u, s)
            except Exception:  # noqa: BLE001, S112  # nosec B112
                continue
            props = ['<Property Name="__row" Type="Edm.Int64" Nullable="false"/>'] + [
                f'<Property Name="{escape(safe_name(c))}" Type="{edm(t)}"' + (' Precision="38" Scale="variable"' if edm(t) == "Edm.Decimal" else "") + "/>"
                for c, t in zip(cols, tys)]
            desc = f'<Annotation Term="Org.OData.Core.V1.Description" String="{escape(s["description"] or s["title"], {chr(34): "&quot;"})}"/>'
            types.append(f'<EntityType Name="{n}"><Key><PropertyRef Name="__row"/></Key>{"".join(props)}{desc}</EntityType>')
            container.append(f'<EntitySet Name="{n}" EntityType="{NS}.{n}"/>')
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
               f'<Schema Namespace="{NS}" xmlns="http://docs.oasis-open.org/odata/ns/edm">{"".join(types)}'
               f'<EntityContainer Name="Container">{"".join(container)}</EntityContainer></Schema>'
               '</edmx:DataServices></edmx:Edmx>')
        return Response(xml, media_type="application/xml", headers={"OData-Version": "4.0"})

    def run(request: Request, u: dict, name: str, count_only: bool = False):
        sets = entity_sets(plat, u)
        if name not in sets:
            return error(404, f"No entity set named {name}.")
        s = sets[name]
        qp = request.query_params
        cols, tys = shape(plat, u, s)
        alias = {safe_name(c): c for c in cols}
        params = list(s["params"])
        where = ""
        try:
            if qp.get("$filter"):
                fp = FilterParser(qp["$filter"], {a: t for a, t in zip(alias, tys)})
                where = " WHERE " + fp.parse()
                params += fp.params
            sel = [x.strip() for x in qp.get("$select", "").split(",") if x.strip()]
            for x in sel:
                if x not in alias and x != "__row":
                    raise FilterError(f"Unknown property '{x}' in $select")
            order = " ORDER BY " + order_by(qp["$orderby"], alias) if qp.get("$orderby") else ""
        except FilterError as e:
            return error(400, public_message(e))
        inner = f"SELECT {', '.join(f'{qi(c)} AS {qi(a)}' for a, c in alias.items())} FROM ({s['sql']}) AS __src"
        if count_only or qp.get("$count", "").lower() == "true":
            n = plat.user_query(u, f"SELECT count(*) FROM ({inner}) AS __t{where}", limit=1, params=params)["rows"][0][0]
            if count_only:
                return Response(str(n), media_type="text/plain", headers={"OData-Version": "4.0"})
        else:
            n = None
        try:
            skip = max(0, int(qp.get("$skiptoken") or qp.get("$skip") or 0))
            top = int(qp["$top"]) if qp.get("$top") else None
        except ValueError:
            return error(400, "$top and $skip must be whole numbers.")
        prefer = re.search(r"odata\.maxpagesize=(\d+)", request.headers.get("prefer", ""))
        page = min(int(prefer.group(1)) if prefer else PAGE, MAX_PAGE)
        start_skip = int(qp.get("$skip") or 0) if qp.get("$skiptoken") else skip
        remaining = None if top is None else max(0, top - (skip - start_skip))
        take = page if remaining is None else min(page, remaining)
        proj = ", ".join(qi(x) for x in sel if x != "__row") if sel else "*"
        sql = f"SELECT {proj} FROM ({inner}) AS __t{where}{order} LIMIT {int(take) + 1} OFFSET {int(skip)}"
        try:
            res = plat.user_query(u, sql, limit=take + 1, params=params)
        except QueryError as e:
            return error(400, public_message(e))
        rows = res["rows"][:take]
        more = len(res["rows"]) > take and (remaining is None or take < remaining)
        out_cols = res["columns"]
        value = []
        for i, r in enumerate(rows):
            rec = {"__row": skip + i} if (not sel or "__row" in sel) else {}
            rec.update(dict(zip(out_cols, r)))
            value.append(rec)
        body = {"@odata.context": f"{base(request)}/$metadata#{name}" + (f"({','.join(sel)})" if sel else "")}
        if n is not None:
            body["@odata.count"] = n
        body["value"] = value
        if more:
            keep = {k: v for k, v in qp.items() if k not in ("$skiptoken",)}
            keep["$skiptoken"] = str(skip + take)
            body["@odata.nextLink"] = f"{base(request)}/{quote(name)}?{urlencode(keep)}"
        audit(u, "odata.read", name, {"rows": len(value), "skip": skip})
        return odata_json(body)

    @app.get("/odata/{name}/$count")
    def count(name: str, request: Request, u: dict = Depends(viewer)):
        return run(request, u, name, count_only=True)

    @app.get("/odata/{name}")
    def entity(name: str, request: Request, u: dict = Depends(viewer)):
        if name == "$metadata":
            raise HTTPException(404)
        return run(request, u, name)
