"""Omnissa Horizon MCP server (FastMCP).

Run via:
    python -m omnissa_horizon_mcp                   # stdio MCP server
    python -m omnissa_horizon_mcp config init --config ./horizon.json
    python -m omnissa_horizon_mcp ping --config ./horizon.json

Environment / config precedence is handled by :mod:`.config`. Credentials come
from HORIZON_* env vars or a JSON config file written by `config init`.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time

from . import __version__
from .client import clear_client, get_client
from .config import ENV_MCP_TOKEN, ConfigError, configure_interactive, load_config
from .tools import register_all


# Paths exempt from bearer-token auth so external health monitors can probe the
# daemon without credentials.
_PUBLIC_PATHS = ("/health", "/healthz")


class _BearerAuthMiddleware:
    """Pure-ASGI middleware that requires ``Authorization: Bearer <token>`` on
    all HTTP requests except the public health paths. Uses constant-time
    comparison to avoid timing attacks. Passed to FastMCP as a Starlette
    ``Middleware`` spec: (class, args, kwargs)."""

    def __init__(self, app, allowed_token: str = "", public_paths=_PUBLIC_PATHS):
        self.app = app
        self.allowed_token = allowed_token
        self.public_paths = public_paths

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if any(path == p or path.startswith(p + "/") for p in self.public_paths):
            return await self.app(scope, receive, send)

        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        expected = "Bearer " + self.allowed_token
        if not hmac.compare_digest(auth, expected):
            body = json.dumps({"detail": "Not authenticated"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def _auth_middleware_spec(allowed_token: str):
    """Return a Starlette Middleware spec tuple for FastMCP's ``middleware``
    argument, or None when no token is configured."""
    if not allowed_token:
        return None
    return (_BearerAuthMiddleware, (), {"allowed_token": allowed_token})


def build_server(config=None):
    """Build a configured FastMCP app. ``config`` may be a HorizonConfig or
    None (in which case credentials are resolved from env/file)."""
    from fastmcp import FastMCP

    if config is None:
        config = load_config()

    mcp = FastMCP(
        "omnissa-horizon",
        version=__version__,
        instructions=(
            "Tools to manage and troubleshoot an Omnissa Horizon (2506) "
            "virtual-desktop environment: restart/log-off user desktops, list "
            "processes and CPU/memory utilization, desktop-pool health and "
            "machine/helpdesk operations. Look a user up with "
            "find_user_session before acting on their desktop."
        ),
    )

    register_all(mcp, config)

    @mcp.tool()
    def horizon_config() -> dict:
        """Return a redacted description of the Horizon environment this server
        is connected to (server, service account, ssl/timeout). Never includes
        the password."""
        return config.redacted()

    _started_at = time.time()
    _health_body = {
        "status": "ok",
        "service": "omnissa-horizon-mcp",
        "version": __version__,
    }

    # HTTP health endpoint(s) for the daemon transport (http/sse/streamable-http).
    # Lets load balancers / monitors / the Docker HEALTHCHECK get a fast 200
    # without performing a blocking login to Horizon.
    async def _health_response(request):  # noqa: ANN001 - Starlette Request
        from starlette.responses import JSONResponse

        body = dict(_health_body)
        body["uptime_seconds"] = int(time.time() - _started_at)
        body["healthy"] = True
        return JSONResponse(body)

    mcp.custom_route("/health", methods=["GET"], name="health")(_health_response)
    mcp.custom_route("/healthz", methods=["GET"], name="healthz")(_health_response)

    return mcp


def _cmd_config(args: argparse.Namespace) -> int:
    try:
        path = configure_interactive(args.config)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Credentials written securely (0600) to: {path}")
    print("You can now start the server (HORIZON_CONFIG_FILE is not needed if")
    print(f"you pass --config {path} or set HORIZON_CONFIG_FILE={path}).")
    return 0


def _cmd_ping(args: argparse.Namespace) -> int:
    try:
        config = load_config(config_file=args.config)
        client = get_client(config)
        client._debug = bool(getattr(args, "debug", False))
        info = client.test_connection()
        print("Connected OK:")
        for k, v in info.items():
            print(f"  {k}: {v}")
        return 0
    except Exception as exc:  # noqa: BLE001 - report any failure to CLI
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="omnissa-horizon-mcp",
        description="Omnissa Horizon MCP server (FastMCP).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=None, help="Path to config JSON file (also used by the default run mode)")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "sse", "streamable-http", "http"],
                        help="MCP transport for the default run mode (default: stdio)")
    parser.add_argument("--host", default=None, help="Bind host for http/sse transports")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for http/sse transports")
    parser.add_argument("--token", default=None,
                        help="Bearer token that gates the network transport (default: $%s)" % ENV_MCP_TOKEN)
    sub = parser.add_subparsers(dest="command")

    # CLI secret/config wizard: `config init --config <file>`
    p_cfg = sub.add_parser("config", help="Configure service-account credentials")
    cfg_sub = p_cfg.add_subparsers(dest="config_command")
    p_init = cfg_sub.add_parser("init", help="Write a credentials config file (0600)")
    p_init.add_argument("--config", required=True, help="Path to the config JSON file")
    p_init.set_defaults(func=_cmd_config)

    # CLI connectivity test.
    p_ping = sub.add_parser("ping", help="Test connectivity/credentials")
    p_ping.add_argument("--config", default=None, help="Path to config JSON file")
    p_ping.add_argument("--debug", action="store_true",
                        help="Print the exact login request being sent (password redacted)")
    p_ping.set_defaults(func=_cmd_ping)

    args = parser.parse_args(argv)

    if getattr(args, "command", None) == "config" and getattr(args, "config_command", None) == "init":
        return _cmd_config(args)
    if getattr(args, "command", None) == "ping":
        return _cmd_ping(args)

    # Default: run the MCP server with the chosen transport.
    try:
        mcp = build_server(config=load_config(config_file=args.config))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if args.transport in ("http", "sse", "streamable-http"):
        auth_token = args.token if args.token is not None else os.environ.get(ENV_MCP_TOKEN, "").strip()
        mw = _auth_middleware_spec(auth_token)
        mcp.run(
            transport=args.transport,
            host=args.host or "0.0.0.0",
            port=args.port,
            middleware=[mw] if mw else None,
        )
    else:
        mcp.run(transport="stdio")

    # Best-effort logout on shutdown.
    try:
        clear_client(load_config(config_file=args.config).client_id)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())