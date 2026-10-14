"""Deployment bundles: export a workspace definition as JSON and import it elsewhere (dev → test → prod, or into Git).

Bundles hold definitions only: connections without secrets, pipelines, quality rules, semantic models, saved queries,
dashboards, alerts' conditions are excluded (they belong to people), glossary and row-level security policies.
References between items are written by name so they resolve on the target install.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import connectors
from .store import now

BUNDLE_VERSION = 1


def export_bundle(p) -> dict:
    s = p.store
    conns = {c["id"]: c for c in s.all("SELECT * FROM connections")}
    queries = {q["id"]: q for q in s.all("SELECT * FROM queries")}
    models = {m["id"]: m for m in s.all("SELECT * FROM semantic_models")}
    pipes = {x["id"]: x["name"] for x in s.all("SELECT id, name FROM pipelines")}
    out: dict[str, Any] = {"tessera_bundle": BUNDLE_VERSION, "exported_at": now()}
    up = p.settings.uploads_dir.resolve()

    def portable(cfg: dict) -> dict:
        cfg = dict(cfg or {})
        for k in ("base_path", "path"):
            v = cfg.get(k)
            if v and Path(v).is_absolute():
                try:
                    cfg[k] = str(Path(v).resolve().relative_to(up))
                except ValueError:
                    pass
        return cfg
    out["connections"] = [{"name": c["name"], "type": c["type"], "config": portable(c["config"]),
                           "secret_fields": [f["key"] for f in connectors.CONNECTOR_TYPES.get(c["type"], {}).get("fields", []) if f.get("secret")]}
                          for c in conns.values()]
    pl = []
    for x in s.all("SELECT * FROM pipelines ORDER BY id"):
        steps = []
        for st in x["steps"]:
            st = dict(st)
            if st.get("type") == "ingest" and st.get("connection_id") in conns:
                st["connection"] = conns[st.pop("connection_id")]["name"]
            if st.get("type") == "run_pipeline" and st.get("pipeline_id") in pipes:
                st["pipeline"] = pipes[st.pop("pipeline_id")]
            steps.append(st)
        pl.append({"name": x["name"], "description": x["description"], "schedule": x["schedule"], "enabled": bool(x["enabled"]),
                   "webhook_url": x["webhook_url"], "steps": steps})
    out["pipelines"] = pl
    out["rules"] = [{"dataset": f"{r['schema_name']}.{r['name']}", "column_name": r["column_name"], "kind": r["kind"], "params": r["params"],
                     "severity": r["severity"], "enabled": bool(r["enabled"])}
                    for r in s.all("SELECT r.*, d.schema_name, d.name FROM rules r JOIN datasets d ON d.id=r.dataset_id")]
    out["catalog"] = [{"dataset": f"{d['schema_name']}.{d['name']}", "description": d["description"], "owner": d["owner"],
                       "endorsement": d.get("endorsement"), "domain": d.get("domain"), "freshness_sla_hours": d["freshness_sla_hours"],
                       "columns": [{k: c.get(k) for k in ("name", "description", "level", "mask", "pii") if c.get(k) not in (None, "")} for c in d["columns"] or []]}
                      for d in s.all("SELECT * FROM datasets")]
    out["models"] = [{k: m[k] for k in ("name", "description", "base", "joins", "dimensions", "measures", "endorsement")} for m in models.values()]
    out["queries"] = [{"name": q["name"], "kind": q.get("kind") or "sql", "sql": q["sql"], "viz": q["viz"], "spec": q.get("spec"),
                       "model": models[q["model_id"]]["name"] if q.get("model_id") in models else None} for q in queries.values()]
    out["dashboards"] = [{"name": d["name"], "description": d["description"], "refresh_seconds": d["refresh_seconds"], "filters": d.get("filters") or [],
                          "tiles": [{**{k: v for k, v in t.items() if k != "query_id"}, **({"query": queries[t["query_id"]]["name"]} if t.get("query_id") in queries else {})}
                                    for t in d["tiles"]]}
                         for d in s.all("SELECT * FROM dashboards")]
    for d in out["dashboards"]:
        for f in d["filters"]:
            if f.get("model_id") in models:
                f["model"] = models[f.pop("model_id")]["name"]
    out["glossary"] = [{k: g[k] for k in ("term", "definition", "owner", "synonyms", "columns", "domain")} for g in s.all("SELECT * FROM glossary")]
    out["rls_policies"] = [{k: r[k] for k in ("dataset", "column_name", "attribute", "description")} for r in s.all("SELECT * FROM rls_policies")]
    return out


def import_bundle(p, b: dict, mode: str = "skip", who: str = "import") -> dict:
    """mode: 'skip' keeps existing items with the same name, 'overwrite' replaces them."""
    if b.get("tessera_bundle") != BUNDLE_VERSION:
        raise ValueError("This is not a Tessera bundle, or it was made by an incompatible version.")
    s = p.store
    rep = {k: {"created": 0, "updated": 0, "skipped": 0} for k in ("connections", "pipelines", "rules", "catalog", "models", "queries", "dashboards", "glossary", "rls_policies")}
    warnings: list[str] = []

    def upsert(table, key, name, row, kind):
        ex = s.one(f"SELECT id FROM {table} WHERE {key}=?", (name,))
        if ex and mode == "skip":
            rep[kind]["skipped"] += 1
            return ex["id"]
        if ex:
            s.update(table, ex["id"], row)
            rep[kind]["updated"] += 1
            return ex["id"]
        rep[kind]["created"] += 1
        return s.insert(table, {key: name, **row})

    for c in b.get("connections") or []:
        if c["type"] not in connectors.CONNECTOR_TYPES:
            warnings.append(f"Connection '{c['name']}': unknown type {c['type']}, skipped.")
            continue
        existed = s.one("SELECT id FROM connections WHERE name=?", (c["name"],))
        upsert("connections", "name", c["name"], {"type": c["type"], "config": json.dumps(c.get("config") or {}), "updated_at": now(),
                                                   **({} if existed else {"created_at": now(), "secrets": None})}, "connections")
        if c.get("secret_fields") and not existed:
            warnings.append(f"Connection '{c['name']}': enter {', '.join(c['secret_fields'])} on this install (secrets are never exported).")
    conn_ids = {c["name"]: c["id"] for c in s.all("SELECT id, name FROM connections")}
    for d in b.get("catalog") or []:
        schema, name = d["dataset"].split(".", 1)
        row = s.one("SELECT * FROM datasets WHERE schema_name=? AND name=?", (schema, name))
        if not row:
            row_id = s.insert("datasets", {"schema_name": schema, "name": name, "columns": "[]", "tags": "[]", "meta": "{}"})
            row = s.one("SELECT * FROM datasets WHERE id=?", (row_id,))
            rep["catalog"]["created"] += 1
        elif mode == "skip" and (row.get("description") or row.get("endorsement") not in (None, "none")):
            rep["catalog"]["skipped"] += 1
            continue
        else:
            rep["catalog"]["updated"] += 1
        cols = {c["name"]: c for c in row["columns"] or []}
        for c in d.get("columns") or []:
            cols[c["name"]] = {**cols.get(c["name"], {"name": c["name"], "type": ""}), **c}
        s.update("datasets", row["id"], {"description": d.get("description") or "", "owner": d.get("owner") or "", "endorsement": d.get("endorsement") or "none",
                                          "domain": d.get("domain"), "freshness_sla_hours": d.get("freshness_sla_hours"), "columns": json.dumps(list(cols.values()))})
    for r in b.get("rules") or []:
        schema, name = r["dataset"].split(".", 1)
        ds = s.one("SELECT id FROM datasets WHERE schema_name=? AND name=?", (schema, name))
        if not ds:
            warnings.append(f"Rule on {r['dataset']}: dataset not found, skipped.")
            continue
        dup = s.one("SELECT id FROM rules WHERE dataset_id=? AND kind=? AND IFNULL(column_name,'')=IFNULL(?, '')", (ds["id"], r["kind"], r["column_name"]))
        if dup and mode == "skip":
            rep["rules"]["skipped"] += 1
            continue
        if dup:
            s.update("rules", dup["id"], {"params": json.dumps(r["params"]), "severity": r["severity"], "enabled": int(r["enabled"])})
            rep["rules"]["updated"] += 1
        else:
            s.insert("rules", {"dataset_id": ds["id"], "column_name": r["column_name"], "kind": r["kind"], "params": json.dumps(r["params"]),
                               "severity": r["severity"], "enabled": int(r["enabled"]), "created_by": who, "created_at": now()})
            rep["rules"]["created"] += 1
    for m in b.get("models") or []:
        upsert("semantic_models", "name", m["name"], {"description": m.get("description", ""), "base": m["base"], "joins": json.dumps(m.get("joins") or []),
                                                       "dimensions": json.dumps(m["dimensions"]), "measures": json.dumps(m["measures"]),
                                                       "endorsement": m.get("endorsement") or "none", "created_by": who, "created_at": now(), "updated_at": now()}, "models")
    model_ids = {m["name"]: m["id"] for m in s.all("SELECT id, name FROM semantic_models")}
    for q in b.get("queries") or []:
        upsert("queries", "name", q["name"], {"kind": q.get("kind") or "sql", "sql": q.get("sql") or "", "viz": json.dumps(q.get("viz") or {}),
                                              "spec": json.dumps(q["spec"]) if q.get("spec") else None, "model_id": model_ids.get(q.get("model")),
                                              "created_by": who, "created_at": now(), "updated_at": now()}, "queries")
    query_ids = {q["name"]: q["id"] for q in s.all("SELECT id, name FROM queries")}
    for d in b.get("dashboards") or []:
        tiles = []
        for t in d.get("tiles") or []:
            t = dict(t)
            if "query" in t:
                qn = t.pop("query")
                if qn not in query_ids:
                    warnings.append(f"Dashboard '{d['name']}': query '{qn}' missing, tile dropped.")
                    continue
                t["query_id"] = query_ids[qn]
            tiles.append(t)
        filters = []
        for f in d.get("filters") or []:
            f = dict(f)
            if "model" in f:
                f["model_id"] = model_ids.get(f.pop("model"))
            filters.append(f)
        upsert("dashboards", "name", d["name"], {"description": d.get("description", ""), "tiles": json.dumps(tiles), "filters": json.dumps(filters),
                                                  "refresh_seconds": d.get("refresh_seconds") or 0, "created_by": who, "created_at": now(), "updated_at": now()}, "dashboards")
    pending_chain = []
    for x in b.get("pipelines") or []:
        steps = []
        for st in x["steps"]:
            st = dict(st)
            if st.get("type") == "ingest" and "connection" in st:
                cname = st.pop("connection")
                if cname not in conn_ids:
                    warnings.append(f"Pipeline '{x['name']}': connection '{cname}' missing.")
                st["connection_id"] = conn_ids.get(cname, 0)
            if st.get("type") == "run_pipeline" and "pipeline" in st:
                pending_chain.append((x["name"], st["id"], st.pop("pipeline")))
                st["pipeline_id"] = 0
            steps.append(st)
        from .orchestrator import next_run
        upsert("pipelines", "name", x["name"], {"description": x.get("description", ""), "steps": json.dumps(steps), "schedule": x.get("schedule"),
                                                 "enabled": int(x.get("enabled", True)), "webhook_url": x.get("webhook_url"),
                                                 "next_run_at": next_run(x["schedule"]) if x.get("schedule") else None,
                                                 "created_by": who, "created_at": now(), "updated_at": now()}, "pipelines")
    pipe_ids = {r["name"]: r["id"] for r in s.all("SELECT id, name FROM pipelines")}
    for pname, sid, target in pending_chain:
        row = s.one("SELECT * FROM pipelines WHERE name=?", (pname,))
        steps = [{**st, "pipeline_id": pipe_ids.get(target, 0)} if st["id"] == sid else st for st in row["steps"]]
        s.update("pipelines", row["id"], {"steps": json.dumps(steps)})
    for g in b.get("glossary") or []:
        upsert("glossary", "term", g["term"], {"definition": g["definition"], "owner": g.get("owner"), "synonyms": json.dumps(g.get("synonyms") or []),
                                                "columns": json.dumps(g.get("columns") or []), "domain": g.get("domain"), "created_at": now(), "updated_at": now()}, "glossary")
    for r in b.get("rls_policies") or []:
        if s.one("SELECT id FROM rls_policies WHERE dataset=? AND column_name=? AND attribute=?", (r["dataset"], r["column_name"], r["attribute"])):
            rep["rls_policies"]["skipped"] += 1
            continue
        s.insert("rls_policies", {**r, "created_by": who, "created_at": now()})
        rep["rls_policies"]["created"] += 1
    return {"report": rep, "warnings": warnings}
