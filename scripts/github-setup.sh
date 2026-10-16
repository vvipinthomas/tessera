#!/usr/bin/env bash
# One-time settings for the public GitHub repository: description, topics, features, labels and starter issues.
# Needs the GitHub CLI signed in with admin rights on the repository:  gh auth login
# Usage: scripts/github-setup.sh [owner/repo]     (default: vvipinthomas/tessera)
set -euo pipefail
REPO="${1:-vvipinthomas/tessera}"

echo "Repository settings for $REPO"
gh repo edit "$REPO" \
  --description "Open-source data platform you run yourself: connectors, streaming, PostgreSQL mirroring, pipelines, data quality, PII masking, semantic models, dashboards, executive briefs and governed AI agents with an MCP server." \
  --homepage "https://vvipinthomas.github.io/tessera/" \
  --enable-issues --enable-discussions --enable-wiki=false \
  --delete-branch-on-merge --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --add-topic data-platform,lakehouse,data-engineering,etl,elt,data-pipeline,data-quality,cdc,change-data-capture \
  --add-topic duckdb,fastapi,python,self-hosted,business-intelligence,dashboards,semantic-layer,data-catalog \
  --add-topic mcp,ai-agents,microsoft-fabric-alternative,open-source

echo "Private vulnerability reporting (SECURITY.md points to it)"
gh api -X PUT "repos/$REPO/private-vulnerability-reporting" >/dev/null || echo "  could not enable; turn it on in Settings → Code security"

echo "Docs site on GitHub Pages (built by .github/workflows/docs.yml into gh-pages)"
gh api -X POST "repos/$REPO/pages" -f "source[branch]=gh-pages" -f "source[path]=/" >/dev/null 2>&1 \
  || echo "  Pages not enabled yet: it needs the gh-pages branch, which the Docs workflow creates on its first run"

echo "Labels"
label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null; }
label "good first issue" "7057ff" "A small, well-described change for a first contribution"
label "help wanted" "008672" "Extra attention is welcome"
label "connector" "0C6B5B" "Sources, destinations and application templates"
label "streaming" "3150C9" "Streams and mirroring"
label "docs" "C68A12" "Documentation"
label "ui" "BE3A2B" "The web app"
label "security" "B60205" "Security hardening (report vulnerabilities privately, see SECURITY.md)"

echo "Starter issues"
issue() {  # title, labels, body; skipped when an issue with the same title exists
  if gh issue list --repo "$REPO" --state all --search "in:title \"$1\"" --json title -q '.[].title' | grep -qxF "$1"; then
    echo "  exists: $1"
  else
    gh issue create --repo "$REPO" --title "$1" --label "$2" --body "$3" >/dev/null && echo "  created: $1"
  fi
}
issue "Zendesk: load tickets incrementally with the incremental export API" "good first issue,connector" \
"Zendesk tickets are copied in full each run. The [incremental export API](https://developer.zendesk.com/api-reference/ticketing/ticket-management/incremental_exports/) returns only tickets changed since a time.

**Where:** \`tessera/saas.py\`, the \`zendesk\` template. See how Salesforce uses \`incremental\` with \`since_params\`, \`since_format\` and \`key\`, and \`format_since\` in \`tessera/connectors.py\` (add a \`unix\` format).
**Test:** add a case to \`test_change_since_filters\` in \`tests/test_saas.py\`.
**Done when:** later runs send \`start_time\` and merge on \`id\`."

issue "HubSpot: changes-only loads through the CRM search API" "help wanted,connector" \
"HubSpot objects are copied in full. The CRM search endpoint (\`POST /crm/v3/objects/{object}/search\`) can filter on \`hs_lastmodifieddate\`. The REST connector only sends GET requests today, so this needs a small POST-with-JSON-body option in \`RestConnector.read\` plus the template change. Please describe the approach in this issue before starting."

issue "Recognise more change-column names in Copy many tables" "good first issue,connector" \
"\`CHANGE_COLUMNS\` in \`tessera/connections_api.py\` lists the names that mark a row's last change (\`updated_at\`, \`modified\`, \`SystemModstamp\`...). Add common ones we miss, for example \`update_time\`, \`last_update\`, \`dt_modified\`, \`mod_date\`, \`etl_updated_at\`, with a test in \`tests/test_bulk_copy.py::test_detection_rules\`."

issue "QuickBooks Online application template" "help wanted,connector" \
"Add a \`quickbooks\` template to \`tessera/saas.py\`: OAuth 2 refresh-token sign-in, the query endpoint with \`STARTPOSITION\`/\`MAXRESULTS\` paging, and objects Customer, Invoice, Payment, Bill, Vendor, Item, Account. Changes-only loads can use \`MetaData.LastUpdatedTime\`. The template test in \`tests/test_saas.py\` runs automatically for every template."

issue "French translation of the interface" "help wanted,ui" \
"First step toward translations (see ROADMAP.md): extract the visible strings in \`tessera/static/*.js\` into a message table and add \`fr\`. Please comment with a proposed structure before a large pull request."

echo "Done. Remaining by hand: Settings → Actions → General → allow GitHub Actions to create releases (already allowed by default),"
echo "and after the first release, Packages → tessera → Package settings → Change visibility → Public if it is private."
