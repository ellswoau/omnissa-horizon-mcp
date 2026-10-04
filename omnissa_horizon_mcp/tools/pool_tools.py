"""Desktop pool tools.

Read/health:
  * ``list_desktop_pools`` / ``desktop_pool_status`` -- pool inventory & health.

Maintenance / recovery (for a pool stopped by errors, or a bad image push):
  * ``enable_desktop_pool`` / ``disable_desktop_pool`` -- re-enable (or disable)
    the pool and its provisioning.
  * ``desktop_pool_push_history`` -- the recent PUSH_IMAGE tasks for a pool.
  * ``rollback_desktop_pool_image`` -- re-push the *previous* golden-image
    snapshot to a pool, keeping its current compute profile (vCPUs, cores per
    socket, RAM).

All tools accept an optional ``site`` (primary/secondary/dr/name); read-only
tools also accept ``site="all"``.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from collections import Counter, defaultdict

from ..client import HorizonClient, HorizonError, get_client
from ._common import site_client, site_clients

POOLS_PATH = "/inventory/v1/desktop-pools"
POOL_TASKS_PATH = "/inventory/v1/desktop-pools/{id}/tasks"
POOL_DETAIL_PATH = "/inventory/v7/desktop-pools/{id}"
ACTION_ENABLE = "/inventory/v1/desktop-pools/action/enable"
ACTION_DISABLE = "/inventory/v1/desktop-pools/action/disable"
ACTION_ENABLE_PROV = "/inventory/v1/desktop-pools/action/enable-provisioning"
ACTION_DISABLE_PROV = "/inventory/v1/desktop-pools/action/disable-provisioning"
ACTION_SCHEDULE_PUSH = "/inventory/v1/desktop-pools/{id}/action/schedule-push-image"
BASE_VMS_PATH = "/external/v1/base-vms"
BASE_SNAPSHOTS_PATH = "/external/v1/base-snapshots"

_IMAGE_PATH_RE = re.compile(r'to image\s+"([^"]+)"')


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
    pools = client.get_all(POOLS_PATH, page_size=200)
    if pool_id:
        pools = [p for p in pools if p.get("id") == pool_id]
    elif pool_name:
        pools = [p for p in pools if (p.get("name") or "").lower() == pool_name.lower()
                 or (p.get("display_name") or "").lower() == pool_name.lower()]
    if not pools:
        return {"status": "no matching pools", "pool_id": pool_id, "pool_name": pool_name}

    sessions = client.get_all("/inventory/v1/sessions", page_size=200)
    sessions_by_pool = defaultdict(list)
    for s in sessions:
        sessions_by_pool[s.get("desktop_pool_id")].append(s)

    machines = []
    try:
        machines = client.get_all("/inventory/v1/machines", page_size=200)
    except Exception:
        machines = []
    machines_by_pool = defaultdict(list)
    for m in machines:
        machines_by_pool[m.get("desktop_pool_id")].append(m)

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


# --------------------------------------------------------------------- helpers
def _resolve_pool(client, pool_id: Optional[str] = None,
                  pool_name: Optional[str] = None) -> Optional[dict]:
    """Resolve a desktop pool by id or (case-insensitive) name/display name."""
    if not pool_id and not pool_name:
        return None
    pools = client.get_all(POOLS_PATH, page_size=200)
    if pool_id:
        for p in pools:
            if p.get("id") == pool_id:
                return p
    if pool_name:
        want = pool_name.strip().lower()
        for p in pools:
            if (p.get("name") or "").lower() == want or (p.get("display_name") or "").lower() == want:
                return p
    return None


def _pool_detail(client, pool_id: str) -> dict:
    return client.get_one(POOL_DETAIL_PATH.format(id=pool_id))


def _pool_state(client, pool_id: str) -> dict:
    """Compact provisioning/enabled state of a pool (from the v7 model)."""
    d = _pool_detail(client, pool_id)
    ps = d.get("provisioning_settings") or {}
    sd = d.get("provisioning_status_data") or {}
    return {
        "enabled": d.get("enabled"),
        "enable_provisioning": d.get("enable_provisioning"),
        "stop_provisioning_on_error": d.get("stop_provisioning_on_error"),
        "source": d.get("source"),
        "image_source": d.get("image_source"),
        "parent_vm_id": ps.get("parent_vm_id"),
        "base_snapshot_id": ps.get("base_snapshot_id"),
        "im_stream_id": ps.get("im_stream_id"),
        "im_tag_id": ps.get("im_tag_id"),
        "compute_profile": _compute_profile(ps),
        "current_image_state": sd.get("instant_clone_current_image_state"),
        "current_operation": sd.get("instant_clone_operation"),
        "last_provisioning_error": sd.get("last_provisioning_error"),
    }


def _compute_profile(ps: dict) -> dict:
    """Extract the pool's compute profile (v7 ``provisioning_settings``)."""
    return {
        "num_cpus": ps.get("compute_profile_num_cpus"),
        "num_cores_per_socket": ps.get("compute_profile_num_cores_per_socket"),
        "ram_mb": ps.get("compute_profile_ram_mb"),
    }


def _pool_tasks(client, pool_id: str) -> list:
    return client.get_all(POOL_TASKS_PATH.format(id=pool_id))


def _parse_image_path(description: Optional[str]) -> Optional[str]:
    """Extract the image path from a PUSH_IMAGE task description.

    e.g. 'Changing 6 user(s) to image "/dc/vm/Folder/VM - /Snapshot".' -> path.
    """
    m = _IMAGE_PATH_RE.search(description or "")
    return m.group(1) if m else None


def _split_image_path(path: str):
    """Split an image path into (base_vm_path, snapshot_path)."""
    if path and " - " in path:
        vm_path, snap_path = path.split(" - ", 1)
        return vm_path, snap_path
    return path, None


def _resolve_base_vm(client, vcenter_id: str, vm_path: Optional[str] = None,
                     vm_id: Optional[str] = None) -> Optional[dict]:
    if not vcenter_id:
        return None
    for v in client.get_all(BASE_VMS_PATH, params={"vcenter_id": vcenter_id}):
        if (vm_id and v.get("id") == vm_id) or (vm_path and v.get("path") == vm_path):
            return v
    return None


def _resolve_base_snapshot(client, vcenter_id: str, base_vm_id: str,
                           snap_path: Optional[str] = None,
                           snap_id: Optional[str] = None) -> Optional[dict]:
    if not (vcenter_id and base_vm_id):
        return None
    snaps = client.get_all(BASE_SNAPSHOTS_PATH,
                           params={"vcenter_id": vcenter_id, "base_vm_id": base_vm_id})
    for s in snaps:
        if snap_id and s.get("id") == snap_id:
            return s
        if snap_path and (s.get("path") == snap_path or s.get("name") == snap_path.strip("/")):
            return s
    return None


def _image_path_for_ids(client, vcenter_id, parent_vm_id, snapshot_id):
    """Resolve (parent_vm_id, snapshot_id) to a human image path, or None."""
    if not (vcenter_id and parent_vm_id and snapshot_id):
        return None
    bv = _resolve_base_vm(client, vcenter_id, vm_id=parent_vm_id)
    if not bv:
        return None
    snap = _resolve_base_snapshot(client, vcenter_id, parent_vm_id, snap_id=snapshot_id)
    if not snap:
        return None
    return f"{bv.get('path')} - {snap.get('path')}"


def _base_snapshots(client, vcenter_id, base_vm_id) -> list:
    if not (vcenter_id and base_vm_id):
        return []
    return client.get_all(BASE_SNAPSHOTS_PATH,
                          params={"vcenter_id": vcenter_id, "base_vm_id": base_vm_id})


def _build_history(client, pool_id: str) -> list:
    """PUSH_IMAGE history (newest first) with parsed image paths."""
    hist = []
    for t in sorted(_pool_tasks(client, pool_id),
                    key=lambda x: x.get("schedule_time") or 0, reverse=True):
        if t.get("operation") != "PUSH_IMAGE":
            continue
        path = _parse_image_path(t.get("description"))
        vm_path, snap_path = _split_image_path(path) if path else (None, None)
        hist.append({
            "task_id": t.get("id"),
            "schedule_time": t.get("schedule_time"),
            "description": t.get("description"),
            "image_path": path,
            "vm_path": vm_path,
            "snapshot_path": snap_path,
            "error_vmtask_count": t.get("error_vmtask_count"),
            "remaining_vmtask_count": t.get("remaining_vmtask_count"),
        })
    return hist


def _select_previous_image(client, pool: dict, history: list) -> dict:
    """Decide which previously-applied image to roll back to.

    Primary source is the pool's push history (the most recent distinct pushed
    image that is not the current one). If that image no longer exists on the
    golden image (its snapshot was deleted), fall back to the golden image's
    snapshot chronology -- the snapshot created immediately before the pool's
    current snapshot. Returns the resolved target ids plus how it was chosen.
    """
    ps = pool.get("provisioning_settings") or {}
    vc = pool.get("vcenter_id")
    cur_pvm = ps.get("parent_vm_id")
    cur_snap = ps.get("base_snapshot_id")
    cur_path = _image_path_for_ids(client, vc, cur_pvm, cur_snap)
    distinct = list(dict.fromkeys(h.get("image_path") for h in history if h.get("image_path")))

    warnings = []
    previous = None
    basis = None

    # 1) Push history first (what the pool was told to push, newest first).
    target_path = None
    if cur_path is not None:
        for p in distinct:
            if p != cur_path:
                target_path = p
                basis = "push_history"
                break
    elif len(distinct) >= 2:
        target_path = distinct[1]
        basis = "push_history"

    if target_path:
        vm_path, snap_path = _split_image_path(target_path)
        bv = _resolve_base_vm(client, vc, vm_path=vm_path)
        snap = _resolve_base_snapshot(client, vc, bv["id"], snap_path=snap_path) if bv else None
        if bv and snap:
            previous = {"parent_vm_id": bv["id"], "snapshot_id": snap["id"], "path": target_path}
        else:
            warnings.append(
                "The image named in the push history no longer exists on the "
                "golden image; falling back to snapshot chronology.")
            basis = None

    # 2) Fallback: previous snapshot on the same golden image by create time.
    if previous is None and vc and cur_pvm:
        vm_path = None
        bv = _resolve_base_vm(client, vc, vm_id=cur_pvm)
        if bv:
            vm_path = bv.get("path")
        snaps = _base_snapshots(client, vc, cur_pvm)
        cur = next((s for s in snaps if s.get("id") == cur_snap), None)
        if cur is None:
            warnings.append(
                "The pool's current snapshot was not found on its golden image "
                "(the image was replaced), so the previous snapshot cannot be "
                "determined automatically.")
        else:
            older = [s for s in snaps
                     if (s.get("created_timestamp") or 0) < (cur.get("created_timestamp") or 0)]
            older.sort(key=lambda s: s.get("created_timestamp") or 0)
            if older:
                prev = older[-1]
                previous = {
                    "parent_vm_id": cur_pvm,
                    "snapshot_id": prev.get("id"),
                    "path": f"{vm_path} - {prev.get('path')}" if vm_path else prev.get("name"),
                }
                basis = "snapshot_chronology"
            else:
                warnings.append("No earlier snapshot exists on the golden image to roll back to.")

    if previous is None:
        warnings.append("Pass snapshot_id (and parent_vm_id) explicitly to choose the target image.")

    return {
        "current_image_path": cur_path,
        "previous_image_path": (previous or {}).get("path"),
        "distinct_images": distinct,
        "target": previous,
        "selection_basis": basis,
        "warnings": warnings,
    }


def _schedule_push(client, pool_id: str, parent_vm_id: str, snapshot_id: str,
                   logoff_policy: str, *, add_virtual_tpm: bool = False,
                   stop_on_first_error: bool = True,
                   start_time: Optional[int] = None) -> dict:
    body = {
        "parent_vm_id": parent_vm_id,
        "snapshot_id": snapshot_id,
        "logoff_policy": logoff_policy,
        "add_virtual_tpm": bool(add_virtual_tpm),
        "stop_on_first_error": bool(stop_on_first_error),
    }
    if start_time:
        # Accept epoch seconds or milliseconds; Horizon expects milliseconds.
        body["start_time"] = int(start_time) if int(start_time) > 10 ** 12 else int(start_time) * 1000
    client.post_json(ACTION_SCHEDULE_PUSH.format(id=pool_id), body)
    return body


def register(mcp: "FastMCP", config: "HorizonConfig") -> None:
    @mcp.tool()
    def list_desktop_pools(site: Optional[str] = None) -> Union[list, dict]:
        """List desktop pools with id, name, type (AUTOMATED/MANUAL/RDS) and
        source (INSTANT_CLONE etc). ``site="all"`` returns a dict keyed by site
        name so the primary and DR sites can be compared."""
        if (site or "").strip().lower() == "all":
            return {name: [_pool_brief(p) for p in client.get_all(POOLS_PATH, page_size=200)]
                    for name, client in site_clients(config, "all")}
        _, client = site_client(config, site)
        return [_pool_brief(p) for p in client.get_all(POOLS_PATH, page_size=200)]

    @mcp.tool()
    def desktop_pool_status(pool_id: Optional[str] = None, pool_name: Optional[str] = None,
                            site: Optional[str] = None) -> dict:
        """Produce a helpdesk status report for desktop pool(s): total machines,
        available (powered-on & not in use), in-use session count, and any
        erroring machines / cloning errors. Provide a pool id or name; if
        neither is given, reports on all pools. ``site="all"`` reports every
        site (keyed by site name)."""
        if (site or "").strip().lower() == "all":
            return {"sites": {name: build_pool_status_report(client, pool_id=pool_id, pool_name=pool_name)
                              for name, client in site_clients(config, "all")}}
        name, client = site_client(config, site)
        report = build_pool_status_report(client, pool_id=pool_id, pool_name=pool_name)
        report["site"] = name
        return report

    @mcp.tool()
    def enable_desktop_pool(pool_name: Optional[str] = None, pool_id: Optional[str] = None,
                            site: Optional[str] = None, enable_provisioning: bool = True) -> dict:
        """Re-enable a desktop pool (and, by default, its provisioning) after it
        was disabled -- e.g. when provisioning stopped on an error. Provide a
        pool name or id. Intended as a ONE-TIME recovery action: re-check the
        pool's error cause first (see desktop_pool_status / list_machines) and
        do not call this repeatedly. Returns the pool state before and after."""
        name, client = site_client(config, site)
        pool = _resolve_pool(client, pool_id=pool_id, pool_name=pool_name)
        if not pool:
            return {"site": name, "status": "no matching pool", "pool_id": pool_id, "pool_name": pool_name}
        pid = pool["id"]
        before = _pool_state(client, pid)
        actions = []
        client.post_json(ACTION_ENABLE, [pid])
        actions.append("enable")
        if enable_provisioning:
            client.post_json(ACTION_ENABLE_PROV, [pid])
            actions.append("enable-provisioning")
        after = _pool_state(client, pid)
        return {"site": name, "status": "submitted", "pool_id": pid, "name": pool.get("name"),
                "actions": actions, "before": before, "after": after}

    @mcp.tool()
    def disable_desktop_pool(pool_name: Optional[str] = None, pool_id: Optional[str] = None,
                             site: Optional[str] = None, disable_provisioning: bool = True) -> dict:
        """Disable a desktop pool (and, by default, its provisioning) so no new
        machines are brokered/provisioned -- e.g. before maintenance. Provide a
        pool name or id. Returns the pool state before and after."""
        name, client = site_client(config, site)
        pool = _resolve_pool(client, pool_id=pool_id, pool_name=pool_name)
        if not pool:
            return {"site": name, "status": "no matching pool", "pool_id": pool_id, "pool_name": pool_name}
        pid = pool["id"]
        before = _pool_state(client, pid)
        actions = []
        if disable_provisioning:
            client.post_json(ACTION_DISABLE_PROV, [pid])
            actions.append("disable-provisioning")
        client.post_json(ACTION_DISABLE, [pid])
        actions.append("disable")
        after = _pool_state(client, pid)
        return {"site": name, "status": "submitted", "pool_id": pid, "name": pool.get("name"),
                "actions": actions, "before": before, "after": after}

    @mcp.tool()
    def desktop_pool_push_history(pool_name: Optional[str] = None, pool_id: Optional[str] = None,
                                  site: Optional[str] = None, limit: int = 20) -> dict:
        """Show a desktop pool's recent golden-image push history (PUSH_IMAGE
        tasks, newest first), including the image path each push targeted. Use
        this to see what image a pool is on and which snapshot to roll back to."""
        name, client = site_client(config, site)
        pool = _resolve_pool(client, pool_id=pool_id, pool_name=pool_name)
        if not pool:
            return {"site": name, "status": "no matching pool", "pool_id": pool_id, "pool_name": pool_name}
        pid = pool["id"]
        detail = _pool_detail(client, pid)
        ps = detail.get("provisioning_settings") or {}
        hist = _build_history(client, pid)[:max(1, int(limit))]
        return {
            "site": name, "pool_id": pid, "name": pool.get("name"),
            "current_image": {
                "parent_vm_id": ps.get("parent_vm_id"),
                "snapshot_id": ps.get("base_snapshot_id"),
                "path": _image_path_for_ids(client, detail.get("vcenter_id"),
                                            ps.get("parent_vm_id"), ps.get("base_snapshot_id")),
            },
            "compute_profile": _compute_profile(ps),
            "history_count": len(hist),
            "history": hist,
        }

    @mcp.tool()
    def rollback_desktop_pool_image(pool_name: Optional[str] = None, pool_id: Optional[str] = None,
                                    site: Optional[str] = None,
                                    logoff_policy: str = "WAIT_FOR_LOGOFF",
                                    start_time: Optional[int] = None,
                                    stop_on_first_error: Optional[bool] = None,
                                    snapshot_id: Optional[str] = None,
                                    parent_vm_id: Optional[str] = None,
                                    confirm: bool = False,
                                    history_limit: int = 20) -> dict:
        """Re-push the PREVIOUS golden-image snapshot to a desktop pool -- for
        rolling back a bad image update.

        The pool is named by ``pool_name`` (or ``pool_id``). The previous
        snapshot is taken from the pool's push history (the most recent distinct
        image that is not the current one); the push keeps the pool's current
        compute profile (vCPUs, cores per socket, RAM) unchanged.

        SAFETY: this is a disruptive maintenance action -- existing sessions are
        logged off and the pool is rebuilt from the image. It therefore defaults
        to a dry run (``confirm=False``) and only schedules the push when
        ``confirm=True``. ``logoff_policy`` is WAIT_FOR_LOGOFF (default) or
        FORCE_LOGOFF. Pass ``snapshot_id``/``parent_vm_id`` to override the
        snapshot chosen from history.
        """
        name, client = site_client(config, site)
        pool = _resolve_pool(client, pool_id=pool_id, pool_name=pool_name)
        if not pool:
            return {"site": name, "status": "no matching pool", "pool_id": pool_id, "pool_name": pool_name}
        pid = pool["id"]
        detail = _pool_detail(client, pid)
        ps = detail.get("provisioning_settings") or {}
        compute = _compute_profile(ps)
        history = _build_history(client, pid)[:max(1, int(history_limit))]

        # 1. Decide the target image (explicit override, else push history /
        #    golden-image snapshot chronology -- resolved per site, so each
        #    datacenter's own golden images are used).
        if snapshot_id:
            target_parent = parent_vm_id or ps.get("parent_vm_id")
            target_snapshot = snapshot_id
            target_path = _image_path_for_ids(client, detail.get("vcenter_id"),
                                              target_parent, target_snapshot)
            selection_basis = "explicit"
            warnings = []
        else:
            sel = _select_previous_image(client, detail, history)
            warnings = list(sel["warnings"])
            selection_basis = sel["selection_basis"]
            tgt = sel["target"]
            if not tgt:
                return {"site": name, "status": "cannot_determine_previous_image",
                        "pool_id": pid, "name": pool.get("name"),
                        "current_image_path": sel["current_image_path"],
                        "distinct_images": sel["distinct_images"],
                        "history": history, "warnings": warnings}
            target_parent = tgt["parent_vm_id"]
            target_snapshot = tgt["snapshot_id"]
            target_path = tgt["path"]

        # 2. Compute reproduction note: the push itself does not carry a compute
        #    profile, so the pool's current values are preserved. Surface them.
        if None in (compute.get("num_cpus"), compute.get("ram_mb")):
            warnings.append("Compute profile could not be read from the pool (fields null); "
                            "the push will still preserve whatever the pool currently has.")
        request_body = {
            "parent_vm_id": target_parent,
            "snapshot_id": target_snapshot,
            "logoff_policy": logoff_policy,
            "add_virtual_tpm": bool(ps.get("add_virtual_tpm") or False),
            "stop_on_first_error": bool(
                detail.get("stop_provisioning_on_error") if stop_on_first_error is None else stop_on_first_error),
        }
        if start_time:
            # Accept epoch seconds or milliseconds; Horizon expects milliseconds.
            _st = int(start_time)
            request_body["start_time"] = _st if _st > 10 ** 12 else _st * 1000

        plan = {
            "site": name, "pool_id": pid, "name": pool.get("name"),
            "current_image": {
                "parent_vm_id": ps.get("parent_vm_id"),
                "snapshot_id": ps.get("base_snapshot_id"),
                "path": _image_path_for_ids(client, detail.get("vcenter_id"),
                                            ps.get("parent_vm_id"), ps.get("base_snapshot_id")),
            },
            "target_previous_image": {
                "parent_vm_id": target_parent,
                "snapshot_id": target_snapshot,
                "path": target_path,
            },
            "compute_profile_preserved": compute,
            "request_body": request_body,
            "logoff_policy": logoff_policy,
            "selection_basis": selection_basis,
            "history": history,
            "warnings": warnings,
        }

        if not confirm:
            plan["status"] = "dry_run"
            plan["note"] = ("Re-run with confirm=True to schedule the push. Sessions "
                            "will be logged off per logoff_policy and the pool rebuilt "
                            "from the target image.")
            return plan

        _schedule_push(client, pid, target_parent, target_snapshot, logoff_policy,
                       add_virtual_tpm=request_body["add_virtual_tpm"],
                       stop_on_first_error=request_body["stop_on_first_error"],
                       start_time=start_time)
        after = _pool_state(client, pid)
        plan["status"] = "push_submitted"
        plan["after"] = after
        return plan
