"""Machine-level tools: list machines and issue power operations
(restart / reset / shutdown) directly against machine ids."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ..client import get_client
from ._common import build_filter, contains_filter, equals_filter


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


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def list_machines(pool_id: Optional[str] = None, machine_name: Optional[str] = None,
                      state: Optional[str] = None, size: int = 200) -> list:
        """List VDI machines (desktops) in the environment. Optionally filter by
        desktop pool id, machine name substring, or machine state (e.g. AVAILABLE
        / CONNECTED / ERROR). Returns id, name, dns, state and assigned users."""
        client = get_client(config)
        params = _machine_filter_params(pool_id, machine_name, state)
        machines = client.get_all("/inventory/v1/machines", params=params, page_size=size, max_items=size)
        return [_machine_brief(m) for m in machines]

    @mcp.tool()
    def restart_machine(machine_id: str) -> dict:
        """Restart (graceful reboot) a specific machine by its id. Use when you
        already know the machine id (from list_machines)."""
        client = get_client(config)
        client.post_json("/inventory/v1/machines/action/restart", [machine_id])
        return {"action": "restart-machine", "machine_ids": [machine_id], "status": "submitted"}

    @mcp.tool()
    def reset_machine(machine_id: str) -> dict:
        """Hard-reset (power-cycle, like unplugging/replugging) a specific
        machine by id. More forceful than restart; unsaved data is lost."""
        client = get_client(config)
        client.post_json("/inventory/v1/machines/action/reset", [machine_id])
        return {"action": "reset-machine", "machine_ids": [machine_id], "status": "submitted"}

    @mcp.tool()
    def shutdown_machine(machine_id: str) -> dict:
        """Gracefully shut down a specific machine by its id (VM power off)."""
        client = get_client(config)
        client.post_json("/inventory/v1/machines/action/shutdown", [machine_id])
        return {"action": "shutdown-machine", "machine_ids": [machine_id], "status": "submitted"}

    @mcp.tool()
    def enter_maintenance_machine(machine_id: str) -> dict:
        """Put a specific machine into maintenance mode, taking it out of the
        pool for service."""
        client = get_client(config)
        client.post_json("/inventory/v1/machines/action/enter-maintenance", [machine_id])
        return {"action": "enter-maintenance", "machine_ids": [machine_id], "status": "submitted"}

    @mcp.tool()
    def exit_maintenance_machine(machine_id: str) -> dict:
        """Take a specific machine out of maintenance mode, returning it to the
        pool."""
        client = get_client(config)
        client.post_json("/inventory/v1/machines/action/exit-maintenance", [machine_id])
        return {"action": "exit-maintenance", "machine_ids": [machine_id], "status": "submitted"}