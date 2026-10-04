# Omnissa Horizon MCP Server (Python / FastMCP)

A [Model Context Protocol](https://modelcontextprotocol.io) server exposing
support/helpdesk operations for an **Omnissa Horizon Server 2506** environment,
built with Python `FastMCP`. It targets the Horizon REST API documented at
https://developer.omnissa.com/horizon-apis/horizon-server/versions/2506/.

> Works both as a direct `python -m` process **and** as a Docker container
> (see "Running via Docker" below).

## Features (helpdesk / support)

**User & session management**
- `find_user_session` / `list_sessions` — find a user's desktop/session(s)
- `restart_user_desktop` / `restart_session` — reboot a user's desktop
- `logoff_user` / `logoff_session` — log a user off (graceful or forced)
- `disconnect_session` — detach the display without logging off
- `send_session_message` — popup a message to a user's session

**Utilization (processes / CPU / memory / network)**
- `user_processes` / `session_processes` — running processes + CPU/mem/disk %
- `user_cpu_memory` / `session_utilization` — CPU, memory, disk & latency stats
- `session_network_performance` — estimated bandwidth (kbps), round-trip latency
  and packet loss (PCoIP/BLAST) to diagnose slow/thin sessions
- `end_session_process` — terminate a runaway process

**Pool & environment health**
- `list_desktop_pools` / `desktop_pool_status` — pool health report (total /
  available / in-use / errors), view model described below
- `list_machines` + `restart` / `reset` / `shutdown` / `enter-maintenance` /
  `exit-maintenance` machine
- `monitor_connection_servers` / `monitor_gateways` / `monitor_farms` /
  `monitor_rds_servers` / `monitor_ad_domains` / `monitor_event_database`
- `enable_desktop_pool` / `disable_desktop_pool` — re-enable (or disable) a
  pool and its provisioning after it was stopped by errors / before maintenance
- `desktop_pool_push_history` — recent golden-image PUSH_IMAGE tasks per pool
- `rollback_desktop_pool_image` — re-push the *previous* golden-image snapshot
  to a pool to roll back a bad update, keeping the pool's current compute
  profile. Defaults to a dry run (`confirm=True` to schedule)
- `list_sites` — the configured connection servers (primary + DR); every tool
  accepts an optional `site` argument (`primary` / `secondary` / `dr` / a site
  name, and `all` on read-only tools)
- `horizon_ping` / `horizon_config_summary` — connectivity & config

The MCP exposes the most common support/helpdesk actions so an assistant can
"restart Jim's hung desktop", "check Bob's CPU/memory", or "is the Sales pool
healthy?" directly against Horizon.

## Geometry & requirements

- Python 3.9+ (tested on 3.12)
- Install: `pip install -r requirements.txt`

## Configuration (secure service-account credentials)

Credentials are never hard-coded. Provide them via a config JSON file or
environment variables (precedence: explicit args > env > file).

### Option A — interactive wizard (recommended)

```bash
cd omnissa-horizon-mcp
python -m omnissa_horizon_mcp config init --config ./horizon.json
```

This prompts for the Connection Server URL, AD domain, service account username
and password (password entered without echoing), and writes the file with
`0600` owner-only permissions. Example result:

```json
{
  "base_url": "https://horizon.example.com",
  "domain": "EXAMPLE",
  "username": "svc-horizon-mcp",
  "password": "…",
  "verify_ssl": true
}
```

### Option B — environment variables

```bash
export HORIZON_BASE_URL="https://horizon.example.com"
export HORIZON_DOMAIN="EXAMPLE"
export HORIZON_USERNAME="svc-horizon-mcp"
export HORIZON_PASSWORD="…"
export HORIZON_VERIFY_SSL="false"        # default true; set false for test CAs
export HORIZON_TIMEOUT="30"
# optional, to point at a config file:
export HORIZON_CONFIG_FILE="./horizon.json"
```

A `.env` file is also honored if `python-dotenv` is installed and loaded (see
`.env.example`). See `omnissa_horizon_mcp/config.py` for all `HORIZON_*` vars.

### Multiple connection servers (primary + DR site)

The environment may span more than one Horizon Connection Server (e.g. a
primary datacenter plus a DR site). Add a `sites` list to the config file, or
set `HORIZON_SITES` to the same JSON array. Each entry may override the
top-level credentials; anything it omits is inherited.

```json
{
  "base_url": "https://cloud.wellertruck.com",
  "domain": "WELLER",
  "username": "horizonaiagent",
  "password": "…",
  "verify_ssl": true,
  "sites": [
    {"name": "primary",   "base_url": "https://cloud.wellertruck.com",   "role": "primary"},
    {"name": "secondary", "base_url": "https://cloud-b.wellertruck.com", "role": "secondary"}
  ]
}
```

Every tool then accepts an optional `site` argument:

* omitted / `primary` -> the default (`base_url`) connection server
* `secondary` / `dr` -> the DR site
* a configured `name` -> that site
* `all` -> read-only listing/monitoring tools query every site (a dict keyed
  by site name)

`list_sites` shows the configured sites. Each site keeps its **own** inventory
and golden images, so pool/snapshot/rollback lookups always resolve against
the selected site.

### Verify credentials before connecting to an MCP client

```bash
python -m omnissa_horizon_mcp ping --config ./horizon.json
```

## Running the MCP server

```bash
# stdio transport (used by Claude Desktop / MCP clients)
python -m omnissa_horizon_mcp --config ./horizon.json   # note: --config passthrough
```

> By default the server resolves config from `HORIZON_CONFIG_FILE` / env vars.
> To point at a specific file, set `export HORIZON_CONFIG_FILE=./horizon.json`
> before launching, and add a `.env` load if desired.

### Example MCP client config (Claude Desktop `claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "omnissa-horizon": {
      "command": "python",
      "args": ["-m", "omnissa_horizon_mcp"],
      "env": {
        "HORIZON_CONFIG_FILE": "/abs/path/to/horizon.json"
      }
    }
  }
}
```

## How `desktop_pool_status` computes "available" vs "used"

It combines three sources:
- `/inventory/v1/desktop-pools` — pool metadata (id, name, type, source, enabled)
- `/inventory/v1/machines` — machines grouped by pool; non-`AVAILABLE`/erroring
  machines are excluded from availability
- `/inventory/v1/sessions` + `/monitor/desktops` — in-use session count and the
  pool's aggregated monitor status (OK/ERROR/…)

Result summary: `total_machines`, `total_in_use_sessions`, `total_errors`, plus
per-pool `machines_by_state`, `in_use_sessions`, `available_estimate` and any
cloning/state `errors`.

## Horizon 2506 API notes

A few Horizon REST details that this server handles explicitly (verified
against the 2506 OpenAPI spec and the *Horizon Server REST Pagination, Filter
and Sorting Guide*):

- **Inventory filters use a JSON filter object**, not the `field 'value'`
  string form. `list_sessions` / `list_machines` build e.g.
  `{"type":"Equals","name":"desktop_pool_id","value":"…"}`, chained with an
  `"And"` wrapper for multiple clauses. Field/operator choices: `user_name`
  (Contains), `desktop_pool_id` (Equals), `session_state` (Equals) for
  sessions; `name` (Contains), `desktop_pool_id` (Equals), `state` (Equals)
  for machines.
- **Sessions are listed from `/inventory/v7/sessions`** (not `v1`): the base
  `SessionInfo` model returned by `/inventory/v1/sessions` has no `user_name`
  field (only the raw `user_id` SID), so it cannot be filtered or displayed by
  username. `user_name` is added from the versioned session models (v4+); v7 is
  the current 2506 model. `find_user_session` therefore filters server-side on
  `user_name` (Equals/Contains) and falls back to an unfiltered client-side
  scan. If sessions are visible but none expose a `user_name`, the tool raises a
  clear error about the likely missing service-account privilege instead of
  silently reporting zero matches.
- **Helpdesk performance calls use `/helpdesk/v1/...` with `session_id`**
  (`historical-data`, `process`, `display-protocol`, and
  `remote-process/action/end-remote-process`). The `/helpdesk/v2/...`
  endpoints take `internal_session_id`, a different internal identifier that
  rejects an inventory session id (`helpdesk.session.find.error`).

## How `rollback_desktop_pool_image` chooses the snapshot

1. Resolve the pool by name/id and read its v7 `provisioning_settings`:
   current `parent_vm_id` + `base_snapshot_id`, and the **compute profile**
   (`compute_profile_num_cpus`, `compute_profile_num_cores_per_socket`,
   `compute_profile_ram_mb`).
2. Read the pool's push history (`/inventory/v1/desktop-pools/{id}/tasks`) and
   pick the most recent distinct pushed image that is not the current one.
3. If that image no longer exists on the golden image (its snapshot was
   deleted), fall back to the golden image's snapshot chronology -- the
   snapshot created immediately before the pool's current snapshot.
4. Re-push it via `POST /inventory/v1/desktop-pools/{id}/action/schedule-push-image`.

**Shared golden images (`pool_image_filters`).** When several pools share one
golden image but each must use its own snapshot family (e.g. `bos1` uses the
`1GB` snapshots, `bos2` the `2GB` ones), add per-pool rules so the fallback
can't cross over. A rule applies when its `pool` regex matches the pool name;
a candidate snapshot is accepted only when its **name** contains the `require`
text (the parent path is deliberately not matched, since it can contain the
other pool's token):

```json
"pool_image_filters": [
  {"pool": "bos1", "require": "1GB"},
  {"pool": "bos2", "require": "2GB"}
]
```

(Pools matching no rule are unrestricted. `HORIZON_POOL_IMAGE_FILTERS` sets the
same list via env.)

The endpoint does **not** accept a compute profile, so the pool's current
vCPUs / cores-per-socket / RAM are preserved. The tool returns the resolved
plan (current image, target image, compute profile, request body and how the
target was chosen) and only schedules the push when `confirm=True`.

> Pushing an image is a maintenance operation: existing sessions are logged off
> (per `logoff_policy`) and the pool is rebuilt. Confirm the target first.

## Security notes

- Passwords are never logged; `redacted()` masks them.
- All requests use `verify_ssl` (on by default). Set false only for
  self-signed/test environments.
- Mutating operations (`restart`, `logoff`, `reset`, `end_session_process`)
  require the appropriate Horizon privileges; check the Connection Server roles
  of the service account.

## Running via Docker

The project ships a `Dockerfile`, `docker-compose.yml` and `.dockerignore`. Run
as a non-root user; the app runs over **stdio** by default (embedded MCP client)
and can also run as a long-lived HTTP/SSE **daemon**.

### Build

```bash
docker build -t omnissa-horizon-mcp .
```

### Option A — stdio (embedded MCP client, e.g. Claude Desktop)

Create your config file with `config init`, then run the container
**interactively** so stdio stays attached; pass credentials via env or a
mounted config file:

```bash
docker run -i --rm \
  -e HORIZON_CONFIG_FILE=/config/horizon.json \
  -v "$(pwd)/horizon.json:/config/horizon.json:ro" \
  omnissa-horizon-mcp
```

Point the MCP client at the container, e.g. Claude Desktop:

```json
{
  "mcpServers": {
    "omnissa-horizon": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "HORIZON_CONFIG_FILE=/config/horizon.json",
               "-v", "/abs/path/horizon.json:/config/horizon.json:ro",
               "omnissa-horizon-mcp"]
    }
  }
}
```

Or pass secrets via `-e` instead of mounting a file:

```bash
docker run -i --rm \
  -e HORIZON_BASE_URL=https://horizon.example.com \
  -e HORIZON_DOMAIN=EXAMPLE \
  -e HORIZON_USERNAME=svc-horizon-mcp \
  -e HORIZON_PASSWORD=*** \
  -e HORIZON_VERIFY_SSL=false \
  omnissa-horizon-mcp
```

> **File-permission note:** the container runs as an unprivileged user, so a
> mounted `horizon.json` must be world/group-readable for it to open, or it will
> fail on permissions. Prefer env vars, or `chown` the file to the container
> UID, if you hit `Permission denied`.

### Option B — network daemon (HTTP / SSE / streamable-http)

```bash
docker run -d --name horizon-mcp -p 8000:8000 \
  -e HORIZON_CONFIG_FILE=/config/horizon.json \
  -v "$(pwd)/horizon.json:/config/horizon.json:ro" \
  omnissa-horizon-mcp --transport http --host 0.0.0.0 --port 8000
```

or with Compose (bundled):

```bash
docker compose up -d --build
```

Then connect an MCP client that supports HTTP/SSE to
`http://<host>:8000/mcp`. Available transports: `stdio` (default), `sse`,
`streamable-http`, `http`.

### Verify the daemon is up

```bash
curl -i http://localhost:8000/health        # HTTP 200 + JSON status
curl -i http://localhost:8000/healthz       # alias
curl -i http://localhost:8000/mcp           # expect JSON-RPC "missing session ID"
```

A health check that requires **no Horizon credentials** and does not block on a
login is exposed at `/health` (alias `/healthz`) whenever the daemon transport
(`http` / `sse` / `streamable-http`) is running:

```json
{"status":"ok","service":"omnissa-horizon-mcp","version":"0.1.0","uptime_seconds":123,"healthy":true}
```

The Docker `HEALTHCHECK` and the bundled `docker-compose.yml` healthcheck both
hit `/health` automatically.

## Authentication (bearer token)

The network transport is gated behind a **bearer token** when configured. With
a token set, every endpoint except `/health` and `/healthz` requires
`Authorization: Bearer <token>` and returns `401` otherwise.

Set it via the env var `HORIZON_MCP_AUTH_TOKEN` or the `--token` flag. Generate
a strong one:

```bash
export HORIZON_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
```

```bash
docker run -d --name horizon-mcp -p 8000:8000 \
  -e HORIZON_CONFIG_FILE=/config/horizon.json \
  -e HORIZON_MCP_AUTH_TOKEN="$HORIZON_MCP_AUTH_TOKEN" \
  -v "$(pwd)/horizon.json:/config/horizon.json:ro" \
  omnissa-horizon-mcp --transport http --host 0.0.0.0 --port 8000
```

Clients must then send the header on every request:
```bash
curl -H "Authorization: Bearer $HORIZON_MCP_AUTH_TOKEN" http://host:8000/mcp
```

Notes:
- When `HORIZON_MCP_AUTH_TOKEN` is unset/empty, auth is **disabled** (open),
  preserving the default behaviour. The bundled `docker-compose.yml` *requires*
  the variable so daemon deployments are secured by default.
- If the token is ever compromised, rotate it and redeploy — the running
  process does not cache it across restarts.
- `/health` stays public so load balancers / uptime monitors can probe liveness
  without a secret.

## Project layout

```
omnissa-horizon-mcp/
├── README.md
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── omnissa_horizon_mcp/
    ├── __init__.py
    ├── __main__.py          # python -m entrypoint
    ├── server.py            # FastMCP app + CLI (config init / ping / run)
    ├── config.py            # secure config & credential resolution
    ├── client.py            # Horizon REST client (auth, pagination, errors)
    └── tools/
        ├── __init__.py      # registers all tool modules
        ├── _common.py       # user-session helpers
        ├── connection_tools.py
        ├── session_tools.py
        ├── utilization_tools.py
        ├── pool_tools.py
        └── machine_tools.py
```