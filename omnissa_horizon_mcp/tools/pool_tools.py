"""Desktop pool tools: list pools and produce support/helpdesk status reports.

A pool status report combines inventory (pool + machines) with live session
information and the monitoring summary to answer: how many machines are
available, how many are in use, and whether anything is erroring.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from collections import Counter, defaultdict

from ..client import get_client


def _pool_brief(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "display_name": p.get("display_name"),
        "type": p.get("type"),
        "source": p.get("source"),
        "enabled": p.get("enabled"),
        "description": p.get("description"),
    }


def count_available(machines: list, sessions: list) -> int:
    """Estimate 'available' machines in a pool: total minus sessions in use."""
    used_ids = {s.get("machine_id") for s in sessions if s.get("machine_id")}
    available = 0
    for m in machines:
        if m.get("state") in ("ERROR", "PROVISIONING_ERROR", "UNAVAILABLE", "DISABLED"):
            continue
        if m.get("id") in used_ids:
            continue
        available += 1
    return available


def build_pool_status_report(client, pool_id: Optional[str] = None,
                             pool_name: Optional[str] = None) -> dict:
    """Build the pool status report. Separated from the tool handler so it can
    be unit-tested with a mocked client."""
    pools = client.get_all("/inventory/v1/desktop-pools", page_size=200)
    if pool_id:
        pools = [p for p in pools if p.get("id") == pool_id]
    elif pool_name:
        pools = [p for p in pools if (p.get("name") or "").lower() == pool_name.lower()
                 or (p.get("display_name") or "").lower() == pool_name.lower()]
    if not pools:
        return {"status": "no matching pools", "pool_id": pool_id, "pool_name": pool_name}

    # Sessions grouped by pool.
    sessions = client.get_all("/inventory/v1/sessions", page_size=200)
    sessions_by_pool = defaultdict(list)
    for s in sessions:
        sessions_by_pool[s.get("desktop_pool_id")].append(s)

    # Machines grouped by pool (may be empty for RDS/farms).
    machines = []
    try:
        machines = client.get_all("/inventory/v1/machines", page_size=200)
    except Exception:
        machines = []
    machines_by_pool = defaultdict(list)
    for m in machines:
        machines_by_pool[m.get("desktop_pool_id")].append(m)

    # Monitor desktop status.
    monitor = client.get_all("/monitor/desktops", page_size=200)
    monitor_by_id = {d.get("id"): d for d in monitor}

    report = []
    for pool in pools:
        pid = pool.get("id")
        pool_machines = machines_by_pool.get(pid, [])
        pool_sessions = sessions_by_pool.get(pid, [])
        state_counts = Counter(m.get("state") for m in pool_machines)
        errors = []
        flagged = set()
        for m in pool_machines:
            if m.get("id") in flagged:
                continue
            mdm = m.get("managed_machine_data") or {}
            err = mdm.get("clone_error_message")
            if err:
                errors.append({"machine": m.get("name"), "error": err})
                flagged.add(m.get("id"))
                continue
            if m.get("state") in ("ERROR", "PROVISIONING_ERROR", "UNAVAILABLE"):
                errors.append({"machine": m.get("name"), "error": f"state={m.get('state')}"})
                flagged.add(m.get("id"))
        mon = monitor_by_id.get(pid)
        report.append({
            "pool_id": pid,
            "name": pool.get("name"),
            "display_name": pool.get("display_name"),
            "type": pool.get("type"),
            "source": pool.get("source"),
            "enabled": pool.get("enabled"),
            "monitor_status": (mon or {}).get("status"),
            "total_machines": len(pool_machines),
            "machines_by_state": dict(state_counts),
            "in_use_sessions": len(pool_sessions),
            "active_sessions": sum(1 for s in pool_sessions if s.get("session_state") not in ("DISCONNECTED", "PENDING")),
            "available_estimate": count_available(pool_machines, pool_sessions),
            "errors": errors[:20],
            "error_count": len(errors),
        })

    return {
        "scope": {"pool_id": pool_id, "pool_name": pool_name, "pools": len(report)},
        "summary": {
            "total_machines": sum(r["total_machines"] for r in report),
            "total_in_use_sessions": sum(r["in_use_sessions"] for r in report),
            "total_errors": sum(r["error_count"] for r in report),
        },
        "pools": report,
    }


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def list_desktop_pools() -> list:
        """List all desktop pools with their id, name, type (AUTOMATED/MANUAL/
        RDS) and source (INSTANT_CLONE etc). Use the pool id/name to drill into
        desktop_pool_status."""
        client = get_client(config)
        pools = client.get_all("/inventory/v1/desktop-pools", page_size=200)
        return [_pool_brief(p) for p in pools]

    @mcp.tool()
    def desktop_pool_status(pool_id: Optional[str] = None, pool_name: Optional[str] = None) -> dict:
        """Produce a helpdesk status report for desktop pool(s): total machines,
        available (powered-on & not in use), in-use session count, and any
        erroring machines / cloning errors. Provide either a pool id or name; if
        neither is given, reports on all pools."""
        client = get_client(config)
        return build_pool_status_report(client, pool_id=pool_id, pool_name=pool_name)