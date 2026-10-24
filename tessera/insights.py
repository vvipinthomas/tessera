"""Automated insights on semantic-model measures: trend, anomalies, forecast, change drivers, decomposition and concentration.

Everything here is deterministic statistics computed through Platform.run_model, so row-level security, the
serving-only rule and workspace checks apply exactly as they do for any other query. An optional language model may
turn the resulting facts into prose, but it only ever sees the facts below, never raw rows.

Methods (kept simple on purpose, and stated in every result so people can judge them):
- Trend: ordinary least squares on complete periods; reported when R² ≥ 0.3.
- Anomalies: residuals from that line, flagged when the modified z-score (0.6745·|r − median| / MAD) exceeds 3.5.
  When the series has a clear seasonal pattern (it explains at least half of the detrended variance), the expected
  value includes the usual seasonal swing, so a normal holiday peak is not flagged.
- Forecast: the best of last value, same period last year, Holt, damped Holt and Holt-Winters (seasonal, when there
  are two full seasons of history and the seasonal pattern explains at least half of the detrended variance),
  chosen by error on a holdout of the most recent full season (or 3 to 6 periods with less history) and refitted
  on all data. A seasonal method must
  also beat the best non-seasonal one by 10% on that holdout. Approximate 95% ranges come from the ETS variance formulas. The holdout error is reported with every forecast.
- Drivers: the measure split by every category dimension in two periods. For additive measures (sums and counts) the
  member changes add up to the total change; for averages, ratios and distinct counts they do not, and the result says so.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from .semantic import GRAINS, _add_months
from .store import local_today
from .warehouse import QueryError

MAX_DIMS = 8


# ---------------------------------------------------------------- helpers
def time_dim(model: dict, name: str | None = None) -> dict:
    tds = [d for d in model["dimensions"] if d["type"] == "time"]
    if name:
        tds = [d for d in tds if d["name"] == name]
    if not tds:
        raise QueryError("This model has no time dimension, so trends and forecasts are not available.")
    return tds[0]


def measure(model: dict, name: str) -> dict:
    for m in model["measures"]:
        if m["name"] == name:
            return m
    raise QueryError(f"Unknown measure '{name}'.")


def additive(m: dict) -> bool:
    if m["agg"] in ("sum", "count", "count_rows"):
        return True
    if m["agg"] == "custom":
        e = (m.get("expr") or "").strip().lower()
        return bool(re.fullmatch(r"(sum|count)\((?!\s*distinct)[^()]*\)(\s*filter\s*\(where [^()]*(\([^()]*\))?[^()]*\))?", e))
    return False


def fmt(v, m: dict) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    f, d = m.get("format", "number"), int(m.get("decimals", 2))
    if f == "percent":
        return f"{v * 100:.{d}f}%"
    if f == "integer":
        return f"{round(v):,}"
    s = _compact(v, d)
    return ("$" + s) if f == "currency" else s


def _compact(v: float, d: int) -> str:
    a = abs(v)
    for lim, div, suf in ((1e9, 1e9, "B"), (1e6, 1e6, "M"), (1e4, 1e3, "K")):
        if a >= lim:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.{d}f}"


def pct(a, b) -> float | None:
    if a in (None, 0) or b is None:
        return None
    return (b - a) / abs(a)


def _signed(v, m: dict) -> str:
    if m.get("format") == "percent":
        return f"{v * 100:+.1f} pts"
    return ("+" if v >= 0 else "−") + fmt(abs(v), m)


def fmt_delta(a, b, m: dict) -> str:
    if m.get("format") == "percent":
        return f"{(b - a) * 100:+.1f} pts"
    p = pct(a, b)
    return f"{p * 100:+.1f}%" if p is not None else fmt(b - a, m)


def period_end(start: date, grain: str) -> date:
    """Exclusive end of the period that starts at `start`."""
    if grain == "day":
        return start + timedelta(days=1)
    if grain == "week":
        return start + timedelta(days=7)
    return _add_months(start, {"month": 1, "quarter": 3, "year": 12}[grain])


def shift(start: date, grain: str, n: int) -> date:
    if grain == "day":
        return start + timedelta(days=n)
    if grain == "week":
        return start + timedelta(days=7 * n)
    return _add_months(start, {"month": 1, "quarter": 3, "year": 12}[grain] * n)


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def label(start: date, grain: str) -> str:
    if grain == "month":
        return start.strftime("%b %Y")
    if grain == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if grain == "year":
        return str(start.year)
    if grain == "week":
        return f"week of {start.isoformat()}"
    return start.isoformat()


def direction_word(delta: float, m: dict) -> str:
    good = delta > 0 if m.get("direction", "up") == "up" else delta < 0
    return "good" if good else "bad"


# ---------------------------------------------------------------- statistics
def ols(y: list[float]) -> dict:
    n = len(y)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(y) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (v - my) for x, v in zip(xs, y)) / sxx if sxx else 0.0
    icpt = my - slope * mx
    fit = [icpt + slope * x for x in xs]
    ss_tot = sum((v - my) ** 2 for v in y)
    ss_res = sum((v - f) ** 2 for v, f in zip(y, fit))
    return {"slope": slope, "intercept": icpt, "r2": 1 - ss_res / ss_tot if ss_tot else 0.0, "fit": fit, "mean": my}


def robust_outliers(y: list[float], fit: list[float], threshold: float = 3.5) -> list[tuple[int, float]]:
    r = [a - b for a, b in zip(y, fit)]
    med = _median(r)
    mad = _median([abs(x - med) for x in r])
    if mad == 0:
        return []
    out = []
    for i, x in enumerate(r):
        z = 0.6745 * (x - med) / mad
        if abs(z) > threshold:
            out.append((i, z))
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def holt(y: list[float], horizon: int) -> dict:
    """Holt's linear method. Returns forecasts with an approximate 95% interval."""
    best = None
    for a in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        for b in (0.01, 0.05, 0.1, 0.2, 0.3, 0.5):
            lvl, tr, sse = y[0], y[1] - y[0], 0.0
            for t in range(1, len(y)):
                f = lvl + tr
                sse += (y[t] - f) ** 2
                nl = a * y[t] + (1 - a) * f
                tr = b * (nl - lvl) + (1 - b) * tr
                lvl = nl
            if best is None or sse < best[0]:
                best = (sse, a, b, lvl, tr)
    sse, a, b, lvl, tr = best
    sigma = math.sqrt(sse / max(1, len(y) - 3))
    out = []
    for h in range(1, horizon + 1):
        var = sigma ** 2 * (1 + sum((a * (1 + j * b)) ** 2 for j in range(1, h)))
        f = lvl + h * tr
        w = 1.96 * math.sqrt(var)
        out.append({"value": f, "lower": f - w, "upper": f + w})
    return {"alpha": a, "beta": b, "sigma": sigma, "points": out}


# ---------------------------------------------------------------- forecasting engine with model selection
SEASON = {"month": 12, "quarter": 4, "week": 52, "day": 7}
PER_YEAR = {"month": 12, "quarter": 4, "week": 52, "day": 365}
METHOD_LABEL = {"naive": "last value", "seasonal_naive": "same period last year", "holt": "Holt linear trend",
                "holt_damped": "Holt damped trend", "holt_winters": "Holt-Winters with seasonality", "holt_winters_mult": "Holt-Winters with proportional seasonality"}


def _fit_holt(y, a, b, phi=1.0):
    lvl, tr, sse, res = y[0], y[1] - y[0], 0.0, []
    for t in range(1, len(y)):
        f = lvl + phi * tr
        res.append(y[t] - f)
        sse += (y[t] - f) ** 2
        nl = a * y[t] + (1 - a) * f
        tr = b * (nl - lvl) + (1 - b) * phi * tr
        lvl = nl
    return sse, lvl, tr, res


def _holt_any(y, h, damped=False):
    best = None
    phi = 0.9 if damped else 1.0
    for a in (0.1, 0.2, 0.3, 0.5, 0.7, 0.9):
        for b in (0.01, 0.05, 0.1, 0.2, 0.3):
            sse, lvl, tr, res = _fit_holt(y, a, b, phi)
            if best is None or sse < best[0]:
                best = (sse, a, b, lvl, tr, res)
    sse, a, b, lvl, tr, res = best
    pts, damp = [], 0.0
    for k in range(1, h + 1):
        damp += phi ** k
        pts.append(lvl + damp * tr)
    if damped:
        cs = [a * (1 + b * phi * (1 - phi ** j) / (1 - phi)) for j in range(1, h)]
    else:
        cs = [a * (1 + j * b) for j in range(1, h)]
    return pts, res, cs, {"alpha": a, "beta": b, **({"phi": phi} if damped else {})}


def _holt_winters(y, h, m, multiplicative=False):
    """Holt-Winters with seasonal indices initialised from all full seasons around a least-squares trend."""
    n = len(y)
    fit = ols(y[: (n // m) * m])
    base = fit["fit"]
    cycles = n // m
    if multiplicative:
        S0 = [sum(y[i + c * m] / base[i + c * m] for c in range(cycles)) / cycles if all(base[i + c * m] for c in range(cycles)) else 1.0 for i in range(m)]
        mean_s = sum(S0) / m
        S0 = [x / mean_s for x in S0]
    else:
        S0 = [sum(y[i + c * m] - base[i + c * m] for c in range(cycles)) / cycles for i in range(m)]
        mean_s = sum(S0) / m
        S0 = [x - mean_s for x in S0]
    best = None
    for a in (0.05, 0.1, 0.2, 0.4, 0.6):
        for b in (0.01, 0.05, 0.15):
            for g in (0.01, 0.1, 0.25):
                lvl, tr = fit["intercept"] + fit["slope"] * (m - 1), fit["slope"]
                S = list(S0)
                sse, res = 0.0, []
                for t in range(m, n):
                    si = S[t % m]
                    f = (lvl + tr) * si if multiplicative else lvl + tr + si
                    res.append(y[t] - f)
                    sse += (y[t] - f) ** 2
                    if multiplicative:
                        nl = a * (y[t] / si if si else y[t]) + (1 - a) * (lvl + tr)
                        S[t % m] = g * (y[t] / nl if nl else si) + (1 - g) * si
                    else:
                        nl = a * (y[t] - si) + (1 - a) * (lvl + tr)
                        S[t % m] = g * (y[t] - nl) + (1 - g) * si
                    tr = b * (nl - lvl) + (1 - b) * tr
                    lvl = nl
                if best is None or sse < best[0]:
                    best = (sse, a, b, g, lvl, tr, list(S), res)
    sse, a, b, g, lvl, tr, S, res = best
    if multiplicative:
        pts = [(lvl + k * tr) * S[(n - 1 + k) % m] for k in range(1, h + 1)]
    else:
        pts = [lvl + k * tr + S[(n - 1 + k) % m] for k in range(1, h + 1)]
    cs = [a * (1 + j * b) + (g if j % m == 0 else 0) for j in range(1, h)]
    return pts, res, cs, {"alpha": a, "beta": b, "gamma": g, "season_length": m, "seasonality": "multiplicative" if multiplicative else "additive"}


def _run(method, y, h, m):
    if method == "naive":
        res = [y[t] - y[t - 1] for t in range(1, len(y))]
        return [y[-1]] * h, res, [1.0] * (h - 1), {}
    if method == "seasonal_naive":
        res = [y[t] - y[t - m] for t in range(m, len(y))]
        pts = [y[len(y) - m + ((k - 1) % m)] for k in range(1, h + 1)]
        cs = [1.0 if j % m == 0 else 0.0 for j in range(1, h)]
        return pts, res, cs, {"season_length": m}
    if method == "holt":
        return _holt_any(y, h)
    if method == "holt_damped":
        return _holt_any(y, h, damped=True)
    return _holt_winters(y, h, m, multiplicative=(method == "holt_winters_mult"))


def seasonal_strength(y: list[float], m: int | None) -> float:
    """Share of detrended variance explained by the period-of-year means (0 = none, 1 = all)."""
    if not m or len(y) < 2 * m:
        return 0.0
    r = [a - b for a, b in zip(y, ols(y)["fit"])]
    means = [sum(r[i::m]) / len(r[i::m]) for i in range(m)]
    tot = sum(x * x for x in r)
    exp = sum(means[i % m] ** 2 for i in range(len(r)))
    return exp / tot if tot else 0.0


def expected(y: list[float], grain: str) -> tuple[list[float], str]:
    """Expected value per period for anomaly detection: trend, plus the usual seasonal swing when there is a real one."""
    fit = ols(y)["fit"]
    m = SEASON.get(grain)
    if m and seasonal_strength(y, m) >= 0.5:
        r = [a - b for a, b in zip(y, fit)]
        means = [sum(r[i::m]) / len(r[i::m]) for i in range(m)]
        return [f + means[i % m] for i, f in enumerate(fit)], "seasonal pattern"
    return fit, "trend"


def candidates(n: int, m: int | None) -> list[str]:
    out = ["naive"]
    if n >= 4:
        out += ["holt", "holt_damped"]
    if m and n >= m + 2:
        out.append("seasonal_naive")
    if m and n >= 2 * m:
        out.append("holt_winters")
        out.append("holt_winters_mult")
    return out


def forecast_series(y: list[float], grain: str, horizon: int) -> dict:
    """Pick the forecasting method that did best on a holdout of recent periods, then refit on all data.

    Candidates: last value, same period last year, Holt, damped Holt and Holt-Winters (when there are at least two
    full seasons). The holdout is the last full season when there are three seasons of history, otherwise the last 3 to 6
    periods, forecast from the point before them.
    Intervals are approximate 95% ranges from the ETS variance formulas using the in-sample one-step error."""
    n = len(y)
    m = SEASON.get(grain)
    m = m if m and n >= m + 2 else None
    cands = candidates(n, m)
    # a full season of holdout when there is room, so seasonal peaks are part of the test
    k = m if m and n >= 3 * m else (max(3, min(6, n // 5)) if n >= 8 else 0)
    scores = {}
    if k:
        train, test = y[:-k], y[-k:]
        for c in cands:
            if c not in candidates(len(train), m if m and len(train) >= m + 2 else None):
                continue
            pts = _run(c, train, k, m)[0]
            scores[c] = sum(abs(p - t) for p, t in zip(pts, test)) / k
    rank = {"naive": 0, "seasonal_naive": 1, "holt_damped": 2, "holt": 3, "holt_winters": 4, "holt_winters_mult": 5}
    chosen = min(scores, key=lambda c: (round(scores[c], 9), rank[c])) if scores else ("holt" if n >= 4 else "naive")
    seasonal = {"seasonal_naive", "holt_winters", "holt_winters_mult"}
    if chosen in seasonal:
        plain = [c for c in scores if c not in seasonal]
        best_plain = min(plain, key=lambda c: (scores[c], rank[c])) if plain else None
        # a seasonal method has to clearly beat the best non-seasonal one, or it is probably fitting noise
        if best_plain and (scores[chosen] > 0.9 * scores[best_plain] or seasonal_strength(y, m) < 0.5):
            chosen = best_plain
    pts, res, cs, params = _run(chosen, y, max(1, horizon), m)
    sigma = math.sqrt(sum(r * r for r in res) / max(1, len(res) - 2)) if res else 0.0
    if chosen in scores and k:
        # in-sample one-step errors understate real error; never let the range be tighter than the holdout error
        hold = _run(chosen, y[:-k], k, m)[0]
        sigma = max(sigma, math.sqrt(sum((p - t) ** 2 for p, t in zip(hold, y[-k:])) / k))
    out = []
    for hh in range(1, max(1, horizon) + 1):
        var = sigma ** 2 * (1 + sum(c * c for c in cs[:hh - 1]))
        w = 1.96 * math.sqrt(var)
        out.append({"value": pts[hh - 1], "lower": pts[hh - 1] - w, "upper": pts[hh - 1] + w, "sd": math.sqrt(var)})
    acc = None
    if chosen in scores and k:
        test = y[-k:]
        mape = (sum(abs((p - t) / t) for p, t in zip(_run(chosen, y[:-k], k, m)[0], test) if t) / k) if all(test) else None
        naive_mae = scores.get("naive")
        acc = {"holdout_periods": k, "mae": scores[chosen], "mape": mape,
               "vs_naive": (1 - scores[chosen] / naive_mae) if naive_mae else None, "all": scores}
    return {"method": chosen, "method_label": METHOD_LABEL[chosen], "params": params, "sigma": sigma, "points": out[:horizon], "accuracy": acc}


# ---------------------------------------------------------------- data access
def series(plat, user, model: dict, mname: str, grain: str = "month", filters: list | None = None, tdim: str | None = None,
           today: date | None = None) -> dict:
    if grain not in GRAINS:
        raise QueryError(f"Grain must be one of {', '.join(GRAINS)}.")
    td = time_dim(model, tdim)
    m = measure(model, mname)
    res = plat.run_model(user, model, {"measures": [mname], "dimensions": [td["name"]], "grains": {td["name"]: grain},
                                       "filters": filters or [], "limit": 5000})
    today = today or local_today()
    pts, partial, future = [], None, 0
    for p, v in res["rows"]:
        if p is None:
            continue
        start = _d(p)
        if start > today:
            future += 1
            continue
        if period_end(start, grain) > today + timedelta(days=1):
            partial = {"period": start.isoformat(), "label": label(start, grain), "value": v}
            continue
        pts.append({"period": start.isoformat(), "label": label(start, grain), "value": float(v) if v is not None else None})
    pts = [p for p in pts if p["value"] is not None]
    return {"measure": m, "time_dim": td, "grain": grain, "points": pts, "partial": partial, "future_periods": future}


def _between(td: dict, start: date, end_excl: date) -> dict:
    return {"field": td["name"], "op": "between", "value": [start.isoformat(), (end_excl - timedelta(days=1)).isoformat()]}


def by_dimension(plat, user, model, mname: str, dim: str, filters: list) -> dict:
    res = plat.run_model(user, model, {"measures": [mname], "dimensions": [dim], "filters": filters, "limit": 1000})
    return {("(blank)" if k is None else str(k)): (float(v) if v is not None else None) for k, v in res["rows"]}


def total(plat, user, model, mname: str, filters: list):
    res = plat.run_model(user, model, {"measures": [mname], "filters": filters, "limit": 1})
    v = res["rows"][0][0] if res["rows"] else None
    return float(v) if v is not None else None


def category_dims(model: dict, only: list[str] | None = None) -> list[dict]:
    ds = [d for d in model["dimensions"] if d["type"] == "category"]
    if only:
        ds = [d for d in ds if d["name"] in only]
    return ds[:MAX_DIMS]


# ---------------------------------------------------------------- analyses
def explain_change(plat, user, model: dict, mname: str, grain: str = "month", filters: list | None = None,
                   current: str | None = None, baseline: str | None = None, dims: list[str] | None = None, top: int = 5) -> dict:
    """Why did a measure move between two periods? Splits the change by every category dimension."""
    filters = [f for f in (filters or []) if not _is_time_filter(model, f)]
    m = measure(model, mname)
    td = time_dim(model)
    if current:
        cur = _d(current)
    else:
        s = series(plat, user, model, mname, grain, filters)
        if len(s["points"]) < 2:
            raise QueryError("Need at least two complete periods to explain a change.")
        cur = _d(s["points"][-1]["period"])
    base = _d(baseline) if baseline else shift(cur, grain, -1)
    fc = filters + [_between(td, cur, period_end(cur, grain))]
    fb = filters + [_between(td, base, period_end(base, grain))]
    tc, tb = total(plat, user, model, mname, fc), total(plat, user, model, mname, fb)
    add = additive(m)
    delta = (tc or 0) - (tb or 0)
    out_dims = []
    for d in category_dims(model, dims):
        a, b = by_dimension(plat, user, model, mname, d["name"], fb), by_dimension(plat, user, model, mname, d["name"], fc)
        members = []
        for k in set(a) | set(b):
            va, vb = a.get(k), b.get(k)
            ch = (vb or 0) - (va or 0) if add else (None if va is None or vb is None else vb - va)
            members.append({"member": k, "baseline": va, "current": vb, "change": ch,
                            "share_of_change": (ch / delta) if add and delta and ch is not None else None})
        members = [x for x in members if x["change"] is not None]
        members.sort(key=lambda x: -abs(x["change"]))
        if not members:
            continue
        if add:
            gross = sum(abs(x["change"]) for x in members)
            same = [x for x in members if delta and (x["change"] > 0) == (delta > 0)]
            score = (abs(same[0]["change"]) / gross) if same and gross else 0.0
        else:
            spread = max(x["change"] for x in members) - min(x["change"] for x in members)
            score = spread / (abs(tb) if tb else 1)
        out_dims.append({"dimension": d["name"], "label": d["label"], "score": score, "members": members[:top], "member_count": len(members)})
    out_dims.sort(key=lambda x: -x["score"])
    summary = None
    if out_dims and tc is not None and tb is not None:
        d0 = out_dims[0]
        head = (f"{m['label']} went from {fmt(tb, m)} in {label(base, grain)} to {fmt(tc, m)} in {label(cur, grain)} "
                f"({fmt_delta(tb, tc, m)}).")
        if add and delta:
            lead = next((x for x in d0["members"] if (x["change"] > 0) == (delta > 0)), None)
            against = next((x for x in d0["members"] if (x["change"] > 0) != (delta > 0) and x["change"]), None)
            parts = [head]
            if lead:
                parts.append(f"By {d0['label']}, {lead['member']} moved most in the same direction ({_signed(lead['change'], m)}).")
            if against and abs(against["change"]) >= 0.25 * abs(delta):
                parts.append(f"{against['member']} partly offset it ({_signed(against['change'], m)}).")
            summary = " ".join(parts)
        elif d0["members"]:
            m0 = d0["members"][0]
            summary = f"{head} The largest movement by {d0['label']} is {m0['member']} ({fmt(m0['baseline'], m)} → {fmt(m0['current'], m)})."
    return {"measure": mname, "label": m["label"], "format": m["format"], "decimals": m["decimals"], "additive": add,
            "current": {"period": cur.isoformat(), "label": label(cur, grain), "value": tc},
            "baseline": {"period": base.isoformat(), "label": label(base, grain), "value": tb},
            "change": delta if tc is not None and tb is not None else None, "pct_change": pct(tb, tc), "dimensions": out_dims, "summary": summary,
            "method": ("Additive measure: member changes sum to the total change. Dimensions are ranked by how much of the gross movement one member carries." if add else
                       "Not additive (average, ratio or distinct count): member changes do not sum to the total; ranked by spread between members.")}


def decompose(plat, user, model: dict, mname: str, path: list[dict] | None = None, dimension: str | None = None,
              mode: str = "high", filters: list | None = None, top: int = 10) -> dict:
    """One level of a decomposition tree. path is a list of {field, value}; dimension picks the split, or mode
    'high'/'low' picks the dimension whose top/bottom member stands out most (an 'AI split')."""
    m = measure(model, mname)
    path = path or []
    dims = {d["name"]: d for d in model["dimensions"]}
    for p in path:
        if p.get("field") not in dims:
            raise QueryError(f"Unknown dimension '{p.get('field')}' in path.")
    flt = list(filters or []) + [{"field": p["field"], "op": "=", "value": p["value"]} for p in path]
    node_total = total(plat, user, model, mname, flt)
    used = {p["field"] for p in path}
    candidates = [d for d in category_dims(model) if d["name"] not in used]
    if not candidates:
        return {"measure": mname, "total": node_total, "dimension": None, "children": [], "remaining": []}
    chosen, why = None, None
    if dimension:
        if dimension not in {d["name"] for d in candidates}:
            raise QueryError(f"'{dimension}' is not an available split here.")
        chosen = dimension
    else:
        best = None
        for d in candidates:
            vals = [v for v in by_dimension(plat, user, model, mname, d["name"], flt).values() if v is not None]
            if len(vals) < 2:
                continue
            mean = sum(vals) / len(vals)
            if not mean:
                continue
            score = (max(vals) / mean) if mode == "high" else (mean / min(vals) if min(vals) > 0 else float("inf"))
            if best is None or score > best[0]:
                best = (score, d["name"])
        chosen = best[1] if best else candidates[0]["name"]
        why = f"Picked {dims[chosen]['label']} because its {'highest' if mode == 'high' else 'lowest'} member stands out most from the average."
    vals = by_dimension(plat, user, model, mname, chosen, flt)
    add = additive(m)
    kids = sorted(({"member": k, "value": v, "share": (v / node_total) if add and node_total and v is not None else None} for k, v in vals.items()),
                  key=lambda x: -(x["value"] if x["value"] is not None else -math.inf))
    if mode == "low" and not dimension:
        kids.reverse()
    others = kids[top:]
    kids = kids[:top]
    if others and add:
        kids.append({"member": f"{len(others)} others", "value": sum(o["value"] or 0 for o in others), "share": None, "other": True})
    return {"measure": mname, "label": m["label"], "format": m["format"], "decimals": m["decimals"], "additive": add, "path": path,
            "total": node_total, "dimension": chosen, "dimension_label": dims[chosen]["label"], "children": kids, "why": why,
            "remaining": [d["name"] for d in candidates if d["name"] != chosen]}


def concentration(vals: dict[str, float]) -> dict | None:
    xs = sorted((v for v in vals.values() if v is not None and v > 0), reverse=True)
    if len(xs) < 3:
        return None
    tot = sum(xs)
    run, k = 0.0, 0
    for v in xs:
        run += v
        k += 1
        if run >= 0.8 * tot:
            break
    return {"members": len(xs), "top_share": xs[0] / tot, "members_for_80pct": k, "fraction_for_80pct": k / len(xs)}


def model_insights(plat, user, model: dict, measures: list[str] | None = None, grain: str = "month", filters: list | None = None,
                   horizon: int = 3, today: date | None = None, with_drivers: bool = True) -> dict:
    """Facts about each measure. Each fact has id, kind, severity (good, bad, warn, info), title, text and data."""
    has_time = any(d["type"] == "time" for d in model["dimensions"])
    measures = measures or [m["name"] for m in model["measures"]][:4]
    facts, series_out = [], {}
    base_filters = [f for f in (filters or []) if not _is_time_filter(model, f)] if has_time else list(filters or [])
    for mn in measures:
        m = measure(model, mn)
        if has_time:
            s = series(plat, user, model, mn, grain, filters, today=today)
            pts = s["points"]
            ys = [p["value"] for p in pts]
            entry = {"points": pts, "partial": s["partial"], "grain": grain}
            if len(ys) >= 2:
                a, b = ys[-2], ys[-1]
                dw = direction_word(b - a, m)
                facts.append({"kind": "change", "measure": mn, "severity": dw if a != b else "info",
                              "title": f"{m['label']}: {fmt(b, m)} in {pts[-1]['label']}",
                              "text": f"{m['label']} was {fmt(b, m)} in {pts[-1]['label']}, {fmt_delta(a, b, m)} vs {pts[-2]['label']} ({fmt(a, m)}).",
                              "data": {"current": b, "previous": a, "pct_change": pct(a, b)}})
            if len(ys) >= 4:
                fit = ols(ys)
                entry["trend"] = {"slope": fit["slope"], "r2": fit["r2"]}
                rel = fit["slope"] / abs(fit["mean"]) if fit["mean"] else 0
                if fit["r2"] >= 0.3 and abs(rel) >= 0.01:
                    facts.append({"kind": "trend", "measure": mn, "severity": direction_word(fit["slope"], m),
                                  "title": f"{m['label']} is trending {'up' if fit['slope'] > 0 else 'down'}",
                                  "text": f"Across {len(ys)} {grain}s, {m['label']} moves about {fmt(abs(fit['slope']), m)} per {grain} "
                                          f"({'+' if rel > 0 else '−'}{abs(rel) * 100:.1f}% of its average; R² {fit['r2']:.2f}).",
                                  "data": {"slope": fit["slope"], "relative": rel, "r2": fit["r2"], "periods": len(ys)}})
                if len(ys) >= 8:
                    exp, basis = expected(ys, grain)
                    outs = robust_outliers(ys, exp)
                    entry["anomalies"] = [{"period": pts[i]["period"], "label": pts[i]["label"], "value": ys[i], "expected": exp[i], "z": z} for i, z in outs]
                    for i, z in outs[-3:]:
                        facts.append({"kind": "anomaly", "measure": mn, "severity": "warn",
                                      "title": f"Unusual {m['label']} in {pts[i]['label']}",
                                      "text": f"{m['label']} was {fmt(ys[i], m)} in {pts[i]['label']}, {'above' if z > 0 else 'below'} the {basis} "
                                              f"of about {fmt(exp[i], m)} (robust z {z:+.1f}).",
                                      "data": {"period": pts[i]["period"], "value": ys[i], "expected": exp[i], "z": z}})
            if len(ys) >= 6:
                fc = forecast_series(ys, grain, horizon)
                last = _d(pts[-1]["period"])
                fpts = [{"period": shift(last, grain, h + 1).isoformat(), "label": label(shift(last, grain, h + 1), grain), **p}
                        for h, p in enumerate(fc["points"])]
                entry["forecast"] = {"points": fpts, "method": fc["method"], "method_label": fc["method_label"], "accuracy": fc["accuracy"]}
                f1 = fpts[0]
                acc = fc["accuracy"]
                acc_txt = f"; off by {acc['mape'] * 100:.1f}% on average over the last {acc['holdout_periods']} {grain}s when tested" if acc and acc.get("mape") is not None else ""
                facts.append({"kind": "forecast", "measure": mn, "severity": "info",
                              "title": f"{m['label']} forecast for {f1['label']}: {fmt(f1['value'], m)}",
                              "text": f"Projected {m['label']} for {f1['label']} is {fmt(f1['value'], m)} "
                                      f"(95% range {fmt(f1['lower'], m)} to {fmt(f1['upper'], m)}; {fc['method_label']} chosen on {len(ys)} {grain}s{acc_txt}).",
                              "data": {"points": fpts, "method": fc["method"], "accuracy": acc}})
            if s["partial"] and s["partial"]["value"] is not None:
                entry["partial_note"] = f"{s['partial']['label']} is still in progress and left out of trend, anomaly and forecast calculations."
            if s["future_periods"]:
                entry["future_note"] = f"{s['future_periods']} period(s) dated after today were left out; check the date quality rules."
            series_out[mn] = entry
            if with_drivers and len(ys) >= 2 and ys[-1] != ys[-2]:
                try:
                    ex = explain_change(plat, user, model, mn, grain, base_filters, current=pts[-1]["period"], baseline=pts[-2]["period"], top=3)
                    if ex["summary"]:
                        facts.append({"kind": "driver", "measure": mn, "severity": direction_word(ex["change"] or 0, m), "title": f"What moved {m['label']}",
                                      "text": ex["summary"], "data": {"dimension": ex["dimensions"][0]["dimension"], "members": ex["dimensions"][0]["members"],
                                                                     "additive": ex["additive"]}})
                except QueryError:
                    pass  # a driver fact is optional; other facts still stand
        if additive(m):
            for d in category_dims(model)[:4]:
                c = concentration(by_dimension(plat, user, model, mn, d["name"], filters or []))
                if c and (c["top_share"] >= 0.5 or (c["members"] >= 5 and c["fraction_for_80pct"] <= 0.2)):
                    facts.append({"kind": "concentration", "measure": mn, "severity": "warn" if c["top_share"] >= 0.5 else "info",
                                  "title": f"{m['label']} is concentrated by {d['label']}",
                                  "text": f"The top {d['label'].lower()} holds {c['top_share'] * 100:.0f}% of {m['label']}; "
                                          f"{c['members_for_80pct']} of {c['members']} account for 80%.",
                                  "data": {"dimension": d["name"], **c}})
    for i, f in enumerate(facts):
        f["id"] = f"F{i + 1}"
    rank = {"bad": 0, "warn": 1, "good": 2, "info": 3}
    kinds = {"change": 0, "driver": 1, "anomaly": 2, "trend": 3, "forecast": 4, "concentration": 5}
    facts.sort(key=lambda f: (measures.index(f["measure"]), kinds[f["kind"]], rank[f["severity"]]))
    return {"model_id": model.get("id"), "model": model["name"], "grain": grain, "facts": facts, "series": series_out,
            "methods": __doc__.split("Methods", 1)[1].strip() if __doc__ else ""}


def analyze_result(model: dict, res: dict, forecast=None, anomalies: bool = False, today: date | None = None) -> dict | None:
    """Forecast band and anomaly markers for a single-time-dimension chart, computed from rows already fetched."""
    fields = res.get("fields") or []
    dims = [f for f in fields if f["kind"] == "dimension"]
    meas = [f for f in fields if f["kind"] == "measure"]
    if len(dims) != 1 or dims[0].get("type") != "time" or not meas or not res.get("rows"):
        return None
    grain = dims[0].get("grain") or "month"
    horizon = 3 if forecast is True else max(0, min(int(forecast or 0), 24))
    today = today or local_today()
    out = {"grain": grain, "measure": meas[0]["name"]}
    pts = []
    for r in res["rows"]:
        if r[0] is None or r[1] is None:
            continue
        start = _d(r[0])
        if start > today:
            continue
        if period_end(start, grain) > today + timedelta(days=1):
            out["partial"] = start.isoformat()
            continue
        pts.append((start, float(r[1])))
    ys = [v for _, v in pts]
    if anomalies and len(ys) >= 8:
        exp, _ = expected(ys, grain)
        out["anomalies"] = [{"period": pts[i][0].isoformat(), "value": ys[i], "expected": exp[i], "z": z} for i, z in robust_outliers(ys, exp)]
    if horizon and len(ys) >= 6:
        fc = forecast_series(ys, grain, horizon)
        out["forecast"] = [{"period": shift(pts[-1][0], grain, h + 1).isoformat(), **p} for h, p in enumerate(fc["points"])]
        out["forecast_method"] = fc["method_label"]
        out["forecast_accuracy"] = fc["accuracy"]
    elif horizon:
        out["note"] = "Forecasts need at least 6 complete periods."
    return out


def _is_time_filter(model: dict, f: dict) -> bool:
    return any(d["name"] == f.get("field") and d["type"] == "time" for d in model["dimensions"])


# ---------------------------------------------------------------- narrative
NARRATIVE_PROMPT = ("You write short, plain-language summaries of business metrics for a dashboard. "
                    "Use ONLY the numbered facts provided; do not add numbers, causes or recommendations that are not in them. "
                    "Write 3 to 5 bullet points, most important first, each ending with the fact ids it relies on in brackets, like [F2]. "
                    "If the facts are thin, say so in one bullet.")


def narrative(provider, facts: list[dict]) -> dict:
    lines = "\n".join(f"{f['id']} ({f['kind']}, {f['severity']}): {f['text']}" for f in facts[:30])
    resp = provider.complete(NARRATIVE_PROMPT, [{"role": "user", "content": "Facts:\n" + lines}], [])
    text = resp["text"].strip()
    known = {f["id"] for f in facts}
    cited = set(re.findall(r"\bF\d+\b", text))
    return {"text": text, "unknown_citations": sorted(cited - known), "tokens_in": resp["tokens_in"], "tokens_out": resp["tokens_out"]}
