"""Configuration and on-disk paths.

The application id is not a secret. The refresh token is, and it lives in
the token file with mode 0600, outside the project directory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from blynclight_teams.errors import ConfigError


def config_dir() -> Path:
    override = os.environ.get("BLYNCLIGHT_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "blynclight-teams"


def state_dir() -> Path:
    override = os.environ.get("BLYNCLIGHT_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "blynclight-teams"


@dataclass
class Config:
    client_id: str
    tenant: str = "organizations"
    poll_seconds: int = 10
    directory: Path | None = None
    status_directory: Path | None = None

    @property
    def config_path(self) -> Path:
        return (self.directory or config_dir()) / "config.toml"

    @property
    def token_path(self) -> Path:
        return (self.directory or config_dir()) / "token.json"

    @property
    def status_path(self) -> Path:
        return (self.status_directory or state_dir()) / "status.json"


def load_config(directory: Path | None = None) -> Config:
    directory = directory or config_dir()
    path = directory / "config.toml"
    data: dict = {}
    if path.is_file():
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must be a TOML table")
        data = loaded

    client_id = os.environ.get("BLYNCLIGHT_CLIENT_ID") or str(data.get("client_id") or "")
    tenant = os.environ.get("BLYNCLIGHT_TENANT") or str(data.get("tenant") or "organizations")
    poll_raw = os.environ.get("BLYNCLIGHT_POLL_SECONDS") or data.get("poll_seconds", 10)
    try:
        poll_seconds = int(poll_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"poll_seconds must be an integer, got {poll_raw!r}") from exc
    if not 5 <= poll_seconds <= 300:
        raise ConfigError("poll_seconds must be between 5 and 300")
    client_id = client_id.strip().strip("{}")
    tenant = tenant.strip() or "organizations"
    return Config(
        client_id=client_id,
        tenant=tenant,
        poll_seconds=poll_seconds,
        directory=directory,
    )


def require_client_id(config: Config) -> Config:
    if not config.client_id:
        raise ConfigError(
            "No Entra application id is configured.\n"
            "Create an app registration with delegated Presence.Read, enable "
            "public client flows, then run:\n"
            "  blynclight-teams login --client-id <application-id>"
        )
    return config


def save_config(config: Config) -> Path:
    directory = config.directory or config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    text = (
        f'client_id = "{_toml_string(config.client_id)}"\n'
        f'tenant = "{_toml_string(config.tenant)}"\n'
        f"poll_seconds = {config.poll_seconds}\n"
    )
    path.write_text(text)
    return path


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
