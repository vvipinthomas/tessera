"""Ask a question in plain English and get a governed chart, with no language model involved.

The parser maps words to a semantic model's measures and dimensions (names, labels and glossary synonyms), to
dimension values ("in BC", "web"), to time ranges ("last quarter", "this year", "last 6 months", "in 2025",
"year to date"), to ranking ("top 5", "lowest 3"), grains ("by month", "monthly", "trend") and visuals ("share",
"breakdown", "compare to last year"). It returns the query it built and a plain description of how it read the
question, so people can see and correct the interpretation. Anything it did not understand is listed, never guessed.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .semantic import _add_months

STOP = {"what", "is", "was", "were", "are", "the", "a", "an", "of", "for", "in", "on", "to", "show", "me", "give", "how", "much", "many",
        "did", "do", "does", "we", "our", "my", "and", "with", "per", "by", "over", "at", "please", "list", "see", "chart", "graph", "plot",
        "total", "number", "count", "all", "each", "every", "which", "who", "where", "when", "has", "have", "had", "that", "this", "it",
        "from", "about", "across", "vs", "versus", "compared", "compare", "than", "be", "been", "there", "make", "get", "tell"}
GRAIN_WORDS = {"daily": "day", "day": "day", "days": "day", "weekly": "week", "week": "week", "weeks": "week", "monthly": "month", "month": "month",
               "months": "month", "quarterly": "quarter", "quarter": "quarter", "quarters": "quarter", "yearly": "year", "annual": "year",
               "annually": "year", "year": "year", "years": "year"}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%$.\- ]+", " ", s.lower().replace("_", " ")).strip()


def _quarter_start(d: date) -> date:
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def time_range(q: str, today: date) -> tuple[tuple[date, date] | None, str | None, str]:
    """Return ((start, end inclusive), label, text with the phrase removed)."""
    t = q
    ms = date(today.year, today.month, 1)
    rules = [
        (r"\b(year to date|ytd|this year so far)\b", lambda m: ((date(today.year, 1, 1), today), "year to date")),
        (r"\b(month to date|mtd)\b", lambda m: ((ms, today), "month to date")),
        (r"\b(quarter to date|qtd)\b", lambda m: ((_quarter_start(today), today), "quarter to date")),
        (r"\blast (\d+|" + "|".join(NUMBERS) + r") (day|week|month|year)s?\b", None),
        (r"\b(last|previous) month\b", lambda m: ((_add_months(ms, -1), ms - timedelta(days=1)), "last month")),
        (r"\bthis month\b", lambda m: ((ms, today), "this month")),
        (r"\b(last|previous) quarter\b", lambda m: ((_add_months(_quarter_start(today), -3), _quarter_start(today) - timedelta(days=1)), "last quarter")),
        (r"\bthis quarter\b", lambda m: ((_quarter_start(today), today), "this quarter")),
        (r"\b(last|previous) year\b(?! *same)", lambda m: ((date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)), f"{today.year - 1}")),
        (r"\bthis year\b", lambda m: ((date(today.year, 1, 1), today), f"{today.year} so far")),
        (r"\bsince (20\d\d)(?:-(\d\d))?\b", lambda m: ((date(int(m.group(1)), int(m.group(2) or 1), 1), today), f"since {m.group(1)}{'-' + m.group(2) if m.group(2) else ''}")),
        (r"\b(?:in |during )?q([1-4]) (20\d\d)\b", lambda m: ((date(int(m.group(2)), 3 * int(m.group(1)) - 2, 1),
                                                             _add_months(date(int(m.group(2)), 3 * int(m.group(1)) - 2, 1), 3) - timedelta(days=1)), f"Q{m.group(1)} {m.group(2)}")),
        (r"\b(?:in |during )?(20\d\d)\b", lambda m: ((date(int(m.group(1)), 1, 1), date(int(m.group(1)), 12, 31)), m.group(1))),
    ]
    for pat, fn in rules:
        m = re.search(pat, t)
        if not m:
            continue
        if fn is None:
            n = m.group(1)
            n = int(n) if n.isdigit() else NUMBERS[n]
            unit = m.group(2)
            if unit == "day":
                rng = (today - timedelta(days=n - 1), today)
            elif unit == "week":
                rng = (today - timedelta(days=7 * n - 1), today)
            elif unit == "month":
                rng = (_add_months(ms, -n), ms - timedelta(days=1))  # last n complete months
            else:
                rng = (date(today.year - n, 1, 1), date(today.year - 1, 12, 31))
            label = f"last {n} {unit}{'s' if n > 1 else ''}" + (" (complete)" if unit in ("month", "year") else "")
        else:
            rng, label = fn(m)
        return rng, label, (t[:m.start()] + " " + t[m.end():]).strip()
    return None, None, t


def _phrases(item: dict, extra: list[str] | None = None) -> list[str]:
    out = {_norm(item["name"]), _norm(item.get("label") or "")}
    for x in extra or []:
        out.add(_norm(x))
    return sorted((p for p in out if p), key=len, reverse=True)


def parse(question: str, model: dict, values: dict[str, list[str]] | None = None, synonyms: dict[str, list[str]] | None = None,
          today: date | None = None) -> dict:
    """values: dimension name -> known member values. synonyms: measure/dimension name -> extra phrases."""
    today = today or date.today()
    values = values or {}
    synonyms = synonyms or {}
    raw = question.strip()
    q = " " + _norm(raw) + " "
    understood, notes = [], []

    rng, rng_label, q = time_range(q, today)
    q = " " + q + " "

    # ranking
    top = None
    m = re.search(r"\b(top|bottom|best|worst|highest|lowest|largest|smallest|biggest)\s+(\d+|" + "|".join(NUMBERS) + r")?\b", q)
    if m:
        n = m.group(2)
        top = {"dir": "asc" if m.group(1) in ("bottom", "worst", "lowest", "smallest") else "desc",
               "n": (int(n) if n and n.isdigit() else NUMBERS.get(n or "", 0)) or (1 if m.group(1) in ("best", "worst", "highest", "lowest", "biggest", "largest", "smallest") else 10)}
        q = q[:m.start()] + " " + q[m.end():]

    # measures
    measures = []
    for meas in model["measures"]:
        for ph in _phrases(meas, synonyms.get(meas["name"])):
            if re.search(rf"\b{re.escape(ph)}s?\b", q):
                measures.append(meas["name"])
                q = re.sub(rf"\b{re.escape(ph)}s?\b", " ", q, count=1)
                break

    # grains and time dimension
    tdims = [d for d in model["dimensions"] if d["type"] == "time"]
    grain = None
    m = re.search(r"\b(?:by|per|each|every)\s+(day|week|month|quarter|year)\b|\b(daily|weekly|monthly|quarterly|yearly|annually|annual)\b", q)
    if m:
        grain = GRAIN_WORDS[m.group(1) or m.group(2)]
        q = q[:m.start()] + " " + q[m.end():]
    fc_words = r"\b(forecast|forecasts|predict|prediction|projection|project|outlook)\b"
    trend = bool(re.search(r"\b(trend|trending|over time|history|timeline|time series)\b", q)) or bool(re.search(fc_words, q))
    q = re.sub(r"\b(trend|trending|over time|history|timeline|time series)\b", " ", q)
    q = re.sub(fc_words, " ", q)

    # dimensions named explicitly ("by province", "province")
    dims = []
    for d in model["dimensions"]:
        if d["type"] == "time":
            continue
        for ph in _phrases(d, synonyms.get(d["name"])):
            if re.search(rf"\b{re.escape(ph)}s?\b", q) or (ph.endswith("y") and re.search(rf"\b{re.escape(ph[:-1])}ies\b", q)):
                dims.append(d["name"])
                q = re.sub(rf"\b{re.escape(ph)}(s|ies)?\b", " ", q, count=1)
                break
    if tdims and (grain or trend):
        dims.insert(0, tdims[0]["name"])

    # filters from known member values ("in BC", "web", "not refunded")
    filters = []
    for dname, vals in values.items():
        hits = []
        for v in sorted({str(x) for x in vals if x is not None and str(x).strip()}, key=len, reverse=True):
            nv = _norm(v)
            if len(nv) < 2 or nv in STOP:
                continue
            mm = re.search(rf"\b(excluding |except |not |without )?{re.escape(nv)}\b", q)
            if mm:
                hits.append((v, bool(mm.group(1))))
                q = q[:mm.start()] + " " + q[mm.end():]
        if hits:
            inc = [v for v, neg in hits if not neg]
            exc = [v for v, neg in hits if neg]
            if inc:
                filters.append({"field": dname, "op": "in", "value": inc})
            if exc:
                filters.append({"field": dname, "op": "not_in", "value": exc})

    if rng and tdims:
        filters.append({"field": tdims[0]["name"], "op": "between", "value": [rng[0].isoformat(), rng[1].isoformat()]})
    elif rng:
        notes.append("This model has no date field, so the time range was ignored.")

    share = bool(re.search(r"\b(share|split|breakdown|mix|proportion|percentage of)\b", q))
    yoy = bool(re.search(r"\b(previous period|prior period|last year same|same period|growth|change)\b", q))
    q = re.sub(r"\b(share|split|breakdown|mix|proportion|percentage of|previous period|prior period|last year same|same period|growth|change)\b", " ", q)

    if not measures:
        # "how many orders" → count measure with that word; otherwise the model's first measure
        measures = [model["measures"][0]["name"]]
        notes.append(f"No measure named; using {model['measures'][0]['label']}.")
    leftovers = [w for w in q.split() if w not in STOP and not re.fullmatch(r"[\d.$%-]+", w) and len(w) > 1]

    spec = {"measures": list(dict.fromkeys(measures)), "dimensions": list(dict.fromkeys(dims)), "filters": filters, "grains": {}, "limit": 5000}
    if tdims and tdims[0]["name"] in spec["dimensions"]:
        spec["grains"][tdims[0]["name"]] = grain or ("day" if rng and (rng[1] - rng[0]).days <= 45 else "month")
    cat = [x for x in spec["dimensions"] if not tdims or x != tdims[0]["name"]]
    timed = bool(tdims) and tdims[0]["name"] in spec["dimensions"]
    if top and cat:
        spec["sort"] = [{"field": spec["measures"][0], "dir": top["dir"]}]
        spec["limit"] = top["n"]
    elif cat and not timed:
        spec["sort"] = [{"field": spec["measures"][0], "dir": "desc"}]

    # visual
    if not spec["dimensions"]:
        viz = {"type": "kpi"}
    elif timed and not cat:
        viz = {"type": "line", "x": tdims[0]["name"], "y": spec["measures"][0], "forecast": 3 if re.search(r"\b(forecast|predict|projection|outlook|next)", raw.lower()) else None}
    elif timed and cat:
        viz = {"type": "multiline", "x": tdims[0]["name"], "series": cat[0], "y": spec["measures"][0]} if len(spec["measures"]) == 1 else {"type": "pivot"}
    elif len(cat) == 1 and len(spec["measures"]) == 2:
        viz = {"type": "combo", "x": cat[0], "y": spec["measures"][0], "y2": spec["measures"][1]}
    elif len(cat) == 1 and share:
        viz = {"type": "donut", "x": cat[0], "y": spec["measures"][0]}
    elif len(cat) == 1:
        viz = {"type": "bar", "x": cat[0], "y": spec["measures"][0]}
    elif len(cat) == 2:
        viz = {"type": "stacked", "x": cat[0], "series": cat[1], "y": spec["measures"][0]}
    else:
        viz = {"type": "table"}
    viz = {k: v for k, v in viz.items() if v is not None}
    compare = (viz["type"] == "kpi" and rng is not None) or (yoy and viz["type"] == "kpi" and rng is not None)

    # plain-language reading
    lab = {x["name"]: x["label"] for x in model["measures"] + model["dimensions"]}
    parts = [" and ".join(lab[x] for x in spec["measures"])]
    if spec["dimensions"]:
        parts.append("by " + ", ".join((f"{lab[x]} ({spec['grains'][x]})" if x in spec["grains"] else lab[x]) for x in spec["dimensions"]))
    for f in filters:
        if f["op"] == "between":
            continue
        parts.append(f"{lab.get(f['field'], f['field'])} {'is' if f['op'] == 'in' else 'is not'} {', '.join(map(str, f['value']))}")
    if rng_label:
        parts.append(rng_label)
    if top and cat:
        parts.append(f"{'top' if top['dir'] == 'desc' else 'bottom'} {top['n']}")
    if compare:
        parts.append("compared with the previous period")
    understood = " · ".join(parts)
    if leftovers:
        notes.append("Not understood: " + ", ".join(dict.fromkeys(leftovers)))
    return {"question": raw, "spec": spec, "viz": viz, "compare": compare, "understood": understood, "notes": notes,
            "confidence": max(0.0, 1.0 - 0.15 * len(set(leftovers)) - (0.3 if any(n.startswith("No measure") for n in notes) else 0))}


def score(question: str, model: dict, synonyms: dict | None = None) -> int:
    """How many of the model's measure and dimension phrases appear in the question (to pick a model)."""
    q = " " + _norm(question) + " "
    n = 0
    for item in model["measures"] + model["dimensions"]:
        if any(re.search(rf"\b{re.escape(p)}s?\b", q) for p in _phrases(item, (synonyms or {}).get(item["name"]))):
            n += 2 if item in model["measures"] else 1
    return n
