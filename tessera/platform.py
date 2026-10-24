"""Platform services shared by the API, the orchestrator and the AI agents."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import connectors, pii, quality, semantic
from .config import Settings
from .errors import UserError
from .notifications import notify
from .security import Vault, restricted
from .store import Store, now, parse_ts
from .warehouse import SCHEMAS, QueryError, Warehouse, check_ident, jsonable, qi, split_fqn, sql_literal


class NotFound(UserError):
    pass


class Platform:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.meta_path)
        self.wh = Warehouse(settings.warehouse_path)
        self.vault = Vault(settings.data_dir)

    def close(self):
        try:
            self.wh.db.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ governed querying
    def rls_for(self, user: dict) -> dict[str, str] | None:
        """Row filters for this user, keyed by serving table. Engineers and admins are not filtered."""
        if not restricted(user["role"]):
            return None
        attrs = user.get("attributes") or {}
        if isinstance(attrs, str):
            attrs = json.loads(attrs or "{}")
        out: dict[str, list[str]] = {}
        for pol in self.store.all("SELECT * FROM rls_policies"):
            vals = attrs.get(pol["attribute"])
            vals = vals if isinstance(vals, list) else ([vals] if vals not in (None, "") else [])
            if "*" in [str(v) for v in vals]:
                continue
            pred = "FALSE" if not vals else f"CAST({qi(pol['column_name'])} AS VARCHAR) IN ({', '.join(sql_literal(str(v)) for v in vals)})"
            out.setdefault(pol["dataset"], []).append(pred)
        return {t: " AND ".join(f"({p})" for p in preds) for t, preds in out.items()}

    def rls_protected(self) -> set[str]:
        return {r["dataset"] for r in self.store.all("SELECT DISTINCT dataset FROM rls_policies")}

    def user_query(self, user: dict, sql: str, limit: int = 10_000, timeout: float | None = None, params=None) -> dict:
        return self.wh.query(sql, restricted=restricted(user["role"]), limit=limit,
                             timeout=timeout or self.settings.query_timeout_seconds, params=params, rls=self.rls_for(user))

    def model(self, mid: int) -> dict:
        m = self.store.one("SELECT * FROM semantic_models WHERE id=?", (mid,))
        if not m:
            raise NotFound("Semantic model not found")
        return m

    def run_model(self, user: dict, model: dict, spec: dict, compare: bool = False) -> dict:
        sql, params, fields = semantic.compile_query(model, spec)
        res = self.user_query(user, sql, limit=min(int(spec.get("limit") or 5000), 50_000), params=params)
        res["fields"] = fields
        res["sql"] = sql
        if compare:
            prev = semantic.previous_period(spec, model)
            if prev:
                psql, pparams, _ = semantic.compile_query(model, prev)
                res["previous"] = self.user_query(user, psql, params=pparams)["rows"]
                res["previous_filters"] = prev["filters"]
        return res

    def run_saved(self, user: dict, q: dict, overrides: dict | None = None) -> dict:
        """Run a saved query (SQL or model-based) with optional dashboard filters and drill overrides."""
        overrides = overrides or {}
        if q.get("kind") == "model":
            model = self.model(q["model_id"])
            spec = dict(q.get("spec") or {})
            dims = {d["name"] for d in model["dimensions"]}
            extra = [f for f in overrides.get("filters") or [] if f.get("model_id") in (None, model["id"]) and f.get("field") in dims]
            spec["filters"] = list(spec.get("filters") or []) + [{k: v for k, v in f.items() if k != "model_id"} for f in extra]
            if overrides.get("grains"):
                spec["grains"] = {**(spec.get("grains") or {}), **overrides["grains"]}
            if overrides.get("dimensions"):
                spec["dimensions"] = [d for d in overrides["dimensions"] if d in dims]
            return self.run_model(user, model, spec, compare=bool((q.get("viz") or {}).get("compare") or overrides.get("compare")))
        return self.user_query(user, q["sql"], limit=int(overrides.get("limit") or 5000))

    # ------------------------------------------------------------ connections
    def connection(self, cid: int) -> dict:
        c = self.store.one("SELECT * FROM connections WHERE id=?", (cid,))
        if not c:
            raise NotFound("Connection not found")
        return c

    def connector(self, cid: int) -> connectors.Connector:
        c = self.connection(cid)
        return connectors.make(c["type"], c["config"], self.vault.decrypt(c["secrets"]), self.settings)

    def public_connection(self, c: dict) -> dict:
        """A connection without its secrets; only the names of the secret fields that are set."""
        t = connectors.CONNECTOR_TYPES.get(c["type"], {})
        try:
            saved = self.vault.decrypt(c["secrets"]) if c.get("secrets") else {}
        except Exception:  # noqa: BLE001 - an unreadable secret is reported as not set
            saved = {}
        keys = [f["key"] for f in t.get("fields", []) if f.get("secret")]
        return {k: v for k, v in c.items() if k != "secrets"} | {"secret_fields_set": [k for k in keys if saved.get(k)] + sorted(set(saved) - set(keys))}

    # ------------------------------------------------------------ datasets / catalog
    def dataset(self, schema: str, name: str, create: bool = False) -> dict | None:
        d = self.store.one("SELECT * FROM datasets WHERE schema_name=? AND name=?", (schema, name))
        if d is None and create:
            self.store.insert("datasets", {"schema_name": schema, "name": name, "columns": "[]", "tags": "[]", "meta": "{}"})
            d = self.store.one("SELECT * FROM datasets WHERE schema_name=? AND name=?", (schema, name))
        return d

    def dataset_by_id(self, did: int) -> dict:
        d = self.store.one("SELECT * FROM datasets WHERE id=?", (did,))
        if not d:
            raise NotFound("Dataset not found")
        return d

    def refresh_columns(self, d: dict) -> dict:
        """Sync catalog columns with the warehouse, keeping descriptions and classifications."""
        old = {c["name"]: c for c in d.get("columns") or []}
        cols = []
        for c in self.wh.columns(d["schema_name"], d["name"]):
            prev = old.get(c["name"], {})
            cols.append({**prev, "name": c["name"], "type": c["type"], "description": prev.get("description", ""),
                         "pii": prev.get("pii"), "level": prev.get("level", "internal"), "mask": prev.get("mask", "none")})
        rc = self.wh.row_count(d["schema_name"], d["name"])
        self.store.update("datasets", d["id"], {"columns": json.dumps(cols), "row_count": rc})
        return self.dataset_by_id(d["id"])

    def sync_catalog(self) -> None:
        """Register warehouse tables that are not yet in the catalog (e.g. created by transforms)."""
        if getattr(self, "_synced_version", None) == self.wh.version:
            return
        self._synced_version = self.wh.version
        known = {(r["schema_name"], r["name"]) for r in self.store.all("SELECT schema_name, name FROM datasets")}
        for t in self.wh.list_tables():
            if t["schema"] in SCHEMAS and (t["schema"], t["name"]) not in known:
                d = self.dataset(t["schema"], t["name"], create=True)
                self.refresh_columns(d)
        live = {(t["schema"], t["name"]) for t in self.wh.list_tables()}
        for r in self.store.all("SELECT id, schema_name, name FROM datasets"):
            if (r["schema_name"], r["name"]) not in live:
                self.store.exec("DELETE FROM datasets WHERE id=?", (r["id"],))

    # ------------------------------------------------------------ ingestion
    def ingest(self, connection_id: int, source: dict, target: str, mode: str = "replace", pipeline_id: int | None = None) -> dict:
        schema, name = split_fqn(target, "raw")
        if schema == "serving":
            raise QueryError("Ingest into raw or curated. Serving is written only by Publish.")
        if mode not in ("replace", "append", "incremental", "merge"):
            raise QueryError("Mode must be replace, append, incremental or merge")
        conn = self.connection(connection_id)
        if source.get("object") and conn["type"] == "rest":
            from . import saas  # application templates know which column shows a change and which field is the key
            cur = source.get("cursor_column") or saas.default_cursor(conn["config"] or {}, source["object"])
            if cur:
                source = {**source, "cursor_column": cur}
            if mode == "merge" and not source.get("key"):
                source = {**source, "key": saas.default_key(conn["config"] or {}, source["object"])}
        keys = [k.strip() for k in (source.get("key") or []) if str(k).strip()] if isinstance(source.get("key"), list) \
            else [k.strip() for k in str(source.get("key") or "").split(",") if k.strip()]
        if mode == "merge" and not keys:
            raise QueryError("Merge loads need the key column(s) that identify a row, for example id.")
        cx = self.connector(connection_id)
        d = self.dataset(schema, name, create=True)
        meta = d.get("meta") or {}
        watermark = meta.get("watermark") if mode in ("incremental", "merge") else None
        if watermark is not None and meta.get("watermark_type") in ("timestamp", "date"):
            watermark = datetime.fromisoformat(watermark) if meta["watermark_type"] == "timestamp" else datetime.fromisoformat(watermark).date()
        if mode == "incremental" and not source.get("cursor_column"):
            raise QueryError("Incremental loads need a cursor_column (for example updated_at).")
        read_source = {k: v for k, v in source.items() if k != "key"}
        table = cx.read(read_source, watermark)
        loaded = 0
        before = {c["name"]: c["type"] for c in self.wh.columns(schema, name)} if self.wh.exists(schema, name) else None
        if table.num_rows or mode == "replace":
            if table.num_columns == 0:
                raise QueryError("Source returned no columns.")
            loaded = self.wh.load_arrow(table, schema, name, mode if mode in ("replace", "merge") else "append", keys or None)
        if before is not None and table.num_columns:
            self.record_drift(d, before, table.column_names, pipeline_id, meta.get("source_columns"))
        if table.num_columns:
            meta["source_columns"] = table.column_names
        if mode in ("incremental", "merge") and table.num_rows and source.get("cursor_column") in table.column_names:
            col = table.column(source["cursor_column"])
            mx = pc.max(col).as_py()
            if mx is not None:
                meta["watermark"] = jsonable(mx)
                meta["watermark_type"] = "timestamp" if pa.types.is_timestamp(col.type) else "date" if pa.types.is_date(col.type) else "value"
        meta["last_mode"] = mode
        self.store.update("datasets", d["id"], {"connection_id": connection_id, "source": json.dumps(source),
                                                 "last_loaded_at": now(), "meta": json.dumps(meta)})
        d = self.refresh_columns(self.dataset_by_id(d["id"]))
        src_label = f"source:{conn['name']}/{source.get('table') or source.get('object') or source.get('path') or 'query'}"
        self.add_lineage(src_label, f"{schema}.{name}", "ingest", pipeline_id)
        return {"dataset": f"{schema}.{name}", "rows_loaded": loaded, "row_count": d["row_count"], "watermark": meta.get("watermark")}

    def record_drift(self, d: dict, before: dict, incoming: list[str], pipeline_id: int | None = None,
                     sent_before: list[str] | None = None) -> list[dict]:
        """Compare a table's columns before and after a load and record what changed: columns the source added, columns
        it stopped sending, and columns whose type changed. Owners and admins are told, with the downstream impact."""
        after = {c["name"]: c["type"] for c in self.wh.columns(d["schema_name"], d["name"])}
        changes = [{"change": "added", "column_name": c, "old_type": None, "new_type": t} for c, t in after.items() if c not in before]
        was_sent = set(sent_before) if sent_before is not None else set(before)  # report a dropped column once
        changes += [{"change": "removed", "column_name": c, "old_type": t, "new_type": None} for c, t in before.items()
                    if c not in incoming and c in was_sent]
        changes += [{"change": "retyped", "column_name": c, "old_type": before[c], "new_type": t} for c, t in after.items()
                    if c in before and before[c] != t]
        if not changes:
            return []
        ts = now()
        for ch in changes:
            self.store.insert("schema_changes", {"dataset_id": d["id"], "ts": ts, "pipeline_id": pipeline_id, **ch})
        fqn = f"{d['schema_name']}.{d['name']}"
        downstream = sorted({r["dst"] for r in self.store.all("SELECT dst FROM lineage WHERE src=?", (fqn,))})
        words = {"added": "new", "removed": "no longer sent", "retyped": "type changed"}
        body = "; ".join(f"{c['column_name']} ({words[c['change']]}" + (f": {c['old_type']} → {c['new_type']}" if c["change"] == "retyped" else "") + ")"
                         for c in changes[:12])
        if downstream:
            body += f". Used by {', '.join(downstream[:6])}{' and more' if len(downstream) > 6 else ''}: check them."
        breaking = any(c["change"] != "added" for c in changes)
        users = {u["id"] for u in self.store.all("SELECT id FROM users WHERE active=1 AND (role='admin' OR lower(email)=lower(?))", (d.get("owner") or "",))}
        for uid in users:
            notify(self, uid, f"Schema changed in {fqn}", body, f"#/catalog/{d['id']}", "warn" if breaking else "info")
        return changes

    def schema_changes(self, dataset_id: int, limit: int = 100) -> list[dict]:
        return self.store.all("SELECT ts, change, column_name, old_type, new_type, pipeline_id FROM schema_changes WHERE dataset_id=? "
                              "ORDER BY id DESC LIMIT ?", (dataset_id, limit))

    def load_table(self, table: pa.Table, target: str, source_label: str) -> dict:
        schema, name = split_fqn(target, "raw")
        self.wh.load_arrow(table, schema, name, "replace")
        d = self.dataset(schema, name, create=True)
        self.store.update("datasets", d["id"], {"last_loaded_at": now(), "source": json.dumps({"upload": source_label})})
        self.add_lineage(f"upload:{source_label}", f"{schema}.{name}", "ingest", None)
        return self.refresh_columns(self.dataset_by_id(d["id"]))

    def transform(self, sql: str, target: str, pipeline_id: int | None = None) -> dict:
        schema, name = split_fqn(target, "curated")
        refs = self.wh.referenced_tables(sql)
        n = self.wh.create_table_as(sql, schema, name)
        d = self.dataset(schema, name, create=True)
        self.store.update("datasets", d["id"], {"last_loaded_at": now(), "source": json.dumps({"sql": sql})})
        self.refresh_columns(self.dataset_by_id(d["id"]))
        for r in refs:
            self.add_lineage(r, f"{schema}.{name}", "transform", pipeline_id)
        return {"dataset": f"{schema}.{name}", "row_count": n, "inputs": refs}

    # ------------------------------------------------------------ quality
    def rules(self, dataset_id: int) -> list[dict]:
        rows = self.store.all("SELECT * FROM rules WHERE dataset_id=? ORDER BY id", (dataset_id,))
        for r in rows:
            r["description_auto"] = quality.describe(r)
            last = self.store.one("SELECT * FROM rule_results WHERE rule_id=? ORDER BY id DESC LIMIT 1", (r["id"],))
            r["last_result"] = last
        return rows

    def add_rule(self, dataset_id: int, rule: dict, created_by: str) -> int:
        d = self.dataset_by_id(dataset_id)
        cols = [c["name"] for c in self.wh.columns(d["schema_name"], d["name"])]
        quality.validate_rule(rule, cols)
        if rule["kind"] == "custom_sql":
            self.wh.validate_select(rule["params"]["sql"], restricted=False)
        return self.store.insert("rules", {"dataset_id": dataset_id, "column_name": rule.get("column_name"), "kind": rule["kind"],
                                           "params": json.dumps(rule.get("params") or {}), "severity": rule.get("severity", "error"),
                                           "enabled": 1, "description": rule.get("description", ""), "created_by": created_by,
                                           "created_at": now()})

    def run_quality(self, dataset_id: int, pipeline_run_id: int | None = None) -> dict:
        d = self.dataset_by_id(dataset_id)
        results = []
        for r in self.store.all("SELECT * FROM rules WHERE dataset_id=? AND enabled=1 ORDER BY id", (dataset_id,)):
            res = quality.run_rule(self.wh, d["schema_name"], d["name"], r)
            self.store.insert("rule_results", {"rule_id": r["id"], "run_at": now(), "passed": int(res["passed"]),
                                               "failing": res["failing"], "total": res["total"],
                                               "sample": json.dumps(res["sample"]) if res["sample"] else None,
                                               "error": res["error"], "pipeline_run_id": pipeline_run_id})
            results.append({"rule_id": r["id"], "kind": r["kind"], "severity": r["severity"],
                            "description": quality.describe(r), **res})
        errors = [r for r in results if not r["passed"] and r["severity"] == "error"]
        warns = [r for r in results if not r["passed"] and r["severity"] == "warn"]
        return {"dataset": f"{d['schema_name']}.{d['name']}", "score": quality.score(results), "rules": len(results),
                "failed_errors": len(errors), "failed_warnings": len(warns), "results": results}

    def latest_quality(self, dataset_id: int) -> dict:
        rows = self.store.all(
            "SELECT r.kind, r.severity, rr.passed, rr.failing, rr.total, rr.error, rr.run_at FROM rules r "
            "JOIN rule_results rr ON rr.id = (SELECT max(id) FROM rule_results WHERE rule_id=r.id) "
            "WHERE r.dataset_id=? AND r.enabled=1", (dataset_id,))
        enabled = self.store.scalar("SELECT count(*) FROM rules WHERE dataset_id=? AND enabled=1", (dataset_id,))
        return {"score": quality.score(rows), "rules": enabled, "evaluated": len(rows),
                "failing": sum(1 for r in rows if not r["passed"]),
                "last_run": max((r["run_at"] for r in rows), default=None)}

    # ------------------------------------------------------------ PII & publishing
    def scan_pii(self, dataset_id: int) -> list[dict]:
        d = self.dataset_by_id(dataset_id)
        findings = pii.scan(self.wh, d["schema_name"], d["name"])
        by = {f["column"]: f for f in findings}
        cols = []
        for c in self.refresh_columns(d)["columns"]:
            f = by.get(c["name"], {})
            manual = c.get("pii_source") == "manual"
            if not manual:
                c.update({"pii": f.get("pii"), "level": f.get("level", "internal"), "mask": f.get("mask", "none"),
                          "pii_method": f.get("method"), "pii_ratio": f.get("ratio")})
            cols.append(c)
        self.store.update("datasets", d["id"], {"columns": json.dumps(cols), "pii_scanned_at": now()})
        return findings

    def set_column(self, dataset_id: int, column: str, patch: dict) -> dict:
        d = self.dataset_by_id(dataset_id)
        cols = d["columns"]
        found = False
        for c in cols:
            if c["name"] == column:
                found = True
                if "description" in patch:
                    c["description"] = str(patch["description"])[:2000]
                if "mask" in patch:
                    if patch["mask"] not in pii.MASKS:
                        raise QueryError("Unknown masking policy")
                    c["mask"] = patch["mask"]
                    c["pii_source"] = "manual"
                if "level" in patch:
                    if patch["level"] not in pii.LEVELS:
                        raise QueryError("Unknown sensitivity level")
                    c["level"] = patch["level"]
                    c["pii_source"] = "manual"
        if not found:
            raise NotFound("Column not found")
        self.store.update("datasets", d["id"], {"columns": json.dumps(cols)})
        return self.dataset_by_id(d["id"])

    def publish(self, dataset_id: int, pipeline_id: int | None = None) -> dict:
        d = self.dataset_by_id(dataset_id)
        if d["schema_name"] == "serving":
            raise QueryError("Dataset is already in serving.")
        if not d.get("pii_scanned_at"):
            self.scan_pii(dataset_id)
            d = self.dataset_by_id(dataset_id)
        n = pii.publish(self.wh, d["schema_name"], d["name"], d["columns"], self.vault.mask_salt)
        self.store.update("datasets", d["id"], {"published_at": now()})
        s = self.dataset("serving", d["name"], create=True)
        masked_cols = [{**c, "mask": "none", "masked_from": c.get("mask")} for c in d["columns"]]
        self.store.update("datasets", s["id"], {"description": d.get("description") or "", "owner": d.get("owner") or "",
                                                 "columns": json.dumps(masked_cols), "last_loaded_at": now(),
                                                 "pii_scanned_at": now(), "source": json.dumps({"published_from": f"{d['schema_name']}.{d['name']}"}),
                                                 "freshness_sla_hours": d.get("freshness_sla_hours"), "row_count": n})
        self.add_lineage(f"{d['schema_name']}.{d['name']}", f"serving.{d['name']}", "publish", pipeline_id)
        masked = [c["name"] for c in d["columns"] if c.get("mask", "none") != "none"]
        return {"dataset": f"serving.{d['name']}", "row_count": n, "masked_columns": masked}

    def export(self, dataset_id: int, fmt: str = "parquet") -> dict:
        d = self.dataset_by_id(dataset_id)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{d['schema_name']}.{d['name']}-{ts}"
        path = self.settings.exports_dir / f"{base}.{fmt}"
        reader = self.wh.arrow(d["schema_name"], d["name"])
        if fmt == "parquet":
            w = None
            for b in reader:
                if w is None:
                    w = pq.ParquetWriter(path, b.schema, compression="zstd")
                w.write_batch(b)
            if w is None:
                pq.write_table(reader.read_all(), path)
            else:
                w.close()
        elif fmt == "csv":
            import pyarrow.csv as pacsv
            with pacsv.CSVWriter(path, reader.schema) as w:
                for b in reader:
                    w.write_batch(b)
        else:
            raise QueryError("Export format must be parquet or csv")
        card = self.datacard(dataset_id)
        (self.settings.exports_dir / f"{base}.datacard.json").write_text(json.dumps(card, indent=2))
        return {"file": path.name, "datacard": f"{base}.datacard.json", "bytes": path.stat().st_size}

    # ------------------------------------------------------------ lineage
    def add_lineage(self, src: str, dst: str, kind: str, pipeline_id: int | None) -> None:
        self.store.exec("INSERT INTO lineage(src,dst,kind,pipeline_id,updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(src,dst,kind) DO UPDATE SET updated_at=excluded.updated_at, pipeline_id=COALESCE(excluded.pipeline_id, lineage.pipeline_id)",
                        (src, dst, kind, pipeline_id, now()))

    def lineage(self, node: str, depth: int = 6) -> dict:
        edges, seen = [], set()
        frontier = [(node, "up"), (node, "down")]
        for _ in range(depth):
            nxt = []
            for n, direction in frontier:
                q = "SELECT * FROM lineage WHERE dst=?" if direction == "up" else "SELECT * FROM lineage WHERE src=?"
                for e in self.store.all(q, (n,)):
                    if e["id"] in seen:
                        continue
                    seen.add(e["id"])
                    edges.append({"src": e["src"], "dst": e["dst"], "kind": e["kind"], "pipeline_id": e["pipeline_id"]})
                    nxt.append((e["src"] if direction == "up" else e["dst"], direction))
            frontier = nxt
        nodes = sorted({node, *[e["src"] for e in edges], *[e["dst"] for e in edges]})
        return {"node": node, "nodes": nodes, "edges": edges}

    # ------------------------------------------------------------ AI readiness
    def readiness(self, dataset_id: int) -> dict:
        d = self.dataset_by_id(dataset_id)
        cols = d.get("columns") or []
        q = self.latest_quality(dataset_id)
        checks = []

        def add(key, label, weight, value, detail, fix):
            checks.append({"key": key, "label": label, "weight": weight, "score": round(max(0.0, min(1.0, value)), 3),
                           "detail": detail, "fix": fix})

        if q["evaluated"]:
            add("quality", "Quality rules pass", 30, q["score"] or 0, f"{q['evaluated']} rules evaluated, {q['failing']} failing on last run",
                "Fix failing rows upstream or adjust rules in Quality.")
        elif q["rules"]:
            add("quality", "Quality rules pass", 30, 0, f"{q['rules']} rules defined but not run yet", "Run quality checks or add a quality step to a pipeline.")
        else:
            add("quality", "Quality rules pass", 30, 0, "No quality rules yet", "Open Quality and accept suggested rules.")
        try:
            summ = self.wh.summarize(d["schema_name"], d["name"])
            comp = 1 - (sum(float(s.get("null_percentage") or 0) for s in summ) / (100 * max(1, len(summ))))
        except Exception:  # noqa: BLE001
            comp = 0
        add("completeness", "Values are complete", 15, comp, f"{comp:.1%} of cells are filled", "Backfill or default empty fields.")
        described = sum(1 for c in cols if (c.get("description") or "").strip())
        doc = (0.4 if (d.get("description") or "").strip() else 0) + 0.6 * (described / max(1, len(cols)))
        add("documentation", "Documented for people and models", 15, doc,
            f"Table description {'present' if d.get('description') else 'missing'}; {described}/{len(cols)} columns described",
            "Describe the table and its columns, or ask the Readiness agent to draft them.")
        if d["schema_name"] == "serving":
            sec = 1.0 if d.get("pii_scanned_at") else 0.5
            sdet = "Published masked copy"
        elif not d.get("pii_scanned_at"):
            sec, sdet = 0.0, "Not scanned for personal data"
        else:
            sensitive = [c for c in cols if c.get("pii")]
            unmasked = [c["name"] for c in sensitive if c.get("mask", "none") == "none"]
            sec = 0.5 + 0.25 * (not unmasked) + 0.25 * bool(d.get("published_at"))
            sdet = f"{len(sensitive)} sensitive column(s), {len(unmasked)} without masking; {'published' if d.get('published_at') else 'not published'}"
        add("security", "Personal data classified and protected", 15, sec, sdet, "Run a PII scan, set masking, then Publish.")
        sla = d.get("freshness_sla_hours") or 24
        lt = parse_ts(d.get("last_loaded_at"))
        if lt:
            age_h = (datetime.now(timezone.utc) - lt).total_seconds() / 3600
            fr = 1.0 if age_h <= sla else max(0.0, 1 - (age_h - sla) / (sla * 3))
            fdet = f"Loaded {age_h:.1f} h ago (target {sla:g} h)"
        else:
            fr, fdet = 0, "Never loaded through Tessera"
        add("freshness", "Fresh within its target", 10, fr, fdet, "Schedule the pipeline that loads it.")
        node = f"{d['schema_name']}.{d['name']}"
        has_lineage = bool(self.store.one("SELECT 1 FROM lineage WHERE dst=?", (node,)))
        add("lineage", "Lineage is known", 10, 1.0 if has_lineage else 0, "Upstream source recorded" if has_lineage else "No upstream recorded",
            "Load it with a connector or pipeline so Tessera records where it came from.")
        dup = 1.0
        idc = [c["name"] for c in cols if c["name"].lower() == "id" or c["name"].lower().endswith("_id")]
        if idc and d.get("row_count"):
            try:
                distinct = self.wh.stat(("distinct", d["schema_name"], d["name"], idc[0]),
                                        lambda: self.wh.internal(f"SELECT count(DISTINCT {qi(idc[0])}) FROM {qi(d['schema_name'])}.{qi(d['name'])}")[0][0])
                dup = distinct / d["row_count"]
            except Exception:  # noqa: BLE001
                pass
        add("uniqueness", "Records are not duplicated", 5, dup, f"Key '{idc[0]}' {dup:.1%} distinct" if idc else "No key column detected",
            "Deduplicate on the business key in a transform step.")
        total = sum(c["weight"] * c["score"] for c in checks)
        grade = "AI-ready" if total >= 85 else "Nearly ready" if total >= 65 else "Needs work"
        return {"dataset": node, "score": round(total, 1), "grade": grade, "checks": checks}

    def datacard(self, dataset_id: int) -> dict:
        d = self.dataset_by_id(dataset_id)
        node = f"{d['schema_name']}.{d['name']}"
        return {"name": node, "description": d.get("description"), "owner": d.get("owner"), "rows": d.get("row_count"),
                "last_loaded_at": d.get("last_loaded_at"),
                "columns": [{k: c.get(k) for k in ("name", "type", "description", "pii", "level", "mask")} for c in d.get("columns") or []],
                "quality": self.latest_quality(dataset_id), "readiness": self.readiness(dataset_id),
                "lineage": self.lineage(node), "generated_at": now(), "generator": "Tessera"}

    # ------------------------------------------------------------ overview
    def overview(self) -> dict:
        ds = self.store.all("SELECT id, schema_name, name, row_count, last_loaded_at FROM datasets ORDER BY schema_name, name")
        scores = []
        for d in ds:
            try:
                scores.append({"id": d["id"], "dataset": f"{d['schema_name']}.{d['name']}", **{k: v for k, v in self.readiness(d["id"]).items() if k in ("score", "grade")},
                               "quality": self.latest_quality(d["id"])["score"], "rows": d["row_count"]})
            except Exception:  # noqa: BLE001
                pass
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        runs = self.store.all("SELECT substr(created_at,1,10) day, status, count(*) n FROM pipeline_runs WHERE created_at>=? GROUP BY 1,2 ORDER BY 1", (since,))
        return {
            "datasets": len(ds), "connections": self.store.scalar("SELECT count(*) FROM connections"),
            "pipelines": self.store.scalar("SELECT count(*) FROM pipelines"),
            "rules": self.store.scalar("SELECT count(*) FROM rules WHERE enabled=1"),
            "published": self.store.scalar("SELECT count(*) FROM datasets WHERE schema_name='serving'"),
            "rows": sum(d["row_count"] or 0 for d in ds if d["schema_name"] != "serving"),
            "readiness": scores, "runs_by_day": runs,
            "recent_runs": self.store.all("SELECT r.id, r.status, r.trigger, r.created_at, r.finished_at, p.name pipeline FROM pipeline_runs r "
                                          "JOIN pipelines p ON p.id=r.pipeline_id ORDER BY r.id DESC LIMIT 8"),
        }


_: Any = (check_ident,)
