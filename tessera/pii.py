"""PII detection and masking policies. Masked copies are published to the `serving` schema."""
from __future__ import annotations

import re

from .warehouse import Warehouse, qi


def luhn(d: str) -> bool:
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        s += n
        alt = not alt
    return s % 10 == 0


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v)


DETECTORS = [
    {"key": "email", "label": "Email address", "level": "confidential", "mask": "hash",
     "test": lambda v: bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}", v))},
    {"key": "ca_sin", "label": "Canadian SIN", "level": "restricted", "mask": "partial",
     "test": lambda v: bool(re.fullmatch(r"\d{3}[- ]?\d{3}[- ]?\d{3}", v)) and _digits(v)[0] not in "08" and luhn(_digits(v))},
    {"key": "us_ssn", "label": "US SSN", "level": "restricted", "mask": "partial",
     "test": lambda v: bool(re.fullmatch(r"(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}", v))},
    {"key": "payment_card", "label": "Payment card number", "level": "restricted", "mask": "partial",
     "test": lambda v: bool(re.fullmatch(r"\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}", v)) and 13 <= len(_digits(v)) <= 19 and luhn(_digits(v))},
    {"key": "iban", "label": "IBAN", "level": "restricted", "mask": "partial",
     "test": lambda v: bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9 ]{11,30}", v))},
    {"key": "phone", "label": "Phone number", "level": "confidential", "mask": "partial",
     "test": lambda v: bool(re.fullmatch(r"(\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", v))},
    {"key": "ip_address", "label": "IP address", "level": "confidential", "mask": "hash",
     "test": lambda v: bool(re.fullmatch(r"(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}", v))},
    {"key": "postal_code", "label": "Postal or ZIP code", "level": "confidential", "mask": "truncate3",
     "test": lambda v: bool(re.fullmatch(r"[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d|\d{5}(-\d{4})?", v))},
]
HEADER_HINTS = [
    (r"(^|_)(full_?name|first_?name|last_?name|surname|given_?name|customer_?name|contact_?name)$", "person_name", "Person name", "confidential", "redact"),
    (r"(^|_)(dob|date_?of_?birth|birth_?date|birthday)$", "date_of_birth", "Date of birth", "restricted", "year_only"),
    (r"(^|_)(address|street|address_?line_?\d?)$", "street_address", "Street address", "confidential", "redact"),
    (r"(^|_)(password|passwd|secret|api_?key|token)$", "credential", "Credential", "restricted", "redact"),
    (r"(^|_)(passport|driver_?licen[cs]e|health_?card)(_?(no|number|num))?$", "government_id", "Government ID", "restricted", "redact"),
]
MASKS = {
    "none": "Keep as is",
    "redact": "Replace with [REDACTED]",
    "partial": "Keep last 4 characters",
    "hash": "Keyed SHA-256 (joinable, not reversible)",
    "email_domain": "Keep email domain only",
    "truncate3": "Keep first 3 characters",
    "year_only": "Keep year only",
    "null": "Replace with NULL",
}
LEVELS = ("public", "internal", "confidential", "restricted")


def scan(wh: Warehouse, schema: str, name: str, sample: int = 1000) -> list[dict]:
    """Classify each column by sampling values. Returns per-column findings."""
    out = []
    table = f"{qi(schema)}.{qi(name)}"
    for c in wh.columns(schema, name):
        col = c["name"]
        vals = [r[0] for r in wh.internal(
            f"SELECT trim(CAST({qi(col)} AS VARCHAR)) FROM {table} WHERE {qi(col)} IS NOT NULL "
            f"AND trim(CAST({qi(col)} AS VARCHAR)) <> '' USING SAMPLE {int(sample)} ROWS")]
        best = None
        if vals:
            for d in DETECTORS:
                hits = sum(1 for v in vals if d["test"](v))
                ratio = hits / len(vals)
                if ratio >= 0.6 and (best is None or ratio > best["ratio"]):
                    best = {"pii": d["key"], "label": d["label"], "level": d["level"], "mask": d["mask"], "ratio": round(ratio, 3), "method": "values"}
        if best is None:
            for pat, key, label, level, mask in HEADER_HINTS:
                if re.search(pat, col.lower()):
                    best = {"pii": key, "label": label, "level": level, "mask": mask, "ratio": None, "method": "column name"}
                    break
        out.append({"column": col, "type": c["type"], **(best or {"pii": None, "label": None, "level": "internal", "mask": "none", "ratio": None, "method": None})})
    return out


def mask_expr(col: str, mask: str, salt: str) -> str:
    c = qi(col)
    v = f"CAST({c} AS VARCHAR)"
    s = salt.replace("'", "")
    if mask in (None, "none"):
        return c
    if mask == "redact":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE '[REDACTED]' END"
    if mask == "null":
        return "CAST(NULL AS VARCHAR)"
    if mask == "partial":
        d = f"regexp_replace({v}, '[\\s-]', '', 'g')"
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE repeat('*', greatest(length({d}) - 4, 0)) || right({d}, 4) END"
    if mask == "hash":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE substr(sha256('{s}' || lower(trim({v}))), 1, 16) END"
    if mask == "email_domain":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE '***@' || split_part({v}, '@', 2) END"
    if mask == "truncate3":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE left(regexp_replace({v}, '\\s', '', 'g'), 3) END"
    if mask == "year_only":
        return f"CAST(year(TRY_CAST({c} AS DATE)) AS INTEGER)"
    raise ValueError(f"Unknown mask {mask}")


def publish(wh: Warehouse, schema: str, name: str, columns: list[dict], salt: str, target: str | None = None) -> int:
    """Create serving.<target> as a masked copy. Columns without a policy pass through unchanged."""
    policy = {c["name"]: c.get("mask", "none") for c in columns}
    target = target or name
    exprs = [f"{mask_expr(c['name'], policy.get(c['name'], 'none'), salt)} AS {qi(c['name'])}" for c in wh.columns(schema, name)]
    with wh.write_lock:
        wh.internal(f"CREATE OR REPLACE TABLE serving.{qi(target)} AS SELECT {', '.join(exprs)} FROM {qi(schema)}.{qi(name)}")
    return wh.row_count("serving", target)
