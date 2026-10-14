"""Executive brief: the numbers leadership asks for, computed the same way every time.

For each measure: the latest complete period against the prior period and the same period last year, year to date
against last year, the trailing twelve months, momentum, the current period's pace, a full-year landing forecast with a
range, the chance of meeting a target, the drivers of the year-over-year change, and the segments gaining or losing.

Every number comes from a governed semantic-model query run as the requesting user (row-level security applies). The
forecast is chosen by holdout accuracy (see insights.forecast_series) and its error on recent history is reported, so
readers can judge how much weight to give it.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import insights as ins
from .warehouse import QueryError

RHO = 0.5  # assumed correlation between forecast errors in different periods, for the full-year range


def _between(td, a: date, b_excl: date) -> dict:
    return {"field": td["name"], "op": "between", "value": [a.isoformat(), (b_excl - timedelta(days=1)).isoformat()]}


def _total(plat, user, model, mname, filters, td, a, b_excl):
    return ins.total(plat, user, model, mname, list(filters) + [_between(td, a, b_excl)])


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _year_start(d: date, fy_month: int) -> date:
    y = d.year if d.month >= fy_month else d.year - 1
    return date(y, fy_month, 1)


def _fy_label(start: date, fy_month: int) -> str:
    return str(start.year) if fy_month == 1 else f"FY{start.year + 1}"


def segments(plat, user, model, mname, dim, filters, td, grain, periods: list[str], top=3):
    """Per-member trend over the given complete periods: slope relative to the member's average."""
    if len(periods) < 6:
        return None
    flt = list(filters) + [_between(td, ins._d(periods[0]), ins.period_end(ins._d(periods[-1]), grain))]
    res = plat.run_model(user, model, {"measures": [mname], "dimensions": [td["name"], dim["name"]], "grains": {td["name"]: grain},
                                       "filters": flt, "limit": 20000})
    idx = {p: i for i, p in enumerate(periods)}
    series: dict[str, list] = {}
    for p, mem, v in res["rows"]:
        if p is None or str(p)[:10] not in idx:
            continue
        series.setdefault("(blank)" if mem is None else str(mem), [0.0] * len(periods))[idx[str(p)[:10]]] = float(v or 0)
    out = []
    for mem, ys in series.items():
        mean = sum(ys) / len(ys)
        if not mean:
            continue
        fit = ins.ols(ys)
        half = len(ys) // 2
        recent, before = sum(ys[half:]) / (len(ys) - half), sum(ys[:half]) / half
        out.append({"member": mem, "slope": fit["slope"], "relative_slope": fit["slope"] / abs(mean), "r2": fit["r2"],
                    "recent_avg": recent, "earlier_avg": before, "change": recent - before, "values": ys})
    if not out:
        return None
    grow = sorted([x for x in out if x["slope"] > 0 and x["r2"] >= 0.2], key=lambda x: -x["slope"])[:top]
    fall = sorted([x for x in out if x["slope"] < 0 and x["r2"] >= 0.2], key=lambda x: x["slope"])[:top]
    return {"dimension": dim["name"], "label": dim["label"], "growing": grow, "declining": fall}


def measure_brief(plat, user, model, mname, grain="month", filters=None, today: date | None = None, targets: dict | None = None,
                  fy_month: int = 1) -> dict:
    today = today or date.today()
    m = ins.measure(model, mname)
    td = ins.time_dim(model)
    filters = [f for f in (filters or []) if not ins._is_time_filter(model, f)]
    s = ins.series(plat, user, model, mname, grain, filters, today=today)
    pts = s["points"]
    if len(pts) < 2:
        raise QueryError(f"{m['label']}: need at least two complete {grain}s of history.")
    ys = [p["value"] for p in pts]
    py = ins.PER_YEAR.get(grain, 12)
    add = ins.additive(m) or bool(m.get("additive_over_time"))  # adds up across periods: landing, pace and targets apply
    last, prev = pts[-1], pts[-2]
    last_d = ins._d(last["period"])
    out = {"measure": mname, "label": m["label"], "format": m["format"], "decimals": m["decimals"], "direction": m.get("direction", "up"),
           "additive": add, "grain": grain, "series": pts[-36:], "partial": s["partial"]}
    out["current"] = {"period": last["period"], "label": last["label"], "value": last["value"]}
    out["vs_prior"] = {"label": prev["label"], "value": prev["value"], "pct": ins.pct(prev["value"], last["value"]), "delta": last["value"] - prev["value"]}
    ly_d = ins.shift(last_d, grain, -py) if grain != "day" else last_d - timedelta(days=364)
    ly = next((p for p in pts if p["period"] == ly_d.isoformat()), None)
    out["vs_last_year"] = {"label": ly["label"], "value": ly["value"], "pct": ins.pct(ly["value"], last["value"]), "delta": last["value"] - ly["value"]} if ly else None

    # record or extreme within the last two years
    window = ys[-min(len(ys), 2 * py):]
    if len(window) >= 6:
        if last["value"] >= max(window):
            out["extreme"] = {"kind": "highest", "periods": len(window)}
        elif last["value"] <= min(window):
            out["extreme"] = {"kind": "lowest", "periods": len(window)}

    # momentum: year-over-year growth of the last 3 periods against that of the 3 before (removes seasonality);
    # without a year of history, the last 3 periods against the 3 before
    if len(ys) >= py + 6 and grain != "day":
        g = lambda seg: ins.pct(sum(ys[seg - py] for seg in seg), sum(ys[i] for i in seg))  # noqa: E731
        n = len(ys)
        recent, before = g(range(n - 3, n)), g(range(n - 6, n - 3))
        if recent is not None and before is not None:
            out["momentum"] = {"basis": "yoy", "recent_growth": recent, "previous_growth": before, "change": recent - before}
    elif len(ys) >= 6:
        a, b = sum(ys[-3:]) / 3, sum(ys[-6:-3]) / 3
        out["momentum"] = {"basis": "sequential", "recent_avg": a, "previous_avg": b, "pct": ins.pct(b, a)}

    # year to date and trailing twelve months, queried directly so ratios are exact
    ys_start = _year_start(last_d, fy_month)
    target = (targets or {}).get(_fy_label(ys_start, fy_month))
    end_excl = ins.period_end(last_d, grain)
    ytd = _total(plat, user, model, mname, filters, td, ys_start, end_excl)
    pys = date(ys_start.year - 1, ys_start.month, 1)
    pend = date(end_excl.year - 1, end_excl.month, end_excl.day) if not (end_excl.month == 2 and end_excl.day == 29) else date(end_excl.year - 1, 3, 1)
    pytd = _total(plat, user, model, mname, filters, td, pys, pend)
    out["ytd"] = {"label": f"{_fy_label(ys_start, fy_month)} to {last['label']}", "value": ytd, "last_year": pytd, "pct": ins.pct(pytd, ytd)}
    ttm_start = ins.shift(last_d, grain, -(py - 1)) if grain != "day" else last_d - timedelta(days=364)
    ttm = _total(plat, user, model, mname, filters, td, ttm_start, end_excl)
    pttm = _total(plat, user, model, mname, filters, td, ins.shift(ttm_start, grain, -py) if grain != "day" else ttm_start - timedelta(days=365), ttm_start)
    out["ttm"] = {"value": ttm, "previous": pttm, "pct": ins.pct(pttm, ttm)}

    # forecast through the end of the year (at least three periods)
    fy_end = _year_start(last_d, fy_month)
    fy_end = date(fy_end.year + 1, fy_end.month, 1)
    remaining = 0
    d = ins.shift(last_d, grain, 1)
    while d < fy_end:
        remaining += 1
        d = ins.shift(d, grain, 1)
    horizon = max(3, remaining)
    fc = ins.forecast_series(ys, grain, horizon)
    fpts = [{"period": ins.shift(last_d, grain, k + 1).isoformat(), "label": ins.label(ins.shift(last_d, grain, k + 1), grain), **p}
            for k, p in enumerate(fc["points"])]
    out["forecast"] = {"method": fc["method"], "method_label": fc["method_label"], "accuracy": fc["accuracy"], "points": fpts}
    nxt = fpts[0]
    out["next"] = nxt

    # current period pace
    if s["partial"] and add and s["partial"]["value"] is not None:
        ps = ins._d(s["partial"]["period"])
        length = (ins.period_end(ps, grain) - ps).days
        elapsed = (today - ps).days
        if length and elapsed / length >= 0.1:
            out["pace"] = {"label": s["partial"]["label"], "to_date": s["partial"]["value"], "elapsed": elapsed / length,
                           "projected": s["partial"]["value"] / (elapsed / length), "forecast": nxt["value"] if nxt["period"] == s["partial"]["period"] else None}

    # full-year landing (additive measures)
    if add:
        rest = fpts[:remaining]
        landing = (ytd or 0) + sum(p["value"] for p in rest)
        sds = [p["sd"] for p in rest]
        var = sum(x * x for x in sds) + 2 * RHO * sum(sds[i] * sds[j] for i in range(len(sds)) for j in range(i + 1, len(sds)))
        sd = math.sqrt(var)
        prior_full = _total(plat, user, model, mname, filters, td, pys, ys_start)
        out["landing"] = {"label": _fy_label(ys_start, fy_month), "value": landing, "lower": landing - 1.96 * sd, "upper": landing + 1.96 * sd, "sd": sd,
                          "actual_to_date": ytd, "forecast_remaining": sum(p["value"] for p in rest), "remaining_periods": remaining,
                          "prior_year": prior_full, "pct_vs_prior": ins.pct(prior_full, landing)}
        if target:
            up = m.get("direction", "up") == "up"
            z = (landing - target) / sd if sd else (math.inf if landing >= target else -math.inf)
            prob = _ncdf(z) if up else 1 - _ncdf(z)
            need = (target - (ytd or 0)) / remaining if remaining else None
            run_rate = (sum(p["value"] for p in rest) / remaining) if remaining else None
            out["target"] = {"value": target, "probability": prob, "gap": landing - target, "required_per_period": need, "forecast_per_period": run_rate,
                             "status": "on_track" if prob >= 0.7 else "at_risk" if prob >= 0.3 else "off_track"}
    elif target and ytd is not None:
        up = m.get("direction", "up") == "up"
        ok = ytd >= target if up else ytd <= target
        nxt_ok = nxt["value"] >= target if up else nxt["value"] <= target
        out["target"] = {"value": target, "probability": None, "gap": ytd - target, "basis": "year to date",
                         "status": "on_track" if ok and nxt_ok else "at_risk" if ok or nxt_ok else "off_track"}

    # what drove the year-over-year change
    if ly:
        try:
            ex = ins.explain_change(plat, user, model, mname, grain, filters, current=last["period"], baseline=ly["period"], top=3)
            out["drivers"] = {"summary": ex["summary"], "dimensions": ex["dimensions"][:2], "additive": ex["additive"], "baseline": ex["baseline"]["label"]}
        except QueryError:
            pass

    # segments gaining and losing over the last 12 complete periods
    seg_periods = [p["period"] for p in pts[-min(len(pts), py):]]
    out["segments"] = [x for x in (segments(plat, user, model, mname, d, filters, td, grain, seg_periods) for d in ins.category_dims(model)[:3]) if x]
    out["headlines"] = headlines(out, m)
    return out


def headlines(b: dict, m: dict) -> list[dict]:
    f = lambda v: ins.fmt(v, m)  # noqa: E731
    good = lambda delta: (delta > 0) == (b["direction"] == "up")  # noqa: E731
    out = []
    if b.get("landing"):
        L = b["landing"]
        t = b.get("target")
        txt = f"{b['label']} is forecast to land at {f(L['value'])} for {L['label']} (likely range {f(L['lower'])} to {f(L['upper'])})"
        if L.get("pct_vs_prior") is not None:
            txt += f", {L['pct_vs_prior'] * 100:+.1f}% on {f(L['prior_year'])} last year"
        if t:
            txt += f". Chance of reaching the {f(t['value'])} target: {t['probability'] * 100:.0f}%"
            if t.get("required_per_period") is not None and t["required_per_period"] > (t.get("forecast_per_period") or 0) and b["direction"] == "up":
                txt += f"; it needs {f(t['required_per_period'])} per {b['grain']} against a forecast of {f(t['forecast_per_period'])}"
        out.append({"kind": "landing", "severity": ("good" if t["status"] == "on_track" else "warn" if t["status"] == "at_risk" else "bad") if t else "info", "text": txt + "."})
    ly = b.get("vs_last_year")
    if ly and ly["pct"] is not None:
        d = b["current"]["value"] - ly["value"]
        if m["format"] == "percent":
            txt = f"{b['label']} was {f(b['current']['value'])} in {b['current']['label']}, {d * 100:+.1f} pts on a year ago"
        else:
            txt = f"{b['label']} was {f(b['current']['value'])} in {b['current']['label']}, {ly['pct'] * 100:+.1f}% on a year ago"
        if b.get("extreme"):
            txt += f" and the {b['extreme']['kind']} in {b['extreme']['periods']} {b['grain']}s"
        out.append({"kind": "yoy", "severity": "good" if good(d) else "bad", "text": txt + "."})
    elif b.get("extreme"):
        out.append({"kind": "extreme", "severity": "good" if (b["extreme"]["kind"] == "highest") == (b["direction"] == "up") else "bad",
                    "text": f"{b['label']} was {f(b['current']['value'])} in {b['current']['label']}, the {b['extreme']['kind']} in {b['extreme']['periods']} {b['grain']}s."})
    t = b.get("target")
    if t and t.get("probability") is None:
        word = {"on_track": "within", "at_risk": "close to", "off_track": "outside"}[t["status"]]
        out.append({"kind": "landing", "severity": "good" if t["status"] == "on_track" else "warn" if t["status"] == "at_risk" else "bad",
                    "text": f"{b['label']} is {f(b['ytd']['value'])} year to date, {word} the {f(t['value'])} target; next {b['grain']} is forecast at {f(b['next']['value'])}."})
    y = b.get("ytd")
    if y and y["pct"] is not None and b["additive"]:
        out.append({"kind": "ytd", "severity": "good" if good(y["pct"]) else "bad", "text": f"Year to date {b['label']} is {f(y['value'])}, {y['pct'] * 100:+.1f}% on the same period last year."})
    mo = b.get("momentum")
    if mo and mo["basis"] == "yoy" and abs(mo["change"]) >= 0.03 and m["format"] != "percent":
        faster = mo["change"] > 0
        out.append({"kind": "momentum", "severity": "good" if good(mo["change"]) else "warn",
                    "text": f"{b['label']} growth is {'accelerating' if faster else 'slowing'}: {mo['recent_growth'] * 100:+.1f}% year over year in the last three {b['grain']}s, "
                            f"against {mo['previous_growth'] * 100:+.1f}% in the three before."})
    elif mo and mo["basis"] == "sequential" and mo["pct"] is not None and abs(mo["pct"]) >= 0.03:
        out.append({"kind": "momentum", "severity": "good" if good(mo["pct"]) else "warn",
                    "text": f"Momentum: the last three {b['grain']}s averaged {f(mo['recent_avg'])}, {mo['pct'] * 100:+.1f}% on the three before."})
    if b.get("pace") and b["pace"].get("forecast"):
        p = b["pace"]
        diff = ins.pct(p["forecast"], p["projected"])
        if diff is not None and abs(diff) >= 0.05:
            out.append({"kind": "pace", "severity": "good" if good(diff) else "warn",
                        "text": f"{p['label']} so far: {f(p['to_date'])} with {p['elapsed'] * 100:.0f}% of the {b['grain']} gone, a straight-line pace of {f(p['projected'])} "
                                f"({diff * 100:+.0f}% against the forecast; early-period pace is noisy)."})
    # the single strongest gaining and losing segment for this measure, if the trend is material
    segs = [(sg, x) for sg in b.get("segments", []) for x in sg["growing"] + sg["declining"] if abs(x["relative_slope"]) >= 0.02]
    for want_up in (False, True):
        pick = sorted([p for p in segs if (p[1]["slope"] > 0) == want_up], key=lambda p: -abs(p[1]["relative_slope"]))[:1]
        for sg, x in pick:
            is_good = want_up == (b["direction"] == "up")
            out.append({"kind": "opportunity" if is_good else "risk", "severity": "good" if is_good else "bad",
                        "text": f"{b['label']} in {sg['label']} {x['member']} is trending {'up' if want_up else 'down'}, about {abs(x['relative_slope']) * 100:.1f}% of its average per {b['grain']} "
                                f"over the last {len(x['values'])} {b['grain']}s ({f(x['earlier_avg'])} → {f(x['recent_avg'])}{' per ' + b['grain'] if b['additive'] else ''})."})
    return out


def brief(plat, user, model, measures, grain="month", filters=None, today=None, targets=None, fy_month=1) -> dict:
    targets = targets or {}
    items, errors = [], []
    for mn in measures:
        try:
            items.append(measure_brief(plat, user, model, mn, grain, filters, today, targets.get(mn), fy_month))
        except QueryError as e:
            errors.append(str(e))
    sev = {"bad": 0, "warn": 1, "good": 2, "info": 3}
    kinds = {"landing": 0, "yoy": 1, "ytd": 2, "risk": 3, "momentum": 4, "pace": 5, "extreme": 6, "opportunity": 7}
    heads = [{**h, "measure": it["measure"]} for i, it in enumerate(items) for h in it["headlines"]]
    heads.sort(key=lambda h: (kinds.get(h["kind"], 9), measures.index(h["measure"]), sev[h["severity"]]))
    for i, h in enumerate(heads):
        h["id"] = f"H{i + 1}"
    return {"model_id": model.get("id"), "model": model["name"], "grain": grain, "as_of": (today or date.today()).isoformat(),
            "fiscal_year_start_month": fy_month, "measures": items, "headlines": heads, "errors": errors, "methods": __doc__.strip()}


# ---------------------------------------------------------------- dashboards, targets and email
def targets_for(store, model_id: int) -> dict:
    out: dict[str, dict] = {}
    for t in store.all("SELECT measure, period, value FROM kpi_targets WHERE model_id=?", (model_id,)):
        out.setdefault(t["measure"], {})[t["period"]] = t["value"]
    return out


def dashboard_scope(store, user, d: dict) -> dict[int, list[str]]:
    from . import workspaces as ws
    scope: dict[int, list[str]] = {}
    for t in d.get("tiles") or []:
        q = store.one("SELECT * FROM queries WHERE id=?", (t.get("query_id"),)) if t.get("query_id") else None
        if not q or q.get("kind") != "model" or ws.role_in(store, user, q.get("workspace_id") or 1) is None:
            continue
        for mn in (q.get("spec") or {}).get("measures") or []:
            scope.setdefault(q["model_id"], [])
            if mn not in scope[q["model_id"]]:
                scope[q["model_id"]].append(mn)
    return scope


def dashboard_brief(plat, user, d: dict, filters=None, grain="month", today=None, max_models=2, max_measures=5) -> dict:
    from . import workspaces as ws
    from .security import restricted
    store = plat.store
    fy = int(store.get_setting("fiscal_year_start_month", 1) or 1)
    sections, errors = [], []
    for mid, meas in list(dashboard_scope(store, user, d).items())[:max_models]:
        try:
            m = plat.model(mid)
            if ws.role_in(store, user, m.get("workspace_id") or 1) is None or (restricted(user["role"]) and not m["base"].startswith("serving.")):
                continue
            dims = {x["name"] for x in m["dimensions"]}
            flt = [{k: v for k, v in f.items() if k != "model_id"} for f in filters or [] if f.get("model_id") in (None, mid) and f.get("field") in dims]
            sections.append(brief(plat, user, m, meas[:max_measures], grain, flt, today, targets_for(store, mid), fy))
        except QueryError as e:
            errors.append(str(e))
    heads = [{**h, "model_id": s["model_id"]} for s in sections for h in s["headlines"]]
    for i, h in enumerate(heads):
        h["id"] = f"H{i + 1}"
    return {"dashboard": d["name"], "dashboard_id": d["id"], "sections": sections, "headlines": heads, "errors": errors,
            "as_of": (today or date.today()).isoformat(), "fiscal_year_start_month": fy, "methods": __doc__.strip()}


def email_html(b: dict) -> str:
    import html
    colors = {"good": "#1C7A52", "bad": "#B8362F", "warn": "#9C6410", "info": "#1F55C6"}
    parts = ["<div style=\"border:1px solid #D2DAE0;border-radius:8px;padding:14px 16px;margin:0 0 18px\">",
             "<div style=\"font:600 12px monospace;letter-spacing:.08em;color:#5A6873;text-transform:uppercase\">Executive brief</div><ul style=\"padding-left:18px;margin:8px 0\">"]
    for h in b["headlines"][:8]:
        parts.append(f"<li style=\"margin:4px 0\"><span style=\"color:{colors.get(h['severity'], '#14212B')}\">●</span> {html.escape(h['text'])}</li>")
    parts.append("</ul>")
    rows = []
    for s in b["sections"]:
        for mb in s["measures"]:
            mm = {"format": mb["format"], "decimals": mb["decimals"]}
            land = mb.get("landing")
            tgt = mb.get("target")
            rows.append("<tr>" + "".join(f"<td style=\"padding:4px 10px;border-top:1px solid #E3E9ED\">{html.escape(str(x))}</td>" for x in (
                mb["label"], ins.fmt(mb["current"]["value"], mm),
                f"{mb['vs_last_year']['pct'] * 100:+.1f}%" if mb.get("vs_last_year") and mb["vs_last_year"]["pct"] is not None else "–",
                ins.fmt(land["value"], mm) if land else ins.fmt(mb["next"]["value"], mm) + " next",
                (f"{tgt['probability'] * 100:.0f}%" if tgt.get("probability") is not None else tgt["status"].replace("_", " ")) if tgt else "–")) + "</tr>")
    if rows:
        parts.append("<table style=\"border-collapse:collapse;font-size:13px\"><tr>" + "".join(
            f"<th style=\"text-align:left;padding:4px 10px;color:#5A6873\">{x}</th>" for x in ("Measure", "Latest", "vs last year", "Full-year forecast", "Target likelihood")) + "</tr>"
            + "".join(rows) + "</table>")
    parts.append("<p style=\"color:#5A6873;font-size:12px;margin:8px 0 0\">Forecasts are statistical projections with stated error, chosen by accuracy on recent history.</p></div>")
    return "".join(parts)
