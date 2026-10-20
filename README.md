# Blynclight for Teams on Linux

A user-space service that turns an Embrava Blynclight to match your Microsoft Teams presence.

Copyright (C) 2026 Abraham Liebsch. Licensed under the [GNU General Public License v3.0](LICENSE) only.

A Blynclight Standard (`2c0d:000c`) and the other ids in `udev/60-blynclight.rules` are USB HID devices. The kernel's generic HID driver already binds them, and the color is an 8-byte output report. Embrava Connect, the Windows app, sends those same reports. It is not a kernel driver, and this project does not install one.

Teams presence is read from Microsoft Graph (`GET /me/presence`) with a one-time device-code sign-in. The service then polls and updates the light when your status changes.

| Teams presence | Light |
| --- | --- |
| Available | Green |
| Available, idle | Dim green |
| Busy, in a call, in a meeting | Red |
| Busy, idle | Dim red |
| Presenting | Flashing red |
| Do not disturb, focusing | Purple |
| Away, be right back | Amber |
| Out of office | Purple |
| Offline, appear offline | Off |

## Set up the Entra app

Presence is a delegated Graph permission, so the service signs in as you.

1. Open [App registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) and register a new app. Any organizational directory is fine. Leave the redirect URI empty.
2. Copy the **Application (client) ID**.
3. Under **Authentication**, set **Allow public client flows** to Yes.
4. Under **API permissions**, add Microsoft Graph delegated permissions **Presence.Read** and **User.Read**.
5. Grant admin consent for Presence.Read if your tenant requires it.

## Install

The hidraw node is root-only until a udev rule grants your login session access. Run this once, then unplug and replug the light:

```bash
sudo install -m 0644 udev/60-blynclight.rules /etc/udev/rules.d/60-blynclight.rules
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
```

Check that the hardware path works:

```bash
./bin/blynclight-teams color available
./bin/blynclight-teams demo
./bin/blynclight-teams off
```

Sign in, then install the user service:

```bash
./bin/blynclight-teams login --client-id <application-id> --upn you@company.com
./bin/blynclight-teams once
./bin/blynclight-teams install --enable
```

`--upn` is your work email. The directory is the part after `@`, looked up in Entra and saved as the tenant id. `--tenant` takes that id, or a domain such as `contoso.com`, directly. A single-tenant app registration rejects a login that does not name its directory.

`install` writes `~/.config/systemd/user/blynclight-teams.service` and, with `--enable`, starts it in your user session. The light turns off when the service stops. Logs go to the user journal:

```bash
journalctl --user -u blynclight-teams.service -f
```

The session file is `~/.config/blynclight-teams/token.json` (mode 0600). `poll_seconds` in `config.toml` defaults to 10 and must be from 5 to 300.

## Commands

```bash
./bin/blynclight-teams login --upn you@company.com
./bin/blynclight-teams run          # foreground poller
./bin/blynclight-teams once         # one Graph read, then set the light
./bin/blynclight-teams color busy   # available, away, dnd, presenting, call, ...
./bin/blynclight-teams color 255 80 0
./bin/blynclight-teams off
./bin/blynclight-teams status
```

Python 3.11 or newer is enough. There are no third-party packages.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
