# Security

## Supported versions

| Version | Security fixes |
|---|---|
| 1.6.x | Yes |
| 1.5.x | Until 1.7.0 is released |
| Older | No; please upgrade |

## Reporting a vulnerability

Please report privately through GitHub's **Report a vulnerability** button on the Security tab of
[the repository](https://github.com/vvipinthomas/tessera/security/advisories/new), with steps to reproduce. Do not open a public issue. You will get an acknowledgement within
five working days and a fix or mitigation plan within 30 days for confirmed issues. Reporters are credited in the
release notes unless they prefer otherwise.

## Design summary

- Passwords: scrypt; lockout after repeated failures. Sessions: HttpOnly, SameSite cookies (Strict; Lax only after an SSO redirect) plus a CSRF header. API tokens are stored as hashes and carry a role no higher than their owner.
- Secrets (connection passwords, provider keys) are encrypted with AES-GCM using `master.key` and are never returned by the API.
- SQL from people and agents is parsed; only single read-only statements run, against a DuckDB instance with external file and network access disabled. Viewers and analysts can only read the published (masked) zone. Row-level security rewrites queries per user.
- AI agents and MCP clients act with the caller's permissions. Personal data is redacted before it reaches a model provider unless an admin turns that off. Every tool call is audited.
- Content Security Policy `script-src 'self'`; no third-party scripts in the app. Pages cannot be framed, except share links on sites an admin allow-lists.
- Share links (off by default) are random 32-byte tokens stored hashed, expire, can be revoked, run with the creator's access narrowed by locked filters, and stop working when the creator is deactivated or loses access. SQL-based tiles are withheld from filter-locked links because a locked filter cannot be proven to apply to them.
- MCP clients and API tokens never get more than their owner's role; write tools over MCP are off by default.
- The OData feed accepts API tokens only (as a Bearer token or as the Basic-auth password); account passwords are never accepted there. It serves published data only, with masking and row-level security.
- HTTP push streams use their own random token per stream, stored hashed and rotatable. Stream, SFTP, Iceberg REST, OpenLineage and REST connections are checked against cloud metadata and link-local addresses, and against private networks when `TESSERA_BLOCK_PRIVATE_HTTP=true`.
- SFTP connections can pin the server's host key and refuse to connect when it changes.
- The Deliver step only sends published (masked) data out of Tessera.
- The web app loads no third-party resources: fonts and scripts are bundled.

## How this release was checked

- An authorization matrix test calls every route without credentials and as a viewer; only the deliberately public routes respond.
- `ruff` lint, CodeQL (Python and JavaScript) on every pull request.
- `bandit` static analysis (configuration in `pyproject.toml`): no findings beyond B608 (SQL built from quoted identifiers or code constants with bound values, reviewed), B110 and B311 (random data for the demo only).
- `pip-audit` on the declared dependencies: no known vulnerabilities at release time.
- Tests for SQL injection and file and network escape through the query guard, row-level security bypass attempts, CSRF, lockout, PII redaction toward AI providers, SCIM deprovisioning, and backup tamper detection.

## Known limits

No independent penetration test has been done. Tessera is a single-node application; put it behind HTTPS (`TESSERA_SECURE_COOKIES=true`) and your normal network controls.
