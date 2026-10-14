# Tessera user guide

This guide walks through Tessera screen by screen. It is organised by what you want to get done, so you can jump to
the part you need. If your Tessera has demo data loaded, every example below works on it as you read.

**Contents**

1. [Getting started](#1-getting-started)
2. [Finding your way around](#2-finding-your-way-around)
3. [For everyone: dashboards, questions and briefs](#3-for-everyone-dashboards-questions-and-briefs)
4. [For analysts: explore, reports, SQL, notebooks, alerts](#4-for-analysts-explore-reports-sql-notebooks-alerts)
5. [For engineers: connections, streams, pipelines, quality](#5-for-engineers-connections-streams-pipelines-quality)
6. [Semantic models, catalog and glossary](#6-semantic-models-catalog-and-glossary)
7. [AI agents and AI clients](#7-ai-agents-and-ai-clients)
8. [Using Tessera from Power BI, Excel and Tableau](#8-using-tessera-from-power-bi-excel-and-tableau)
9. [For admins](#9-for-admins)
10. [Troubleshooting and FAQ](#10-troubleshooting-and-faq)

---

## 1. Getting started

### First sign-in (the person who installs Tessera)

1. Open Tessera in a browser. On a fresh install you see **Set up Tessera**.
2. Enter the **setup code**. It is printed in the server console and saved as `setup-code.txt` in the data folder
   (Docker: `docker compose logs tessera | grep "Setup code"`).
3. Enter your name, work email and a password of at least 12 characters. You become the first admin.
4. Leave **Start in demo mode** ticked if you want to learn with sample data. Tessera loads a retail workspace (orders,
   customers, a certified Sales model, a dashboard, an alert, a notebook and a live checkout stream) and starts a
   guided tour.

### Demo mode and the tour

While demo data is loaded, the top bar shows **Demo data**, **Take the tour** and **Switch to real data**.

- **Take the tour** walks through every main screen (about 25 short steps). Press **Next** or **Back**; close it
  with ✕ at any time.
- **Switch to real data** shows exactly what will be removed, including anything you built on demo data, then
  deletes it. Users, roles, SSO, tokens and settings stay. An admin can load the demo again from the same place.

### Signing in later

Use your email and password, or **Sign in with single sign-on** if your admin has set it up. Five wrong passwords
lock the account for a while; an admin can unlock it.

### Roles in one line each

| Role | You can |
|---|---|
| Viewer | Open dashboards, reports and briefs, ask questions, read the catalog |
| Analyst | Also explore data, write SQL on published data, build reports and dashboards, set alerts, use AI agents |
| Engineer | Also connect sources, run streams and pipelines, write quality rules, publish data |
| Admin | Also manage users, SSO, email, AI providers, row-level security, integrations and backups |

If a menu item or button is missing, your role does not include it.

---

## 2. Finding your way around

- **The left rail** groups screens: **Analyze** (Dashboards, Explore, Reports, Notebooks, SQL, AI agents), **Data**
  (Catalog, Semantic models, Glossary, Connections, Streams, Pipelines, Transforms) and **Operate** (Monitor, Alerts,
  Workspaces, Admin). The current screen is marked with a small green tile.
- **Search** (top bar, or **Ctrl K** / **⌘ K**) finds datasets, measures, dashboards, reports and glossary terms.
  Type a question and pick **Ask:** to get a chart straight away.
- **The bell** shows notifications: alerts that fired, failed pipeline runs, shared items.
- **Home** shows data health at a glance: dataset count, quality rules, published datasets, AI readiness per dataset
  and pipeline runs over the last two weeks.
- Tessera follows your system's light or dark setting and works on phones (the rail becomes a top bar).

---

## 3. For everyone: dashboards, questions and briefs

### Reading a dashboard

Open **Dashboards** and pick one.

- **Filters** across the top (date range with presets, drop-downs with multi-select) apply to every tile.
- **Click a bar or slice** to filter the whole page by it (cross-filtering). The active cross-filter appears as a chip;
  click its ✕ to clear it, or use **Reset filters**.
- **Change the time grain** of a line chart (day, week, month, quarter, year) with the selector on the tile.
- **Tile menu (⋯)**: **View data**, **Explain change** and **Deep dive** (on tiles built from a semantic model).
- **Views ▾** saves the current filters as a named view and switches between saved views.
- **Refresh** reloads now. **Live** refreshes every 5 seconds while the tab is visible, for dashboards on streaming
  data; it remembers your choice for that dashboard in this browser.
- **Excel** exports every tile with the current filters. **Print / PDF** prints a clean page. **TV mode** goes full
  screen and refreshes every minute.

### Insights and Ask AI

- **✦ Insights** opens a side panel of automated facts about the measures on the page: the latest change and what
  drove it, the trend, unusual points, a forecast and how concentrated the result is. Each fact says how it was worked
  out. **Summarise with AI** writes a short summary using only those facts (needs an AI provider).
- **Ask AI** answers questions about the dashboard in a chat, using the same governed measures. Every number in an
  answer is checked against the data; a badge shows whether all figures were verified.
- **Explain change** (tile menu) splits a change between two periods across every dimension and ranks what moved it
  most. **Deep dive** opens a decomposition tree: start from the total and split by any dimension, or let Tessera pick
  the split that stands out.

### The executive brief

Press **Executive brief** on any dashboard. For each measure you see:

- the latest period against the previous one and the same period last year, year to date, and momentum (whether
  year-over-year growth is speeding up or slowing down),
- the **full-year forecast** with its likely range, and the **chance of hitting the target** (admins and analysts set
  targets with **Targets**),
- a **what-if slider**: drag it to see where the year lands if the rest of the year runs higher or lower,
- the drivers behind the change and the segments trending up or down.

The headline list at the top ranks what leadership should know first. **Monthly / Quarterly** switches the view.
**Copy summary** copies the headlines as text; **Email weekly** subscribes people to the brief by email; **Print /
PDF** makes a handout. Forecasts state their method (for example seasonal Holt-Winters) and how accurate it was on
recent history.

### Asking questions in plain English

In **Explore** (or the search bar), type a question such as:

- `top 5 provinces by revenue last quarter`
- `monthly orders in BC since 2026-03`
- `refund rate by channel excluding store this year`

Tessera shows **Understood as:** with the measure, breakdown, filters and time range it used, and lists any words it
did not understand. It uses certified definitions and glossary synonyms (so `sales` finds Revenue if the glossary says
so). No AI provider is needed.

### Shared links

If someone sends you a Tessera share link, it opens a read-only copy of a dashboard without signing in. Links expire,
can be revoked, and may be locked to filters (for example only BC).

---

## 4. For analysts: explore, reports, SQL, notebooks, alerts

### Explore and reports

1. **Explore** → pick a **model** (for example Sales).
2. Tick **measures** (Revenue, Orders) and **dimensions** (Province, Order date). Choose a time grain next to date
   dimensions.
3. Add **filters** with **+ Add filter**.
4. Pick a **visual**: Auto, KPI, Bar, Line, Area, Lines, Combo, Donut, Treemap, Stacked, Waterfall, Funnel, Scatter,
   Pivot or Table. Options include **Compare with previous period**, **Forecast**, **Flag anomalies** and a **Target**
   line.
5. **Save as report**, **Add to dashboard**, **Create alert**, or export to **Excel** / **CSV**. **Generated SQL**
   shows the exact query.

**Reports** lists saved reports; open one to change it, export it or add it to a dashboard.

### Building a dashboard

1. **Dashboards → New dashboard**, then **Edit**.
2. **Add tile** (a saved report or a text tile) and **Add filter** (a date range or a drop-down on a model field).
3. Drag tiles to reorder; drag a tile's right edge to resize it on a 12-column grid; choose short, medium or tall in each tile's height selector.
4. Press **Done**. **Share** creates a read-only link or embed code if your admin has turned sharing on.
5. **Subscribe** emails the dashboard on a schedule, with an Excel or CSV attachment and optionally the executive
   brief at the top.

### SQL

**SQL** runs read-only queries on published data (`serving.*`; engineers can also read `raw` and `curated`). The left
panel lists tables and columns; click a column to insert it. **Run (Ctrl+Enter)**, then pick a chart type to visualise
the result and **Save as chart** to reuse it. Only one `SELECT` runs at a time, and queries stop after 60 seconds.

### Notebooks

**Notebooks → New notebook** mixes Markdown and SQL cells, each SQL cell with its own chart. **Run all** refreshes
every cell. Notebooks are visible to everyone who can see their workspace.

### Alerts and subscriptions

**Alerts → New alert** watches a model measure, a report value, a dataset's quality, data freshness or pipeline
failures, on a schedule. Choose the condition (for example *Refund rate above 8%*) and where to send it: in the app,
by email or to a webhook (Teams, Slack). **Check now** tests it immediately. Alerts and subscriptions run with your
permissions, including row-level security.

---

## 5. For engineers: connections, streams, pipelines, quality

### Connecting a source

1. **Connections → New connection**, choose the connector, name it, fill in the fields. Use a read-only account.
   Passwords and keys are encrypted and never shown again.
2. **Save and test**. **Browse** lists tables, files, topics or collections.
3. On the connection page, **Load data**: choose the table or path, the target table (for example `raw.orders`) and a
   mode:
   - **replace**: reload everything,
   - **append**: add rows,
   - **incremental**: add only rows whose **cursor column** (for example `updated_at`) is newer than last time.
   For SFTP, use the cursor column `_source_modified` to load only new or changed files. For Iceberg, add a row filter
   or a snapshot id; for Delta, a version.
4. **Upload file** on the Catalog page loads a CSV, Excel, JSON or Parquet file directly.

The connector catalog at the bottom of **Connections** shows which connectors are *verified* by Tessera's tests and
which need a driver installed on the server.

### Streams

**Streams** shows each stream's status, events per minute, rows landed and lag. To add one:

1. Create a Kafka, Azure Event Hubs, Kinesis or MQTT **connection** first (HTTP push needs none).
2. **Streams → New stream**, choose the source kind, the connection, the topic or stream name, and the table to land in
   (for example `raw.orders_live`).
3. Choose the payload (JSON, or text stored in a `value` column) and where to start (**From the beginning** or
   **Only new events**).
4. Optional, under **Batching, de-duplication and retention**: batch size and wait, a **de-duplicate on** column such
   as `event_id`, and how many days to keep.
5. **Create and start**. For HTTP push, copy the endpoint and token from the dialog; it is shown once.

On a stream's page: **Stop** / **Start**, **Preview** (reads a few events without moving the position; stop the stream
first), **Reset position** (earliest or latest), **Rotate token** (HTTP push), **Query latest rows**. Events that are
not valid JSON go to `<table>__rejects` with the reason. If the source is unreachable the status becomes *retrying*
with the error shown, and the stream recovers on its own.

### Pipelines

**Pipelines → New pipeline** builds a graph of steps. Each step has an id, the steps it runs after, and retries.

| Step | Does |
|---|---|
| Ingest | Loads from a connection into `raw` (or `curated`) |
| Transform | Runs a `SELECT` into a `curated` table |
| Quality | Runs the dataset's rules; fails the run on error rules, any failing rule, or never |
| Scan for personal data | Detects personal data and proposes masking |
| Publish | Writes the masked copy to `serving` |
| Deliver | Writes a published dataset to a database, warehouse, Delta lake, S3 or folder |
| Export | Writes Parquet or CSV with a datacard |
| Webhook | Calls a URL |
| Trigger another pipeline | Chains pipelines |

Set a **schedule** in cron form (`0 6 * * *` is 06:00 daily in the server's time zone) and an optional completion
webhook. **Run now** starts a run; the run page shows the graph coloured by status and a live log. **Edit as JSON**
shows the whole definition, useful for copying between environments.

### Visual transforms

**Transforms** prepares data by pointing and clicking: select, drop, rename, filter, remove duplicates, cast, calculated
columns, fill empty values, trim and change case, find and replace, join, aggregate, sort, limit. The preview updates as
you go and the generated SQL is shown for review. **Run into table** runs it once; **Add to a pipeline** adds it as a
transform step.

### Data quality

On a dataset in the **Catalog**, open **Quality**:

- **Suggest rules** proposes rules from the data (columns that are never empty, unique keys, value ranges, allowed
  values); **Accept all** or pick the ones you want.
- **Add rule** for any of nine kinds: not null, unique, range, pattern, allowed values, not in the future, freshness,
  row count and custom SQL. Each rule is an *error* or a *warning*.
- **Run checks** runs them now and shows failing examples. Scores and history appear on the dataset and on Home.

### Personal data and publishing

On a dataset, **Columns** shows each column's sensitivity and masking policy. **Scan for personal data** detects
emails, phone numbers, Canadian SINs, card numbers (Luhn-checked), US SSNs, IBANs, IP addresses, postal codes, names,
birth dates and addresses. Adjust the policy per column (redact, keyed hash, last 4, first 3, year only, email domain,
empty), then **Publish masked copy**. Only the masked copy in `serving` is visible to analysts, viewers, BI tools and AI.

---

## 6. Semantic models, catalog and glossary

### Semantic models

A model defines business measures once, so every report agrees.

1. **Semantic models → New model**, pick a published table (Tessera can suggest a starting model from it).
2. Add **measures** (sum, average, count, distinct count or a custom expression), with a format (currency, number,
   percent). Set **Better when** (higher or lower) so changes are coloured correctly, and **Sums over time** for
   measures that add up across periods (revenue does; a rate does not).
3. Add **dimensions** (categories, numbers, dates with grains) and optional joins to lookup tables.
4. **Save model**. Engineers can **Promote** it and admins can **Certify** it; certified models are marked with a tick everywhere.

### Catalog

Each dataset has tabs: **Readiness** (an AI-readiness score with the fixes that would raise it), **Columns**,
**Preview**, **Profile** (statistics per column), **Quality** and **Lineage** (where it comes from and what depends
on it). **Endorse / domain** marks datasets as promoted or certified and groups them by domain. Owners and
descriptions can be written by hand, by the AI steward agent, or imported from dbt.

### Glossary

**Glossary → Add term** records a business definition, its synonyms and the columns it refers to. Synonyms are used by
plain-English questions and by AI agents.

---

## 7. AI agents and AI clients

**AI agents** (analyst role) offers several assistants:

| Agent | Good for |
|---|---|
| Data Analyst | Questions about the numbers, using certified measures first |
| Data Incident Responder | "What broke last night and what does it affect?" |
| Semantic Modeler | Drafting a model from a table and the glossary |
| Quality Steward | Suggesting and applying quality rules, writing descriptions |
| Pipeline Engineer | Explaining and fixing pipeline runs |
| AI Readiness Advisor | Raising a dataset's readiness score |

Agents work within your permissions, show each tool they used, and flag any number that did not come from a query.
Personal data is removed before anything is sent to the AI provider. An admin chooses the provider (Anthropic,
OpenAI, Azure OpenAI, Ollama on your own machine, or any OpenAI-compatible endpoint).

To use Tessera from Claude Desktop, Claude Code or an IDE, create an API token and follow [MCP.md](MCP.md).

---

## 8. Using Tessera from Power BI, Excel and Tableau

1. Create an API token: **Admin → API tokens** (or **API tokens** if you are not an admin), role *viewer* or
   *analyst*.
2. Copy the feed URL from **Admin → Integrations**, or use `https://<your tessera>/odata`.
3. In **Power BI Desktop**: Get data → OData feed → URL → **Basic**, any user name, the token as the password.
   In **Excel**: Data → Get Data → From Other Sources → From OData Feed. In **Tableau**: Connect → OData.
4. Pick published datasets or saved reports. You see exactly the rows Tessera would show you, masked.

More detail, including scheduled refresh, is in [INTEGRATIONS.md](INTEGRATIONS.md#power-bi-excel-and-tableau).

---

## 9. For admins

| Task | Where |
|---|---|
| Add people, change roles, reset passwords, unlock accounts | Admin → Users |
| Single sign-on (Entra ID, Okta, Google, Keycloak, Auth0) with group-to-role mapping | Admin → Single sign-on |
| Automatic user provisioning (SCIM 2.0) | Admin → Provisioning |
| Row-level security (for example region managers see only their province) | Admin → Row-level security |
| Email server for alerts and subscriptions | Admin → Email |
| AI providers and whether personal data may reach them | Admin → AI providers |
| MCP server on or off, and whether AI clients may change things | Admin → MCP server |
| Power BI/Excel feed, OpenLineage, dbt import | Admin → Integrations |
| Sharing and embedding, backups, audit retention, sessions | Admin → Operations |
| Move definitions between environments | Admin → Deployment (**Download bundle** / **Import**) |
| Everything that happened, filterable and exportable | Admin → Audit log |
| Teams with their own content and roles | Workspaces |

**Row-level security** in practice: create a policy on `serving.orders` with column `province` and attribute `region`,
then set each person's attribute under **User attributes** (for example `BC, AB`, or `*` for everything). People with no
value see no rows; engineers and admins are not filtered.

**Backups**: **Admin → Operations → Download backup** at any time, or schedule backups as described in
[DEPLOY.md](DEPLOY.md#backups). Keep the master key safe; without it stored connection passwords cannot be read.

---

## 10. Troubleshooting and FAQ

**I lost the setup code.** It is in `setup-code.txt` in the data folder until setup completes. If setup already
happened, create an admin from the server: `tessera create-user you@example.com --role admin`.

**I forgot my password.** Ask an admin (Admin → Users → Set password), or on the server run
`tessera reset-password you@example.com`, which also unlocks the account.

**A dashboard tile shows an error.** Open the tile menu → **View data** to see the query's message. Often a published
dataset was renamed or a model field removed; edit the report to fix it.

**Numbers differ between people.** Row-level security is probably filtering one of them. Admins can check the person's
attributes under Admin → Row-level security.

**"This needs the analyst role or higher."** Your role does not include that screen; ask an admin.

**A stream says retrying.** The error is on the stream page. Common causes: wrong topic name, wrong credentials,
the broker not reachable from the Tessera server, or `TESSERA_BLOCK_PRIVATE_HTTP=true` blocking a private address.

**Power BI says the credentials are invalid.** Use **Basic** authentication with the API token as the password, not
your Tessera password. Check that the token was not revoked and that its role is at least viewer.

**A pipeline step failed.** Open the run: the log shows the step's error and each retry. Quality-gate failures list
the rules that failed. Fix the data or the rule, then **Run now**.

**Is my data sent anywhere?** Only to the places you configure: AI providers (with personal data removed unless an
admin allows it), email, webhooks, OpenLineage and deliveries. Tessera has no telemetry.

**How big can it get?** One server handles team and department workloads up to hundreds of gigabytes. Very large or
highly concurrent workloads are better served by a distributed warehouse, which Tessera can read from and deliver to.
