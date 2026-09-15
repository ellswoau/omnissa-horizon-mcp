"""Thin REST client for the Omnissa Horizon Server 2506 API.

Handles authentication (login/refresh), bearer-token injection, request
retries on expiry, error mapping, and list pagination.

API conventions (Horizon 2506):
  * Base path is ``/rest`` (Swagger ``servers: [{url: '/rest'}]``).
  * ``POST /rest/login`` body ``{username, password:[...], domain}`` returns
    ``{access_token, refresh_token}`` JWT bearer tokens.
  * Authenticated calls send ``Authorization: Bearer <access_token>``.
  * List endpoints accept ``page`` / ``size`` query params and return arrays.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from .config import HorizonConfig


class HorizonError(Exception):
    """Raised for Horizon API errors, carrying status + parsed detail."""

    def __init__(self, status: Optional[int], message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - helper
        return f"HorizonError(status={self.status}, message={self.message!r})"


class HorizonClient:
    """Stateful client bound to one Horizon Connection Server config."""

    BASE = "/rest"
    DEFAULT_PAGE_SIZE = 100

    def __init__(self, config: HorizonConfig):
        self.config = config
        self.base_url = config.base_url
        self.verify_ssl = config.verify_ssl
        self.timeout = config.timeout
        self._session = requests.Session()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._lock = threading.Lock()
        self._token_expires_at: float = 0.0
        self._debug = False

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _is_input_mismatch(resp: requests.Response) -> bool:
        """True if Horizon answers 400 with a 'cannot be mapped' input-mismatch
        error (i.e. the login body shape is wrong)."""
        if resp.status_code != 400:
            return False
        try:
            payload = resp.json()
        except ValueError:
            return False
        text = json.dumps(payload).lower()
        return "mismatch" in text or "cannot be mapped" in text

    def _login(self) -> None:
        base = {
            "username": self.config.username,
            "domain": self.config.domain,
        }
        url = f"{self.base_url}{self.BASE}/login"

        # Most real Horizon Connection Servers expect a plain-string password.
        # The 2506 swagger documents an array form (used for smart-card/cert
        # logins); some builds require it. Try string first, then fall back to
        # the array only for the specific "cannot be mapped" mismatch error.
        candidate_bodies = [
            {**base, "password": self.config.password},
            {**base, "password": [self.config.password]},
        ]

        last_resp = None
        for body in candidate_bodies:
            if self._debug:
                debug_body = dict(body)
                if "password" in debug_body:
                    debug_body["password"] = "*REDACTED*"
                print(f"[debug] POST {url}", flush=True)
                print(f"[debug]   headers: {dict(self._session.headers)}", flush=True)
                print(f"[debug]   body: {json.dumps(debug_body)}", flush=True)
            resp = self._session.post(
                url, json=body, timeout=self.timeout, verify=self.verify_ssl,
            )
            if resp.status_code < 400:
                last_resp = resp
                break
            # Only retry with the alternate shape on a type-mismatch; genuine
            # auth failures (401) or other errors should surface immediately.
            if not self._is_input_mismatch(resp):
                last_resp = resp
                break
            last_resp = resp

        self._raise_for(last_resp, url)
        data = last_resp.json()
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        if not self._access_token:
            raise HorizonError(
                last_resp.status_code,
                "Login succeeded but no access_token returned.",
            )
        # Horizon access tokens are short-lived; treat as ~8 minutes (420s).
        self._token_expires_at = time.time() + 420

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        url = f"{self.base_url}{self.BASE}/refresh"
        try:
            resp = self._session.post(
                url,
                json={"refresh_token": self._refresh_token},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if resp.status_code >= 200 and resp.status_code < 300:
                data = resp.json()
                self._access_token = data.get("access_token", self._access_token)
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                self._token_expires_at = time.time() + 420
                return True
        except requests.RequestException:
            pass
        return False

    def _ensure_token(self) -> str:
        with self._lock:
            if not self._access_token or time.time() >= self._token_expires_at:
                if self._access_token and self._refresh():
                    return self._access_token
                self._login()
            return self._access_token

    def logout(self) -> None:
        if not self._access_token:
            return
        try:
            self._session.post(
                f"{self.base_url}{self.BASE}/logout",
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException:
            pass
        finally:
            self._access_token = None

    # ------------------------------------------------------------ request core
    @staticmethod
    def _error_detail(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return resp.text[:500]

    def _raise_for(self, resp: requests.Response, url: str) -> None:
        if resp.status_code < 200 or resp.status_code >= 300:
            detail = self._error_detail(resp)
            msg = f"Horizon API {resp.status_code} for {url}: {detail if not isinstance(detail, dict) else json.dumps(detail)}"
            raise HorizonError(resp.status_code, msg, detail)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        retry_on_auth: bool = True,
    ) -> requests.Response:
        url = f"{self.base_url}{self.BASE}{path}"
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        # Token may have expired mid-flight: force a fresh login once.
        if resp.status_code == 401 and retry_on_auth:
            self._access_token = None
            token = self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        self._raise_for(resp, url)
        return resp

    def get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("GET", path, params=params)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def post_json(self, path: str, json_body: Any = None, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("POST", path, params=params, json_body=json_body)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------- pagination
    def get_all(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int = 10000,
    ) -> List[Any]:
        """Fetch all pages of a paginated list endpoint.

        Horizon list endpoints return plain arrays and use page/size. We walk
        pages until the server returns fewer than ``page_size`` items or we hit
        ``max_items`` (safety guard).
        """
        collected: List[Any] = []
        page = 1
        p = dict(params or {})
        while True:
            p["page"] = page
            p["size"] = page_size
            batch = self.get_json(path, params=p)
            if batch is None:
                batch = []
            if isinstance(batch, dict):
                # Some resources wrap arrays in an envelope; unwrap common keys.
                batch = batch.get("items") or batch.get("results") or batch.get("list") or []
            if not isinstance(batch, list):
                # Not a list (e.g. filter-returned object/generic). Return as-is.
                return collected + [batch] if batch else collected
            collected.extend(batch)
            if len(batch) < page_size or len(collected) >= max_items:
                break
            page += 1
        return collected[:max_items]

    def get_one(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Fetch a single resource; if an array is returned take the first item."""
        data = self.get_json(path, params=params)
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and return basic connectivity info (no sensitive data)."""
        self._ensure_token()
        return {
            "connected": True,
            "server": self.config.client_id,
            "user": self.config.username,
            "domain": self.config.domain,
        }


# Registry so tools can share one client per config (keyed by client_id).
_client_registry: Dict[str, HorizonClient] = {}
_registry_lock = threading.Lock()


def get_client(config: HorizonConfig) -> HorizonClient:
    key = config.client_id
    with _registry_lock:
        client = _client_registry.get(key)
        if client is None or client.config != config:
            client = HorizonClient(config)
            _client_registry[key] = client
        return client


def clear_client(key: str) -> None:
    with _registry_lock:
        client = _client_registry.pop(key, None)
    if client:
        try:
            client.logout()
        except Exception:
            pass