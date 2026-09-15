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
from typing import Optional

# Environment variable names
ENV_BASE_URL = "HORIZON_BASE_URL"
ENV_USERNAME = "HORIZON_USERNAME"
ENV_PASSWORD = "HORIZON_PASSWORD"
ENV_DOMAIN = "HORIZON_DOMAIN"
ENV_CLIENT_ID = "HORIZON_CLIENT_ID"
ENV_VERIFY_SSL = "HORIZON_VERIFY_SSL"
ENV_CONFIG_FILE = "HORIZON_CONFIG_FILE"
ENV_TIMEOUT = "HORIZON_TIMEOUT"

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

    def redacted(self) -> dict:
        """Return a dict safe for logging (password redacted)."""
        d = asdict(self)
        d["password"] = _PASSWORD_TAG if d.get("password") else ""
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