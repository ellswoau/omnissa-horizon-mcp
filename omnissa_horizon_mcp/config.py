"""Configuration and secure credential handling for the Horizon MCP server.

Credentials can be supplied from (in order of precedence):
  1. Explicit keyword arguments (e.g. when called programmatically)
  2. Environment variables (HORIZON_*)
  3. A JSON config file (HORIZON_CONFIG_FILE, or --config)

Password values are never logged and config files are written with
0600 permissions when created via the ``horizon config init`` wizard.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Environment variable names
ENV_BASE_URL = "HORIZON_BASE_URL"
ENV_USERNAME = "HORIZON_USERNAME"
ENV_PASSWORD = "HORIZON_PASSWORD"
ENV_DOMAIN = "HORIZON_DOMAIN"
ENV_CLIENT_ID = "HORIZON_CLIENT_ID"
ENV_VERIFY_SSL = "HORIZON_VERIFY_SSL"
ENV_CONFIG_FILE = "HORIZON_CONFIG_FILE"
ENV_TIMEOUT = "HORIZON_TIMEOUT"
# Optional JSON array of extra sites (multi-site / DR). Each entry:
# {"name": "secondary", "base_url": "https://cloud-b.example.com",
#  "role": "secondary", "username"/"password"/"domain"/"verify_ssl"/"timeout"}
ENV_SITES = "HORIZON_SITES"
# Optional JSON array of per-pool snapshot-name filters for the image-rollback
# fallback, e.g. [{"pool": "bos1", "require": "1GB"}, {"pool": "bos2", "require": "2GB"}].
# A rule applies when its ``pool`` regex matches the pool name; a candidate
# snapshot is accepted only when its name contains the ``require`` text. Keeps
# pools that share one golden image from rolling back to another pool's image.
ENV_POOL_IMAGE_FILTERS = "HORIZON_POOL_IMAGE_FILTERS"
# Optional bearer token that gates the network MCP endpoints when set.
ENV_MCP_TOKEN = "HORIZON_MCP_AUTH_TOKEN"

_PASSWORD_TAG = "***REDACTED***"


@dataclass
class HorizonConfig:
    """Resolved configuration for a single Horizon Connection Server."""

    base_url: str = ""
    username: str = ""
    password: str = ""
    domain: str = ""
    # Optional friendly label for the connection server (used as cache key).
    client_id: str = ""
    verify_ssl: bool = True
    # Connection / request timeout in seconds.
    timeout: int = 30
    # Friendly name of this connection server (default/primary site).
    site_name: str = ""
    # Optional role tag for the default site: "primary" / "secondary" / "site".
    site_role: str = ""
    # Extra connection servers (multi-site / DR). Raw definitions; each entry
    # overrides the top-level credentials only where it sets them.
    sites: List[Dict[str, Any]] = field(default_factory=list)
    # Per-pool snapshot-name filters for the image-rollback fallback.
    pool_image_filters: List[Dict[str, Any]] = field(default_factory=list)

    def redacted(self) -> dict:
        """Return a dict safe for logging (password redacted)."""
        d = asdict(self)
        d["password"] = _PASSWORD_TAG if d.get("password") else ""
        d["sites"] = [
            {**s, "password": _PASSWORD_TAG if s.get("password") else ""}
            for s in (d.get("sites") or [])
        ]
        return d

    def is_complete(self) -> bool:
        return bool(self.base_url and self.username and self.password and self.domain)


class ConfigError(Exception):
    """Raised when configuration/credentials are missing or invalid."""


def _as_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    config_file: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    domain: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
    timeout: Optional[int] = None,
) -> HorizonConfig:
    """Load and merge configuration from kwargs, env and a config file.

    Raises :class:`ConfigError` if essential credentials are missing.
    """
    cfg = HorizonConfig()

    # 1. Load from well-known config file (env or explicit path).
    config_file = config_file or os.environ.get(ENV_CONFIG_FILE)
    if config_file and Path(config_file).exists():
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        for key in ("base_url", "username", "password", "domain",
                    "client_id", "verify_ssl", "timeout"):
            if key in data and data[key] is not None:
                setattr(cfg, key, data[key])
        if isinstance(data.get("sites"), list):
            cfg.sites = data["sites"]
        if isinstance(data.get("pool_image_filters"), list):
            cfg.pool_image_filters = data["pool_image_filters"]
        for key in ("site_name", "site_role"):
            if data.get(key):
                setattr(cfg, key, str(data[key]))

    # 2. Env variables override the file.
    if os.environ.get(ENV_BASE_URL):
        cfg.base_url = os.environ[ENV_BASE_URL].strip()
    if os.environ.get(ENV_USERNAME):
        cfg.username = os.environ[ENV_USERNAME].strip()
    if os.environ.get(ENV_PASSWORD):
        cfg.password = os.environ[ENV_PASSWORD]
    if os.environ.get(ENV_DOMAIN):
        cfg.domain = os.environ[ENV_DOMAIN].strip()
    if os.environ.get(ENV_CLIENT_ID):
        cfg.client_id = os.environ[ENV_CLIENT_ID].strip()
    if os.environ.get(ENV_VERIFY_SSL) is not None:
        cfg.verify_ssl = _as_bool(os.environ[ENV_VERIFY_SSL], True)
    if os.environ.get(ENV_TIMEOUT):
        try:
            cfg.timeout = int(os.environ[ENV_TIMEOUT])
        except ValueError:
            pass
    if os.environ.get(ENV_SITES):
        try:
            parsed = json.loads(os.environ[ENV_SITES])
            if isinstance(parsed, list):
                cfg.sites = parsed
        except json.JSONDecodeError:
            pass
    if os.environ.get(ENV_POOL_IMAGE_FILTERS):
        try:
            parsed = json.loads(os.environ[ENV_POOL_IMAGE_FILTERS])
            if isinstance(parsed, list):
                cfg.pool_image_filters = parsed
        except json.JSONDecodeError:
            pass

    # 3. Explicit arguments win.
    if base_url is not None:
        cfg.base_url = base_url.strip()
    if username is not None:
        cfg.username = username.strip()
    if password is not None:
        cfg.password = password
    if domain is not None:
        cfg.domain = domain.strip()
    if verify_ssl is not None:
        cfg.verify_ssl = bool(verify_ssl)
    if timeout is not None:
        cfg.timeout = int(timeout)

    cfg.base_url = cfg.base_url.rstrip("/")
    if not cfg.site_name:
        cfg.site_name = "primary"
    if not cfg.site_role:
        cfg.site_role = "primary"
    if not cfg.client_id:
        # Stable cache key derived from the server endpoint.
        cfg.client_id = cfg.base_url.replace("https://", "").replace("http://", "")

    if not cfg.is_complete():
        missing = [
            name for name, val in (
                ("base_url", cfg.base_url),
                ("username", cfg.username),
                ("password", cfg.password),
                ("domain", cfg.domain),
            ) if not val
        ]
        raise ConfigError(
            "Incomplete Horizon credentials. Missing: "
            + ", ".join(missing)
            + ". Set HORIZON_* env vars or run `python -m omnissa_horizon_mcp "
              "config init --config <file>`."
        )
    return cfg


_SITE_ROLE_ALIASES = {
    "primary": "primary", "prod": "primary", "production": "primary",
    "secondary": "secondary", "dr": "secondary", "disaster-recovery": "secondary",
    "disaster_recovery": "secondary", "standby": "secondary",
}


def _match_site(base: "HorizonConfig", site: str) -> Optional[Dict[str, Any]]:
    """Find a configured site definition by name or role alias."""
    key = (site or "").strip().lower()
    if not key:
        return None
    role = _SITE_ROLE_ALIASES.get(key)
    for s in base.sites or []:
        if key == str(s.get("name", "")).lower():
            return s
        srole = str(s.get("role", "")).lower()
        if role and _SITE_ROLE_ALIASES.get(srole) == role:
            return s
    return None


def _site_to_config(base: "HorizonConfig", site: Dict[str, Any]) -> "HorizonConfig":
    """Build a HorizonConfig for one site definition, inheriting defaults."""
    name = str(site.get("name") or site.get("role") or "site")
    base_url = (site.get("base_url") or base.base_url).rstrip("/")
    if base_url == base.base_url:
        # Same endpoint as the default server: reuse it (keeps one client).
        return base
    host = base_url.replace("https://", "").replace("http://", "")
    return HorizonConfig(
        base_url=base_url,
        username=site.get("username") or base.username,
        password=site.get("password") or base.password,
        domain=site.get("domain") or base.domain,
        client_id=f"{name}:{host}",
        verify_ssl=base.verify_ssl if site.get("verify_ssl") is None else bool(site["verify_ssl"]),
        timeout=int(site.get("timeout") or base.timeout),
        site_name=name,
        site_role=str(site.get("role") or "site"),
        pool_image_filters=base.pool_image_filters,
    )


def resolve_site(base: "HorizonConfig", site: Optional[str] = None) -> "HorizonConfig":
    """Resolve a tool's ``site`` argument to a concrete connection-server config.

    ``None``/``primary`` selects the default (or a site flagged primary),
    ``secondary``/``dr`` the DR site, or any configured site name. Raises
    ValueError naming the known sites when the request is unknown.
    """
    if not (site or "").strip():
        return base
    site_def = _match_site(base, site)
    if site_def is None and (site or "").strip().lower() in ("primary", "default"):
        return base
    if site_def is None:
        known = ", ".join(str(s.get("name", "?")) for s in (base.sites or [])) or "(none)"
        raise ValueError(f"Unknown site {site!r}. Configured sites: {known}")
    return _site_to_config(base, site_def)


def iter_site_configs(base: "HorizonConfig") -> List["HorizonConfig"]:
    """Return every configured site (default first), de-duplicated by endpoint."""
    out: List[HorizonConfig] = [base]
    seen = {base.base_url}
    for s in base.sites or []:
        cfg = _site_to_config(base, s)
        if cfg.base_url in seen:
            continue
        seen.add(cfg.base_url)
        out.append(cfg)
    return out


def list_sites_public(base: "HorizonConfig") -> List[Dict[str, Any]]:
    """Public (redacted) description of every configured site."""
    return [{
        "name": cfg.site_name or "primary",
        "role": cfg.site_role or "site",
        "base_url": cfg.base_url,
        "username": cfg.username,
        "domain": cfg.domain,
        "verify_ssl": cfg.verify_ssl,
    } for cfg in iter_site_configs(base)]


def configure_interactive(config_file: str) -> str:
    """Prompt securely for credentials and write a 0600 config file.

    The password is requested with ``getpass`` so it is never echoed to the
    terminal, and the resulting file is only readable by the owner.
    """
    import getpass

    data = {}
    # Seed from an existing file if present.
    p = Path(config_file).expanduser()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    print("Omnissa Horizon Connection Server\n--------------------------------")
    data["base_url"] = input(
        f"Connection Server URL [{data.get('base_url', '')}]: "
    ).strip() or data.get("base_url", "")
    data["domain"] = input(
        f"AD Domain [{data.get('domain', '')}]: "
    ).strip() or data.get("domain", "")
    data["username"] = input(
        f"Service account username [{data.get('username', '')}]: "
    ).strip() or data.get("username", "")
    data["password"] = getpass.getpass("Service account password: ") or data.get("password", "")
    data["verify_ssl"] = data.get("verify_ssl", True)

    if not (data.get("base_url") and data.get("username") and data.get("password") and data.get("domain")):
        raise ConfigError("All fields are required.")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    return str(p)