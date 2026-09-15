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
curl -i http://localhost:8000/mcp        # expect a JSON-RPC "missing session ID" response
```

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