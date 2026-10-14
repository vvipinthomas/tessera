# Changelog

All notable changes to Tessera. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org).

## [Unreleased]

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

[Unreleased]: https://github.com/vvipinthomas/tessera/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/vvipinthomas/tessera/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/vvipinthomas/tessera/releases/tag/v1.4.0
