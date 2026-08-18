# byteforge-aegis-mcp

A **read-only** MCP server over the ByteForge Aegis admin API, deployed as a
container behind nginx with Gatekeeper auth.

## Why

Agents working on Aegis and on tenant integrations repeatedly need to answer
"what is actually configured in production?" — is the webhook URL set, is
`allow_self_registration` on, which sites exist, does this user exist. Before
this server that question was answered with hand-rolled curls carrying the
master API key, or not answered at all — a tenant provisioning request was
once signed off partly on assertion, because the site config could not be
read back.

## Read-only by construction

Every tool maps to a `GET`. The Aegis client this wraps also exposes
`create_site`, `update_site`, `delete_site`, `delete_user` and friends — none
are reachable here, and none should be added. The value of this server is
that it can be handed to an agent with no possibility of changing production
state. A mutating server, if ever wanted, belongs in a separate and
separately-authorized deployment.

## Tools

| Tool | What it answers |
|---|---|
| `aegis_health` | Which build is live (`status`, `service`, `version`) |
| `aegis_list_sites` | Every tenant on the instance, with secrets |
| `aegis_get_site` | One site's full config, by **UUID or domain** |
| `aegis_list_users` | All users on a site |
| `aegis_find_user` | One user on a site, by email (case-insensitive) |

`aegis_get_site` and `aegis_list_users` accept a domain as well as a UUID.
The admin API addresses sites by UUID only (`utils/identifiers.py`
`resolve_site` rejects non-UUIDs), so a domain is resolved through the public
`by-domain` lookup first — callers almost always know the domain, not the
UUID.

## Secrets in responses

Site reads include `tenant_api_key`, `webhook_secret` and `mailgun_api_key`
in full. This was a deliberate choice by @jmazzahacks over returning presence
booleans. The consequence: anything read here lands in the calling agent's
transcript, so **responses should not be pasted into tickets or other shared
surfaces**. `AEGIS_MASTER_API_KEY` spans every site on the instance.

## Configuration

| Variable | Purpose |
|---|---|
| `AEGIS_API_URL` | Aegis instance to read (e.g. `https://aegis.example.com`) |
| `AEGIS_MASTER_API_KEY` | Master key. Spans every site |
| `MCP_TRANSPORT` | `stdio` for local dev, `streamable-http` in Docker |
| `FASTMCP_HOST` / `FASTMCP_PORT` | Bind address. FastMCP reads these specifically |

See `example.env`.

## Local development

```sh
uv venv --python 3.13 .venv
source .venv/bin/activate
uv pip install -r requirements.txt

AEGIS_API_URL=https://aegis.example.com \
AEGIS_MASTER_API_KEY=... \
MCP_TRANSPORT=stdio \
python aegis_mcp_server.py
```

The venv lives in `.venv/` rather than at the repo root as the sibling Aegis
repos do — `uv venv` refuses to create one in a non-empty directory.

To exercise it over the wire the way it is deployed:

```sh
MCP_TRANSPORT=streamable-http FASTMCP_HOST=127.0.0.1 FASTMCP_PORT=8931 \
AEGIS_API_URL=... AEGIS_MASTER_API_KEY=... python aegis_mcp_server.py &

curl -s -X POST http://127.0.0.1:8931/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A `tools/list` that succeeds **without** a preceding `initialize` is the
signal that stateless mode is on.

## Build and publish

```sh
./build-publish.sh            # or --no-cache
```

Publishes `ghcr.io/jmazzahacks/byteforge-aegis-mcp:<n>` and `:latest`, then
advances `VERSION`. `VERSION` is gitignored and owned entirely by the script —
never edit it by hand.

Unlike `byteforge-aegis`, this image does **not** bake `VERSION` into itself,
so the script's write-after-build ordering is correct here. If a version
endpoint is ever added, the write must move to *before* `docker build` or
every image will report one version behind.

## Deployment

Runs behind an `mcp.<domain>` umbrella vhost with Gatekeeper
`auth_request` auth. See `nginx-mcp-aegis.conf` for the location block. The
container publishes no host port — nginx reaches it by container name on the
shared docker network, which matters because the master key makes direct
exposure unacceptable.

## Transport

`streamable-http` with `stateless_http=True`. Not SSE: when Claude Code's
long-lived SSE GET dies it reconnects without re-running `initialize`, the
server sees `tools/call` first, and the resulting `-32602` wedges the client
until a manual `/mcp` reload. Stateless streamable-http has no per-session
state to lose, so that failure is structurally impossible.
