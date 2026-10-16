# Roadmap

What we plan to build next, roughly in order. Vote with a 👍 on the linked issue, or open one if what you need is
missing. Dates are deliberately absent: this is a volunteer project and quality comes first.

## Next

- **Mirroring for SQL Server, MySQL and MongoDB** (change tracking, binlog and change streams), like PostgreSQL today.
- **Changes-only loads for HubSpot, Zendesk and Microsoft Graph** (search API, incremental exports, delta queries).
- **More business applications**: QuickBooks, Xero, NetSuite, Workday, Google Analytics and Google Ads, Marketo.
- **Schema registry payloads**: Avro and Protobuf for Kafka and Event Hubs streams.
- **Google Pub/Sub and Apache Pulsar** streams.
- **Amazon MSK IAM authentication** for Kafka streams.
- **Windowed stream aggregates**: rolling counts and sums over the last N minutes as live tiles.
- **Translations** of the interface (French first, then Spanish and German).

## Later

- **Read replicas for dashboards**: a second, read-only Tessera serving dashboards from a snapshot.
- **Postgres wire protocol endpoint** for tools without OData support.
- **Column-level lineage** from transforms and dbt.
- **Scheduled data exports to SharePoint and Google Drive.**
- **Plugin interface** for third-party connectors.

## Not planned

- **Python notebooks or user code inside the server.** Running arbitrary code would bypass the permission model.
  Use the API or MCP from your own notebook instead.
- **A distributed engine.** Tessera stays a single-server product; for very large data, read from and deliver to a
  distributed warehouse.
