"""Shared helpers for tool implementations."""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from ..client import HorizonClient, HorizonError
from ..config import HorizonConfig, iter_site_configs, resolve_site


def site_client(config: HorizonConfig, site: Optional[str] = None) -> Tuple[str, HorizonClient]:
    """Return ``(site_name, client)`` for a tool's optional ``site`` argument.

    ``site=None``/``primary`` targets the default connection server;
    ``secondary``/``dr`` or a configured site name targets that site.
    """
    cfg = resolve_site(config, site)
    from ..client import get_client
    return (cfg.site_name or "primary", get_client(cfg))


def site_clients(config: HorizonConfig, site: Optional[str] = None) -> List[Tuple[str, HorizonClient]]:
    """Return ``[(site_name, client)]`` for one site, or every site when
    ``site='all'`` (used by read-only listing/monitoring tools)."""
    if (site or "").strip().lower() == "all":
        from ..client import get_client
        return [((c.site_name or "primary"), get_client(c)) for c in iter_site_configs(config)]
    return [site_client(config, site)]

#: Inventory endpoint used to list sessions.
#:
#: ``/inventory/v1/sessions`` returns the base ``SessionInfo`` model, which has
#: NO ``user_name`` field (only the raw ``user_id`` SID), so it can neither be
#: filtered nor displayed by username. The versioned session endpoints add
#: ``user_name`` from v4 onwards (``Supported Filters: Equals, StartsWith,
#: Contains``); v7 is the current 2506 model.
SESSIONS_PATH = "/inventory/v7/sessions"


def build_filter(clauses: List[Optional[Dict]]) -> Optional[str]:
    """Build the Horizon ``filter`` query value from filter clauses.

    Horizon expects a JSON filter object (which ``requests`` URL-encodes for
    us), not the ``field 'value'`` string form::

        {"type": "Equals", "name": "desktop_pool_id", "value": "<id>"}

    A single clause is sent as-is; multiple clauses are combined with an
    ``"And"`` chain, per the Horizon Server REST Pagination, Filter and Sorting
    Guide. Returns ``None`` when there is nothing to filter.
    """
    clauses = [c for c in clauses if c]
    if not clauses:
        return None
    if len(clauses) == 1:
        return json.dumps(clauses[0], separators=(",", ":"))
    return json.dumps({"type": "And", "filters": clauses}, separators=(",", ":"))


def equals_filter(name: str, value) -> Dict:
    """A single-value ``Equals`` filter clause."""
    return {"type": "Equals", "name": name, "value": value}


def contains_filter(name: str, value) -> Dict:
    """A ``Contains`` filter clause (substring match)."""
    return {"type": "Contains", "name": name, "value": value}


def _normalise_user(value: str) -> str:
    """Normalise ``DOMAIN\\user`` / ``user@domain`` to ``domain/user`` lower."""
    return (value or "").strip().lower().replace("\\", "/")


def user_variants(username: str) -> set:
    """Return the set of normalised username forms to match a session against.

    Accepts ``user``, ``DOMAIN\\user`` and ``user@domain`` and yields the
    equivalent slash/at forms plus the bare account name, because Horizon
    reports ``user_name`` as ``DOMAIN\\user``.
    """
    base = _normalise_user(username)
    if not base:
        return set()
    variants = {base}
    if "/" in base:
        domain, _, user = base.partition("/")
        variants.add(user)
        variants.add(f"{user}@{domain}")
    elif "@" in base:
        user, _, domain = base.partition("@")
        variants.add(user)
        variants.add(f"{domain}/{user}")
    local = base.split("/")[-1].split("@")[0]
    if local:
        variants.add(local)
    return {v for v in variants if v}


def session_matches_user(session: Dict, variants: set) -> bool:
    """True if a session's ``user_name`` matches any of ``variants``."""
    key = _normalise_user(session.get("user_name"))
    if not key or not variants:
        return False
    if key in variants:
        return True
    if key.split("/")[-1].split("@")[0] in variants:
        return True
    return any(key.endswith("/" + v) for v in variants)


def lookup_user_session(
    client: HorizonClient,
    username: str,
    *,
    active_only: bool = True,
) -> List[Dict]:
    """Find sessions belonging to ``username``.

    Horizon usernames may be ``user``, ``DOMAIN\\user`` or ``user@domain``, and
    the ``user_name`` field is reported as ``DOMAIN\\user``. A server-side
    filter on ``user_name`` is attempted first; if the server rejects it (or
    returns nothing) the full session list is scanned client-side as a
    fallback.

    If sessions are visible but *none* of them expose a ``user_name``, a
    :class:`HorizonError` is raised explaining the likely missing privilege
    instead of silently reporting zero matches.
    """
    variants = user_variants(username)
    if not variants:
        return []

    raw = (username or "").strip()
    local = _normalise_user(username).split("/")[-1].split("@")[0]

    # Server-side attempts first: exact match on the value as supplied, then a
    # contains match on the bare account name (handles DOMAIN\user prefixes).
    attempts = [("Equals", raw)]
    if local and local.lower() != raw.lower():
        attempts.append(("Equals", local))
    if local:
        attempts.append(("Contains", local))

    sessions: Optional[List[Dict]] = None
    for op, value in attempts:
        params = {"filter": build_filter([{"type": op, "name": "user_name", "value": value}])}
        try:
            rows = client.get_all(SESSIONS_PATH, params=params, page_size=200)
        except HorizonError:
            # Filter unsupported / rejected -- fall through to the next attempt.
            continue
        sessions = rows
        if rows:
            break

    if not sessions:
        # Unfiltered scan (also covers servers that reject the user_name
        # filter outright).
        sessions = client.get_all(SESSIONS_PATH, page_size=200)

    if not sessions:
        return []

    if all(not s.get("user_name") for s in sessions):
        raise HorizonError(
            None,
            "Horizon returned no 'user_name' for any of the "
            f"{len(sessions)} visible session(s). This usually means the "
            "service account lacks the privilege needed to resolve session "
            "user names (e.g. MACHINE_VIEW on the session's access group). "
            "Check the Connection Server role/privileges of the service "
            "account.",
        )

    matches = []
    for s in sessions:
        if not session_matches_user(s, variants):
            continue
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
