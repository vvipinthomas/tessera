# Running Tessera in an organization

## Identity

- **Single sign-on**: OpenID Connect with PKCE (Entra ID, Okta, Google Workspace, Keycloak, Auth0). Admin → Single sign-on.
- **SCIM 2.0 provisioning**: Admin → Provisioning. Generate a token and give the identity provider the tenant URL `https://<host>/scim/v2`. Supported: Users and Groups (list with `eq` filters, create, read, replace, patch, delete). Deleting or deactivating a user ends their sessions and revokes their API tokens; nothing is hard-deleted.
- **Group mappings**: map an IdP group to a Tessera role and, optionally, to a workspace role. A user gets the highest role among their groups. Admin is never a default role.

## Workspaces

Workspaces group dashboards, reports, notebooks and semantic models. The **General** workspace is organization-wide. Private workspaces are invisible to non-members, including in search and to AI agents and MCP clients. Workspace roles: viewer (read), contributor (create and edit; also needs the global analyst role), admin (manage members). Data access rules (roles, row-level security) apply inside every workspace.

## Sharing and embedding

Off by default. Admin → Operations turns it on and lists the sites allowed to frame dashboards (`frame-ancestors`). Analysts who can contribute to a dashboard create links that expire after 1 to 365 days, optionally locked to the slicer values they have selected. Viewers of a link see the dashboard read-only with the creator's access narrowed by those filters; nothing else in Tessera is reachable with the token. Revoke links from the dashboard's Share dialog; turning sharing off revokes all of them.

## Demo mode

The demo workspace (sample retail data and a guided tour) can be loaded at setup or later by an admin from the top bar. "Switch to real data" removes exactly what the demo created plus anything built on demo data, after listing it, and leaves users, roles, SSO, tokens, workspaces and settings alone.

## Container image

Release images are published for amd64 and arm64 at `ghcr.io/vvipinthomas/tessera:<version>`. `docker compose up -d --build` builds from the included Dockerfile (Python virtual environment, non-root user 10001, `/data` volume, health check on `/api/ready`). The base image is a build argument: `docker build --build-arg BASE=<your approved Python 3.11+ Debian/Ubuntu image> .` for registries that mirror or harden their own bases.

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness |
| `GET /api/ready` | Readiness: metadata store and warehouse reachable (503 otherwise) |
| `GET /metrics` | Prometheus: request counts and latency histograms by route, rate-limit hits, users, datasets, alerts by state, pipeline runs by status, warehouse size. Needs `TESSERA_METRICS_TOKEN` or an admin session |

Every response carries `X-Request-ID` (an incoming one is kept). With `TESSERA_LOG_JSON=1` logs are JSON lines, including one access record per API call with the request ID. `TESSERA_RATE_LIMIT` (default 1200 per minute) limits each API token, session or IP; excess calls get HTTP 429 with `Retry-After`.

## Audit

Every sign-in, change, query, export, agent and MCP tool call is recorded. Admin → Operations exports CSV or JSON lines for a date range (for your SIEM) and sets retention (at least 30 days, or keep forever). Read notifications older than 90 days and expired sessions are purged daily.

## Backup and restore

- Online: Admin → Operations → Download backup (or `POST /api/admin/backup`). Consistent snapshot of the metadata database (SQLite online backup) and the warehouse (after a checkpoint), plus uploaded files, with SHA-256 checksums in `manifest.json`.
- Offline: `tessera backup --out tessera.tar.gz [--include-key]` with the server stopped.
- Restore: `tessera restore tessera.tar.gz [--force]` into an empty data folder. Checksums are verified and unsafe archive paths are refused.

Include `master.key` only if the archive is stored securely; otherwise keep the key separately. Without it, stored connection secrets cannot be decrypted.

## What is not included

Tessera runs on one node. There is no high-availability metadata store, multi-node query engine or zero-downtime upgrade. It has not had an independent penetration test. Plan capacity and recovery accordingly.
