"""Utilization / helpdesk tools: process listing, CPU & memory stats, and the
ability to terminate a runaway process in a user's desktop."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ..client import get_client
from ._common import lookup_user_session

# /helpdesk/v1/... takes `session_id` (the inventory session id). The
# /helpdesk/v2/... endpoints take `internal_session_id`, which is a different,
# internal identifier and rejects an inventory session id.
HELPDESK_HISTORICAL_PATH = "/helpdesk/v1/performance/historical-data"
HELPDESK_PROCESS_PATH = "/helpdesk/v1/performance/process"
HELPDESK_DISPLAY_PATH = "/helpdesk/v1/performance/display-protocol"
HELPDESK_END_PROCESS_PATH = "/helpdesk/v1/performance/remote-process/action/end-remote-process"


def _helpdesk(client, session_id: str, path: str, **extra) -> list:
    params = {"session_id": session_id}
    params.update(extra)
    data = client.get_json(path, params=params)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("results") or []
    return data if isinstance(data, list) else [data]


def _describe_process(p: dict) -> dict:
    return {
        "name": p.get("name"),
        "pid": p.get("process_id"),
        "user": p.get("user_name"),
        "cpu_pct": p.get("cpu"),
        "memory_pct": p.get("memory"),
        "disk_pct": p.get("disk"),
        "create_time": p.get("create_time"),
    }


def _protocol_network(client, session_id: str) -> dict:
    """Fetch display-protocol performance and return a normalised network
    summary: estimated bandwidth, latency and packet loss for PCoIP/BLAST.
    Returns an empty dict if the endpoint is unavailable for the session."""
    try:
        data = client.get_json(
            HELPDESK_DISPLAY_PATH,
            params={"session_id": session_id},
        )
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    pcoip = data.get("pcoip_performance_data") or {}
    blast = data.get("blast_performance_data") or data.get("blast_performance_data_v2") or {}

    if pcoip:
        out = {
            "protocol": "PCoIP",
            "estimated_bandwidth_kbps": pcoip.get("network_rx_bandwidth"),
            "rx_bandwidth_kbps": pcoip.get("network_rx_bandwidth"),
            "tx_bandwidth_kbps": pcoip.get("network_tx_bandwidth"),
            "estimated_available_bandwidth_kbps": pcoip.get("network_tx_bandwidth_active_limit"),
            "bandwidth_limit_kbps": pcoip.get("network_tx_bandwidth_limit"),
            "latency_ms": pcoip.get("network_round_trip_latency"),
            "rx_packet_loss_pct": pcoip.get("network_rx_packet_loss"),
            "tx_packet_loss_pct": pcoip.get("network_tx_packet_loss"),
        }
    elif blast:
        out = {
            "protocol": "BLAST",
            "estimated_bandwidth_kbps": blast.get("session_bandwidth_uplink"),
            "uplink_bandwidth_kbps": blast.get("session_bandwidth_uplink"),
            "bandwidth_uplink_kbps": blast.get("session_bandwidth_uplink"),
            "tx_bytes": blast.get("session_bytes_transmitted"),
            "uplink_packet_loss_pct": blast.get("session_packet_loss_uplink"),
        }
    return out


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def session_processes(session_id: str, process_filter: Optional[str] = None) -> dict:
        """List running processes in a user's desktop session along with their
        CPU, memory and disk utilization (%). Session id comes from
        find_user_session. Optionally filter by a substring of the process name
        (e.g. 'chrome')."""
        client = get_client(config)
        params = {"process_filter": process_filter} if process_filter else None
        data = _helpdesk(client, session_id, HELPDESK_PROCESS_PATH)
        if process_filter:
            data = [p for p in data if process_filter.lower() in (p.get("name") or "").lower()]
        data.sort(key=lambda p: p.get("cpu", 0), reverse=True)
        return {"session_id": session_id, "process_count": len(data), "processes": [_describe_process(p) for p in data]}

    @mcp.tool()
    def user_processes(username: str, process_filter: Optional[str] = None) -> dict:
        """List processes + CPU/memory for a given user's desktop. Resolves the
        user's active session(s) and reports per-session processes. This covers
        the 'list processes, cpu and memory of a target user' use case."""
        client = get_client(config)
        sessions = lookup_user_session(client, username, active_only=False)
        result = []
        for s in sessions:
            sid = s.get("id")
            if not sid:
                continue
            data = _helpdesk(client, sid, HELPDESK_PROCESS_PATH)
            if process_filter:
                data = [p for p in data if process_filter.lower() in (p.get("name") or "").lower()]
            result.append({
                "session_id": sid,
                "user_name": s.get("user_name"),
                "desktop": s.get("desktop_name") or s.get("machine_name"),
                "process_count": len(data),
                "processes": [_describe_process(p) for p in data],
            })
        return {"username": username, "sessions": result}

    @mcp.tool()
    def session_utilization(session_id: str) -> dict:
        """Report a desktop's CPU/memory utilization (plus disk IOPS/latency)
        AND network performance (estimated bandwidth, latency, packet loss) for
        a user session. CPU/mem come from historical-data (last ~15 min);
        network figures come from the display-protocol performance endpoint.
        Session id comes from find_user_session."""
        client = get_client(config)
        data = _helpdesk(client, session_id, HELPDESK_HISTORICAL_PATH)
        latest = data[-1] if data else {}
        return {
            "session_id": session_id,
            "samples": len(data),
            "latest": {
                "cpu_pct": latest.get("cpu"),
                "overall_cpu_pct": latest.get("overall_cpu"),
                "memory_pct": latest.get("memory"),
                "disk_read_iops": latest.get("disk_read_iops"),
                "disk_write_iops": latest.get("disk_write_iops"),
                "disk_latency_ms": latest.get("disk_latency"),
                "latency_ms": latest.get("latency"),
            },
            "network": _protocol_network(client, session_id),
        }

    @mcp.tool()
    def user_cpu_memory(username: str) -> dict:
        """Report CPU and memory utilization of a target user's desktop(s).
        Resolves the user's active session(s) and returns current utilization
        plus network performance (bandwidth/latency) per session from the
        helpdesk performance endpoints."""
        client = get_client(config)
        sessions = lookup_user_session(client, username, active_only=False)
        result = []
        for s in sessions:
            sid = s.get("id")
            if not sid:
                continue
            data = _helpdesk(client, sid, HELPDESK_HISTORICAL_PATH)
            latest = data[-1] if data else {}
            result.append({
                "session_id": sid,
                "user_name": s.get("user_name"),
                "desktop": s.get("desktop_name") or s.get("machine_name"),
                "cpu_pct": latest.get("cpu"),
                "overall_cpu_pct": latest.get("overall_cpu"),
                "memory_pct": latest.get("memory"),
                "latency_ms": latest.get("latency"),
                "samples": len(data),
                "network": _protocol_network(client, sid),
            })
        return {"username": username, "sessions": result}

    @mcp.tool()
    def session_network_performance(session_id: str) -> dict:
        """Return network performance for a user session: estimated bandwidth
        (kbps), round-trip latency (ms) and packet loss, from the display-
        protocol performance endpoint (PCoIP or BLAST). Session id comes from
        find_user_session. Useful for diagnosing slow/thin sessions."""
        client = get_client(config)
        net = _protocol_network(client, session_id)
        if not net:
            return {"session_id": session_id, "network": "unavailable"}
        return {"session_id": session_id, "network": net}

    @mcp.tool()
    def end_session_process(session_id: str, process_id: int, name: str,
                            create_time_stamp: Optional[int] = None) -> dict:
        """Terminate a process running on a user's desktop. process_id and name
        come from user_processes/session_processes; create_time_stamp is the
        process creation time (epoch ms) and is provided automatically when
        available. Use with caution -- terminating critical processes can crash
        the session."""
        client = get_client(config)
        # Try to fill create_time_stamp from the process listing if omitted.
        if create_time_stamp is None:
            procs = _helpdesk(client, session_id, HELPDESK_PROCESS_PATH)
            for p in procs:
                if p.get("process_id") == process_id:
                    create_time_stamp = p.get("create_time")
                    name = p.get("name") or name
                    break
        body = {
            "process_id": int(process_id),
            "name": name,
            "create_time_stamp": int(create_time_stamp or 0),
        }
        client.post_json(
            HELPDESK_END_PROCESS_PATH,
            body,
            params={"session_id": session_id},
        )
        return {"status": "end-process submitted", "session_id": session_id, "process": body}