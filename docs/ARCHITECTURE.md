# Architecture

Tessera is one Python process that serves a web app and an API, runs background work (scheduler, pipeline workers,
stream workers, alert and subscription delivery) and keeps all state in one folder.

```
                     ┌────────────────────────── Tessera process (FastAPI + uvicorn) ──────────────────────────┐
 browser (SPA) ─────▶│  auth: sessions, OIDC, API tokens, SCIM      security headers, CSP, rate limit, audit     │
 Power BI/Excel ────▶│  /odata   OData v4 feed (published data, masking + RLS)                                   │
 AI clients ────────▶│  /mcp     MCP server (governed tools, resources, prompts)                                 │
 webhooks/apps ─────▶│  /api/streams/{id}/events  HTTP push                                                      │
                     │                                                                                           │
                     │  connectors ──▶ ingest ──▶ raw ──transform──▶ curated ──quality──▶ PII scan ──publish──▶ serving
                     │  stream workers ──micro-batches──▶ raw                                     deliver ──▶ warehouses, lakes
                     │                                                                                           │
                     │  query guard (DuckDB parse tree) ─▶ RLS rewrite ─▶ result cache ─▶ DuckDB (no file/network access)
                     │  semantic models ─▶ insights, forecasts, executive brief ─▶ dashboards, reports, alerts, email
                     │  AI agents ─▶ tools (same guard, PII firewall, grounding check) ─▶ model providers              │
                     └──────────────┬───────────────────────────────────────────────┬───────────────────────────┘
                                    ▼                                               ▼
                         tessera.db (SQLite, WAL)                        warehouse.duckdb
                         users, connections (secrets AES-GCM),           raw / curated / serving schemas
                         catalog, rules, pipelines, runs, models,
                         dashboards, streams, audit
```

## Modules

| Module | Responsibility |
|---|---|
| `app.py` | Routes for auth, users, connections, datasets, pipelines, SQL, dashboards; security headers; wiring |
| `warehouse.py` | DuckDB access, the read-only query guard, row-level security rewrite, result cache keyed by data version |
| `platform.py` | Ingest, transform, publish, quality runs, PII scans, catalog, lineage, readiness |
| `connectors.py`, `connectors_lake.py` | Batch connectors returning Arrow tables (files, SQL, REST, S3, MongoDB, Elasticsearch, Delta, Iceberg, SFTP) |
| `streaming.py`, `streams_api.py` | Stream readers (Kafka, Kinesis, MQTT, simulated), workers, positions, rejects, retention, HTTP push |
| `orchestrator.py` | Scheduler, dependency graph execution, retries, webhooks, OpenLineage events |
| `interop.py`, `odata.py` | Deliver step, OpenLineage, dbt import, OData feed |
| `quality.py`, `pii.py` | Rule compilation to SQL; personal-data detection and masking policies |
| `semantic.py`, `nlq.py` | Semantic models compiled to parameterised SQL; offline plain-English question parser |
| `insights.py`, `brief.py`, `insights_api.py` | Trends, outliers, decomposition, six-method forecasting, executive brief |
| `reporting.py`, `share.py` | Subscriptions, alerts, email, exports; share links and embeds |
| `agents.py`, `mcp.py` | Agent tool registry and providers; MCP server over Streamable HTTP and stdio |
| `enterprise.py`, `workspaces.py` | SCIM, metrics, rate limits, readiness, backups, sessions, audit export; workspace roles |
| `demo.py`, `sample.py` | Demo workspace install and exact removal |
| `static/` | Dependency-free JavaScript single-page app and the landing page |

## Security model

- **Three zones.** Data enters `raw` only through connectors and streams. Engineers transform it into `curated`.
  Publishing writes a masked copy into `serving`. Viewers, analysts, dashboards for them, OData, share links and AI
  agents acting for them can only read `serving`.
- **Query guard.** Every interactive statement is parsed by DuckDB (`json_serialize_sql`). Only a single `SELECT` or
  `EXPLAIN` runs. Table references come from the parse tree, including CTEs and subqueries. DuckDB runs with
  `enable_external_access=false`, so SQL cannot read files, attach databases, install extensions or reach the network.
- **Row-level security.** Policies map a published table's column to a user attribute. Before a restricted user's
  query runs, each reference to a protected table is rewritten to a per-query filtered view. This holds for SQL,
  Explore, dashboards, exports, subscriptions, alerts, forecasts, OData and AI tools.
- **Secrets.** Connection passwords, tokens and model keys are encrypted with AES-256-GCM using `master.key` and are
  never returned by the API. API tokens, share tokens and stream tokens are stored as SHA-256 hashes.
- **AI.** Agents and MCP clients act with the caller's role. Tool results pass a PII firewall before reaching a model
  provider. Numbers in answers are checked against tool output, and repeated identical tool calls are refused.
- **Web.** Content Security Policy `script-src 'self'`, `frame-ancestors 'none'` (except allow-listed embeds),
  SameSite cookies plus a CSRF header, scrypt password hashing and lockout.

## Design decisions

| Decision | Why | Trade-off |
|---|---|---|
| DuckDB + SQLite in one process | Fast analytics on one machine, nothing else to install, simple backups | One node; no horizontal scale-out |
| Micro-batch streaming with positions saved after the write | At-least-once delivery without a distributed log or checkpoint store | Seconds of latency; not a stream processor |
| Arrow everywhere between connectors and the warehouse | Zero-copy loads and automatic schema evolution | Very wide nested JSON is flattened |
| No front-end framework | Small, auditable, strict CSP, no build step | More hand-written DOM code |
| Statistical forecasting chosen by holdout accuracy | Explainable, cheap, honest about error | No external drivers (promotions, prices) |
| SQL and Markdown notebooks only | Running user code would bypass permissions | No Python in notebooks |
| OData rather than a Postgres wire endpoint | Native connectors in Power BI, Excel and Tableau, stateless, easy to secure | Read-only; no DirectQuery |
