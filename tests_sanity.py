"""Offline sanity tests using a mocked requests transport.

Runs without a real Horizon server, verifying:
  * login request shape and bearer-token injection
  * paginated listing (get_all)
  * desktop-pool status aggregation
"""
import json
import unittest
from unittest import mock

from omnissa_horizon_mcp.client import HorizonClient
from omnissa_horizon_mcp.config import HorizonConfig


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        # Non-empty so client's "no content" short-circuit isn't triggered.
        self.content = b"{}" if payload is None else json.dumps(payload).encode()

    def json(self):
        return self._payload


def _ok(payload):
    return FakeResponse(200, payload)


def make_client():
    cfg = HorizonConfig(
        base_url="https://hs.example.com", username="svc", password="pw",
        domain="CORP", client_id="hs.example.com")
    return HorizonClient(cfg)


class HorizonClientTest(unittest.TestCase):

    def test_login_injects_bearer_and_parses_token(self):
        client = make_client()
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if url.endswith("/rest/login"):
                return _ok({"access_token": "AT", "refresh_token": "RT"})
            return _ok({})

        with mock.patch.object(client._session, "request", side_effect=fake_request):
            tok = client._ensure_token()
            self.assertEqual(tok, "AT")

        login = calls[0]
        self.assertEqual(login["method"], "POST")
        self.assertTrue(login["url"].endswith("/rest/login"))
        body = login.get("json")
        self.assertEqual(body["username"], "svc")
        self.assertEqual(body["domain"], "CORP")
        # Real Horizon servers expect a plain-string password (not an array).
        self.assertEqual(body["password"], "pw")

    def test_login_falls_back_to_array_on_mismatch(self):
        client = make_client()
        calls = []
        mismatch = FakeResponse(400, {
            "status": "BAD_REQUEST", "timestamp": 1234,
            "errors": [{"error_key": "attr.common.request.input.mismatch.error",
                          "error_message": "Value for password in the input request "
                                            "cannot be mapped to the corresponding field."}],
        })

        def fake_request(method, url, **kwargs):
            calls.append(kwargs.get("json", {}).get("password"))
            if isinstance(kwargs.get("json", {}).get("password"), list):
                return _ok({"access_token": "AT", "refresh_token": "RT"})
            return mismatch

        with mock.patch.object(client._session, "request", side_effect=fake_request):
            tok = client._ensure_token()
            self.assertEqual(tok, "AT")
        # First sent as string, then fell back to array on the mismatch.
        self.assertEqual(calls, ["pw", ["pw"]])

    def test_pagination_walks_until_short_page(self):
        client = make_client()
        sessions = [{"id": f"s{i}", "user_name": f"user{i}"} for i in range(250)]

        def fake_request(method, url, **kwargs):
            params = kwargs.get("params", {})
            page = params.get("page", 1)
            size = params.get("size")
            chunk = sessions[(page - 1) * size:(page - 1) * size + size]
            return _ok(chunk)

        with mock.patch.object(client._session, "request", side_effect=fake_request):
            client._access_token = "AT"
            client._token_expires_at = 1e18
            out = client.get_all("/inventory/v1/sessions", page_size=100)
        self.assertEqual(len(out), 250)

    def test_pool_status_report_builds(self):
        from omnissa_horizon_mcp.tools.pool_tools import build_pool_status_report, count_available

        pools = [{"id": "pool1", "name": "Sales", "type": "AUTOMATED", "source": "INSTANT_CLONE", "enabled": True}]
        machines = [
            {"id": "m1", "name": "m1", "desktop_pool_id": "pool1", "state": "AVAILABLE"},
            {"id": "m2", "name": "m2", "desktop_pool_id": "pool1", "state": "CONNECTED"},
            {"id": "m3", "name": "m3", "desktop_pool_id": "pool1", "state": "ERROR",
             "managed_machine_data": {"clone_error_message": "vCenter down"}},
        ]
        sessions = [
            {"id": "s1", "desktop_pool_id": "pool1", "machine_id": "m2",
             "session_state": "CONNECTED", "user_name": "a"},
        ]
        monitor = [{"id": "pool1", "status": "OK", "name": "Sales"}]

        # available = total minus those in use / in bad state
        self.assertEqual(count_available(machines, sessions), 1)

        client = make_client()

        def route(path, **kw):
            if path == "/inventory/v1/desktop-pools":
                return pools
            if path == "/inventory/v1/sessions":
                return sessions
            if path == "/inventory/v1/machines":
                return machines
            if path == "/monitor/desktops":
                return monitor
            return []

        client.get_all = mock.MagicMock(side_effect=route)
        report = build_pool_status_report(client, pool_id="pool1")
        self.assertEqual(report["summary"]["total_machines"], 3)
        self.assertEqual(report["summary"]["total_in_use_sessions"], 1)
        self.assertEqual(report["summary"]["total_errors"], 1)
        pool = report["pools"][0]
        self.assertEqual(pool["available_estimate"], 1)
        self.assertEqual(pool["monitor_status"], "OK")
        self.assertEqual(pool["error_count"], 1)

    def test_network_performance_parsing(self):
        from omnissa_horizon_mcp.tools.utilization_tools import _protocol_network

        client = make_client()
        client.get_json = mock.MagicMock(return_value={
            "pcoip_performance_data": {
                "network_rx_bandwidth": 1200,
                "network_tx_bandwidth": 3400,
                "network_tx_bandwidth_active_limit": 10000,
                "network_round_trip_latency": 18,
                "network_rx_packet_loss": 0,
                "network_tx_packet_loss": 1,
            },
            "blast_performance_data": {},
        })
        net = _protocol_network(client, "s1")
        self.assertEqual(net["protocol"], "PCoIP")
        self.assertEqual(net["estimated_bandwidth_kbps"], 1200)
        self.assertEqual(net["estimated_available_bandwidth_kbps"], 10000)
        self.assertEqual(net["latency_ms"], 18)

        # BLAST fallback
        client.get_json = mock.MagicMock(return_value={
            "pcoip_performance_data": {},
            "blast_performance_data": {"session_bandwidth_uplink": 25000},
        })
        net = _protocol_network(client, "s1")
        self.assertEqual(net["protocol"], "BLAST")
        self.assertEqual(net["estimated_bandwidth_kbps"], 25000)

        # Unavailable -> empty dict, no crash
        client.get_json = mock.MagicMock(side_effect=Exception("down"))
        self.assertEqual(_protocol_network(client, "s1"), {})


class FakeMCP:
    """Minimal stand-in for FastMCP that captures registered tool functions."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def registered(module, client_id):
    """Register a tool module against a fake MCP; return (tools, client)."""
    from omnissa_horizon_mcp.client import get_client

    cfg = HorizonConfig(
        base_url="https://hs.example.com", username="svc", password="pw",
        domain="CORP", client_id=client_id)
    mcp = FakeMCP()
    module.register(mcp, cfg)
    return mcp.tools, get_client(cfg)


class FilterGrammarTest(unittest.TestCase):
    """DEFECT 1: Horizon needs a JSON filter object, not `field 'value'`."""

    def test_build_filter_single_and_chain(self):
        from omnissa_horizon_mcp.tools._common import build_filter, equals_filter, contains_filter

        self.assertIsNone(build_filter([]))
        self.assertIsNone(build_filter([None]))
        one = json.loads(build_filter([equals_filter("state", "AVAILABLE")]))
        self.assertEqual(one, {"type": "Equals", "name": "state", "value": "AVAILABLE"})
        chain = json.loads(build_filter(
            [equals_filter("desktop_pool_id", "p1"), contains_filter("name", "pod")]))
        self.assertEqual(chain["type"], "And")
        self.assertEqual(len(chain["filters"]), 2)
        self.assertEqual(chain["filters"][0]["name"], "desktop_pool_id")
        self.assertEqual(chain["filters"][1], {"type": "Contains", "name": "name", "value": "pod"})

    def test_list_sessions_filter(self):
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "filter.sessions")
        captured = {}

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            captured["path"] = path
            captured["params"] = params
            return []

        client.get_all = fake_get_all
        tools["list_sessions"](username="aellsworth", pool_id="p1", session_state="CONNECTED")

        self.assertEqual(captured["path"], "/inventory/v7/sessions")
        obj = json.loads(captured["params"]["filter"])
        self.assertEqual(obj["type"], "And")
        self.assertIn({"type": "Contains", "name": "user_name", "value": "aellsworth"}, obj["filters"])
        self.assertIn({"type": "Equals", "name": "desktop_pool_id", "value": "p1"}, obj["filters"])
        self.assertIn({"type": "Equals", "name": "session_state", "value": "CONNECTED"}, obj["filters"])

    def test_list_sessions_no_filter_passes_none(self):
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "filter.sessions.none")
        captured = {}

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            captured["params"] = params
            return []

        client.get_all = fake_get_all
        tools["list_sessions"]()
        self.assertIsNone(captured["params"])

    def test_list_machines_filter(self):
        from omnissa_horizon_mcp.tools import machine_tools

        tools, client = registered(machine_tools, "filter.machines")
        captured = {}

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            captured["path"] = path
            captured["params"] = params
            return []

        client.get_all = fake_get_all
        tools["list_machines"](pool_id="p1", machine_name="pod", state="AVAILABLE")

        self.assertEqual(captured["path"], "/inventory/v1/machines")
        obj = json.loads(captured["params"]["filter"])
        self.assertIn({"type": "Equals", "name": "desktop_pool_id", "value": "p1"}, obj["filters"])
        self.assertIn({"type": "Contains", "name": "name", "value": "pod"}, obj["filters"])
        self.assertIn({"type": "Equals", "name": "state", "value": "AVAILABLE"}, obj["filters"])


class HelpdeskEndpointTest(unittest.TestCase):
    """DEFECT 2: helpdesk calls must use /helpdesk/v1/... with `session_id`."""

    def test_paths_are_v1(self):
        from omnissa_horizon_mcp.tools import utilization_tools as ut

        for path in (ut.HELPDESK_HISTORICAL_PATH, ut.HELPDESK_PROCESS_PATH,
                     ut.HELPDESK_DISPLAY_PATH, ut.HELPDESK_END_PROCESS_PATH):
            self.assertTrue(path.startswith("/helpdesk/v1/"), path)
            self.assertNotIn("internal_session_id", path)

    def test_session_processes_uses_session_id(self):
        from omnissa_horizon_mcp.tools import utilization_tools as ut

        tools, client = registered(ut, "helpdesk.process")
        calls = []
        client.get_json = lambda path, params=None: (calls.append((path, params)), [])[1]
        tools["session_processes"]("sid-1")
        self.assertEqual(calls[0][0], "/helpdesk/v1/performance/process")
        self.assertEqual(calls[0][1], {"session_id": "sid-1"})

    def test_session_utilization_uses_session_id(self):
        from omnissa_horizon_mcp.tools import utilization_tools as ut

        tools, client = registered(ut, "helpdesk.util")
        calls = []

        def fake_get_json(path, params=None):
            calls.append((path, params))
            return {} if path.endswith("display-protocol") else []

        client.get_json = fake_get_json
        tools["session_utilization"]("sid-2")
        paths = [c[0] for c in calls]
        self.assertIn("/helpdesk/v1/performance/historical-data", paths)
        self.assertIn("/helpdesk/v1/performance/display-protocol", paths)
        for _, params in calls:
            self.assertEqual(params, {"session_id": "sid-2"})

    def test_end_session_process_path_and_param(self):
        from omnissa_horizon_mcp.tools import utilization_tools as ut

        tools, client = registered(ut, "helpdesk.endproc")
        calls = []
        client.get_json = lambda path, params=None: (calls.append((path, params)), [])[1]
        client.post_json = lambda path, body=None, params=None: calls.append((path, params))
        tools["end_session_process"]("sid-3", 1234, "notepad.exe", create_time_stamp=99)
        post = calls[-1]
        self.assertEqual(post[0], "/helpdesk/v1/performance/remote-process/action/end-remote-process")
        self.assertEqual(post[1], {"session_id": "sid-3"})


class UserLookupTest(unittest.TestCase):
    """DEFECT 3: user lookup must use user_name on the versioned endpoint."""

    def test_server_side_filter_and_match(self):
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "lookup.ok")
        calls = []
        sessions = [
            {"id": "s1", "user_name": "WELLER\\aellsworth", "session_state": "CONNECTED"},
            {"id": "s2", "user_name": "WELLER\\bob", "session_state": "CONNECTED"},
        ]

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            calls.append((path, params))
            if params and "filter" in params:
                obj = json.loads(params["filter"])
                if obj.get("name") == "user_name":
                    return [sessions[0]]
            return sessions

        client.get_all = fake_get_all
        out = tools["find_user_session"]("WELLER\\aellsworth")
        self.assertEqual(out["found"], 1)
        self.assertEqual(out["sessions"][0]["session_id"], "s1")
        # A server-side filter on user_name must have been attempted.
        self.assertEqual(calls[0][0], "/inventory/v7/sessions")
        self.assertIn("filter", calls[0][1])
        self.assertIn("user_name", calls[0][1]["filter"])

    def test_bare_username_matches_domain_prefixed(self):
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "lookup.bare")
        sessions = [
            {"id": "s1", "user_name": "WELLER\\aellsworth", "session_state": "CONNECTED"},
        ]

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            if params and "filter" in params:
                return []  # force client-side fallback
            return sessions

        client.get_all = fake_get_all
        out = tools["find_user_session"]("aellsworth")
        self.assertEqual(out["found"], 1)

    def test_all_null_user_name_raises_actionable_error(self):
        from omnissa_horizon_mcp.client import HorizonError
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "lookup.null")
        sessions = [
            {"id": "s1", "user_name": None, "session_state": "CONNECTED"},
            {"id": "s2", "user_name": None, "session_state": "CONNECTED"},
        ]
        client.get_all = lambda path, params=None, page_size=100, max_items=10000: sessions
        with self.assertRaises(HorizonError) as ctx:
            tools["find_user_session"]("aellsworth")
        self.assertIn("privilege", str(ctx.exception).lower())

    def test_no_sessions_returns_zero_without_error(self):
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "lookup.empty")
        client.get_all = lambda path, params=None, page_size=100, max_items=10000: []
        out = tools["find_user_session"]("aellsworth")
        self.assertEqual(out["found"], 0)

    def test_rejected_filter_falls_back_to_scan(self):
        from omnissa_horizon_mcp.client import HorizonError
        from omnissa_horizon_mcp.tools import session_tools

        tools, client = registered(session_tools, "lookup.fallback")
        sessions = [
            {"id": "s1", "user_name": "WELLER\\aellsworth", "session_state": "CONNECTED"},
        ]

        def fake_get_all(path, params=None, page_size=100, max_items=10000):
            if params and "filter" in params:
                raise HorizonError(400, "filter not supported")
            return sessions

        client.get_all = fake_get_all
        out = tools["find_user_session"]("aellsworth")
        self.assertEqual(out["found"], 1)


class SiteResolutionTest(unittest.TestCase):
    """Multi-site (primary + DR) config resolution."""

    def base_cfg(self):
        from omnissa_horizon_mcp.config import HorizonConfig
        return HorizonConfig(
            base_url="https://cloud-a.example.com", username="svc", password="pw",
            domain="WELLER", site_name="primary", site_role="primary",
            sites=[{"name": "secondary", "base_url": "https://cloud-b.example.com",
                    "role": "secondary", "verify_ssl": False}])

    def test_resolve_primary_secondary_and_alias(self):
        from omnissa_horizon_mcp.config import resolve_site
        cfg = self.base_cfg()
        self.assertEqual(resolve_site(cfg, None).base_url, "https://cloud-a.example.com")
        self.assertEqual(resolve_site(cfg, "primary").base_url, "https://cloud-a.example.com")
        self.assertEqual(resolve_site(cfg, "dr").base_url, "https://cloud-b.example.com")
        self.assertEqual(resolve_site(cfg, "secondary").base_url, "https://cloud-b.example.com")
        self.assertFalse(resolve_site(cfg, "secondary").verify_ssl)
        self.assertEqual(resolve_site(cfg, "secondary").username, "svc")  # inherited
        self.assertNotEqual(resolve_site(cfg, "secondary").client_id,
                            resolve_site(cfg, "primary").client_id)

    def test_iter_sites_and_unknown(self):
        from omnissa_horizon_mcp.config import iter_site_configs, resolve_site, list_sites_public
        cfg = self.base_cfg()
        self.assertEqual([c.site_name for c in iter_site_configs(cfg)], ["primary", "secondary"])
        self.assertEqual(len(list_sites_public(cfg)), 2)
        with self.assertRaises(ValueError):
            resolve_site(cfg, "nope")

    def test_redaction_masks_site_passwords(self):
        cfg = self.base_cfg()
        cfg.sites = [{"name": "secondary", "base_url": "https://cloud-b.example.com",
                      "password": "SECRET"}]
        r = cfg.redacted()
        self.assertEqual(r["password"], "***REDACTED***")
        self.assertEqual(r["sites"][0]["password"], "***REDACTED***")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_site_inherits_pool_image_filters(self):
        from omnissa_horizon_mcp.config import resolve_site
        cfg = self.base_cfg()
        cfg.pool_image_filters = [{"pool": "bos1", "require": "1GB"}]
        self.assertEqual(resolve_site(cfg, "secondary").pool_image_filters,
                         [{"pool": "bos1", "require": "1GB"}])


class PoolAdminToolsTest(unittest.TestCase):
    """Pool enable/provisioning + snapshot-rollback tools."""

    def test_enable_desktop_pool_posts_endpoints(self):
        from omnissa_horizon_mcp.tools import pool_tools
        tools, client = registered(pool_tools, "pool.enable")
        calls = []
        client.get_all = lambda path, **kw: [{"id": "p1", "name": "Sales"}]
        client.get_one = lambda path, **kw: {"enabled": False, "enable_provisioning": False,
                                             "provisioning_settings": {"compute_profile_num_cpus": 4}}
        client.post_json = lambda path, body=None, params=None: calls.append((path, body))
        out = tools["enable_desktop_pool"](pool_name="Sales")
        paths = [c[0] for c in calls]
        self.assertIn("/inventory/v1/desktop-pools/action/enable", paths)
        self.assertIn("/inventory/v1/desktop-pools/action/enable-provisioning", paths)
        self.assertEqual(calls[0][1], ["p1"])
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(out["actions"], ["enable", "enable-provisioning"])

    def test_parse_and_split_image_path(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        desc = ('Changing 6 user(s) to image "/dc/vm/Folder/Win11 TL Golden Image3 - '
                '/Microsoft Updates/Update 9/30/26". This task will start ...')
        self.assertEqual(pt._parse_image_path(desc),
                         "/dc/vm/Folder/Win11 TL Golden Image3 - /Microsoft Updates/Update 9/30/26")
        self.assertEqual(pt._split_image_path("/dc/vm/Folder/VM - /Snap A"),
                         ("/dc/vm/Folder/VM", "/Snap A"))
        self.assertIsNone(pt._parse_image_path("no image here"))

    def _routing_gets(self, current_snapshot_id="snap-B"):
        def route(path, params=None, **kw):
            if path == "/inventory/v1/desktop-pools":
                return [{"id": "p1", "name": "tl1-vdi"}]
            if path.endswith("/tasks"):
                return [
                    {"id": "t2", "operation": "PUSH_IMAGE", "schedule_time": 2000,
                     "description": 'Changing 6 user(s) to image "/dc/vm/Folder/VM - /Snap B".'},
                    {"id": "t1", "operation": "PUSH_IMAGE", "schedule_time": 1000,
                     "description": 'Changing 6 user(s) to image "/dc/vm/Folder/VM - /Snap A".'},
                ]
            if path == "/external/v1/base-vms":
                return [{"id": "vm-1", "path": "/dc/vm/Folder/VM"}]
            if path == "/external/v1/base-snapshots":
                return [{"id": "snap-B", "path": "/Snap B"}, {"id": "snap-A", "path": "/Snap A"}]
            return []
        return route

    def test_select_previous_image_skips_current(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()
        client.get_all = self._routing_gets()
        pool = {"id": "p1", "vcenter_id": "vc1",
                "provisioning_settings": {"parent_vm_id": "vm-1", "base_snapshot_id": "snap-B"}}
        hist = [{"image_path": "/dc/vm/Folder/VM - /Snap B"},
                {"image_path": "/dc/vm/Folder/VM - /Snap A"}]
        sel = pt._select_previous_image(client, pool, hist)
        self.assertEqual(sel["current_image_path"], "/dc/vm/Folder/VM - /Snap B")
        self.assertEqual(sel["previous_image_path"], "/dc/vm/Folder/VM - /Snap A")
        self.assertEqual(sel["selection_basis"], "push_history")

    def test_select_previous_falls_back_to_chronology(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()

        def route(path, params=None, **kw):
            if path == "/external/v1/base-vms":
                return [{"id": "vm-1", "path": "/dc/vm/Folder/VM"}]
            if path == "/external/v1/base-snapshots":
                return [{"id": "snap-A", "path": "/Snap A", "created_timestamp": 1000},
                        {"id": "snap-B", "path": "/Snap B", "created_timestamp": 2000}]
            return []
        client.get_all = route
        pool = {"id": "p1", "vcenter_id": "vc1",
                "provisioning_settings": {"parent_vm_id": "vm-1", "base_snapshot_id": "snap-B"}}
        sel = pt._select_previous_image(client, pool, [])  # no push history
        self.assertEqual(sel["selection_basis"], "snapshot_chronology")
        self.assertEqual(sel["target"]["snapshot_id"], "snap-A")
        self.assertEqual(sel["target"]["parent_vm_id"], "vm-1")
        self.assertEqual(sel["target"]["path"], "/dc/vm/Folder/VM - /Snap A")

    def test_select_previous_none_when_current_snapshot_gone(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()

        def route(path, params=None, **kw):
            if path == "/external/v1/base-vms":
                return [{"id": "vm-1", "path": "/dc/vm/Folder/VM"}]
            if path == "/external/v1/base-snapshots":
                return [{"id": "snap-X", "path": "/Snap X", "created_timestamp": 1000}]
            return []
        client.get_all = route
        pool = {"id": "p1", "vcenter_id": "vc1",
                "provisioning_settings": {"parent_vm_id": "vm-1", "base_snapshot_id": "snap-GONE"}}
        sel = pt._select_previous_image(client, pool, [])
        self.assertIsNone(sel["target"])
        self.assertTrue(any("not found" in w for w in sel["warnings"]))

    def _detail(self):
        return {"id": "p1", "name": "tl1-vdi", "vcenter_id": "vc1",
                "enabled": True, "enable_provisioning": True, "stop_provisioning_on_error": True,
                "provisioning_settings": {"parent_vm_id": "vm-1", "base_snapshot_id": "snap-B",
                                          "compute_profile_num_cpus": 4,
                                          "compute_profile_num_cores_per_socket": 4,
                                          "compute_profile_ram_mb": 12288}}

    def test_rollback_dry_run_resolves_previous_and_preserves_compute(self):
        from omnissa_horizon_mcp.tools import pool_tools
        tools, client = registered(pool_tools, "pool.rb.dry")
        client.get_all = self._routing_gets()
        client.get_one = lambda path, **kw: self._detail()
        posted = []
        client.post_json = lambda path, body=None, params=None: posted.append((path, body))
        out = tools["rollback_desktop_pool_image"](pool_name="tl1-vdi")  # default dry run
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(posted, [])
        self.assertEqual(out["compute_profile_preserved"],
                         {"num_cpus": 4, "num_cores_per_socket": 4, "ram_mb": 12288})
        self.assertEqual(out["target_previous_image"]["snapshot_id"], "snap-A")
        self.assertEqual(out["target_previous_image"]["parent_vm_id"], "vm-1")
        self.assertEqual(out["request_body"]["logoff_policy"], "WAIT_FOR_LOGOFF")

    def test_rollback_confirm_posts_schedule_push_image(self):
        from omnissa_horizon_mcp.tools import pool_tools
        tools, client = registered(pool_tools, "pool.rb.confirm")
        client.get_all = self._routing_gets()
        client.get_one = lambda path, **kw: self._detail()
        posted = []
        client.post_json = lambda path, body=None, params=None: posted.append((path, body))
        out = tools["rollback_desktop_pool_image"](pool_name="tl1-vdi", confirm=True,
                                                   logoff_policy="FORCE_LOGOFF")
        self.assertEqual(out["status"], "push_submitted")
        self.assertEqual(len(posted), 1)
        path, body = posted[0]
        self.assertEqual(path, "/inventory/v1/desktop-pools/p1/action/schedule-push-image")
        self.assertEqual(body["snapshot_id"], "snap-A")
        self.assertEqual(body["parent_vm_id"], "vm-1")
        self.assertEqual(body["logoff_policy"], "FORCE_LOGOFF")
        # Compute profile is not part of the push body -> preserved on the pool.
        self.assertNotIn("compute_profile_num_cpus", body)

    def test_rollback_no_history_returns_actionable_status(self):
        from omnissa_horizon_mcp.tools import pool_tools
        tools, client = registered(pool_tools, "pool.rb.nohist")

        def route(path, params=None, **kw):
            if path == "/inventory/v1/desktop-pools":
                return [{"id": "p1", "name": "tl1-vdi"}]
            if path == "/external/v1/base-vms":
                return [{"id": "vm-1", "path": "/dc/vm/Folder/VM"}]
            if path == "/external/v1/base-snapshots":
                return [{"id": "snap-B", "path": "/Snap B"}]
            return []  # no tasks
        client.get_all = route
        client.get_one = lambda path, **kw: self._detail()
        client.post_json = lambda path, body=None, params=None: None
        out = tools["rollback_desktop_pool_image"](pool_name="tl1-vdi")
        self.assertEqual(out["status"], "cannot_determine_previous_image")
        self.assertTrue(out["warnings"])

    # -- shared-golden-image snapshot-name filter (bos1 -> 1GB, bos2 -> 2GB) --
    def _bos_route(self):
        snaps = [
            {"id": "snap-2gb", "name": "Browser Updates 2GB",
             "path": "/Browser Updates 2GB", "created_timestamp": 1000},
            {"id": "snap-1gb", "name": "Browser Updates 1GB - ACTUALLY",
             "path": "/Browser Updates 2GB/Browser Updates 1GB - ACTUALLY",
             "created_timestamp": 2000},
            {"id": "snap-2gb2", "name": "Officeconnect Update 2GB",
             "path": "/Browser Updates 2GB/Browser Updates 1GB - ACTUALLY/Officeconnect Update 2GB",
             "created_timestamp": 3000},
        ]

        def route(path, params=None, **kw):
            if path == "/external/v1/base-vms":
                return [{"id": "vm-1", "path": "/dc/vm/BOS"}]
            if path == "/external/v1/base-snapshots":
                return snaps
            return []
        return route

    def _bos_pool(self, name, cur_snap):
        return {"id": "p1", "name": name, "vcenter_id": "vc1",
                "provisioning_settings": {"parent_vm_id": "vm-1", "base_snapshot_id": cur_snap}}

    def test_snapshot_allowed_ignores_parent_path(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        snap = {"name": "Browser Updates 1GB - ACTUALLY",
                "path": "/Browser Updates 2GB/Browser Updates 1GB - ACTUALLY"}
        self.assertTrue(pt._snapshot_allowed(snap, ["1GB"]))
        # "2GB" only appears in the parent path -> must NOT satisfy a 2GB rule.
        self.assertFalse(pt._snapshot_allowed(snap, ["2GB"]))

    def test_chronology_filter_keeps_pool_family(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()
        client.get_all = self._bos_route()
        # bos2 is on the newest 2GB snapshot; the previous 2GB one is snap-2gb.
        sel = pt._select_previous_image(client, self._bos_pool("bos2-vdi", "snap-2gb2"),
                                        [], [{"pool": "bos2", "require": "2GB"}])
        self.assertEqual(sel["selection_basis"], "snapshot_chronology")
        self.assertEqual(sel["target"]["snapshot_id"], "snap-2gb")
        self.assertEqual(sel["image_filter"], ["2GB"])

    def test_chronology_filter_excludes_other_family(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()
        client.get_all = self._bos_route()
        # bos1 is on a 1GB snapshot; the only earlier one is 2GB -> no target.
        sel = pt._select_previous_image(client, self._bos_pool("bos1-vdi", "snap-1gb"),
                                        [], [{"pool": "bos1", "require": "1GB"}])
        self.assertIsNone(sel["target"])
        self.assertTrue(any("requirement" in w for w in sel["warnings"]))

    def test_chronology_without_filter_can_pick_wrong_family(self):
        from omnissa_horizon_mcp.tools import pool_tools as pt
        client = mock.MagicMock()
        client.get_all = self._bos_route()
        sel = pt._select_previous_image(client, self._bos_pool("bos2-vdi", "snap-2gb2"), [])
        # Without a filter the naive "most recent older" is the 1GB snapshot.
        self.assertEqual(sel["target"]["snapshot_id"], "snap-1gb")


if __name__ == "__main__":
    unittest.main(verbosity=2)