# Spanning Backup MCP Server

An [MCP](https://modelcontextprotocol.io) server for the
[Spanning Backup for Microsoft 365](https://spanning.com) API. Spanning backs up a Microsoft 365
tenant's mailboxes, OneDrive, calendars, contacts, SharePoint sites and Teams channels every
day. This server lets an AI agent read which users are licensed for backup, how each day's
backup went per user and per service, and the tenant's license usage. It is read-only.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Spanning-API-Token` | Yes | string | none | - | Spanning API token | `your-api-token` |
| `X-Spanning-Admin-Email` | Yes | string | none | email | Email of the Spanning administrator the token belongs to | `admin@example.com` |
| `X-Spanning-Platform` | No | string | `m365` | `m365` (also `o365`, `office365`); `gws` (`gapps`) and `salesforce` are recognised but not supported | Which Spanning product the account is | `o365` |
| `X-Spanning-Region` | No | string | detected | `us`, `eu`, `uk`, `ca`, `ap`, `af` | Data region of the Spanning account | `us` |

Where to get them:

- A Spanning administrator generates an API token in the Spanning Backup for Microsoft 365
  admin console (its settings area). The token is tied to the administrator who created it,
  so send that administrator's email with it.
- The region is the one the account was created in. When it is not sent, the server tries
  each regional API once and remembers which one accepts the credentials (only the region code
  is kept, keyed by a hash of the email and token).

How credentials are sent to Spanning: HTTP Basic authentication (admin email as user name,
API token as password) to `https://o365-api-{region}.spanningbackup.com/external/...`.

A request to `/mcp` without the two required headers gets HTTP 401 naming the missing ones; an
invalid email, platform or region gets HTTP 400.

## Tools

All tools return a JSON string and are read-only. Users are identified by their Microsoft 365
user principal name. Backup results are per day and per service type and count objects as
`successful`, `partial` and `failed`; each row also carries an `outcome` word.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `spanning_status` | Check the connection: credentials work, region, platform, tenant state | - |
| `spanning_get_license_usage` | Purchased standard / archive licenses vs assigned users and shared mailboxes | - |
| `spanning_list_users` | Tenant users and whether each is licensed (assigned) for backup | limit (default 50, max 200; larger values are capped), assignedOnly, includeBackupSummary, pageToken |
| `spanning_get_user` | One user's license state and today's backup results | **userId** |
| `spanning_list_services` | Services backed up for a user with the latest result of each (last 7 days) | **userId** |
| `spanning_list_backups` | A user's daily backup results for one service, newest first | **userId**, **service** (`mail`, `drive`, `calendar`, `contacts`, `sites`, `teams`), days (default 7, max 30: the API limit) |
| `spanning_queue_restore` | Explains how to restore; the API cannot start a restore, so nothing is queued | **userId**, **service**, **items** |
| `spanning_get_restore_status` | Explains that restore jobs are not exposed by the API | **restoreId** |
| `spanning_list_audit_log` | Explains that the admin audit log is not exposed by the API | since, until |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Spanning returned HTTP 401 for tenant", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Every call is a read, so 429, 5xx and network errors are
retried with backoff.

## Run locally

```bash
uv sync
uv run python -m spanning_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t spanning-mcp:dev .
docker run --rm -p 8080:8080 spanning-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Spanning-API-Token: your-api-token" \
  -H "X-Spanning-Admin-Email: admin@example.com" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# a user's mail backups for the last 14 days
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Spanning-API-Token: your-api-token" \
  -H "X-Spanning-Admin-Email: admin@example.com" \
  -H "X-Spanning-Region: us" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"spanning_list_backups","arguments":{"userId":"jane@example.com","service":"mail","days":14}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (9).** Kept as is because the tool names are an existing contract.
- **What the API offers.** Spanning's published API for Microsoft 365
  (`https://o365-docs.spanningbackup.com/swagger/json`) covers the tenant, users with their
  daily backup summaries, and license assignment. Services and backup history are derived from
  those summaries. It has no restore, restore-status or audit-log operations, so those three
  tools change nothing and explain where to do it (the Spanning web app).
- **Regions.** The API is served per data region; the region is detected when not given.
- **Platforms.** Only Microsoft 365 accounts are supported; Google Workspace and Salesforce
  connections get a clear error instead of failing calls.
- **Read-only.** License assignment exists in the API but is not exposed as a tool.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
