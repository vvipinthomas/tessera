"""Ready-made connections to business applications, built on the REST connector.

Each template knows the application's address, how it signs in (API key, basic auth or OAuth 2), and its main
objects: where they live, where the records are in the response, and how its pages link together. People fill in
only what is specific to them (a subdomain, a tenant, a token) and pick objects from a list.

The paging rules follow each vendor's documentation. The test suite checks every template against a simulated API
that pages the way the vendor documents; it does not call the real services.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .connectors_base import ConnectorError

F = lambda key, label, **kw: {"key": key, "label": label, **kw}  # noqa: E731

GRAPH_CC = {"token_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"}  # nosec B105 - a URL, not a password

# Dataverse primary keys are the singular table name plus "id"
DATAVERSE_KEYS = {"accounts": "accountid", "contacts": "contactid", "opportunities": "opportunityid", "leads": "leadid",
                  "systemusers": "systemuserid"}

TEMPLATES: dict[str, dict] = {
    "salesforce": {
        "label": "Salesforce", "category": "CRM", "docs": "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        "about": "Accounts, contacts, opportunities, leads and cases through the REST API (SOQL). Uses an External Client App or "
                 "Connected App with the OAuth client credentials flow and a run-as user.",
        "fields": [F("my_domain", "My Domain", required=True, placeholder="acme (from acme.my.salesforce.com)"),
                   F("api_version", "API version", placeholder="v62.0", default="v62.0"),
                   F("client_id", "Consumer key", required=True), F("client_secret", "Consumer secret", secret=True, required=True)],
        "base_url": "https://{my_domain}.my.salesforce.com", "auth": "oauth2_client_credentials",
        "token_url": "https://{my_domain}.my.salesforce.com/services/oauth2/token",  # nosec B105 - a URL
        "health_path": "/services/data/{api_version}/limits",
        "objects": [{"name": n, "path": "/services/data/{api_version}/query",
                     "params": {"q": f"SELECT FIELDS(STANDARD) FROM {n}"}, "records_path": "records",
                     "pagination": {"type": "next_url", "next_path": "nextRecordsUrl", "max_pages": 10000},
                     "incremental": {"cursor_column": "SystemModstamp", "key": "Id", "lookback_seconds": 300,
                                     "since_params": {"q": f"SELECT FIELDS(STANDARD) FROM {n} WHERE SystemModstamp >= %SINCE% ORDER BY SystemModstamp"}}}
                    for n in ("Account", "Contact", "Opportunity", "Lead", "Case", "User", "Campaign", "Task")],
    },
    "microsoft_graph": {
        "label": "Microsoft 365 (Graph): users, groups, SharePoint lists", "category": "Collaboration",
        "docs": "https://learn.microsoft.com/graph/api/overview",
        "about": "Microsoft Entra users and groups, and SharePoint list items. Register an app in Entra ID with application "
                 "permissions (User.Read.All, Group.Read.All, Sites.Read.All) and admin consent.",
        "fields": [F("tenant_id", "Tenant ID", required=True), F("client_id", "Application (client) ID", required=True),
                   F("client_secret", "Client secret", secret=True, required=True),
                   F("sharepoint_lists", "SharePoint lists (optional)", placeholder="site-id/list-id, one per line", type="textarea")],
        "base_url": "https://graph.microsoft.com/v1.0", "auth": "oauth2_client_credentials", **GRAPH_CC,
        "scope": "https://graph.microsoft.com/.default", "health_path": "/organization",
        "objects": [
            {"name": "users", "path": "/users", "records_path": "value", "pagination": {"type": "next_url", "next_path": "@odata.nextLink"}},
            {"name": "groups", "path": "/groups", "records_path": "value", "pagination": {"type": "next_url", "next_path": "@odata.nextLink"}},
        ],
        "objects_from": {"field": "sharepoint_lists", "name": "sharepoint:{item}",
                         "path": "/sites/{site}/lists/{list}/items", "params": {"expand": "fields"}, "records_path": "value",
                         "pagination": {"type": "next_url", "next_path": "@odata.nextLink"}},
    },
    "dynamics365": {
        "label": "Microsoft Dynamics 365 / Dataverse", "category": "CRM", "docs": "https://learn.microsoft.com/power-apps/developer/data-platform/webapi/overview",
        "about": "Dataverse tables through the Web API. Register an Entra app, add it as an application user in the environment "
                 "with a read role.",
        "fields": [F("org_host", "Environment host", required=True, placeholder="contoso.crm3.dynamics.com"),
                   F("tenant_id", "Tenant ID", required=True), F("client_id", "Application (client) ID", required=True),
                   F("client_secret", "Client secret", secret=True, required=True),
                   F("tables", "Extra tables (optional)", placeholder="Entity set names, comma-separated: incidents, cr123_projects")],
        "base_url": "https://{org_host}/api/data/v9.2", "auth": "oauth2_client_credentials", **GRAPH_CC,
        "scope": "https://{org_host}/.default", "health_path": "/WhoAmI",
        "headers": {"Prefer": "odata.maxpagesize=5000", "OData-Version": "4.0"},
        "objects": [{"name": n, "path": f"/{n}", "records_path": "value", "pagination": {"type": "next_url", "next_path": "@odata.nextLink", "max_pages": 10000},
                     "incremental": {"cursor_column": "modifiedon", "key": DATAVERSE_KEYS[n], "lookback_seconds": 300,
                                     "since_params": {"$filter": "modifiedon ge %SINCE%"}}}
                    for n in ("accounts", "contacts", "opportunities", "leads", "systemusers")],
        "objects_from": {"field": "tables", "name": "{item}", "path": "/{item}", "records_path": "value",
                         "pagination": {"type": "next_url", "next_path": "@odata.nextLink", "max_pages": 10000}},
    },
    "hubspot": {
        "label": "HubSpot", "category": "CRM", "docs": "https://developers.hubspot.com/docs/api/crm/understanding-the-crm",
        "about": "CRM objects through the v3 API, with a private app access token (crm.objects.*.read scopes).",
        "fields": [F("token", "Private app access token", secret=True, required=True)],
        "base_url": "https://api.hubapi.com", "auth": "bearer", "health_path": "/crm/v3/objects/contacts?limit=1",
        "objects": [{"name": n, "path": f"/crm/v3/objects/{n}", "params": {"limit": 100, "archived": "false"}, "records_path": "results",
                     "pagination": {"type": "cursor", "cursor_path": "paging.next.after", "cursor_param": "after", "max_pages": 10000}}
                    for n in ("contacts", "companies", "deals", "tickets", "products", "line_items")],
    },
    "stripe": {
        "label": "Stripe", "category": "Finance", "docs": "https://docs.stripe.com/api/pagination",
        "about": "Payments data with a restricted secret key (read-only).",
        "fields": [F("token", "Restricted secret key", secret=True, required=True, placeholder="rk_live_…")],
        "base_url": "https://api.stripe.com/v1", "auth": "bearer", "health_path": "/balance",
        "objects": [{"name": n, "path": f"/{n}", "params": {"limit": 100}, "records_path": "data",
                     "pagination": {"type": "cursor", "cursor_from": "last_record", "cursor_field": "id", "cursor_param": "starting_after",
                                    "more_path": "has_more", "max_pages": 100000},
                     "incremental": {"cursor_column": "created", "cursor_param": "created[gt]", "key": "id"}}
                    for n in ("customers", "charges", "invoices", "subscriptions", "payment_intents", "refunds", "products", "prices", "payouts")],
    },
    "github": {
        "label": "GitHub", "category": "Engineering", "docs": "https://docs.github.com/rest/using-the-rest-api/using-pagination-in-the-rest-api",
        "about": "Repositories, issues, pull requests, commits and releases with a fine-grained personal access token (read-only).",
        "fields": [F("token", "Fine-grained token", secret=True, required=True), F("org", "Organization or user", required=True),
                   F("repos", "Repositories (optional)", placeholder="api, web, mobile")],
        "base_url": "https://api.github.com", "auth": "bearer", "health_path": "/rate_limit",
        "headers": {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        "objects": [{"name": "repos", "path": "/orgs/{org}/repos", "params": {"per_page": 100, "type": "all"},
                     "pagination": {"type": "link_header", "max_pages": 1000}}],
        "objects_from": {"field": "repos", "multi": [
            {"name": "{item}:issues", "path": "/repos/{org}/{item}/issues", "params": {"per_page": 100, "state": "all"},
             "incremental": {"cursor_column": "updated_at", "key": "id", "lookback_seconds": 300, "since_params": {"since": "%SINCE%"}}},
            {"name": "{item}:pulls", "path": "/repos/{org}/{item}/pulls", "params": {"per_page": 100, "state": "all"}},
            {"name": "{item}:commits", "path": "/repos/{org}/{item}/commits", "params": {"per_page": 100},
             "incremental": {"cursor_column": "commit_committer_date", "key": "sha", "lookback_seconds": 86400, "since_params": {"since": "%SINCE%"}}},
            {"name": "{item}:releases", "path": "/repos/{org}/{item}/releases", "params": {"per_page": 100}}],
            "pagination": {"type": "link_header", "max_pages": 1000}},
    },
    "jira": {
        "label": "Jira Cloud", "category": "Engineering", "docs": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
        "about": "Issues (by JQL) and projects, with your Atlassian email and an API token.",
        "fields": [F("site", "Site", required=True, placeholder="acme (from acme.atlassian.net)"), F("username", "Atlassian email", required=True),
                   F("token", "API token", secret=True, required=True),
                   F("jql", "Issues to load (JQL)", placeholder="project = OPS ORDER BY updated DESC", default="updated >= -365d ORDER BY updated DESC")],
        "base_url": "https://{site}.atlassian.net/rest/api/3", "auth": "basic", "health_path": "/myself",
        "objects": [
            {"name": "issues", "path": "/search/jql", "params": {"jql": "{jql}", "maxResults": 100,
                                                                "fields": "summary,status,issuetype,priority,assignee,reporter,project,created,updated,resolutiondate,labels"},
             "records_path": "issues", "pagination": {"type": "cursor", "cursor_path": "nextPageToken", "cursor_param": "nextPageToken", "max_pages": 10000},
             # JQL compares in the user's time zone, so look back a day; the merge on the issue id removes repeats
             "incremental": {"cursor_column": "fields_updated", "key": "id", "lookback_seconds": 86400, "since_format": "jira",
                             "since_params": {"jql": '{jql_where}updated >= "%SINCE%" ORDER BY updated ASC'}}},
            {"name": "projects", "path": "/project/search", "params": {"maxResults": 50}, "records_path": "values",
             "pagination": {"type": "offset", "offset_param": "startAt", "size_param": "maxResults", "page_size": 50, "last_path": "isLast"}},
        ],
    },
    "zendesk": {
        "label": "Zendesk Support", "category": "Support", "docs": "https://developer.zendesk.com/api-reference/introduction/pagination/",
        "about": "Tickets, users and organizations with an agent email and API token.",
        "fields": [F("subdomain", "Subdomain", required=True, placeholder="acme (from acme.zendesk.com)"),
                   F("email", "Agent email", required=True), F("token", "API token", secret=True, required=True)],
        "base_url": "https://{subdomain}.zendesk.com/api/v2", "auth": "basic", "username": "{email}/token", "health_path": "/users/me.json",
        "objects": [{"name": n, "path": f"/{n}.json", "params": {"page[size]": 100}, "records_path": n,
                     "pagination": {"type": "next_url", "next_path": "links.next", "more_path": "meta.has_more", "max_pages": 100000}}
                    for n in ("tickets", "users", "organizations", "groups")],
    },
    "servicenow": {
        "label": "ServiceNow", "category": "IT", "docs": "https://developer.servicenow.com/dev.do#!/reference/api/latest/rest/c_TableAPI",
        "about": "Any table through the Table API, with a user that has read access (basic auth).",
        "fields": [F("instance", "Instance", required=True, placeholder="acme (from acme.service-now.com)"),
                   F("username", "User", required=True), F("token", "Password", secret=True, required=True),
                   F("tables", "Extra tables (optional)", placeholder="u_assets, sc_req_item")],
        "base_url": "https://{instance}.service-now.com/api/now", "auth": "basic", "health_path": "/table/sys_user?sysparm_limit=1",
        "objects": [{"name": n, "path": f"/table/{n}", "params": {"sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"},
                     "records_path": "result",
                     "pagination": {"type": "offset", "offset_param": "sysparm_offset", "size_param": "sysparm_limit", "page_size": 1000, "max_pages": 100000},
                     "incremental": {"cursor_column": "sys_updated_on", "key": "sys_id", "lookback_seconds": 86400, "since_format": "servicenow",
                                     "since_params": {"sysparm_query": "sys_updated_on>=javascript:gs.dateGenerate(%SINCE%)^ORDERBYsys_updated_on"}}}
                    for n in ("incident", "change_request", "problem", "sys_user", "cmdb_ci")],
        "objects_from": {"field": "tables", "name": "{item}", "path": "/table/{item}", "records_path": "result",
                         "pagination": {"type": "offset", "offset_param": "sysparm_offset", "size_param": "sysparm_limit", "page_size": 1000, "max_pages": 100000},
                         "incremental": {"cursor_column": "sys_updated_on", "key": "sys_id", "lookback_seconds": 86400, "since_format": "servicenow",
                                     "since_params": {"sysparm_query": "sys_updated_on>=javascript:gs.dateGenerate(%SINCE%)^ORDERBYsys_updated_on"}}},
    },
    "shopify": {
        "label": "Shopify", "category": "Commerce", "docs": "https://shopify.dev/docs/api/admin-rest",
        "about": "Orders, products and customers from a custom app's Admin API access token (REST Admin API).",
        "fields": [F("shop", "Shop", required=True, placeholder="acme (from acme.myshopify.com)"),
                   F("token", "Admin API access token", secret=True, required=True), F("api_version", "API version", default="2026-04")],
        "base_url": "https://{shop}.myshopify.com/admin/api/{api_version}", "auth": "header", "header_name": "X-Shopify-Access-Token",
        "health_path": "/shop.json",
        "objects": [{"name": n, "path": f"/{n}.json", "params": {"limit": 250, **({"status": "any"} if n == "orders" else {})},
                     "records_path": n, "pagination": {"type": "link_header", "max_pages": 100000},
                     "incremental": {"cursor_column": "updated_at", "key": "id", "lookback_seconds": 300, "since_params": {"updated_at_min": "%SINCE%"}}}
                    for n in ("orders", "products", "customers")],
    },
    "airtable": {
        "label": "Airtable", "category": "Collaboration", "docs": "https://airtable.com/developers/web/api/list-records",
        "about": "Tables in one base, with a personal access token (data.records:read).",
        "fields": [F("base_id", "Base ID", required=True, placeholder="appXXXXXXXXXXXXXX"), F("token", "Personal access token", secret=True, required=True),
                   F("tables", "Tables", required=True, placeholder="Projects, Tasks")],
        "base_url": "https://api.airtable.com/v0/{base_id}", "auth": "bearer", "health_path": "",
        "objects": [],
        "objects_from": {"field": "tables", "name": "{item}", "path": "/{item_url}", "params": {"pageSize": 100}, "records_path": "records",
                         "pagination": {"type": "cursor", "cursor_path": "offset", "cursor_param": "offset", "max_pages": 100000}},
    },
}

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def fill(text, values: dict, what: str = ""):
    if not isinstance(text, str):
        return text

    def sub(m):
        k = m.group(1)
        v = values.get(k)
        if v in (None, ""):
            raise ConnectorError(f"{what or 'This connection'} needs '{k}'.")
        return str(v)
    return PLACEHOLDER.sub(sub, text)


def deep_fill(obj, values: dict):
    if isinstance(obj, dict):
        return {k: deep_fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_fill(v, values) for v in obj]
    return fill(obj, values) if isinstance(obj, str) and PLACEHOLDER.search(obj) else obj


SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


def catalog() -> list[dict]:
    return [{"key": k, "label": t["label"], "category": t["category"], "about": t["about"], "docs": t["docs"],
             "auth": t["auth"], "fields": t["fields"], "objects": [o["name"] for o in t["objects"]],
             "incremental": [o["name"] for o in t["objects"] if o.get("incremental")] + _pattern_incremental(t)} for k, t in TEMPLATES.items()]


def _pattern_incremental(t: dict) -> list[str]:
    spec = t.get("objects_from") or {}
    return [o["name"] for o in spec.get("multi") or [spec] if o and (o.get("incremental") or spec.get("incremental"))]


def build(key: str, values: dict) -> tuple[dict, dict]:
    """Template + what the person typed -> (config, secrets) for a REST connection."""
    t = TEMPLATES.get(key)
    if not t:
        raise ConnectorError(f"Unknown application template '{key}'.")
    vals = {f["key"]: (values.get(f["key"]) if values.get(f["key"]) not in (None, "") else f.get("default")) for f in t["fields"]}
    for f in t["fields"]:
        if f.get("required") and vals.get(f["key"]) in (None, ""):
            raise ConnectorError(f"{t['label']} needs {f['label']}.")
    # host parts go into URLs: refuse anything that could change the host or path
    for k in ("my_domain", "org_host", "site", "subdomain", "instance", "shop", "base_id", "tenant_id", "org", "api_version"):
        v = vals.get(k)
        if v not in (None, "") and not SAFE_VALUE.match(str(v)):
            raise ConnectorError(f"'{v}' is not a valid {k.replace('_', ' ')}.")
    secret_keys = {f["key"] for f in t["fields"] if f.get("secret")}
    config = {"template": key, "auth": t["auth"], "base_url": fill(t["base_url"], vals, t["label"])}
    for k in ("token_url", "scope", "header_name", "health_path"):
        if t.get(k) is not None:
            config[k] = fill(t[k], vals, t["label"])
    if t.get("username"):
        config["username"] = fill(t["username"], vals, t["label"])
    if t.get("headers"):
        config["headers"] = t["headers"]
    for k, v in vals.items():
        if k not in secret_keys and v not in (None, ""):
            config.setdefault(k, v)
    if key == "jira":
        where = re.split(r"\border\s+by\b", str(config.get("jql") or ""), flags=re.I)[0].strip()
        config["jql_where"] = f"({where}) AND " if where else " "  # never empty: placeholders must have a value
    secrets = {k: vals[k] for k in secret_keys if vals.get(k)}
    return config, secrets


def _items(config: dict, field: str) -> list[str]:
    raw = config.get(field) or ""
    return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]


def objects(t: dict, config: dict) -> list[dict]:
    out = [deep_fill(o, config) for o in t["objects"]]
    spec = t.get("objects_from")
    if spec:
        for item in _items(config, spec["field"]):
            vals = {**config, "item": item, "item_url": quote(item, safe="")}
            if "/" in item and spec["field"] == "sharepoint_lists":
                vals["site"], vals["list"] = item.split("/", 1)
            for o in spec.get("multi") or [spec]:
                o2 = {k: v for k, v in {**spec, **o}.items() if k not in ("field", "multi")}
                out.append(deep_fill(o2, vals))
    return out


def resolve(config: dict, source: dict) -> dict:
    """{"object": "customers"} -> a full REST source (path, params, records_path, pagination, incremental settings)."""
    t = TEMPLATES.get(config.get("template") or "")
    if not t:
        raise ConnectorError("This connection is not from an application template; give a path instead of an object.")
    obj = next((o for o in objects(t, config) if o["name"] == source["object"]), None)
    if not obj:
        raise ConnectorError(f"{t['label']} has no object '{source['object']}'. Use Browse to see the list.")
    out = {"path": obj["path"], "params": dict(obj.get("params") or {}), "records_path": obj.get("records_path"),
           "pagination": obj.get("pagination") or {"type": "none"}}
    inc = obj.get("incremental") or {}
    if inc.get("cursor_param"):
        out["cursor_param"] = inc["cursor_param"]
    if inc.get("since_params"):
        out.update({"since_params": inc["since_params"], "since_format": inc.get("since_format", "iso"),
                    "lookback_seconds": inc.get("lookback_seconds", 0)})
    if source.get("cursor_column") or inc.get("cursor_column"):
        out["cursor_column"] = source.get("cursor_column") or inc.get("cursor_column")
    out["params"].update(source.get("params") or {})
    return out


def default_cursor(config: dict, object_name: str) -> str | None:
    t = TEMPLATES.get(config.get("template") or "")
    if not t:
        return None
    obj = next((o for o in objects(t, config) if o["name"] == object_name), None)
    return ((obj or {}).get("incremental") or {}).get("cursor_column")


def default_key(config: dict, object_name: str) -> list[str]:
    """The field that identifies a record, so changed records replace their old version (merge loads)."""
    t = TEMPLATES.get(config.get("template") or "")
    obj = next((o for o in objects(t, config) if o["name"] == object_name), None) if t else None
    k = ((obj or {}).get("incremental") or {}).get("key")
    return [k] if k else []


def incremental_apps() -> dict[str, list[str]]:
    """Which objects of which applications load only what changed (for the docs and the UI)."""
    return {k: [o["name"] for o in t["objects"] if o.get("incremental")] for k, t in TEMPLATES.items()}
