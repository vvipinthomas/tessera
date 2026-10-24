# Changelog

All notable changes to Tessera. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org).

## [Unreleased]

## [1.7.1] - 2026-10-24

### Fixed
- Release builds: the 1.7.0 release workflow could not build the standalone landing page, so no package files were
  attached to the release. 1.7.1 attaches the wheel, the source package and the landing page.
- Dates in executive briefs, insights, plain-English questions and the demo data now follow `TESSERA_TIMEZONE`, like
  schedules do, instead of the server clock's zone.
- `/api/ready` (which needs no sign-in) no longer returns internal error text; details go to the server log.
- Email checks use one strict pattern with the 254-character limit, so long crafted input cannot slow the server.
- Exports and deliveries stream from DuckDB without an extra in-memory copy on DuckDB 2.0.

### Changed
- CodeQL runs the security-and-quality query suite. Licence metadata follows PEP 639. GitHub Actions are current.

## [1.7.0] - 2026-10-16

### Added
- **Business application connections** (Connections → Connect an application): Salesforce, Microsoft Dynamics 365 /
  Dataverse, Microsoft 365 (Graph, including SharePoint lists), HubSpot, Stripe, GitHub, Jira Cloud, Zendesk,
  ServiceNow, Shopify and Airtable. Sign-in, paging and objects are set up; you fill in what is yours.
- **Changes-only loads for applications**: Salesforce, Dynamics 365, Jira, ServiceNow, Shopify, GitHub issues and
  commits ask the application only for records changed since the last run (SOQL, OData, JQL and encoded-query
  filters), merged on the record id. Stripe loads new records.
- **OAuth 2 for any REST API**: client credentials and refresh token, with token caching, renewal and one retry after a
  rejected token. Page-continuation flags (`more_path`, `last_path`) and keys with dots such as `@odata.nextLink`.
- **Copy many tables**: copy a whole database, lake or folder in one step. Tessera creates one pipeline and picks merge,
  incremental or full loads per table from its primary key and change column. `POST /api/connections/{id}/copy`.
- **PostgreSQL mirroring**: tables kept in step from the database's change log (logical decoding with the built-in
  `test_decoding` plugin). Inserts, updates, deletes, truncates and key changes are applied within seconds, the slot
  is advanced only after a successful write, and status, lag, Sync now, Pause and Stop are shown on Connections.
- **Merge load mode**: upsert on key columns, last row per key wins; with a cursor column, only changed rows.
- **Schema history**: every load records columns added, no longer sent or retyped. Owners and admins are notified with
  the downstream tables to check. Catalog → dataset → Schema history and `GET /api/datasets/{id}/schema-changes`.
- Deploy to Cloudflare entirely from GitHub (the workflow creates the bucket and sets the Worker's secrets), with a
  step-by-step guide for doing it from a phone and testing on Android ([docs/CLOUDFLARE_MOBILE.md](docs/CLOUDFLARE_MOBILE.md)).

### Changed
- Appends and merges widen a column whose type no longer fits instead of converting the new values (a decimal
  arriving in an integer column used to be rounded).
- SQL table names may contain spaces, `#`, `@` and `-` (they are always quoted).
- A connection reports only the secret fields that are actually set.
- CI and `make lint` syntax-check the web app's JavaScript.

### Fixed
- **Row-level security with DuckDB 2.0**: DuckDB 2.0 writes table names back from a new field, so the per-person filter
  was dropped and a restricted viewer could see every row. The rewrite now sets that field, and Tessera re-checks every
  rewritten query and refuses to run it if a protected table would be read unfiltered. Fresh installs pick up DuckDB 2.0,
  so upgrade if you installed 1.5 or 1.6 recently.
- Exports and deliveries failed on DuckDB 2.0 (Arrow methods were renamed); both 1.x and 2.x now work. DuckDB is capped
  below 3.0 until the next major version is tested.
- The CI container check used a one-letter container name, which current Docker rejects.

## [1.6.0] - 2026-10-16

### Added
- **Tessera on Cloudflare**: a Worker front door (`cloudflare/`) and Tessera in Cloudflare Containers, deployed with
  `npx wrangler deploy`. All connectors, streams and features run unchanged. A starting page while a container wakes,
  a keep-warm cron so schedules and streams run on time, and the visitor's real address passed through.
- **Cloud mode** (`tessera cloud-run`) for hosts with temporary disks: restore from an S3-compatible bucket (R2, S3,
  MinIO) on start, consistent backups every few minutes when anything changed, after every pipeline run and on
  shutdown, retention, and one-time point-in-time restore. Status and "Back up now" in Admin → Operations.
- Cloudflare Workers AI as an AI provider; R2 Data Catalog (Iceberg) and Delta-on-R2 documented and listed.
- `TESSERA_SETUP_CODE` to set the first-run code on hosted platforms.
- `TESSERA_TRUST_PROXY_HEADERS` so sign-in lockout, rate limits and the audit log see each visitor's address behind a
  proxy; turned on in the HTTPS, Tunnel and Cloudflare setups.

### Fixed
- Behind a reverse proxy every visitor shared the proxy's address, so one person's failed sign-ins could lock out
  everyone and rate limits applied to all users together.

## [1.5.0] - 2026-10-14

### Added
- **Streaming sources**: Apache Kafka (Confluent, Redpanda, MSK), Azure Event Hubs, Amazon Kinesis, MQTT and HTTP push
  streams land continuously in raw tables. Positions are saved after each batch (at-least-once), optional
  de-duplication by key, rejected payloads kept in `<table>__rejects`, retention, preview, reset, lag and throughput,
  automatic retry with back-off, and resume after restart. Streams page, stream health in the *data health* AI tool,
  and a simulated checkout feed in demo mode.
- **Live dashboards**: a Live toggle refreshes a dashboard every 5 seconds while it is visible.
- **Apache Iceberg** connector (REST, Glue, Hive and SQL catalogs) with filter pushdown, snapshot time travel and
  incremental loads.
- **SFTP** connector with host-key pinning and incremental loads by file modification time.
- **OData v4 feed** at `/odata` so Power BI, Excel and Tableau read published datasets and saved reports with masking
  and row-level security. API tokens are accepted as the Basic-auth password.
- **Deliver** pipeline step: write published data to any SQL database or warehouse, a Delta Lake table (including
  OneLake), or Parquet/CSV on S3 or disk.
- **OpenLineage** events (START, COMPLETE, FAIL, ABORT with schema facets) for every pipeline run.
- **dbt manifest import**: descriptions, owners, tags, tests as quality rules and lineage.
- Admin → Integrations page.
- Deployment kit: Caddy HTTPS compose file, Cloudflare Tunnel compose file, AWS CloudFormation template with
  encrypted EBS data volume and nightly S3 backups.
- Documentation: user guide, deployment, configuration, integrations and architecture guides; contributor docs, CI,
  issue templates and release workflow.

### Changed
- New design system ("Mosaic"): Instrument Sans with condensed tabular numerals for figures, mineral palette,
  sentence-case labels, radius hierarchy, refreshed light and dark themes, landing page and logo. Fonts are bundled,
  so the app makes no external requests.
- `TESSERA_MAX_UPLOAD_MB` is read from the environment.
- `tessera mcp` defaults to port 8710.

### Fixed
- Queries returning `TIMESTAMP WITH TIME ZONE` failed on installs without `pytz` (pandas 3 no longer installs it);
  `pytz` is now a dependency.
- The standalone landing page linked to a comparison section that no longer exists.

## [1.4.0]

### Added
- Demo mode toggle with a guided tour of every screen, and exact removal when switching to real data.
- Plain-English questions in Explore and the search bar, offline.
- Six new visuals (combo, multi-line, scatter, treemap, waterfall, funnel), KPI targets and a drag-and-drop dashboard
  designer.
- Share links and embeds (off by default) with expiry, revocation and locked filters.
- Query result cache invalidated by data version; memoised briefs and insights.
- MongoDB, Elasticsearch/OpenSearch and Delta Lake connectors.

### Security
- Route authorization matrix test; pipelines, runs and AI provider listings now require the analyst role.

## [1.3.0]

### Added
- Executive brief: year over year, year to date, momentum, full-year landing forecast, target likelihood, what-if,
  drivers and trending segments; fiscal years; email section in subscriptions.
- Seasonal forecasting (six methods chosen by holdout accuracy) and seasonality-aware anomaly detection.

## [1.2.0]

### Added
- Workspaces, SCIM 2.0 provisioning, Prometheus metrics, rate limiting, readiness probe, backups with checksums.
- Insights engine (trends, outliers, decomposition, explain change) and AI-assisted dashboard analysis.
- MCP server with governed tools, resources and prompts; agent reliability guards.

## [1.1.0]

### Added
- Semantic models, row-level security, reports, subscriptions and alerts, visual transforms, notebooks, glossary,
  deployment bundles.

## [1.0.0]

### Added
- First release: connectors, pipelines, data quality, PII detection and masking, catalog, SQL, dashboards, AI agents,
  OIDC SSO and audit.

[Unreleased]: https://github.com/vvipinthomas/tessera/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/vvipinthomas/tessera/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/vvipinthomas/tessera/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/vvipinthomas/tessera/releases/tag/v1.4.0
