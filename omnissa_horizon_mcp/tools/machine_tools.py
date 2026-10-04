"""Machine-level tools: list machines and issue power operations
(restart / reset / shutdown) directly against machine ids.

Read-only listing accepts ``site`` (and ``site='all'``); machine actions take a
single ``site``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ._common import build_filter, contains_filter, equals_filter, site_client, site_clients


def _machine_brief(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "dns_name": m.get("dns_name"),
        "state": m.get("state"),
        "type": m.get("type"),
        "desktop_pool_id": m.get("desktop_pool_id"),
        "agent_version": m.get("agent_version"),
        "operating_system": m.get("operating_system"),
        "users": list(m.get("user_ids") or []),
    }


def _machine_filter_params(pool_id: Optional[str], machine_name: Optional[str],
                           state: Optional[str]) -> Optional[dict]:
    """Build the ``filter`` query params for /inventory machines listing.

    Horizon expects a JSON filter object, not ``field 'value'``. The machine
    name field is ``name`` (Contains for the documented substring search);
    pool id and state are exact Equals matches.
    """
    clauses = []
    if pool_id:
        clauses.append(equals_filter("desktop_pool_id", pool_id))
    if machine_name:
        clauses.append(contains_filter("name", machine_name))
    if state:
        clauses.append(equals_filter("state", state))
    if not clauses:
        return None
    return {"filter": build_filter(clauses)}


def _machine_action(client, path: str, machine_id: str, action: str) -> dict:
    client.post_json(path, [machine_id])
    return {"action": action, "machine_ids": [machine_id], "status": "submitted"}


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def list_machines(pool_id: Optional[str] = None, machine_name: Optional[str] = None,
                      state: Optional[str] = None, size: int = 200,
                      site: Optional[str] = None) -> Union[list, dict]:
        """List VDI machines (desktops). Optionally filter by desktop pool id,
        machine name substring, or machine state (e.g. AVAILABLE / CONNECTED /
        ERROR). ``site='all'`` returns a dict keyed by site name."""
        params = _machine_filter_params(pool_id, machine_name, state)

        def _fetch(client):
            return [_machine_brief(m) for m in client.get_all(
                "/inventory/v1/machines", params=params, page_size=size, max_items=size)]

        if (site or "").strip().lower() == "all":
            return {name: _fetch(client) for name, client in site_clients(config, "all")}
        _, client = site_client(config, site)
        return _fetch(client)

    @mcp.tool()
    def restart_machine(machine_id: str, site: Optional[str] = None) -> dict:
        """Restart (graceful reboot) a specific machine by its id. Use when you
        already know the machine id (from list_machines)."""
        _, client = site_client(config, site)
        return _machine_action(client, "/inventory/v1/machines/action/restart",
                               machine_id, "restart-machine")

    @mcp.tool()
    def reset_machine(machine_id: str, site: Optional[str] = None) -> dict:
        """Hard-reset (power-cycle, like unplugging/replugging) a specific
        machine by id. More forceful than restart; unsaved data is lost."""
        _, client = site_client(config, site)
        return _machine_action(client, "/inventory/v1/machines/action/reset",
                               machine_id, "reset-machine")

    @mcp.tool()
    def shutdown_machine(machine_id: str, site: Optional[str] = None) -> dict:
        """Gracefully shut down a specific machine by its id (VM power off)."""
        _, client = site_client(config, site)
        return _machine_action(client, "/inventory/v1/machines/action/shutdown",
                               machine_id, "shutdown-machine")

    @mcp.tool()
    def enter_maintenance_machine(machine_id: str, site: Optional[str] = None) -> dict:
        """Put a specific machine into maintenance mode, taking it out of the
        pool for service."""
        _, client = site_client(config, site)
        return _machine_action(client, "/inventory/v1/machines/action/enter-maintenance",
                               machine_id, "enter-maintenance")

    @mcp.tool()
    def exit_maintenance_machine(machine_id: str, site: Optional[str] = None) -> dict:
        """Take a specific machine out of maintenance mode, returning it to the
        pool."""
        _, client = site_client(config, site)
        return _machine_action(client, "/inventory/v1/machines/action/exit-maintenance",
                               machine_id, "exit-maintenance")
