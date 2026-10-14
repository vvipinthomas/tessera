"""Demo mode: install the sample retail workspace so people can learn every feature, then remove it cleanly when
they switch to their own data.

Everything the sample creates is recorded (metadata row ids per table, warehouse tables, uploaded files). Turning demo
mode off deletes exactly those, plus anything people built on top of demo data (reports and dashboards on the demo
model, alerts on demo datasets), after showing what will go. Users, settings, tokens, SSO and workspaces are untouched.
"""
from __future__ import annotations

import shutil

from . import sample
from .store import now

TRACKED = ("connections", "pipelines", "pipeline_runs", "datasets", "rules", "rule_results", "lineage", "semantic_models", "queries",
           "dashboards", "alerts", "glossary", "notebooks", "kpi_targets", "subscriptions", "deliveries", "dashboard_views", "rls_policies", "streams")


def status(store) -> dict:
    d = store.get_setting("demo") or {}
    return {"enabled": bool(d.get("installed_at")), "installed_at": d.get("installed_at"),
            "counts": {t: len(ids) for t, ids in (d.get("items") or {}).items() if ids}, "tables": d.get("tables", [])}


def install(plat, orch, user: dict) -> dict:
    store = plat.store
    if (store.get_setting("demo") or {}).get("installed_at") or store.one("SELECT id FROM connections WHERE name='Sample retail files'"):
        raise ValueError("The demo workspace is already installed.")
    before = {t: store.scalar(f"SELECT coalesce(max(id), 0) FROM {t}") for t in TRACKED}
    tables_before = {f"{t['schema']}.{t['name']}" for t in plat.wh.list_tables()}
    out = sample.install(plat, orch, user)
    items = {t: [r["id"] for r in store.all(f"SELECT id FROM {t} WHERE id > ? ORDER BY id", (before[t],))] for t in TRACKED}
    tables = sorted({f"{t['schema']}.{t['name']}" for t in plat.wh.list_tables()} - tables_before)
    store.set_setting("demo", {"installed_at": now(), "by": user["email"], "items": items, "tables": tables, "dashboard_id": out.get("dashboard_id")})
    return {**out, **status(store)}


def plan_removal(plat) -> dict:
    """What turning demo mode off will delete, including things people built on demo data."""
    store = plat.store
    d = store.get_setting("demo") or {}
    items = {t: set(v) for t, v in (d.get("items") or {}).items()}
    models = items.get("semantic_models", set())
    if models:
        q = ",".join("?" * len(models))
        items.setdefault("queries", set()).update(r["id"] for r in store.all(f"SELECT id FROM queries WHERE model_id IN ({q})", list(models)))
        items.setdefault("kpi_targets", set()).update(r["id"] for r in store.all(f"SELECT id FROM kpi_targets WHERE model_id IN ({q})", list(models)))
    qids = items.get("queries", set())
    for dsh in store.all("SELECT id, tiles FROM dashboards"):
        tiles = [t for t in dsh["tiles"] or [] if t.get("query_id")]
        if tiles and all(t["query_id"] in qids for t in tiles):
            items.setdefault("dashboards", set()).add(dsh["id"])
    demo_tables = set(d.get("tables") or [])
    ds_ids = items.get("datasets", set())
    for a in store.all("SELECT id, source FROM alerts"):
        src = a["source"] or {}
        if (src.get("type") == "model" and src.get("model_id") in models) or (src.get("type") == "query" and src.get("query_id") in qids) \
                or (src.get("type") in ("quality", "freshness") and src.get("dataset_id") in ds_ids):
            items.setdefault("alerts", set()).add(a["id"])
    dids = items.get("dashboards", set())
    for s in store.all("SELECT id, dashboard_id FROM subscriptions"):
        if s["dashboard_id"] in dids:
            items.setdefault("subscriptions", set()).add(s["id"])
    user_built = {"queries": len(qids - set(d.get("items", {}).get("queries", []))),
                  "dashboards": len(dids - set(d.get("items", {}).get("dashboards", [])))}
    return {"items": {t: sorted(v) for t, v in items.items() if v}, "tables": sorted(demo_tables), "user_built": user_built}


def remove(plat, stop_stream=None) -> dict:
    store = plat.store
    if not (store.get_setting("demo") or {}).get("installed_at"):
        raise ValueError("Demo mode is not on.")
    plan = plan_removal(plat)
    if stop_stream:
        for sid in plan["items"].get("streams") or []:
            stop_stream(sid)
    order = ("streams", "subscriptions", "dashboard_views", "alerts", "kpi_targets", "dashboards", "queries", "notebooks", "glossary", "semantic_models",
             "rule_results", "rules", "pipeline_runs", "pipelines", "lineage", "rls_policies", "datasets", "deliveries", "connections")
    deleted = {}
    for t in order:
        ids = plan["items"].get(t) or []
        if ids:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                store.exec(f"DELETE FROM {t} WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            deleted[t] = len(ids)
    for fqn in plan["tables"]:
        s_, n_ = fqn.split(".", 1)
        if plat.wh.exists(s_, n_):
            plat.wh.drop(s_, n_)
        store.exec("DELETE FROM lineage WHERE src=? OR dst=?", (fqn, fqn))
    folder = plat.settings.uploads_dir / "sample"
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    for f in plat.settings.exports_dir.glob("*orders_enriched*"):
        f.unlink(missing_ok=True)
    store.set_setting("demo", {})
    return {"deleted": deleted, "tables_dropped": len(plan["tables"]), "user_built": plan["user_built"]}
