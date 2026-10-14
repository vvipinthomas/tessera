# Tessera MCP server

Tessera includes a [Model Context Protocol](https://modelcontextprotocol.io) server, so Claude Desktop, Claude Code, IDE assistants and your own agents can work with governed data without copying it anywhere.

## Connect

1. Create an API token under **Admin → API tokens** (or **API tokens** for non-admins). Pick the lowest role that does the job: a viewer token can search the catalog and glossary; an analyst token can also query semantic models and run read-only SQL on published data; an engineer token adds pipeline runs, run logs and raw/curated data.
2. Connect a client.

**Claude Code (Streamable HTTP)**

```bash
claude mcp add --transport http tessera https://tessera.example.com/mcp --header "Authorization: Bearer tsr_..."
```

**Claude Desktop and other stdio clients** (Tessera must be installed on that machine; it only relays messages):

```json
{
  "mcpServers": {
    "tessera": {
      "command": "tessera",
      "args": ["mcp", "--url", "https://tessera.example.com"],
      "env": { "TESSERA_TOKEN": "tsr_..." }
    }
  }
}
```

## What it exposes

| Kind | Items |
|---|---|
| Tools (read-only) | `search_catalog`, `glossary`, `list_datasets`, `describe_dataset`, `get_datacard`, `lineage_impact`, `data_health`, `profile_dataset`, `suggest_rules`, `readiness_report`, `run_sql`, `list_models`, `query_model`, `executive_brief`, `find_insights`, `explain_change`, `decompose`, `propose_chart`, `suggest_model`, `recent_runs`, `run_details`, `list_connections`, `draft_pipeline` |
| Tools (write, off by default) | `create_rule`, `run_quality`, `update_description` |
| Resources | `tessera://catalog`, `tessera://glossary`, `tessera://dataset/{schema}.{table}` (datacard), `tessera://model/{id}` |
| Prompts | `leadership-update`, `explain-metric-change`, `triage-data-incident`, `document-dataset` |

Tools are filtered by the token's role; tools the token cannot use are neither listed nor callable. Tool annotations mark read-only tools so clients can skip confirmation for them.

## Guarantees

- **Same permissions as the web app.** Calls run as the token's user with the token's role: row-level security, the serving-only rule for viewers and analysts, and workspace visibility all apply.
- **PII firewall.** Values that look like emails, SINs, SSNs, card numbers, phones or IPs, and columns classified as personal data, are redacted before results leave Tessera, unless an admin has explicitly turned that off.
- **Writes are opt-in.** An admin enables them under **Admin → MCP server**, and only engineer tokens get them.
- **Audited.** Every call is recorded as `mcp.tool` with timing and arguments.
- **Protected.** Bearer token required; browser `Origin` headers from other sites are rejected (DNS-rebinding protection); per-caller rate limits apply.

## Protocol notes

Protocol versions 2025-06-18, 2025-03-26 and 2024-11-05. Stateless JSON responses on `POST /mcp` (no server-sent event stream, no sessions); `GET /mcp` returns 405 as the specification allows. Batches are accepted for older clients.
