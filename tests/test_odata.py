"""OData v4 feed for Power BI, Excel and Tableau: discovery, metadata, query options, paging and governance."""
import base64
import xml.etree.ElementTree as ET

import pytest
from conftest import Client, add_user


@pytest.fixture(scope="module")
def od(sample_env):
    c, app = sample_env["client"], sample_env["app"]
    tok = c.post("/api/tokens", json={"name": "power bi", "role": "viewer"}).json()["token"]
    basic = {"Authorization": "Basic " + base64.b64encode(f"powerbi:{tok}".encode()).decode()}
    return {"c": c, "app": app, "tok": tok, "basic": basic, "anon": Client(app)}


def get(od, url, headers=None):
    return od["anon"].get(url, headers={**od["basic"], **(headers or {})})


def test_auth(od):
    assert od["anon"].get("/odata/").status_code == 401
    r = od["anon"].get("/odata/", headers={"Authorization": "Basic " + base64.b64encode(b"admin@acme.ca:correct horse battery 9").decode()})
    assert r.status_code == 401, "account passwords are never accepted, only API tokens"
    assert "Basic" in r.headers.get("www-authenticate", "")
    assert od["anon"].get("/odata/", headers={"Authorization": "Bearer " + od["tok"]}).status_code == 200


def test_service_document_and_metadata(od):
    r = get(od, "/odata/")
    assert r.status_code == 200 and r.headers["odata-version"] == "4.0"
    names = {x["name"] for x in r.json()["value"]}
    assert "orders_enriched" in names and "customers" in names
    assert any(n.startswith("report_") for n in names), "saved reports are exposed too"
    assert not any("raw" in n or "curated" in n for n in names), "only published data"
    m = get(od, "/odata/$metadata")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(m.text)
    ns = {"e": "http://docs.oasis-open.org/odata/ns/edm"}
    et = {x.get("Name"): x for x in root.iter("{http://docs.oasis-open.org/odata/ns/edm}EntityType")}
    props = {p.get("Name"): p.get("Type") for p in et["orders_enriched"].findall("e:Property", ns)}
    assert props["__row"] == "Edm.Int64" and props["order_total"] in ("Edm.Double", "Edm.Decimal")
    assert props["order_date"] in ("Edm.Date", "Edm.DateTimeOffset")
    assert et["orders_enriched"].find("e:Key/e:PropertyRef", ns).get("Name") == "__row"


def test_query_options(od):
    total = od["c"].post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched"}).json()["rows"][0][0]
    assert get(od, "/odata/orders_enriched/$count").text == str(total)
    r = get(od, "/odata/orders_enriched?$top=5&$select=order_id,province&$orderby=order_id desc&$count=true").json()
    assert r["@odata.count"] == total and len(r["value"]) == 5
    assert set(r["value"][0]) == {"order_id", "province"}
    ids = [x["order_id"] for x in r["value"]]
    assert ids == sorted(ids, reverse=True)
    f = get(od, "/odata/orders_enriched?$filter=" + "province eq 'BC' and (order_total gt 100 or contains(channel,'web')) and status ne null&$count=true&$top=0").json()
    want = od["c"].post("/api/sql", json={"sql": "select count(*) from serving.orders_enriched where province='BC' and (order_total>100 or contains(channel,'web')) and status is not null"}).json()["rows"][0][0]
    assert f["@odata.count"] == want and want > 0
    d = get(od, "/odata/orders_enriched?$filter=order_date ge 2026-01-01&$count=true&$top=1").json()
    assert d["@odata.count"] >= 0


@pytest.mark.parametrize("bad", [
    "province eq 'BC'; drop table x", "1 eq 1) or (1 eq 1", "nosuchcol eq 1", "province eq 'x' union select", "exec('x')",
    "province eq", "contains(province)",
])
def test_filter_rejects_injection_and_nonsense(od, bad):
    r = get(od, "/odata/orders_enriched?$filter=" + bad)
    assert r.status_code == 400, (bad, r.text)
    assert "error" in r.json()


def test_server_paging(od):
    seen, url, pages = 0, "/odata/orders_enriched?$select=order_id", 0
    while url:
        r = get(od, url, {"Prefer": "odata.maxpagesize=2000"}).json()
        seen += len(r["value"])
        pages += 1
        nxt = r.get("@odata.nextLink")
        url = nxt[nxt.index("/odata/"):] if nxt else None
    total = int(get(od, "/odata/orders_enriched/$count").text)
    assert seen == total and pages == -(-total // 2000)
    # $top across pages stops exactly
    r = get(od, "/odata/orders_enriched?$top=2500", {"Prefer": "odata.maxpagesize=2000"}).json()
    nxt = r["@odata.nextLink"]
    r2 = get(od, nxt[nxt.index("/odata/"):], {"Prefer": "odata.maxpagesize=2000"}).json()
    assert len(r["value"]) == 2000 and len(r2["value"]) == 500 and "@odata.nextLink" not in r2


def test_reports_and_masking(od):
    names = [x["name"] for x in get(od, "/odata/").json()["value"] if x["name"].startswith("report_")]
    r = get(od, f"/odata/{names[0]}?$top=3")
    assert r.status_code == 200 and r.json()["value"]
    cust = get(od, "/odata/customers?$top=5").json()["value"]
    emails = [x.get("email") for x in cust if x.get("email")]
    assert emails and all("@" not in e for e in emails), emails


def test_row_level_security_applies(od):
    c, app = od["c"], od["app"]
    p = c.post("/api/rls", json={"dataset": "serving.orders_enriched", "column_name": "province", "attribute": "region"}).json()
    try:
        bc = add_user(c, app, "odata.bc@acme.ca", "analyst")
        uid = next(u["id"] for u in c.get("/api/users").json() if u["email"] == "odata.bc@acme.ca")
        c.put(f"/api/users/{uid}/attributes", json={"attributes": {"region": "BC"}})
        tok = bc.post("/api/tokens", json={"name": "excel", "role": "analyst"}, headers={"x-tessera-csrf": "1"}).json()["token"]
        h = {"Authorization": "Basic " + base64.b64encode(f"x:{tok}".encode()).decode()}
        rows = od["anon"].get("/odata/orders_enriched?$select=province&$top=50000", headers=h).json()["value"]
        assert rows and {x["province"] for x in rows} == {"BC"}
        assert od["anon"].get("/odata/orders_enriched?$filter=province eq 'ON'&$count=true&$top=0", headers=h).json()["@odata.count"] == 0
    finally:
        c.delete(f"/api/rls/{p['id']}")
