# Configuration

Tessera reads its settings from environment variables at start-up. Everything else (users, SSO, email, AI providers,
sharing, OpenLineage, retention) is set in the app under **Admin** and stored in the data folder.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `TESSERA_DATA_DIR` | `./tessera-data` | The data folder: metadata database, DuckDB warehouse, uploads, exports, `master.key` |
| `TESSERA_HOST` | `127.0.0.1` | Address to listen on. The container image uses `0.0.0.0` |
| `TESSERA_PORT` | `8710` | Port to listen on |
| `TESSERA_BASE_URL` | – | Public address, for example `https://tessera.example.com`. Used for SSO redirects, links in emails, the OData feed and share links |
| `TESSERA_SECURE_COOKIES` | `false` | Set `true` whenever people reach Tessera over HTTPS. Adds `Secure` cookies and HSTS |
| `TESSERA_SESSION_HOURS` | `12` | How long a sign-in lasts |
| `TESSERA_MASTER_KEY` | generated | Base64 32-byte key that encrypts stored secrets. If unset, `master.key` is created in the data folder. Generate one with `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` |
| `TESSERA_IMPORT_DIRS` | – | Extra server folders the Files, SQLite, Delta and Iceberg connectors may read, separated by `:` (`;` on Windows) |
| `TESSERA_MAX_UPLOAD_MB` | `512` | Largest file accepted by the upload page |
| `TESSERA_TIMEZONE` | `UTC` | Time zone for pipeline, alert and subscription schedules, for example `America/Vancouver` |
| `TESSERA_WORKERS` | `2` | Pipeline runs that may execute at the same time |
| `TESSERA_SCHEDULER_INTERVAL` | `20` | Seconds between scheduler checks |
| `TESSERA_RETRY_BASE_SECONDS` | `2` | First retry delay for failing pipeline steps (doubles each attempt) |
| `TESSERA_QUERY_TIMEOUT` | `60` | Seconds before an interactive query is cancelled |
| `TESSERA_MAX_RESULT_ROWS` | `10000` | Row limit for interactive results (exports and OData page through larger results) |
| `TESSERA_BLOCK_PRIVATE_HTTP` | `false` | Stop REST, Iceberg REST, OpenLineage, streaming and SFTP connections from reaching private network addresses. Cloud metadata and link-local addresses are always blocked |
| `TESSERA_RATE_LIMIT` | `1200` | API requests per minute per caller (token, session or IP) |
| `TESSERA_METRICS_TOKEN` | – | Bearer token that may read `/metrics` (otherwise an admin session is required) |
| `TESSERA_LOG_JSON` | `false` | JSON log lines with request IDs, and an access log |
| `TESSERA_MCP_ALLOWED_ORIGINS` | – | Extra browser origins allowed to call `/mcp`, comma-separated |
| `TESSERA_TRUST_PROXY_HEADERS` | `false` | Use the first `X-Forwarded-For` address as the caller's address (sign-in lockout, rate limits, audit). Turn on only when Tessera is reachable solely through your proxy |
| `TESSERA_SETUP_CODE` | generated | Set the one-time setup code yourself (8+ characters) instead of reading it from the console; useful on hosted platforms |

### Cloud mode (`tessera cloud-run`)

For hosts whose disk does not survive a restart. See [CLOUDFLARE.md](CLOUDFLARE.md).

| Variable | Default | What it does |
|---|---|---|
| `TESSERA_CLOUD_BUCKET` | – | S3-compatible bucket for backups (required) |
| `TESSERA_CLOUD_ENDPOINT` | AWS S3 | Endpoint, for Cloudflare R2 `https://<account id>.r2.cloudflarestorage.com` |
| `TESSERA_CLOUD_ACCESS_KEY_ID` / `TESSERA_CLOUD_SECRET_ACCESS_KEY` | – | Credentials (or the platform's own role on AWS) |
| `TESSERA_CLOUD_REGION` | `auto` | Region (R2 uses `auto`) |
| `TESSERA_CLOUD_PREFIX` | `tessera/` | Folder inside the bucket |
| `TESSERA_CLOUD_BACKUP_MINUTES` | `5` | Back up this often when anything changed (also after every pipeline run and on shutdown) |
| `TESSERA_CLOUD_KEEP` | `48` | Backups to keep |
| `TESSERA_CLOUD_RESTORE_KEY` | – | Restore this backup instead of the latest, once |

Cloud mode requires `TESSERA_MASTER_KEY`, because backups deliberately leave the key out.

## Command line

```
tessera serve [--host 0.0.0.0] [--port 8710]          run the server
tessera create-user someone@example.com --role engineer
tessera reset-password someone@example.com            also unlocks the account
tessera backup --out backup.tar.gz [--include-key]    stop the server first, or use Admin → Operations
tessera restore backup.tar.gz [--force]
tessera mcp --url https://tessera.example.com         MCP stdio bridge for desktop AI clients (token in TESSERA_TOKEN)
tessera cloud-run [--port 8710]                       run with data kept in an S3/R2 bucket (cloud mode)
tessera version
```

Every command accepts `--data-dir` before the sub-command, for example `tessera --data-dir /srv/tessera serve`.

## Roles

| Role | Can |
|---|---|
| viewer | Open dashboards, reports and published datasets; ask questions; read the catalog and glossary |
| analyst | Everything a viewer can, plus SQL on published data, Explore, building reports and dashboards, alerts, subscriptions, AI agents and watching streams |
| engineer | Everything an analyst can, plus connections, streams, pipelines, transforms, quality rules, raw and curated data, models |
| admin | Everything, plus users, SSO, SCIM, email, AI providers, row-level security, integrations, sharing, backups and the audit log |

Workspaces add per-workspace roles (viewer, contributor, admin) on top; see [ENTERPRISE.md](ENTERPRISE.md).

## Health and monitoring endpoints

| Endpoint | Use |
|---|---|
| `/api/health` | Liveness: the process answers |
| `/api/ready` | Readiness: metadata and warehouse are reachable (use for load balancers and container health checks) |
| `/metrics` | Prometheus metrics: requests, latency, pipeline runs, query cache |
| `/api/docs` | Interactive REST API reference |
