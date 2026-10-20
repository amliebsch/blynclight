"""Command line for the Blynclight Teams service."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from blynclight_teams import __version__
from blynclight_teams.config import load_config, require_client_id, save_config
from blynclight_teams.device import discover, open_lights, project_root
from blynclight_teams.errors import BlynclightError, ConfigError
from blynclight_teams.graph import GraphClient, resolve_tenant
from blynclight_teams.presence import LightState, appearance_for, describe, named_state
from blynclight_teams.service import render_systemd_unit, serve, sync_once

log = logging.getLogger("blynclight_teams")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blynclight-teams",
        description="Drive an Embrava Blynclight from Microsoft Teams presence.",
    )
    parser.add_argument("--version", action="version", version=f"blynclight-teams {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="sign in to Microsoft Graph with a device code")
    login.add_argument("--client-id", help="Entra application (client) id")
    login.add_argument(
        "--tenant",
        help="tenant id or directory domain, such as contoso.com",
    )
    login.add_argument(
        "--upn",
        help="work email; the directory is the domain after @",
    )
    login.set_defaults(func=cmd_login)

    run = sub.add_parser("run", help="follow Teams presence until stopped")
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", help="read presence once and set the light")
    once.set_defaults(func=cmd_once)

    color = sub.add_parser("color", help="set a color without talking to Teams")
    color.add_argument("color", help="name (available, busy, dnd, away, ...) or red 0-255")
    color.add_argument("green", nargs="?", type=int)
    color.add_argument("blue", nargs="?", type=int)
    color.set_defaults(func=cmd_color)

    off = sub.add_parser("off", help="turn the light off")
    off.set_defaults(func=cmd_off)

    demo = sub.add_parser("demo", help="cycle the presence colors, then turn the light off")
    demo.set_defaults(func=cmd_demo)

    status = sub.add_parser("status", help="show the light and the last presence reading")
    status.set_defaults(func=cmd_status)

    install = sub.add_parser("install", help="install the systemd user service")
    install.add_argument(
        "--enable",
        action="store_true",
        help="enable and start the service after installing it",
    )
    install.set_defaults(func=cmd_install)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (BlynclightError, ConfigError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


def cmd_login(args: argparse.Namespace) -> int:
    config = load_config()
    if args.client_id:
        config.client_id = args.client_id.strip()
    if args.upn and args.tenant:
        raise ConfigError("Pass --upn or --tenant, not both.")
    supplied = (args.upn or args.tenant or "").strip()
    if supplied:
        config.tenant = resolve_tenant(supplied)
        print(f"Directory {config.tenant}")
    require_client_id(config)
    save_config(config)
    _token, presence, account = GraphClient(config).login()
    who = f" as {account}" if account else ""
    state = appearance_for(presence.availability, presence.activity)
    print(f"Signed in{who}.")
    print(f"Presence is {presence.label}, which maps to {describe(state)}.")
    print("Start the service with: blynclight-teams install --enable")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    config = require_client_id(load_config())
    stop = threading.Event()

    def handle(signum: int, _frame: object) -> None:
        log.info("signal %s, turning the light off", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    return serve(config, stop)


def cmd_once(args: argparse.Namespace) -> int:
    config = require_client_id(load_config())
    client = GraphClient(config)
    presence = client.get_presence()
    state = appearance_for(presence.availability, presence.activity)
    print(f"{presence.label} -> {describe(state)}")
    with open_lights() as lights:
        lights.apply(state)
    return 0


def cmd_color(args: argparse.Namespace) -> int:
    if args.green is None and args.blue is None:
        state = named_state(args.color)
        if state is None:
            raise BlynclightError(
                f"Unknown color {args.color!r}. "
                "Use a name such as available, busy, call, dnd, away, presenting, "
                "or three numbers: red green blue."
            )
    else:
        if args.green is None or args.blue is None:
            raise BlynclightError("Pass a color name, or all three of red green blue.")
        try:
            red = int(args.color)
        except ValueError as exc:
            raise BlynclightError("When passing numbers, red green and blue are each 0..255.") from exc
        if not all(0 <= value <= 255 for value in (red, args.green, args.blue)):
            raise BlynclightError("red, green, and blue must each be 0..255.")
        state = LightState(red, args.green, args.blue)
    with open_lights() as lights:
        lights.apply(state)
    print(describe(state))
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    with open_lights() as lights:
        lights.off()
    print("off")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    names = ["available", "away", "busy", "call", "presenting", "dnd", "offline"]
    with open_lights() as lights:
        try:
            for name in names:
                state = named_state(name)
                assert state is not None
                lights.apply(state)
                print(f"{name}: {describe(state)}")
                time.sleep(1.2)
        finally:
            lights.off()
    print("off")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    found = discover()
    if found:
        for item in found:
            print(f"light: {item.label}")
    else:
        print("light: not found")
    if config.token_path.is_file():
        print(f"session: {config.token_path}")
    else:
        print("session: not signed in")
    if config.client_id:
        print(f"client_id: {config.client_id}")
        print(f"tenant: {config.tenant}")
    else:
        print("client_id: not set")
    status_path = config.status_path
    if status_path.is_file():
        try:
            payload = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"last presence: unreadable ({exc})")
        else:
            print(
                "last presence: "
                f"{payload.get('availability')}/{payload.get('activity')} "
                f"-> {payload.get('light')} at {payload.get('updated')}"
            )
    else:
        print("last presence: the service has not reported one yet")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "blynclight-teams.service"
    unit_path.write_text(render_systemd_unit())
    reload = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if reload.returncode != 0:
        print("systemctl --user daemon-reload did not succeed. The unit file is installed.")
    rules = project_root() / "udev" / "60-blynclight.rules"
    print(f"Installed {unit_path}")
    print("Install the udev rule, then unplug and replug the light:")
    print(f"  sudo install -m 0644 {rules} /etc/udev/rules.d/60-blynclight.rules")
    print("  sudo udevadm control --reload")
    print("  sudo udevadm trigger --subsystem-match=hidraw")
    if args.enable:
        config = load_config()
        if not config.client_id or not config.token_path.is_file():
            raise BlynclightError(
                "Sign in before enabling the service:\n"
                "  blynclight-teams login --client-id <application-id>"
            )
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "blynclight-teams.service"],
            check=False,
        )
        return result.returncode
    print("After login and the udev rule:")
    print("  systemctl --user enable --now blynclight-teams.service")
    return 0
