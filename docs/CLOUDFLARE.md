# Tessera on Cloudflare

Run the complete Tessera, with all 25 connectors, streams, pipelines, dashboards and AI, on Cloudflare. Nothing is
cut down: Cloudflare runs the same container image you would run anywhere else, and Cloudflare R2 keeps its data.

```
 people, Power BI, AI clients ──HTTPS──▶  Worker  ──────────▶  Tessera container  ──backups──▶  R2 bucket
                                      (front door,          (Python, DuckDB, every        (latest state,
                                       Access, cron)         connector and stream)         restored on start)
```

| Piece | Cloudflare product | Job |
|---|---|---|
| Front door | Workers | TLS, your domain, optional Cloudflare Access sign-in, wakes and keeps Tessera running, shows a "starting" page |
| Tessera | Containers | The full application, built from this repository's `Dockerfile` |
| Durable storage | R2 | Consistent backups every few minutes, after every pipeline run and on every shutdown; restored automatically |
| Your lakehouse (optional) | R2, R2 Data Catalog | Delta and Iceberg tables Tessera reads and writes |
| AI (optional) | Workers AI, AI Gateway | Models for Tessera's agents, or a gateway in front of Anthropic and OpenAI |

## Why it works this way

Cloudflare Containers give each container a fresh disk whenever it sleeps, restarts or is replaced by a deploy.
Tessera keeps its data on disk (SQLite and DuckDB), so on Cloudflare it runs in **cloud mode**
(`tessera cloud-run`):

- **On start**, a container with an empty disk restores the newest backup from R2.
- **While running**, Tessera writes a consistent backup to R2 every 5 minutes when anything changed, and straight
  after every pipeline run.
- **On shutdown** (sleep, deploy, platform maintenance), Cloudflare sends `SIGTERM`; Tessera stops pipelines and
  streams, writes a final backup, then exits.

A planned stop loses nothing. An unplanned crash loses at most the changes since the last backup (5 minutes by
default, configurable). The encryption key is **not** stored in R2: it lives in Workers secrets and is handed to the
container at start.

## What you need

- A Cloudflare account on the **Workers Paid** plan (Containers require it).
- **Docker** running on the machine you deploy from: Wrangler builds the image locally and pushes it to Cloudflare.
- **Node.js 20 or newer.**
- About 20 minutes.

## Deploy

```bash
git clone https://github.com/vvipinthomas/tessera.git
cd tessera/cloudflare
npm ci
npx wrangler login
```

**1. Create the bucket for Tessera's data**

```bash
npx wrangler r2 bucket create tessera-data
```

**2. Create an R2 API token** so the container can write backups. In the Cloudflare dashboard: **R2 → Manage API
tokens → Create API token**, permission **Object Read & Write**, limited to the `tessera-data` bucket. Copy the
**Access Key ID**, the **Secret Access Key** and your **Account ID** (shown on the R2 overview page).

**3. Store the secrets**

```bash
# The key that encrypts saved connection passwords. Keep a copy in your password manager:
# without it, a restored Tessera cannot read the passwords it saved.
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
npx wrangler secret put TESSERA_MASTER_KEY

npx wrangler secret put TESSERA_SETUP_CODE                 # any phrase of 8+ characters, used once to create the admin
npx wrangler secret put R2_ACCOUNT_ID
npx wrangler secret put TESSERA_CLOUD_ACCESS_KEY_ID
npx wrangler secret put TESSERA_CLOUD_SECRET_ACCESS_KEY
```

**4. Deploy**

```bash
npx wrangler deploy
```

Wrangler builds the image, uploads it and prints the address, for example `https://tessera.<your-subdomain>.workers.dev`.
The first deploy can take several minutes while Cloudflare provisions the container.

**5. Open it.** You will see *Tessera is starting* for up to a minute, then the setup screen. Enter your setup code,
create the admin account, and leave **Start in demo mode** ticked if you want the guided tour.

**Deploying from GitHub instead.** Fork the repository, add `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as
repository secrets, set the Tessera secrets once as in step 3, then run **Actions → Deploy to Cloudflare**.

## Your own domain

Dashboard: **Workers & Pages → tessera → Settings → Domains & Routes → Add → Custom domain**, for example
`data.example.com`. Then tell Tessera its public address (used in emails, share links, SSO and the OData feed):

```jsonc
// cloudflare/wrangler.jsonc → "vars"
"TESSERA_BASE_URL": "https://data.example.com"
```

and run `npx wrangler deploy` again.

## Sign-in with Cloudflare Access (optional)

**Zero Trust → Access → Applications → Add an application → Self-hosted** for your Tessera domain, with a policy
such as "emails ending in @example.com". Add **Bypass** policies for the paths machines call with their own tokens:
`/odata/*`, `/mcp`, `/api/streams/*/events`, `/scim/v2/*` and `/api/auth/oidc/callback`. Tessera's own roles,
workspaces and row-level security still apply after Access lets someone in.

## Settings

Put plain settings in `"vars"` in `wrangler.jsonc`; put anything sensitive in a secret (`npx wrangler secret put`).
Every variable whose name starts with `TESSERA_` is passed to the container, so everything in
[CONFIGURATION.md](CONFIGURATION.md) works here too.

| Setting | Default | What it does |
|---|---|---|
| `TESSERA_CLOUD_BACKUP_MINUTES` | `5` | How often to back up when something changed. Lower means less to lose in a crash |
| `TESSERA_CLOUD_KEEP` | `48` | Backups to keep in R2 (older ones are deleted) |
| `TESSERA_CLOUD_BUCKET` | `tessera-data` | The bucket for backups |
| `TESSERA_CLOUD_PREFIX` | `tessera/` | Folder inside the bucket, so one bucket can hold several Tesseras |
| `KEEP_WARM` | `true` | A cron pings Tessera every 5 minutes so it never sleeps and schedules, alerts and streams run on time. Set `false` to let it sleep when nobody uses it (cheaper; scheduled work then waits until the next visit) |
| `SLEEP_AFTER` | `30m` | How long without requests before an idle Tessera sleeps (only matters when `KEEP_WARM` is `false`) |
| `instance_type` | `standard-3` | 2 vCPU, 8 GiB memory, 16 GB disk. Use `standard-4` (4 vCPU, 12 GiB, 20 GB) for larger data |

## Operating it

| Task | How |
|---|---|
| Is it running? | `https://<your tessera>/__tessera/status` |
| Logs | `npx wrangler tail`, or **Workers & Pages → tessera → Logs** (container logs under **Containers**) |
| Backup status, back up now | **Admin → Operations → Cloud backups** in Tessera |
| See the backups | **R2 → tessera-data → tessera/backups/** |
| Upgrade | `git pull && npx wrangler deploy`. The old container writes its final backup, the new one restores it. Visitors see the starting page for about a minute |
| Go back to an earlier point | Add `"TESSERA_CLOUD_RESTORE_KEY": "backups/tessera-<time>.tar.gz"` to `vars` and deploy. The new container restores that backup once and makes it the latest; remove the setting afterwards |
| Move off Cloudflare | Download the latest backup from R2 and `tessera restore` it on a server or AWS (see [DEPLOY.md](DEPLOY.md)) with the same master key |

## Your lakehouse on R2

Tessera reads and writes open table formats directly on R2, so your data stays in your account in formats Spark,
Databricks, Snowflake, DuckDB and Trino can read too.

**Iceberg with R2 Data Catalog.** Turn on the catalog for a bucket (**R2 → bucket → Settings → R2 Data Catalog**), and
create an API token with R2 and Data Catalog permissions. In Tessera: **Connections → New → Apache Iceberg tables**:

| Field | Value |
|---|---|
| Catalog | `rest` |
| Catalog URI | the *Catalog URI* shown on the bucket's settings page |
| Warehouse | the *Warehouse* name shown there |
| Token | the API token |

**Delta tables on R2.** **Connections → New → Delta Lake tables** with folder `s3://<bucket>/lake` and storage
options:

```json
{"AWS_ENDPOINT_URL": "https://<account id>.r2.cloudflarestorage.com", "AWS_REGION": "auto",
 "AWS_ACCESS_KEY_ID": "<key id>", "AWS_S3_ALLOW_UNSAFE_RENAME": "true"}
```

and the secret key in the encrypted *Storage secret* field. Add a **Deliver** step to a pipeline to publish gold tables
there. (`AWS_S3_ALLOW_UNSAFE_RENAME` is safe while Tessera is the only writer to those tables.)

**Plain files.** The **S3** connector works with R2: bucket name, endpoint
`https://<account id>.r2.cloudflarestorage.com`, region `auto`, and the key pair.

## AI on Cloudflare

- **Workers AI:** **Admin → AI providers → Cloudflare Workers AI**. Base URL
  `https://api.cloudflare.com/client/v4/accounts/<account id>/ai/v1`, an API token with Workers AI permission, and a
  model that supports function calling (Tessera's agents call tools), for example a Llama 3.3 70B instruct model.
- **AI Gateway** in front of Anthropic or OpenAI (caching, rate limits, logs): keep the provider type, and set its base
  URL to the gateway's, for example `https://gateway.ai.cloudflare.com/v1/<account id>/<gateway>/anthropic`.

## Limits to know

- **One instance.** Tessera is a single-server product, so `max_instances` is 1. Size it with `instance_type`.
- **Data size.** The warehouse must fit the container disk (16 GB on `standard-3`, 20 GB on `standard-4`, with room for
  temporary files) and be backed up within the interval; a few gigabytes is comfortable. Larger data belongs in R2 as
  Delta or Iceberg tables, or in a warehouse Tessera reads from.
- **Crash window.** An unplanned crash can lose up to `TESSERA_CLOUD_BACKUP_MINUTES` of changes. Pipelines can simply
  be re-run; HTTP push senders should retry on errors.
- **Uploads** through the Worker are limited by your Cloudflare plan's request body size (100 MB on most plans). Load
  bigger files from R2 or S3 with a connector instead.
- **Inbound traffic is HTTP only.** That covers the app, the API, OData, MCP and HTTP push. Streams *read from* Kafka,
  Event Hubs, Kinesis and MQTT, which works because containers can connect out.

## Running it without Containers

If you prefer to run Tessera on your own server and only use Cloudflare in front of it (no inbound ports, Access,
WAF), use a Cloudflare Tunnel instead: see [DEPLOY.md → Cloudflare](DEPLOY.md#cloudflare).
