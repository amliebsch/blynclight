"""Poll Teams presence and push the matching color to the light."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Protocol

from blynclight_teams.config import Config
from blynclight_teams.device import Lights, open_lights, project_root
from blynclight_teams.errors import (
    BlynclightError,
    DevicePermissionError,
    GraphTemporaryError,
    NotSignedIn,
)
from blynclight_teams.graph import GraphClient, Presence
from blynclight_teams.presence import LightState, appearance_for, describe

log = logging.getLogger("blynclight_teams")


class PresenceSource(Protocol):
    def get_presence(self) -> Presence: ...


class LightTarget(Protocol):
    def apply(self, state: LightState) -> None: ...


def sync_once(client: PresenceSource, lights: LightTarget) -> tuple[Presence, LightState]:
    presence = client.get_presence()
    state = appearance_for(presence.availability, presence.activity)
    lights.apply(state)
    return presence, state


def serve(config: Config, stop: threading.Event, client: GraphClient | None = None) -> int:
    """Follow presence until `stop` is set. Turns the light off on the way out."""
    client = client or GraphClient(config)
    lights: Lights | None = None
    last_key: tuple | None = None
    last_problem: str | None = None
    try:
        while not stop.is_set():
            try:
                presence = client.get_presence()
                state = appearance_for(presence.availability, presence.activity)
                key = (presence.label, state.key())
                if lights is None:
                    lights = open_lights()
                    log.info("light is open")
                if key != last_key:
                    lights.apply(state)
                    log.info("presence %s -> %s", presence.label, describe(state))
                    _write_status(config, presence, state)
                    last_key = key
                last_problem = None
            except BlynclightError as exc:
                level = logging.ERROR if isinstance(exc, (NotSignedIn, DevicePermissionError)) else logging.WARNING
                message = str(exc)
                if message != last_problem:
                    log.log(level, "%s", message)
                    last_problem = message
                if not isinstance(exc, GraphTemporaryError):
                    if lights is not None:
                        lights.close()
                    lights = None
                    last_key = None
            if stop.wait(config.poll_seconds):
                break
    finally:
        if lights is not None:
            try:
                lights.off()
            except BlynclightError as exc:
                log.warning("could not turn the light off: %s", exc)
            lights.close()
    return 0


def render_systemd_unit(root: Path | None = None) -> str:
    root = root or project_root()
    pythonpath = root / "src"
    return f"""[Unit]
Description=Embrava Blynclight follows Microsoft Teams presence
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={pythonpath}
ExecStart=/usr/bin/python3 -m blynclight_teams run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _write_status(config: Config, presence: Presence, state: LightState) -> None:
    path = config.status_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "availability": presence.availability,
        "activity": presence.activity,
        "light": describe(state),
        "updated": _utc_now(),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
