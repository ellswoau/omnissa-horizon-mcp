"""Session operations frequently used by support/helpdesk.

Restart / logoff / disconnect a desktop session, send a balloon message, and
look up a user's active session(s). Read-only lookups accept ``site`` (and
``site='all'``); session actions take a single ``site``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from ..client import HorizonClient
from ._common import (
    SESSIONS_PATH,
    build_filter,
    contains_filter,
    equals_filter,
    lookup_user_session,
    site_client,
    site_clients,
    summarize_session,
)


def _require_session(client: HorizonClient, session_id: str) -> str:
    """Validate / normalise a session id string."""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty.")
    return sid


def _session_filter_params(username: Optional[str], pool_id: Optional[str],
                           session_state: Optional[str]) -> Optional[dict]:
    """Build the ``filter`` query params for /inventory sessions listing.

    Horizon expects a JSON filter object, not ``field 'value'``. ``user_name``
    uses a Contains match (robust to ``DOMAIN\\user`` prefixes) while pool id
    and state are exact Equals matches.
    """
    clauses = []
    if username:
        clauses.append(contains_filter("user_name", username))
    if pool_id:
        clauses.append(equals_filter("desktop_pool_id", pool_id))
    if session_state:
        clauses.append(equals_filter("session_state", session_state))
    if not clauses:
        return None
    return {"filter": build_filter(clauses)}


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def find_user_session(username: str, active_only: bool = True,
                          site: Optional[str] = None) -> dict:
        """Locate a user's Horizon session(s) by username. Returns session ids
        and related desktop/pool info that other tools (restart/logoff/etc)
        require. Use this first when investigating a user whose desktop you need
        to manage. ``site='all'`` searches every configured site."""
        if (site or "").strip().lower() == "all":
            results = {}
            total = 0
            for name, client in site_clients(config, "all"):
                matches = lookup_user_session(client, username, active_only=active_only)
                total += len(matches)
                results[name] = [summarize_session(s) for s in matches]
            return {"username": username, "found": total, "sites": results}
        name, client = site_client(config, site)
        matches = lookup_user_session(client, username, active_only=active_only)
        return {
            "username": username,
            "site": name,
            "found": len(matches),
            "sessions": [summarize_session(s) for s in matches],
        }

    @mcp.tool()
    def list_sessions(username: Optional[str] = None, pool_id: Optional[str] = None,
                      session_state: Optional[str] = None, size: int = 200,
                      site: Optional[str] = None) -> Union[list, dict]:
        """List Horizon user sessions (paginated). Optionally filter by username,
        desktop pool id, or session state (e.g. CONNECTED / DISCONNECTED /
        PENDING). ``site='all'`` returns a dict keyed by site name."""
        params = _session_filter_params(username, pool_id, session_state)

        def _fetch(client):
            return [summarize_session(s) for s in client.get_all(
                SESSIONS_PATH, params=params, page_size=size, max_items=size)]

        if (site or "").strip().lower() == "all":
            return {name: _fetch(client) for name, client in site_clients(config, "all")}
        _, client = site_client(config, site)
        return _fetch(client)

    @mcp.tool()
    def restart_session(session_id: str, site: Optional[str] = None) -> dict:
        """Restart (reboot) the Windows desktop/machine backing one or more user
        sessions. Pass a single session_id (from find_user_session). The machine
        must be Virtual Center managed and not an RDS/application session."""
        _, client = site_client(config, site)
        sid = _require_session(client, session_id)
        client.post_json("/inventory/v1/sessions/action/restart", [sid])
        return {"action": "restart", "session_ids": [sid], "status": "submitted"}

    @mcp.tool()
    def restart_user_desktop(username: str, site: Optional[str] = None) -> dict:
        """Restart the desktop for a user. Resolves their active session(s) then
        issues a machine restart for each. Reports each session id that was
        restarted, and any that were skipped."""
        _, client = site_client(config, site)
        sessions = lookup_user_session(client, username, active_only=False)
        sids = [s["id"] for s in sessions if s.get("id")]
        if not sids:
            return {"username": username, "status": "no active session found"}
        client.post_json("/inventory/v1/sessions/action/restart", sids)
        return {"username": username, "status": "restart submitted", "session_ids": sids}

    @mcp.tool()
    def logoff_session(session_id: str, forced: bool = False, site: Optional[str] = None) -> dict:
        """Log a user session off. Pass a single session_id. By default logoff
        is graceful and will not log off locked sessions; set forced=True to
        log off locked sessions as well."""
        _, client = site_client(config, site)
        sid = _require_session(client, session_id)
        params = {"forced": str(forced).lower()}
        client.post_json("/inventory/v1/sessions/action/logoff", [sid], params=params)
        return {"action": "logoff", "session_ids": [sid], "forced": forced, "status": "submitted"}

    @mcp.tool()
    def logoff_user(username: str, forced: bool = False, site: Optional[str] = None) -> dict:
        """Log off all of a user's active sessions. Resolves their sessions then
        logs each off. Use when a user has hung/disconnected and needs their
        session terminated."""
        _, client = site_client(config, site)
        sessions = lookup_user_session(client, username, active_only=False)
        sids = [s["id"] for s in sessions if s.get("id")]
        if not sids:
            return {"username": username, "status": "no session found"}
        params = {"forced": str(forced).lower()}
        client.post_json("/inventory/v1/sessions/action/logoff", sids, params=params)
        return {"username": username, "status": "logoff submitted", "session_ids": sids}

    @mcp.tool()
    def disconnect_session(session_id: str, site: Optional[str] = None) -> dict:
        """Disconnect a user session (leaves the desktop running, detaches the
        display). Equivalent to closing the Horizon client without logging off."""
        _, client = site_client(config, site)
        sid = _require_session(client, session_id)
        client.post_json("/inventory/v1/sessions/action/disconnect", [sid])
        return {"action": "disconnect", "session_ids": [sid], "status": "submitted"}

    @mcp.tool()
    def send_session_message(session_id: str, message: str, message_type: str = "INFORMATION",
                             site: Optional[str] = None) -> dict:
        """Send a popup/balloon message to a user's session (e.g. 'Please save
        your work -- maintenance in 5 minutes'). message_type is
        INFORMATION, WARNING or ERROR."""
        _, client = site_client(config, site)
        sid = _require_session(client, session_id)
        body = {"message": message, "message_type": message_type.upper(), "session_ids": [sid]}
        client.post_json("/inventory/v1/sessions/action/send-message", body)
        return {"action": "send-message", "session_ids": [sid], "status": "submitted"}
