"""Sample workspace: generated retail data (no real people) wired into a pipeline, rules and a dashboard."""
from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta

from .store import now


def luhn_ok(d: str) -> bool:
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        s += n
        alt = not alt
    return s % 10 == 0


def generate(folder, n_customers: int = 800, n_orders: int = 9000, seed: int = 7) -> None:
    r = random.Random(seed)
    first = ["Amara", "Liam", "Priya", "Noah", "Mei", "Owen", "Fatima", "Lucas", "Aiko", "Ethan", "Sofia", "Arjun", "Chloe", "Mateo", "Hana", "Ravi", "Zoe", "Kai"]
    last = ["Singh", "Tremblay", "Nguyen", "MacDonald", "Chen", "Roy", "Patel", "Gagnon", "Kim", "Wilson", "Thomas", "Bouchard", "Ali", "Martin"]
    prov = ["BC", "AB", "ON", "QC", "MB", "SK", "NS", "NB"]
    L = "ABCEGHJKLMNPRSTVXY"

    def sin():
        while True:
            d = str(r.randint(1, 7)) + "".join(str(r.randint(0, 9)) for _ in range(8))
            if luhn_ok(d):
                return f"{d[:3]}-{d[3:6]}-{d[6:]}"

    def card():
        while True:
            d = "4" + "".join(str(r.randint(0, 9)) for _ in range(15))
            if luhn_ok(d):
                return " ".join(d[i:i + 4] for i in range(0, 16, 4))

    with open(folder / "customers.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["customer_id", "full_name", "email", "phone", "sin", "province", "postal_code", "signup_date"])
        for i in range(n_customers):
            f, l_ = r.choice(first), r.choice(last)
            email = f"{f}.{l_}{r.randint(10, 99)}@{r.choice(['mail.ca', 'example.com', 'shaw.ca', 'telus.net'])}".lower()
            if r.random() < 0.03:
                email = email.replace("@", "_at_")
            if r.random() < 0.02:
                email = ""
            sd = date(2021, 1, 1) + timedelta(days=r.randint(0, 1700))
            w.writerow([f"C{100000 + i}", f"{f} {l_}", email, f"({r.choice(['604', '778', '236', '403', '416'])}) {r.randint(200, 999)}-{r.randint(0, 9999):04d}",
                        sin(), r.choice(prov), f"{r.choice(L)}{r.randint(0, 9)}{r.choice(L)} {r.randint(0, 9)}{r.choice(L)}{r.randint(0, 9)}", sd.isoformat()])

    cats = {"Outdoor": 68.0, "Kitchen": 41.0, "Books": 18.5, "Electronics": 189.0, "Apparel": 52.0, "Garden": 33.0}
    with open(folder / "orders.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "customer_id", "order_date", "status", "channel", "product_category", "quantity", "unit_price", "order_total", "card_number"])
        # Three years of history from the first of a month: about 1.4% monthly growth, a holiday peak and a
        # January dip, and mobile steadily taking share from stores, so trends, YoY and forecasts have something to find.
        today = date.today()
        start = date(today.year - 3, today.month, 1)
        days = [start + timedelta(days=k) for k in range((today - start).days)]
        season = {1: 0.82, 2: 0.86, 3: 0.95, 4: 0.98, 5: 1.0, 6: 0.97, 7: 0.95, 8: 0.98, 9: 1.0, 10: 1.05, 11: 1.3, 12: 1.45}
        # one unusual month (a simulated flash sale eight months ago) for anomaly detection to find
        promo = date(today.year, today.month, 1)
        for _ in range(8):
            promo = date(promo.year - (promo.month == 1), 12 if promo.month == 1 else promo.month - 1, 1)
        weights = [(1.014 ** ((d.year - start.year) * 12 + d.month - start.month)) * season[d.month] * (1.5 if (d.year, d.month) == (promo.year, promo.month) else 1)
                   for d in days]
        dates = sorted(r.choices(days, weights, k=n_orders))
        span = max(1, len(days) - 1)
        for i in range(n_orders):
            oid = f"O{500000 + i}"
            if r.random() < 0.008 and i > 10:
                oid = f"O{500000 + r.randint(0, i - 1)}"
            cat = r.choice(list(cats))
            q = r.choice([1, 1, 1, 2, 2, 3, 4])
            price = round(cats[cat] * r.uniform(0.6, 1.5), 2)
            total = round(q * price, 2)
            if r.random() < 0.006:
                total = -total
            d = dates[i]
            t = (d - start).days / span
            if r.random() < 0.004:
                d = date.today() + timedelta(days=r.randint(20, 300))
            status = r.choices(["paid", "shipped", "refunded", "pending"], [50, 35, 7, 8])[0]
            if r.random() < 0.006:
                status = r.choice(["PAID ", "shiped", "unknown"])
            w.writerow([oid, f"C{100000 + r.randint(0, n_customers - 1)}", min(d, date.today() + timedelta(days=400)).isoformat(), status,
                        r.choices(["web", "store", "mobile", "partner"], [45, 34 - 18 * t, 16 + 18 * t, 5])[0], cat, q, price, total, card()])


def install(platform, orch, user: dict) -> dict:
    s = platform.store
    folder = platform.settings.uploads_dir / "sample"
    folder.mkdir(parents=True, exist_ok=True)
    generate(folder)
    cid = s.insert("connections", {"name": "Sample retail files", "type": "file", "config": json.dumps({"base_path": "sample"}),
                                   "secrets": None, "created_by": user["id"], "created_at": now(), "updated_at": now(),
                                   "last_test_ok": 1, "last_test_msg": "Sample folder", "last_test_at": now()})
    steps = [
        {"id": "load_customers", "type": "ingest", "connection_id": cid, "source": {"path": "customers.csv"}, "target": "raw.customers", "mode": "replace"},
        {"id": "load_orders", "type": "ingest", "connection_id": cid, "source": {"path": "orders.csv"}, "target": "raw.orders", "mode": "replace", "retries": 1},
        {"id": "enrich", "type": "transform", "depends_on": ["load_customers", "load_orders"], "target": "curated.orders_enriched",
         "sql": "SELECT o.order_id, o.customer_id, o.order_date, trim(lower(o.status)) AS status, o.channel, o.product_category, "
                "o.quantity, o.unit_price, o.order_total, o.card_number, c.province, c.email "
                "FROM raw.orders o LEFT JOIN raw.customers c USING (customer_id)"},
        {"id": "quality_gate", "type": "quality", "depends_on": ["enrich"], "dataset": "curated.orders_enriched", "fail_on": "error"},
        {"id": "customers_quality", "type": "quality", "depends_on": ["load_customers"], "dataset": "raw.customers", "fail_on": "error"},
        {"id": "scan_customers", "type": "pii_scan", "depends_on": ["customers_quality"], "dataset": "raw.customers"},
        {"id": "scan_orders", "type": "pii_scan", "depends_on": ["quality_gate"], "dataset": "curated.orders_enriched"},
        {"id": "publish_orders", "type": "publish", "depends_on": ["scan_orders"], "dataset": "curated.orders_enriched"},
        {"id": "publish_customers", "type": "publish", "depends_on": ["scan_customers"], "dataset": "raw.customers"},
    ]
    pid = s.insert("pipelines", {"name": "Retail daily load", "description": "Loads sample retail files, enriches orders, gates on quality, masks personal data and publishes.",
                                 "steps": json.dumps(steps), "schedule": "0 6 * * *", "enabled": 1, "created_by": user["email"],
                                 "created_at": now(), "updated_at": now()})
    from .orchestrator import next_run
    s.update("pipelines", pid, {"next_run_at": next_run("0 6 * * *")})
    orch.trigger(pid, "manual", user["email"], wait=True)

    oe = platform.dataset("curated", "orders_enriched")
    if oe:
        for rule in [
            {"kind": "not_null", "column_name": "customer_id", "severity": "error"},
            {"kind": "not_null", "column_name": "order_id", "severity": "error"},
            {"kind": "unique", "column_name": "order_id", "severity": "warn"},
            {"kind": "range", "column_name": "order_total", "params": {"min": 0}, "severity": "warn"},
            {"kind": "allowed_values", "column_name": "status", "params": {"values": ["paid", "shipped", "refunded", "pending"]}, "severity": "warn"},
            {"kind": "not_future", "column_name": "order_date", "severity": "warn"},
            {"kind": "regex", "column_name": "email", "params": {"pattern": r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$", "label": "email"}, "severity": "warn"},
            {"kind": "freshness", "column_name": "order_date", "params": {"hours": 72}, "severity": "warn"},
        ]:
            platform.add_rule(oe["id"], rule, "sample")
        s.update("datasets", oe["id"], {"description": "One row per order line, joined to the customer's province and email. Built nightly from raw.orders and raw.customers.",
                                         "owner": user["email"], "freshness_sla_hours": 24})
        for col, desc in {"order_id": "Order identifier from the commerce system", "order_total": "quantity × unit_price in CAD, before tax",
                          "status": "paid, shipped, refunded or pending", "channel": "Sales channel: web, store, mobile or partner"}.items():
            platform.set_column(oe["id"], col, {"description": desc})
    cu = platform.dataset("raw", "customers")
    if cu:
        platform.add_rule(cu["id"], {"kind": "unique", "column_name": "customer_id", "severity": "error"}, "sample")
        platform.add_rule(cu["id"], {"kind": "regex", "column_name": "email", "params": {"pattern": r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$", "label": "email"}, "severity": "warn"}, "sample")
    orch.trigger(pid, "manual", user["email"], wait=True)

    from . import semantic
    paid = "status IN ('paid','shipped')"
    model = semantic.validate_model({
        "name": "Sales", "description": "Governed sales metrics on published, masked order data. Revenue counts paid and shipped orders.",
        "base": "serving.orders_enriched", "joins": [],
        "dimensions": [
            {"name": "order_date", "label": "Order date", "column": "order_date", "type": "time", "description": "Date the order was placed"},
            {"name": "province", "label": "Province", "column": "province", "type": "category"},
            {"name": "channel", "label": "Channel", "column": "channel", "type": "category"},
            {"name": "category", "label": "Product category", "column": "product_category", "type": "category"},
            {"name": "status", "label": "Order status", "column": "status", "type": "category"},
        ],
        "measures": [
            {"name": "revenue", "label": "Revenue", "agg": "custom", "expr": f"sum(order_total) FILTER (WHERE {paid})", "format": "currency", "decimals": 0,
             "description": "Sum of order_total for paid and shipped orders, CAD before tax"},
            {"name": "orders", "label": "Orders", "agg": "count_distinct", "column": "order_id", "format": "integer", "additive_over_time": True,
             "description": "Distinct orders. Each order has one date, so monthly counts add up to the year."},
            {"name": "aov", "label": "Average order value", "agg": "custom", "expr": f"avg(order_total) FILTER (WHERE {paid})", "format": "currency", "decimals": 2},
            {"name": "units", "label": "Units sold", "agg": "sum", "column": "quantity", "format": "integer"},
            {"name": "refund_rate", "label": "Refund rate", "agg": "custom", "expr": "count(*) FILTER (WHERE status = 'refunded') / count(*)", "format": "percent", "decimals": 1,
             "direction": "down", "description": "Share of orders refunded; lower is better"},
        ]}, platform.wh)
    mid = s.insert("semantic_models", {**{k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in model.items()},
                                       "endorsement": "certified", "created_by": user["email"], "created_at": now(), "updated_at": now()})

    # a full-year revenue target 15% above last year's actuals, so the executive brief has something to measure against
    try:
        yr = date.today().year
        last = platform.run_model(user, {**model, "id": mid}, {"measures": ["revenue"], "filters": [
            {"field": "order_date", "op": "between", "value": [f"{yr - 1}-01-01", f"{yr - 1}-12-31"]}]})["rows"][0][0]
        if last:
            s.insert("kpi_targets", {"model_id": mid, "measure": "revenue", "period": str(yr), "value": round(last * 1.15, -3),
                                     "note": "Sample: last year plus 15%", "created_by": user["email"], "updated_at": now()})
            s.insert("kpi_targets", {"model_id": mid, "measure": "refund_rate", "period": str(yr), "value": 0.06,
                                     "note": "Sample: keep refunds at or below 6%", "created_by": user["email"], "updated_at": now()})
    except Exception:  # noqa: BLE001
        pass

    def mq(name, spec, viz):
        return s.insert("queries", {"name": name, "kind": "model", "model_id": mid, "spec": json.dumps(spec), "sql": "", "viz": json.dumps(viz),
                                    "created_by": user["email"], "created_at": now(), "updated_at": now()})
    not_future = [{"field": "order_date", "op": "<=", "value": date.today().isoformat()}]
    ids = [
        mq("Revenue", {"measures": ["revenue"], "filters": not_future}, {"type": "kpi"}),
        mq("Orders", {"measures": ["orders"], "filters": not_future}, {"type": "kpi"}),
        mq("Average order value", {"measures": ["aov"], "filters": not_future}, {"type": "kpi"}),
        mq("Refund rate", {"measures": ["refund_rate"], "filters": not_future}, {"type": "kpi"}),
        mq("Revenue by month", {"measures": ["revenue"], "dimensions": ["order_date"], "grains": {"order_date": "month"}, "filters": not_future},
           {"type": "area", "x": "order_date", "y": "revenue", "drill": True}),
        mq("Revenue by province", {"measures": ["revenue"], "dimensions": ["province"], "filters": [*not_future, {"field": "province", "op": "not_null"}]},
           {"type": "bar", "x": "province", "y": "revenue", "crossfilter": True}),
        mq("Orders by channel", {"measures": ["orders"], "dimensions": ["channel"], "filters": not_future}, {"type": "donut", "x": "channel", "y": "orders", "crossfilter": True}),
        mq("Revenue by category and channel", {"measures": ["revenue"], "dimensions": ["category", "channel"], "filters": not_future},
           {"type": "stacked", "x": "category", "series": "channel", "y": "revenue"}),
        mq("Category performance", {"measures": ["orders", "units", "revenue", "aov", "refund_rate"], "dimensions": ["category"], "filters": not_future,
                                    "sort": [{"field": "revenue", "dir": "desc"}]}, {"type": "table", "heat": "revenue"}),
    ]
    tiles = [{"query_id": ids[0], "w": 3}, {"query_id": ids[1], "w": 3}, {"query_id": ids[2], "w": 3}, {"query_id": ids[3], "w": 3},
             {"query_id": ids[4], "w": 12}, {"query_id": ids[5], "w": 6}, {"query_id": ids[6], "w": 6}, {"query_id": ids[7], "w": 6},
             {"query_id": ids[8], "w": 6},
             {"type": "text", "w": 12, "text": "**How to read this:** click a province bar or a channel slice to filter the whole page. "
                                                "Use the date range above for period comparisons. Personal data is masked in the published dataset."}]
    filters = [{"model_id": mid, "field": "order_date", "label": "Order date", "type": "date_range"},
               {"model_id": mid, "field": "province", "label": "Province", "type": "select"},
               {"model_id": mid, "field": "channel", "label": "Channel", "type": "select"}]
    did = s.insert("dashboards", {"name": "Sales overview", "description": "Certified Sales model on published, masked data. Safe for analysts and viewers.",
                                  "tiles": json.dumps(tiles), "filters": json.dumps(filters), "refresh_seconds": 0, "created_by": user["email"],
                                  "created_at": now(), "updated_at": now()})
    from .orchestrator import next_run
    s.insert("alerts", {"name": "Refund rate above 8%", "source": json.dumps({"type": "model", "model_id": mid, "measure": "refund_rate"}),
                        "condition": json.dumps({"op": ">", "threshold": 0.08}), "channels": json.dumps({"inapp": True}), "schedule": "0 * * * *",
                        "owner_id": user["id"], "enabled": 1, "state": "unknown", "next_run_at": next_run("0 * * * *"), "created_at": now()})
    s.insert("glossary", {"term": "Revenue", "definition": "Sum of order totals for paid and shipped orders, in CAD before tax. Refunded and pending orders are excluded.",
                          "owner": user["email"], "synonyms": json.dumps(["sales", "GMV"]), "columns": json.dumps(["curated.orders_enriched.order_total"]),
                          "domain": "Sales", "created_at": now(), "updated_at": now()})
    s.insert("glossary", {"term": "Refund rate", "definition": "Share of order lines with status refunded among all order lines in the period.",
                          "owner": user["email"], "synonyms": json.dumps(["return rate"]), "columns": json.dumps(["curated.orders_enriched.status"]),
                          "domain": "Sales", "created_at": now(), "updated_at": now()})
    for fqn_, dom, end in (("curated.orders_enriched", "Sales", "promoted"), ("serving.orders_enriched", "Sales", "certified"), ("serving.customers", "Customers", "promoted")):
        sc, nm = fqn_.split(".")
        d = platform.dataset(sc, nm)
        if d:
            s.update("datasets", d["id"], {"domain": dom, "endorsement": end})
    s.insert("notebooks", {"name": "Refund investigation", "owner_id": user["id"], "created_by": user["email"], "created_at": now(), "updated_at": now(),
                           "cells": json.dumps([
                               {"id": "c1", "type": "markdown", "source": "# Refund investigation\nWhich channels and categories drive refunds? Data is the published, masked order feed."},
                               {"id": "c2", "type": "sql", "source": "select channel, count(*) filter (where status = 'refunded') as refunds,\n       round(100.0 * count(*) filter (where status = 'refunded') / count(*), 1) as refund_pct\nfrom serving.orders_enriched\ngroup by 1 order by refund_pct desc",
                                "viz": {"type": "bar", "x": "channel", "y": "refund_pct"}},
                               {"id": "c3", "type": "markdown", "source": "## By category\nSame measure split by product category."},
                               {"id": "c4", "type": "sql", "source": "select product_category, round(100.0 * count(*) filter (where status = 'refunded') / count(*), 1) as refund_pct\nfrom serving.orders_enriched group by 1 order by 2 desc"},
                           ])})
    from .streaming import install_demo
    stream_id = install_demo(platform, user)
    s.audit(user, "sample.installed", "Retail sample", {"pipeline_id": pid, "dashboard_id": did, "model_id": mid})
    return {"connection_id": cid, "pipeline_id": pid, "dashboard_id": did, "stream_id": stream_id}
