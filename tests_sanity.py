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
        self.assertEqual(body["password"], ["pw"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)