# Integrations

Tessera is meant to sit alongside the tools a team already has. This page covers how data gets in (streaming and
batch), how it gets out (BI tools, warehouses, lakes) and how Tessera reports to the rest of the data stack
(lineage, dbt, orchestrators, AI clients). Admins see most of this on **Admin → Integrations**.

- [Power BI, Excel and Tableau](#power-bi-excel-and-tableau)
- [Streaming sources](#streaming-sources)
- [Lakehouse tables: Delta Lake and Apache Iceberg](#lakehouse-tables-delta-lake-and-apache-iceberg)
- [SFTP drops](#sftp-drops)
- [Delivering data to other systems](#delivering-data-to-other-systems)
- [Microsoft Fabric, Databricks and Snowflake](#microsoft-fabric-databricks-and-snowflake)
- [OpenLineage (Purview, DataHub, Marquez, OpenMetadata)](#openlineage)
- [dbt](#dbt)
- [Orchestrators (Airflow, Dagster, Prefect, ADF, CI)](#orchestrators)
- [AI clients over MCP](#ai-clients-over-mcp)
- [REST API](#rest-api)

---

## Power BI, Excel and Tableau

Tessera publishes a read-only **OData v4 feed** at `https://<your tessera>/odata`. It lists:

- every **published dataset** (the `serving` zone), and
- every **saved report** the token's owner can see, including reports built on semantic models, so Power BI shows
  the same certified Revenue as Tessera.

Masking and row-level security apply exactly as in the app: a token owned by a BC regional manager returns only BC
rows. Raw and curated data are never exposed.

**Create a token.** Admin → API tokens (or **API tokens** for non-admins) → Create token, role *viewer* or *analyst*.

**Power BI Desktop.** Get data → OData feed → paste the URL → **Basic**: any user name, the token as the password →
choose tables → Load. For scheduled refresh in the Power BI service, the URL must be reachable from Microsoft's cloud,
or go through an on-premises data gateway.

**Excel.** Data → Get Data → From Other Sources → From OData Feed → URL → **Basic**, token as the password.

**Tableau.** Connect → To a Server → OData → URL → user name (anything) and the token as the password.

Query options supported: `$select`, `$filter` (eq, ne, gt, ge, lt, le, and, or, not, parentheses, `contains`,
`startswith`, `endswith`, `tolower`, `toupper`, `year`, `month`, `day`, `length`, null), `$orderby`, `$top`, `$skip`,
`$count` and server-driven paging (`@odata.nextLink`, 5,000 rows per page, or `Prefer: odata.maxpagesize=n` up to
50,000). Every row has a synthetic `__row` key. Each read is recorded in the audit log.

---

## Streaming sources

**Data → Streams.** A stream reads a source continuously and appends to a raw table. Events arrive in micro-batches
(up to *max events per batch*, or whatever arrived within *max wait*, default 2 seconds).

| Source | Connection type | Position saved |
|---|---|---|
| Apache Kafka, Confluent Cloud, Redpanda, Amazon MSK, Aiven, WarpStream | Kafka | Offset per partition |
| Azure Event Hubs (Standard tier and above, through its Kafka endpoint) | Azure Event Hubs | Offset per partition |
| Amazon Kinesis Data Streams | Kinesis | Sequence number per shard |
| MQTT (Mosquitto, EMQX, HiveMQ, AWS IoT Core, Azure Event Grid MQTT) | MQTT | Broker-side, QoS 1 persistent session |
| Anything that can POST JSON: webhooks, Segment, Snowplow, your apps | none (HTTP push) | Each request is stored before the reply |

**Delivery guarantee.** At least once. Tessera saves a stream's position only after the batch is in the warehouse, so a
crash or restart re-reads the last batch rather than losing it. Set **De-duplicate on column** (for example
`event_id`) to make replays harmless; the latest event per key is kept.

**What lands.** Your event fields as columns (nested objects are flattened, new fields add columns automatically), plus
`_key`, `_partition`, `_offset`, `_event_time` (broker time) and `_ingested_at`. Payloads that are not valid JSON go
to `<table>__rejects` with the raw text and the reason, and the stream keeps going.

**Operations.** Each stream shows status, events per minute, consumer lag (Kafka) or time behind latest (Kinesis),
rows landed, rejects and the last error. Failing streams retry with back-off and are listed by the *data health* AI tool.
Streams that were running start again when Tessera restarts. **Retention** trims rows older than N days, hourly.
**Preview** reads a few events from the saved position without moving it. **Reset position** jumps to earliest or latest.

**HTTP push.** Creating an HTTP push stream shows a token once:

```bash
curl -X POST https://tessera.example.com/api/streams/7/events \
  -H "Authorization: Bearer tsk_..." -H "Content-Type: application/json" \
  -d '[{"event_id":"e-1","type":"signup","plan":"pro"}]'
```

It accepts one object, an array, `{"events": [...]}` or NDJSON (`Content-Type: application/x-ndjson`), up to 5 MB or
50,000 events per request. A 200 response means the events are stored.

**Live dashboards.** Build tiles on the stream's table (after a transform and publish, or directly in SQL for
engineers), then press **Live** on the dashboard to refresh every 5 seconds.

**Event Hubs settings.** Namespace (for example `contoso-events`) and a connection string with *Listen* rights. Each
event hub is a topic. **Confluent Cloud:** security `SASL_SSL`, mechanism `PLAIN`, API key and secret.
**Amazon MSK with IAM auth** is not supported yet; use SASL/SCRAM.

**Limits.** This is micro-batch ingestion into one server, not a stream processor. Avro and Protobuf payloads with a
schema registry are on the roadmap; today payloads are JSON or text.

---

## Lakehouse tables: Delta Lake and Apache Iceberg

**Delta Lake** connections read tables from a folder Tessera may access, or from `s3://`, `abfss://` (Azure Data Lake
and OneLake) and `gs://` paths with storage options. Pick a version to time-travel; use a cursor column for incremental
loads.

**Apache Iceberg** connections use a catalog:

| Catalog | Settings |
|---|---|
| REST (Apache Polaris, Snowflake Open Catalog, Unity Catalog's Iceberg REST, Nessie, Lakekeeper, Tabular) | URI, warehouse, token or `client_id:secret` |
| AWS Glue | Catalog *glue*, region in extra properties (`{"glue.region": "us-east-1"}`) |
| Hive metastore | URI `thrift://host:9083` |
| SQL (shared with PyIceberg or Spark's JDBC catalog) | Database URI, warehouse, catalog name |

Load by `namespace.table`, optionally with a **row filter** pushed down to the scan (`amount > 0 and region = 'west'`),
a **snapshot id** for time travel, and a **cursor column** for incremental loads. Storage credentials for S3, ADLS or
GCS go in extra properties using PyIceberg's names (for example `s3.access-key-id`).

---

## SFTP drops

The SFTP connection signs in with a password or a private key and reads CSV, JSON, Parquet or Excel files that match a
pattern such as `daily/*.csv`.

- **Pin the host key.** Leave *Host key fingerprint* empty, press **Test**, check the `SHA256:...` value with the
  server's owner, and paste it in. From then on Tessera refuses to connect if the server's key changes.
- **Only new files.** Use mode *incremental* with cursor column `_source_modified`. Files that have not changed since the
  last load are not downloaded again. Each row records `_source_file` and `_source_modified`.

---

## Delivering data to other systems

Add a **Deliver** step to a pipeline to push a published dataset out after it is refreshed:

| Destination connection | Result |
|---|---|
| PostgreSQL, SQL Server / Azure SQL / Fabric Warehouse, Snowflake, BigQuery, Databricks SQL, Redshift, MySQL, Oracle, Trino, ClickHouse, SQLite, any SQLAlchemy database | A table (`name`, `schema.name` or `db.schema.name`), replaced or appended |
| Delta Lake (local, S3, ADLS/OneLake, GCS) | A Delta table at a relative folder such as `gold/orders` |
| S3 or Files | A Parquet or CSV file |

Only **published** data can leave, so masking has always been applied. The destination appears in lineage as
`dest:<connection>/<table>`. Use a separate connection with write permission for deliveries; keep source connections
read-only.

```json
{"id": "to_snowflake", "type": "deliver", "depends_on": ["publish"], "dataset": "serving.orders",
 "connection_id": 4, "table": "ANALYTICS.PUBLIC.ORDERS", "mode": "replace"}
```

---

## Microsoft Fabric, Databricks and Snowflake

- **Read from Fabric:** the *SQL Server* connector reaches a Fabric Warehouse or Lakehouse SQL endpoint; the *Delta*
  connector reads OneLake tables at `abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<item>.Lakehouse/Tables`.
- **Write to Fabric:** deliver to a Delta folder in OneLake, or to a Fabric Warehouse through the SQL Server connector.
  Power BI in Fabric can also read Tessera directly through OData.
- **Databricks:** read and deliver through *Databricks SQL*; read Unity Catalog tables as Delta, or through Unity
  Catalog's Iceberg REST endpoint with the Iceberg connector.
- **Snowflake:** read and deliver through the *Snowflake* connector; read Iceberg tables in Snowflake Open Catalog
  through the Iceberg connector.

These driver connectors use each vendor's standard SQLAlchemy driver and are not exercised by Tessera's test suite;
install the extra (`tessera[snowflake]`, `tessera[databricks]`, `tessera[sqlserver]`) on the server.

---

## OpenLineage

**Admin → Integrations → OpenLineage.** Turn it on, give the collector URL (Tessera posts to `<URL>/api/v1/lineage`
unless the URL already ends in `/lineage`), a namespace and an optional API key (sent as a Bearer token, stored
encrypted). **Send test event** checks the connection.

For every pipeline run Tessera sends `START`, then `COMPLETE`, `FAIL` (with the error) or `ABORT`. Completed events
list the run's inputs (connections, uploads, streams, upstream tables) and outputs, with each output's column schema.
Compatible collectors include Marquez, DataHub, OpenMetadata, Atlan, Astronomer and Microsoft Purview (through its
OpenLineage support). If the collector is down, the run still succeeds and its log says the event was not delivered.

---

## dbt

**Admin → Integrations → dbt → Import** `target/manifest.json` from any dbt project (dbt Core or Cloud artifacts).
Tessera matches dbt sources to raw datasets and models to published, then curated, then raw datasets by table name,
and brings over:

- model and column descriptions, owners (`meta.owner`) and tags (plus a `dbt` tag),
- `not_null`, `unique` and `accepted_values` tests as quality rules with the same severity,
- the model dependency graph as lineage.

Importing again updates descriptions and adds only new rules. Models with no matching dataset are listed so you can
load them. Over the API: `curl -F file=@target/manifest.json -H "Authorization: Bearer <engineer token>" https://<tessera>/api/integrations/dbt`.

---

## Orchestrators

Create an API token with the **engineer** role and call:

```bash
curl -X POST https://tessera.example.com/api/pipelines/3/run -H "Authorization: Bearer tsr_..."
# {"run_id": 128}
curl https://tessera.example.com/api/runs/128 -H "Authorization: Bearer tsr_..."
# {"status": "running" | "success" | "failed", "steps": {...}, "log": [...]}
```

**Airflow**

```python
from airflow.providers.http.operators.http import HttpOperator
from airflow.providers.http.sensors.http import HttpSensor

run = HttpOperator(task_id="tessera_orders", http_conn_id="tessera", endpoint="/api/pipelines/3/run", method="POST",
                   headers={"Authorization": "Bearer {{ var.value.tessera_token }}"}, response_filter=lambda r: r.json()["run_id"])
wait = HttpSensor(task_id="tessera_orders_done", http_conn_id="tessera",
                  endpoint="/api/runs/{{ ti.xcom_pull('tessera_orders') }}",
                  headers={"Authorization": "Bearer {{ var.value.tessera_token }}"},
                  response_check=lambda r: r.json()["status"] in ("success", "failed"), poke_interval=15)
run >> wait
```

**Azure Data Factory / Fabric Data pipelines:** a Web activity (POST to `/api/pipelines/<id>/run`) followed by an
Until loop with a Web activity on `/api/runs/<run_id>`. **Dagster, Prefect, GitHub Actions, GitLab CI:** the same two
HTTP calls. Pipelines can also POST a completion event to any webhook (Teams, Slack, PagerDuty).

---

## AI clients over MCP

Claude Desktop, Claude Code, IDE assistants and your own agents connect to `https://<your tessera>/mcp` with an API
token and get governed tools, datacards and prompts with the token owner's permissions. See [MCP.md](MCP.md).

## REST API

Everything the app does goes through a documented REST API. The interactive reference is at `/api/docs` on your
server and the OpenAPI description at `/api/openapi.json`. Authenticate with `Authorization: Bearer <token>`.
