"""Connection / environment tools: connectivity checks and monitoring.

Most read-only tools accept an optional ``site`` argument so the same call can
target the primary connection server, the DR/secondary site, a named site, or
(briefly) every configured site with ``site="all"``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ..config import list_sites_public
from ..client import get_client
from ._common import site_client, site_clients


def _monitor(config, path: str, site: Optional[str]):
    """Read a monitoring collection for one site (list) or all sites (dict)."""
    if (site or "").strip().lower() == "all":
        out = {}
        for name, client in site_clients(config, "all"):
            try:
                out[name] = client.get_all(path)
            except Exception as exc:  # keep going if one site is unreachable
                out[name] = {"error": str(exc)}
        return out
    _, client = site_client(config, site)
    return client.get_all(path)


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def list_sites() -> list:
        """List the configured Horizon connection servers (sites) with their
        role, URL and bound service-account domain. Pass a site's ``name`` (or
        the alias ``primary``/``secondary``/``dr``) as the ``site`` argument on
        other tools to target it; ``site="all"`` reads every site."""
        return list_sites_public(config)

    @mcp.tool()
    def horizon_ping(site: Optional[str] = None) -> dict:
        """Verify connectivity and service-account authentication to a Horizon
        Connection Server. Returns server, user and domain only -- never
        credentials. Useful as a first sanity check or connection test."""
        name, client = site_client(config, site)
        info = client.test_connection()
        info["site"] = name
        return info

    @mcp.tool()
    def horizon_config_summary() -> dict:
        """Return a redacted description of how this Horizon MCP server is
        configured (which Connection Servers it targets and the bound service
        account). Password is never exposed."""
        d = config.redacted()
        d["verify_ssl"] = config.verify_ssl
        d["timeout_seconds"] = config.timeout
        d["sites"] = list_sites_public(config)
        return d

    @mcp.tool()
    def monitor_connection_servers(site: Optional[str] = None) -> Union[list, dict]:
        """List Horizon Connection Servers and their health/status from the
        monitoring API. ``site="all"`` returns a dict keyed by site name."""
        return _monitor(config, "/monitor/connection-servers", site)

    @mcp.tool()
    def monitor_gateways(site: Optional[str] = None) -> Union[list, dict]:
        """List Unified Access Gateways (UAG) health/status from monitoring.
        ``site="all"`` returns a dict keyed by site name."""
        return _monitor(config, "/monitor/gateways", site)

    @mcp.tool()
    def monitor_farms(site: Optional[str] = None) -> Union[list, dict]:
        """List RDSH farms and their aggregate status from monitoring.
        ``site="all"`` returns a dict keyed by site name."""
        return _monitor(config, "/monitor/farms", site)

    @mcp.tool()
    def monitor_rds_servers(site: Optional[str] = None) -> Union[list, dict]:
        """List RDS Host servers and health from monitoring. ``site="all"``
        returns a dict keyed by site name."""
        return _monitor(config, "/monitor/rds-servers", site)

    @mcp.tool()
    def monitor_ad_domains(site: Optional[str] = None) -> Union[list, dict]:
        """List configured Active Directory domains and their health.
        ``site="all"`` returns a dict keyed by site name."""
        return _monitor(config, "/monitor/ad-domains", site)

    @mcp.tool()
    def monitor_event_database(site: Optional[str] = None) -> dict:
        """Return event database connectivity status for one site."""
        name, client = site_client(config, site)
        data = client.get_one("/monitor/event-database")
        if isinstance(data, dict):
            data.setdefault("site", name)
        return data
