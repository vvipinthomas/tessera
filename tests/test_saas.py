"""Application templates and OAuth. Every object of every template is read from a simulated API that pages the way
the template says the vendor pages (next links, cursors, last-record cursors, Link headers, offsets), and signs in
the way the vendor expects (Bearer, Basic, custom header or OAuth client credentials). Real services are not called."""
import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tessera import connectors, saas
from tessera.connectors import ConnectorError

VALUES = {
    "salesforce": {"my_domain": "acme", "client_id": "cid", "client_secret": "csec"},
    "microsoft_graph": {"tenant_id": "t-1", "client_id": "cid", "client_secret": "csec", "sharepoint_lists": "site1/list1"},
    "dynamics365": {"org_host": "contoso.crm3.dynamics.com", "tenant_id": "t-1", "client_id": "cid", "client_secret": "csec", "tables": "incidents"},
    "hubspot": {"token": "pat-1"},
    "stripe": {"token": "rk_test_1"},
    "github": {"token": "ghp_1", "org": "acme", "repos": "api"},
    "jira": {"site": "acme", "username": "me@acme.ca", "token": "jt"},
    "zendesk": {"subdomain": "acme", "email": "me@acme.ca", "token": "zt"},
    "servicenow": {"instance": "acme", "username": "svc", "token": "pw", "tables": "u_assets"},
    "shopify": {"shop": "acme", "token": "shpat_1"},
    "airtable": {"base_id": "appABC", "token": "pat", "tables": "Projects, Big Tasks"},
}


def put(obj, path, value):
    if not path:
        return value
    if path.startswith("@"):
        obj[path] = value
        return obj
    parts, cur = path.split("."), obj
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return obj


class FakeApi:
    """Serves three pages of two records for one object, paging as its spec says."""

    def __init__(self, cfg, obj, auth_check):
        self.cfg, self.obj, self.auth_check, self.calls, self.tokens = cfg, obj, auth_check, [], 0

    def page_of(self, n):
        return [{"id": f"r{n * 2 + i}", "created": 1700000000 + n * 2 + i, "name": f"row {n * 2 + i}"} for i in range(2)]

    def __call__(self, req: httpx.Request):
        url = str(req.url)
        if req.method == "POST":  # OAuth token endpoint
            body = parse_qs(req.content.decode())
            assert body["grant_type"] == ["client_credentials"] and body["client_id"] == ["cid"] and body["client_secret"] == ["csec"]
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600})
        self.calls.append(url)
        self.auth_check(req)
        pg, q = self.obj["pagination"], parse_qs(urlparse(url).query)
        t = pg["type"]
        if t == "next_url":
            n = int(q.get("pg", ["0"])[0])
        elif t == "link_header":
            n = int(q.get("pg", ["0"])[0])
        elif t == "offset":
            n = int(q.get(pg["offset_param"], ["0"])[0]) // 2
        elif t == "cursor" and pg.get("cursor_from") == "last_record":
            last = q.get(pg["cursor_param"], [None])[0]
            n = 0 if last is None else int(last[1:]) // 2 + 1
        else:
            cur = q.get(pg["cursor_param"], [None])[0]
            n = 0 if cur is None else int(cur.replace("c", ""))
        rows = self.page_of(n)
        last = n == 2
        body = {} if self.obj.get("records_path") else rows
        if self.obj.get("records_path"):
            put(body, self.obj["records_path"], rows)
        headers = {}
        base = self.cfg["base_url"]
        if t == "next_url":
            nxt = None if last else (f"{self.obj['path']}?pg={n + 1}" if pg["next_path"] == "nextRecordsUrl" else f"{base}{self.obj['path']}?pg={n + 1}")
            put(body, pg["next_path"], nxt)
            if pg.get("more_path"):
                put(body, pg["more_path"], not last)
        elif t == "link_header" and not last:
            headers["Link"] = f'<{base}{self.obj["path"]}?pg={n + 1}>; rel="next"'
        elif t == "cursor" and pg.get("cursor_from") != "last_record":
            put(body, pg["cursor_path"], None if last else f"c{n + 1}")
        elif t == "cursor":
            put(body, pg["more_path"], not last)
        elif t == "offset":
            if pg.get("last_path"):
                put(body, pg["last_path"], last)
            if n > 2:
                rows.clear()
        if t == "offset" and n > 2:
            body = put({}, self.obj["records_path"], [])
        return httpx.Response(200, json=body, headers=headers)


def auth_checker(key, cfg):
    def check(req):
        a = req.headers.get("authorization", "")
        if cfg["auth"].startswith("oauth2"):
            assert a.startswith("Bearer tok"), a
        elif cfg["auth"] == "bearer":
            assert a == f"Bearer {VALUES[key]['token']}"
        elif cfg["auth"] == "basic":
            user, pw = base64.b64decode(a.split()[1]).decode().split(":", 1)
            assert user == cfg["username"] and pw == VALUES[key]["token"], (user, pw)
        elif cfg["auth"] == "header":
            assert req.headers[cfg["header_name"]] == VALUES[key]["token"]
    return check


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(connectors, "guard_url", lambda url, block: None)
    yield
    connectors.RestConnector.http_factory = None


@pytest.mark.parametrize("key", list(saas.TEMPLATES))
def test_every_object_of_every_template_pages_to_the_end(key, settings):
    cfg, sec = saas.build(key, VALUES[key])
    tpl = saas.TEMPLATES[key]
    objs = saas.objects(tpl, cfg)
    assert objs, key
    for obj in objs:
        api = FakeApi(cfg, obj, auth_checker(key, cfg))
        connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(api), timeout=5, **kw)
        cx = connectors.make("rest", cfg, sec, settings)
        assert {"name": obj["name"], "label": obj["name"], "kind": "object"} in cx.browse()
        t = cx.read({"object": obj["name"]})
        assert t.num_rows == 6, (key, obj["name"], api.calls)
        assert sorted(t.column("id").to_pylist()) == [f"r{i}" for i in range(6)]
        for c in api.calls:
            assert c.startswith(cfg["base_url"]), (key, c)


def test_template_urls_and_auth_are_built_from_the_form():
    cfg, sec = saas.build("microsoft_graph", VALUES["microsoft_graph"])
    assert cfg["token_url"] == "https://login.microsoftonline.com/t-1/oauth2/v2.0/token" and cfg["scope"] == "https://graph.microsoft.com/.default"
    assert sec == {"client_secret": "csec"} and "client_secret" not in cfg
    cfg, _ = saas.build("dynamics365", VALUES["dynamics365"])
    assert cfg["base_url"] == "https://contoso.crm3.dynamics.com/api/data/v9.2" and cfg["scope"] == "https://contoso.crm3.dynamics.com/.default"
    cfg, _ = saas.build("zendesk", VALUES["zendesk"])
    assert cfg["username"] == "me@acme.ca/token" and cfg["base_url"] == "https://acme.zendesk.com/api/v2"
    cfg, _ = saas.build("salesforce", VALUES["salesforce"])
    assert cfg["api_version"] == "v62.0", "defaults fill in"
    airtable = saas.objects(saas.TEMPLATES["airtable"], saas.build("airtable", VALUES["airtable"])[0])
    assert [o["path"] for o in airtable] == ["/Projects", "/Big%20Tasks"]


@pytest.mark.parametrize("bad", ["acme.evil.com/x", "a b", "../x", "acme@evil", "x?y"])
def test_host_fields_cannot_redirect_requests(bad):
    with pytest.raises(ConnectorError, match="not a valid"):
        saas.build("zendesk", {**VALUES["zendesk"], "subdomain": bad})


def test_required_fields_and_unknown_objects(settings):
    with pytest.raises(ConnectorError, match="needs"):
        saas.build("stripe", {})
    cfg, sec = saas.build("stripe", VALUES["stripe"])
    cx = connectors.make("rest", cfg, sec, settings)
    with pytest.raises(ConnectorError, match="no object"):
        cx.read({"object": "nope"})


def test_oauth_token_reuse_and_refresh_after_401(settings):
    tokens, seen = [], []

    def handler(req):
        if req.method == "POST":
            tokens.append(1)
            return httpx.Response(200, json={"access_token": f"tok{len(tokens)}", "expires_in": 3600})
        seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer tok1" and len(seen) == 3:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"value": [{"id": len(seen)}]})
    connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    cfg, sec = saas.build("microsoft_graph", VALUES["microsoft_graph"])
    cx = connectors.make("rest", cfg, sec, settings)
    cx.read({"object": "users"})
    cx.read({"object": "groups"})
    assert len(tokens) == 1, "the token is reused while it is valid"
    cx.read({"object": "users"})
    assert len(tokens) == 2 and seen[-1] == "Bearer tok2", "a 401 signs in again once and retries"


def test_oauth_errors_are_readable(settings):
    connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."})), **kw)
    cfg, sec = saas.build("microsoft_graph", VALUES["microsoft_graph"])
    with pytest.raises(ConnectorError, match="Invalid client secret"):
        connectors.make("rest", cfg, sec, settings).read({"object": "users"})


def test_app_connections_through_the_api(admin, monkeypatch):
    apps = {a["key"]: a for a in admin.get("/api/connectors/apps").json()}
    assert {"salesforce", "microsoft_graph", "dynamics365", "stripe", "hubspot"} <= set(apps)
    r = admin.post("/api/connections/from-app", json={"app": "stripe", "name": "Stripe live", "values": VALUES["stripe"]})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["config"]["template"] == "stripe" and "token" not in json.dumps(c["config"])
    assert admin.post("/api/connections/from-app", json={"app": "stripe", "name": "x", "values": {}}).status_code == 400
    # incremental loads pick the template's cursor column on their own
    pages = {"n": 0}

    def handler(req):
        q = parse_qs(urlparse(str(req.url)).query)
        start = 0 if "created[gt]" not in q else int(q["created[gt]"][0]) - 1700000000 + 1
        rows = [{"id": f"r{i}", "created": 1700000000 + i} for i in range(start, 4)]
        pages["n"] += 1
        return httpx.Response(200, json={"data": rows, "has_more": False})
    connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"object": "customers"}, "target": "raw.stripe_customers", "mode": "incremental"})
    assert r.status_code == 200, r.text
    assert r.json()["rows_loaded"] == 4 and r.json()["watermark"] == 1700000003
    r = admin.post(f"/api/connections/{c['id']}/ingest", json={"source": {"object": "customers"}, "target": "raw.stripe_customers", "mode": "incremental"})
    assert r.json()["rows_loaded"] == 0


@pytest.mark.parametrize("key,obj,watermark,param,expected", [
    ("salesforce", "Account", "2026-03-01T10:00:00.000+0000", "q",
     "SELECT FIELDS(STANDARD) FROM Account WHERE SystemModstamp >= 2026-03-01T09:55:00Z ORDER BY SystemModstamp"),
    ("dynamics365", "opportunities", "2026-03-01T10:00:00Z", "$filter", "modifiedon ge 2026-03-01T09:55:00Z"),
    ("jira", "issues", "2026-03-01T10:00:00.000-0700", "jql", '(updated >= -365d) AND updated >= "2026/02/28 17:00" ORDER BY updated ASC'),
    ("servicenow", "u_assets", "2026-03-01 10:00:00", "sysparm_query",
     "sys_updated_on>=javascript:gs.dateGenerate('2026-02-28','10:00:00')^ORDERBYsys_updated_on"),
    ("shopify", "orders", "2026-03-01T10:00:00-05:00", "updated_at_min", "2026-03-01T14:55:00Z"),
    ("github", "api:issues", "2026-03-01T10:00:00Z", "since", "2026-03-01T09:55:00Z"),
])
def test_change_since_filters(settings, key, obj, watermark, param, expected):
    seen = []

    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"access_token": "tok1", "expires_in": 3600})
        seen.append(parse_qs(urlparse(str(req.url)).query))
        return httpx.Response(200, json={} if key != "github" else [])
    connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    cfg, sec = saas.build(key, VALUES[key])
    cx = connectors.make("rest", cfg, sec, settings)
    cx.read({"object": obj}, watermark)
    assert seen[0][param] == [expected]
    cx.read({"object": obj})
    assert "%SINCE%" not in json.dumps(seen[1]), "the first load has no change filter"
    assert saas.default_key(cfg, obj) and saas.default_cursor(cfg, obj)


def test_jira_without_a_filter_and_custom_jql():
    cfg, _ = saas.build("jira", {**VALUES["jira"], "jql": "project = OPS order by created"})
    assert cfg["jql_where"] == "(project = OPS) AND "
    inc = saas.TEMPLATES["jira"]["objects"][0]["incremental"]["since_params"]["jql"]
    assert saas.deep_fill(inc, cfg).startswith("(project = OPS) AND updated >=")


def test_salesforce_merge_keeps_one_row_per_record(admin):
    state = {"phase": 1}

    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        q = parse_qs(urlparse(str(req.url)).query).get("q", [""])[0]
        if "WHERE SystemModstamp" in q:
            assert ">= 2026-03-01T09:55:00Z" in q, q
            rows = [{"Id": "001A", "Name": "Acme Renamed", "SystemModstamp": "2026-03-02T08:00:00.000+0000"},
                    {"Id": "001C", "Name": "New Co", "SystemModstamp": "2026-03-02T09:00:00.000+0000"}]
        else:
            rows = [{"Id": "001A", "Name": "Acme", "SystemModstamp": "2026-02-01T10:00:00.000+0000"},
                    {"Id": "001B", "Name": "Globex", "SystemModstamp": "2026-03-01T10:00:00.000+0000"}]
        return httpx.Response(200, json={"records": rows, "done": True})
    connectors.RestConnector.http_factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    c = admin.post("/api/connections/from-app", json={"app": "salesforce", "name": "SF", "values": VALUES["salesforce"]}).json()
    body = {"source": {"object": "Account"}, "target": "raw.sf_account", "mode": "merge"}
    r = admin.post(f"/api/connections/{c['id']}/ingest", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["watermark"] == "2026-03-01T10:00:00.000+0000"
    assert admin.post(f"/api/connections/{c['id']}/ingest", json=body).status_code == 200
    rows = admin.post("/api/sql", json={"sql": "select Id, Name from raw.sf_account order by Id"}).json()["rows"]
    assert rows == [["001A", "Acme Renamed"], ["001B", "Globex"], ["001C", "New Co"]]
    plan = admin.post(f"/api/connections/{c['id']}/copy", json={"objects": ["Account", "Contact"], "run": False}).json()["tables"]
    assert all(t["mode"] == "merge" and t["key"] == ["Id"] and t["cursor"] == "SystemModstamp" for t in plan)
    apps = {a["key"]: a for a in admin.get("/api/connectors/apps").json()}
    assert "Account" in apps["salesforce"]["incremental"] and apps["hubspot"]["incremental"] == []
