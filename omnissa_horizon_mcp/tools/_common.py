"""Shared helpers for tool implementations."""
from __future__ import annotations

from typing import Dict, List, Optional

from ..client import HorizonClient


def lookup_user_session(
    client: HorizonClient,
    username: str,
    *,
    active_only: bool = True,
) -> List[Dict]:
    """Find sessions belonging to ``username``.

    Horizon usernames may be ``user``, ``DOMAIN\\user`` or ``user@domain``.
    The ``user_name`` cookie on a session is typically ``DOMAIN\\user``.
    """
    needle = username.strip().lower().replace("\\", "/")
    # Normalise a leading domain in the search.
    if "\\" in username.strip():
        needle = username.strip().lower()
        needle = needle.replace("\\", "/")
    else:
        needle = username.strip().lower()

    wanted = {needle}
    if "/" in needle:
        wanted.add(needle.split("/")[-1])
    if "@" in needle:
        wanted.add(needle.split("@")[0])

    sessions = client.get_all("/inventory/v1/sessions", page_size=200)
    matches = []
    for s in sessions:
        user = (s.get("user_name") or "")
        key = user.lower().replace("\\", "/")
        if not key:
            continue
        if any(key == w or key.endswith("/" + w.lstrip("/")) for w in wanted):
            if active_only and s.get("session_state") == "DISCONNECTED":
                continue
            matches.append(s)
    return matches


def summarize_session(s: Dict) -> Dict:
    """Build a compact human-readable summary of a SessionInfo record."""
    return {
        "session_id": s.get("id"),
        "user_name": s.get("user_name"),
        "state": s.get("session_state"),
        "session_type": s.get("session_type"),
        "protocol": s.get("session_protocol"),
        "desktop_pool_id": s.get("desktop_pool_id"),
        "desktop_name": s.get("desktop_name") or s.get("machine_name"),
        "machine_id": s.get("machine_id"),
        "dns_name": s.get("dns_name") or s.get("machine_dns"),
        "client": (s.get("client_data") or {}).get("client_host_name"),
        "start_time": s.get("start_time"),
        "idle_duration_minutes": s.get("idle_duration"),
        "disconnected_time": s.get("disconnected_time"),
    }