"""Connection / environment tools: connectivity checks and monitoring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ..client import get_client


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def horizon_ping() -> dict:
        """Verify connectivity and service-account authentication to the
        Horizon Connection Server. Returns server, user and domain only -- never
        credentials. Useful as a first sanity check or connection test."""
        client = get_client(config)
        return client.test_connection()

    @mcp.tool()
    def horizon_config_summary() -> dict:
        """Return a redacted description of how this Horizon MCP server is
        configured (which Connection Server it targets and the bound service
        account). Password is never exposed."""
        d = config.redacted()
        d["verify_ssl"] = config.verify_ssl
        d["timeout_seconds"] = config.timeout
        return d

    @mcp.tool()
    def monitor_connection_servers() -> list:
        """List Horizon Connection Servers and their health/status from the
        monitoring API."""
        return get_client(config).get_all("/monitor/connection-servers")

    @mcp.tool()
    def monitor_gateways() -> list:
        """List Unified Access Gateways (UAG) health/status from monitoring."""
        return get_client(config).get_all("/monitor/gateways")

    @mcp.tool()
    def monitor_farms() -> list:
        """List RDSH farms and their aggregate status from monitoring."""
        return get_client(config).get_all("/monitor/farms")

    @mcp.tool()
    def monitor_rds_servers() -> list:
        """List RDS Host servers and health from monitoring."""
        return get_client(config).get_all("/monitor/rds-servers")

    @mcp.tool()
    def monitor_ad_domains() -> list:
        """List configured Active Directory domains and their health."""
        return get_client(config).get_all("/monitor/ad-domains")

    @mcp.tool()
    def monitor_event_database() -> dict:
        """Return event database connectivity status."""
        return get_client(config).get_one("/monitor/event-database")