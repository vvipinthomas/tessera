# Deploying Tessera

Tessera is a single Python process. It keeps all of its state in one data folder: `tessera.db` (SQLite metadata),
`warehouse.duckdb` (the analytical store), `imports/`, `exports/` and `master.key`. Three things matter wherever
you run it:

1. **A persistent disk** for the data folder. Anything that wipes the disk on restart, sleep or rescheduling loses data.
2. **HTTPS** in front of it, with `TESSERA_SECURE_COOKIES=true` and `TESSERA_BASE_URL` set to the public address.
3. **Backups** of the data folder, including `master.key` (without it, stored connection passwords cannot be decrypted).

Run exactly one Tessera process per data folder. It is not designed for several replicas sharing a disk.

| Option | Good for | Effort |
|---|---|---|
| [Your computer](#your-computer) | Trying it, personal analysis | 2 minutes |
| [Any server with Docker](#any-server-with-docker) | A team on a VM you already have | 10 minutes |
| [AWS](#aws) | A managed, backed-up install from one template | 15 minutes |
| [Cloudflare Containers](CLOUDFLARE.md) | Running all of Tessera on Cloudflare, with data kept in R2 and no server to manage | 20 minutes |
| [Cloudflare from a phone](CLOUDFLARE_MOBILE.md) | The same, deployed through GitHub Actions without a computer | 30 minutes |
| [Cloudflare Tunnel](#cloudflare) | Publishing a Tessera that runs on your own server, without opening ports, behind single sign-on | 20 minutes |

Sizing: 2 vCPU and 8 GB RAM serve a team of dozens with tens of gigabytes. DuckDB uses memory first and spills to
disk, so give the data volume room for about twice your largest table. On a 2-vCPU machine a mixed load ran at about
160 requests per second with p95 latency near 130 ms.

---

## Your computer

```bash
git clone https://github.com/vvipinthomas/tessera.git && cd tessera
python -m venv .venv && source .venv/bin/activate
pip install -e ".[s3,kafka,mqtt,iceberg,sftp,delta,mongodb]"
tessera serve                       # http://127.0.0.1:8710
```

`tessera serve` listens on 127.0.0.1 only. To reach it from other machines on your network, use
`tessera serve --host 0.0.0.0` and put HTTPS in front before anyone signs in from elsewhere.

On Windows, use PowerShell and `.venv\Scripts\Activate.ps1`. Everything else is the same.

---

## Any server with Docker

Works on any Linux VM (Ubuntu, Debian, Amazon Linux, RHEL), on-premises or in any cloud.

```bash
# 1. Install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/
git clone https://github.com/vvipinthomas/tessera.git && cd tessera

# 2a. HTTP on port 8710 (inside a private network only)
docker compose up -d --build

# 2b. HTTPS with an automatic Let's Encrypt certificate
#     First point a DNS A record at this server and open ports 80 and 443.
TESSERA_DOMAIN=tessera.example.com \
  docker compose -f docker-compose.yml -f deploy/https/docker-compose.https.yml up -d --build

# 3. Read the one-time setup code and open the site
docker compose logs tessera | grep "Setup code"
```

The HTTPS file adds a [Caddy](https://caddyserver.com) container that obtains and renews the certificate, removes the
direct port, and turns on secure cookies. Put `TESSERA_DOMAIN=...` in a `.env` file next to `docker-compose.yml` so
later commands pick it up.

**Where the data is.** In the `tessera-data` Docker volume. To keep it in a folder you choose instead, add a
`docker-compose.override.yml`:

```yaml
services:
  tessera:
    volumes: !override
      - /srv/tessera/data:/data
```

and run `sudo chown -R 10001:10001 /srv/tessera/data` once (the container runs as user 10001, not root).

**Upgrading.** `git pull && docker compose up -d --build`. Database changes are applied automatically at start-up.
Take a backup first.

---

## AWS

The template [`deploy/aws/tessera-ec2.yaml`](https://github.com/vvipinthomas/tessera/blob/main/deploy/aws/tessera-ec2.yaml) creates everything in one stack:

| Resource | Why |
|---|---|
| EC2 instance (Amazon Linux 2023, Docker) | Runs Tessera and, when you give a domain, Caddy for HTTPS |
| Encrypted gp3 EBS **data volume** mounted at `/srv/tessera` | Holds the data folder. Snapshotted, not deleted, if the stack is deleted |
| Elastic IP | A stable address for your DNS record |
| Private, versioned, encrypted **S3 bucket** | Nightly consistent backups, kept 35 days. Retained if the stack is deleted |
| IAM role with Systems Manager | Shell access through Session Manager, so no SSH port is open |
| Security group | 443 (and 80 for the certificate) from the range you choose |

### Create the stack

Console: CloudFormation → Create stack → Upload a template file → `deploy/aws/tessera-ec2.yaml`.

Or with the AWS CLI:

```bash
aws cloudformation deploy \
  --stack-name tessera \
  --template-file deploy/aws/tessera-ec2.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides DomainName=tessera.example.com AllowedCidr=203.0.113.0/24 InstanceType=m7i.large DataVolumeGiB=100

aws cloudformation describe-stacks --stack-name tessera --query "Stacks[0].Outputs"
```

Then:

1. **DNS.** Create an A record for your domain pointing at the `PublicIp` output. Caddy gets the certificate on the
   first request after the record resolves (a minute or two). Without a domain, Tessera is at `http://<PublicIp>:8710`;
   use that only from a trusted network.
2. **Setup code.** Connect with Session Manager (`aws ssm start-session --target <instance-id>`, or EC2 → Connect →
   Session Manager in the console) and run `sudo cat /srv/tessera/data/setup-code.txt`. Setup takes about five minutes
   after the stack completes; progress is in `/var/log/tessera-setup.log`.
3. Open the URL, enter the code and create the admin.

### Backups and restore

A cron job runs `/usr/local/bin/tessera-backup` at the hour you chose. It stops Tessera for a few seconds, writes a
checksummed archive with `tessera backup --include-key`, starts Tessera, and uploads the archive to
`s3://<BackupBucket>/backups/`. The archive contains `master.key`, which is why the bucket blocks public access,
requires TLS and is encrypted. Logs are in `/var/log/tessera-backup.log`.

To restore onto a new instance:

```bash
aws s3 cp s3://<BackupBucket>/backups/tessera-backup-<time>.tar.gz /tmp/
cd /opt/tessera && $(cat .compose-cmd) stop tessera
$(cat .compose-cmd) run --rm --no-deps -v /tmp:/restore tessera tessera restore /restore/tessera-backup-<time>.tar.gz --force
$(cat .compose-cmd) start tessera
```

### Upgrading on AWS

```bash
cd /opt/tessera && sudo git fetch --tags && sudo git checkout v1.5.1   # the new tag
sudo $(cat .compose-cmd) up -d --build
```

### Other AWS options

- **Amazon ECS on EC2** works with the same container image if you pin the task to one instance and mount an EBS
  volume (or a host path) at `/data`. Run one task only.
- **AWS Fargate with EFS, App Runner, Lambda and Lightsail containers are not recommended.** Fargate and App Runner
  disks are temporary; EFS is a network file system, which DuckDB and SQLite do not handle well for locking or speed;
  Lambda cannot keep a server running.
- **Load balancer instead of Caddy:** put an Application Load Balancer with an ACM certificate in front of port 8710,
  set `TESSERA_SECURE_COOKIES=true` and `TESSERA_BASE_URL`, and set the target group health check to `/api/ready`.
- **Snapshots:** you can add an Amazon Data Lifecycle Manager policy for daily EBS snapshots of the data volume as a
  second layer of protection.

---

## Cloudflare

There are two ways to use Cloudflare:

- **Run Tessera on Cloudflare** with Containers, a Worker in front and data kept in R2. No server to manage. See
  **[CLOUDFLARE.md](CLOUDFLARE.md)**.
- **Publish a Tessera that runs on your own server** through a Cloudflare Tunnel, described here. Use this when the
  data must stay on your hardware, or when the warehouse is too big for a container's disk.

The tunnel setup:

```
browser ──HTTPS──▶ Cloudflare edge (TLS, WAF, optional Access SSO) ──Tunnel──▶ cloudflared ──▶ Tessera on your server
```

Tessera runs on any machine you have (an office server, a home lab, a VM in any cloud). The machine needs no open
inbound ports: `cloudflared` makes an outbound connection to Cloudflare.

### 1. Create the tunnel

1. In the Cloudflare dashboard, open **Zero Trust → Networks → Tunnels → Create a tunnel**, choose **Cloudflared**,
   name it `tessera`, and copy the **token** shown in the install command.
2. Under **Public hostnames**, add `tessera.example.com` (a domain on your Cloudflare account) with service type
   **HTTP** and URL `tessera:8710`.

### 2. Start Tessera with the tunnel

```bash
git clone https://github.com/vvipinthomas/tessera.git && cd tessera
cat > .env <<'EOF'
TUNNEL_TOKEN=<token from step 1>
TESSERA_DOMAIN=tessera.example.com
EOF
docker compose -f docker-compose.yml -f deploy/cloudflare/docker-compose.tunnel.yml up -d --build
docker compose logs tessera | grep "Setup code"
```

Open `https://tessera.example.com`. Tessera's port is not published on the host at all; only the tunnel can reach it.

### 3. Optional: Cloudflare Access in front

Access adds your identity provider's sign-in before anyone reaches Tessera's own sign-in page.

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**, domain `tessera.example.com`, with a
   policy such as "emails ending in @example.com".
2. Machines also call Tessera, so add **Bypass** (or service-token) policies for these paths, which carry their own
   tokens:
   - `/odata/*` (Power BI, Excel, Tableau)
   - `/mcp` (AI clients)
   - `/api/streams/*/events` (HTTP push streams)
   - `/scim/v2/*` (user provisioning from Entra ID or Okta)
   - `/api/auth/oidc/callback` if you also use Tessera's own SSO
3. If you use Tessera's built-in OpenID Connect SSO as well, people sign in twice. Many teams use Access for the
   network boundary and Tessera SSO for roles; both work.

### 4. Optional: the landing page on Cloudflare Pages

The marketing page can be built as one static file and hosted for free:

```bash
python scripts/build_artifact.py site/index.html
npx wrangler pages deploy site --project-name tessera-site
```

### Backups with Cloudflare

Cloudflare does not hold any Tessera data. Back up the server's data folder as in [Backups](#backups), for example to
Cloudflare R2 with any S3 tool: `aws s3 cp backup.tar.gz s3://bucket/ --endpoint-url https://<account>.r2.cloudflarestorage.com`.

---

## Backups

| Method | Downtime | Command |
|---|---|---|
| Admin page | None | **Admin → Operations → Download backup** |
| API (scriptable) | None | `curl -X POST https://tessera.example.com/api/admin/backup -H "Authorization: Bearer <admin token>" -H "x-tessera-csrf: 1" -o backup.tar.gz` |
| Command line | Stop first | `tessera backup --out backup.tar.gz [--include-key]` |

Restore with `tessera restore backup.tar.gz --force` while Tessera is stopped. Archives carry a SHA-256 manifest and
restore refuses a tampered or truncated file.

## Production checklist

- [ ] HTTPS in front, `TESSERA_SECURE_COOKIES=true`, `TESSERA_BASE_URL` set
- [ ] The data folder on a persistent, backed-up disk; `master.key` stored safely (or `TESSERA_MASTER_KEY` from a secret store)
- [ ] Nightly backups, and one restore tried on a spare machine
- [ ] SSO configured (Admin → Single sign-on) and the setup admin's password stored in a vault
- [ ] `TESSERA_TIMEZONE` set for schedules; `TESSERA_LOG_JSON=true` if you ship logs
- [ ] `/metrics` scraped with `TESSERA_METRICS_TOKEN`; `/api/ready` used as the health check
- [ ] `TESSERA_BLOCK_PRIVATE_HTTP=true` if REST connectors must not reach your internal network
- [ ] Email configured (Admin → Email) so alerts and subscriptions can be delivered

See [CONFIGURATION.md](CONFIGURATION.md) for every setting.
