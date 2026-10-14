from datetime import date, datetime, timedelta

import pyarrow as pa
import pytest

from tessera import pii, quality
from tessera.warehouse import Warehouse


@pytest.fixture
def wh(tmp_path):
    w = Warehouse(tmp_path / "q.duckdb")
    today = date.today()
    w.load_arrow(pa.table({
        "id": ["a", "b", "b", "c", None],
        "amount": [10.0, -5.0, 3.0, None, 1e6],
        "status": ["paid", "paid", "PAID ", "refunded", "paid"],
        "email": ["x@y.ca", "bad_at_y.ca", "", None, "q@r.com"],
        "d": [today, today, today + timedelta(days=30), today, today],
        "ts": [datetime.now() - timedelta(hours=1)] * 5,
    }), "raw", "t")
    return w


def run(wh, kind, col=None, **params):
    return quality.run_rule(wh, "raw", "t", {"kind": kind, "column_name": col, "params": params})


def test_rule_kinds(wh):
    assert run(wh, "not_null", "id")["failing"] == 1
    assert run(wh, "not_null", "email")["failing"] == 2  # NULL and empty string
    r = run(wh, "unique", "id")
    assert r["failing"] == 1 and r["sample"]["rows"][0] == ["b", 2]
    assert run(wh, "range", "amount", min=0)["failing"] == 1
    assert run(wh, "range", "amount", min=0, max=1000)["failing"] == 2
    assert run(wh, "regex", "email", pattern=quality.PATTERNS["email"])["failing"] == 1  # empties are not_null's job
    assert run(wh, "allowed_values", "status", values=["paid", "refunded"])["failing"] == 1
    assert run(wh, "not_future", "d")["failing"] == 1
    assert run(wh, "freshness", "ts", hours=24)["passed"]
    assert not run(wh, "freshness", "ts", hours=0.1)["passed"]
    assert run(wh, "row_count", min=5)["passed"] and not run(wh, "row_count", min=6)["passed"]
    assert run(wh, "custom_sql", sql="select * from raw.t where amount > 100")["failing"] == 1


def test_broken_rule_reports_error_not_crash(wh):
    r = run(wh, "custom_sql", sql="drop table raw.t")
    assert not r["passed"] and r["error"]
    assert wh.row_count("raw", "t") == 5


def test_rule_validation():
    with pytest.raises(Exception):
        quality.validate_rule({"kind": "regex", "column_name": "a", "params": {"pattern": "("}}, ["a"])
    with pytest.raises(Exception):
        quality.validate_rule({"kind": "not_null", "column_name": "zzz"}, ["a"])
    with pytest.raises(Exception):
        quality.validate_rule({"kind": "range", "column_name": "a", "params": {}}, ["a"])


def test_score():
    assert quality.score([{"kind": "not_null", "failing": 10, "total": 100, "passed": False},
                          {"kind": "row_count", "failing": 0, "total": 100, "passed": True}]) == 0.95
    assert quality.score([]) is None


def test_suggestions(wh):
    wh.load_arrow(pa.table({"order_id": [f"O{i}" for i in range(200)], "total": [float(i) for i in range(200)],
                            "email": [f"p{i}@mail.ca" for i in range(200)], "channel": ["web", "store"] * 100}), "raw", "s")
    kinds = {(s["kind"], s["column_name"]) for s in quality.suggest(wh, "raw", "s")}
    assert ("unique", "order_id") in kinds
    assert ("not_null", "order_id") in kinds
    assert ("regex", "email") in kinds
    assert ("allowed_values", "channel") in kinds
    assert ("row_count", None) in kinds


def test_luhn_and_detectors():
    assert pii.luhn("4111111111111111") and not pii.luhn("4111111111111112")
    det = {d["key"]: d["test"] for d in pii.DETECTORS}
    assert det["payment_card"]("4111 1111 1111 1111") and not det["payment_card"]("4111 1111 1111 1112")
    assert det["ca_sin"]("130-692-544") and not det["ca_sin"]("123-456-789")
    assert det["email"]("a.b@c.io") and not det["email"]("a_at_c.io")
    assert det["ip_address"]("10.0.0.1") and not det["ip_address"]("999.1.1.1")
    assert det["postal_code"]("V3S 1A1")


def test_scan_and_publish_masks(tmp_path):
    w = Warehouse(tmp_path / "p.duckdb")
    w.load_arrow(pa.table({"customer_name": ["Ann Lee", "Bo Chen"], "email": ["ann@x.ca", "bo@y.com"],
                           "card": ["4111 1111 1111 1111", "5500 0000 0000 0004"], "sin": ["130-692-544", "130-692-544"],
                           "dob": ["1990-05-01", "1985-12-31"], "amount": [1, 2]}), "raw", "c")
    f = {x["column"]: x for x in pii.scan(w, "raw", "c")}
    assert f["email"]["pii"] == "email" and f["card"]["pii"] == "payment_card" and f["sin"]["pii"] == "ca_sin"
    assert f["customer_name"]["pii"] == "person_name" and f["dob"]["pii"] == "date_of_birth"
    assert f["amount"]["pii"] is None
    cols = [{"name": k, "mask": v["mask"]} for k, v in f.items()]
    pii.publish(w, "raw", "c", cols, salt="s3cret")
    rows = w.query("select * from serving.c order by amount", restricted=True)
    r0 = dict(zip(rows["columns"], rows["rows"][0]))
    assert r0["customer_name"] == "[REDACTED]"
    assert r0["card"] == "************1111"
    assert r0["sin"] == "*****2544"
    assert r0["dob"] == 1990
    assert len(r0["email"]) == 16 and "@" not in r0["email"]
    assert r0["amount"] == 1
    # keyed hash: same input -> same token (joinable), different salt -> different token
    again = w.internal(f"select {pii.mask_expr('email', 'hash', 's3cret')} from raw.c order by amount")[0][0]
    other = w.internal(f"select {pii.mask_expr('email', 'hash', 'other')} from raw.c order by amount")[0][0]
    assert again == r0["email"] and other != r0["email"]


def test_manual_classification_survives_rescan(admin):
    admin.post("/api/uploads", files={"file": ("p.csv", b"id,notes\n1,hello\n2,world\n", "text/csv")}, data={"target": "raw.p"})
    did = next(d["id"] for d in admin.get("/api/datasets").json() if d["name"] == "p")
    admin.patch(f"/api/datasets/{did}/columns/notes", json={"mask": "redact", "level": "confidential"})
    admin.post(f"/api/datasets/{did}/pii-scan")
    col = next(c for c in admin.get(f"/api/datasets/{did}").json()["columns"] if c["name"] == "notes")
    assert col["mask"] == "redact"
    admin.post(f"/api/datasets/{did}/publish")
    assert admin.post("/api/sql", json={"sql": "select notes from serving.p"}).json()["rows"] == [["[REDACTED]"], ["[REDACTED]"]]
