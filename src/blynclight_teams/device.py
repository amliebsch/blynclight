"""HID reports for the Embrava Blynclight family.

The device enumerates as a vendor-defined HID with an 8-byte output
report and no report id. hidraw still wants a leading 0 report number,
so each write is 9 bytes. On the wire the light sees:

    red, blue, green, control, 0, 0, 0xff, 0x22

Color order on the wire is red, blue, green. Callers pass red, green, blue.
The trailing 0xff 0x22 marks the end of the command. Current firmware
ignores a report that omits it. The control byte is:

    bit 0  off    (1 turns the LED off)
    bit 1  dim
    bit 2  flash
    bits 3-5  flash speed, stored as 1, 2, or 4

A speed of 0 leaves the LED dark even when off is clear, so steady colors
still send speed 1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from blynclight_teams.errors import (
    BlynclightError,
    DeviceIOError,
    DeviceNotFound,
    DevicePermissionError,
)
from blynclight_teams.presence import LightState

# Product ids that speak this report. Headsets and the desk nameplate do not.
SUPPORTED: dict[tuple[int, int], str] = {
    (0x2C0D, 0x0001): "Blynclight",
    (0x2C0D, 0x0002): "Blynclight Plus",
    (0x2C0D, 0x0003): "Blynclight Mini",
    (0x2C0D, 0x000A): "Blynclight Mini",
    (0x2C0D, 0x000C): "Blynclight",
    (0x2C0D, 0x0010): "Blynclight Plus",
    (0x0E53, 0x2516): "Blynclight",
    (0x0E53, 0x2517): "Blynclight Mini",
    (0x0E53, 0x2519): "Blynclight Mini",
}


def build_report(
    red: int,
    green: int,
    blue: int,
    *,
    off: bool = False,
    dim: bool = False,
    flash: bool = False,
    speed: int = 1,
) -> bytes:
    """Build the 9-byte hidraw buffer for one color update."""
    for name, value in (("red", red), ("green", green), ("blue", blue)):
        if not 0 <= value <= 255:
            raise ValueError(f"{name} must be 0..255, got {value}")
    if speed not in (1, 2, 4):
        raise ValueError(f"speed must be 1, 2, or 4, got {speed}")

    word = (red << 40) | (blue << 32) | (green << 24) | (speed << 19)
    if off:
        word |= 1 << 16
    if dim:
        word |= 1 << 17
    if flash:
        word |= 1 << 18
    return bytes([0]) + word.to_bytes(6, "big") + bytes((0xFF, 0x22))


@dataclass(frozen=True)
class DiscoveredLight:
    path: Path
    vendor_id: int
    product_id: int
    name: str

    @property
    def label(self) -> str:
        model = SUPPORTED.get((self.vendor_id, self.product_id), self.name)
        return f"{model} {self.vendor_id:04x}:{self.product_id:04x} at {self.path}"


def parse_hid_uevent(text: str) -> tuple[int, int, str] | None:
    """Return vendor id, product id, and HID name from a hidraw uevent."""
    vendor = product = None
    name = ""
    for line in text.splitlines():
        if line.startswith("HID_ID="):
            parts = line.split("=", 1)[1].split(":")
            if len(parts) != 3:
                return None
            try:
                vendor = int(parts[1], 16)
                product = int(parts[2], 16)
            except ValueError:
                return None
        elif line.startswith("HID_NAME="):
            name = line.split("=", 1)[1]
    if vendor is None or product is None:
        return None
    return vendor, product, name


def discover(sysfs: Path = Path("/sys/class/hidraw")) -> list[DiscoveredLight]:
    """Find plugged-in lights this report format can drive."""
    found: list[DiscoveredLight] = []
    if not sysfs.is_dir():
        return found
    for node in sorted(sysfs.iterdir()):
        uevent = node / "device" / "uevent"
        try:
            parsed = parse_hid_uevent(uevent.read_text())
        except OSError:
            continue
        if parsed is None:
            continue
        vendor, product, name = parsed
        if (vendor, product) not in SUPPORTED:
            continue
        found.append(
            DiscoveredLight(
                path=Path("/dev") / node.name,
                vendor_id=vendor,
                product_id=product,
                name=name or SUPPORTED[(vendor, product)],
            )
        )
    return found


class Light:
    """One open hidraw handle."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self._fd: int | None = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as exc:
            raise DevicePermissionError(_permission_message(path)) from exc
        except FileNotFoundError as exc:
            raise DeviceNotFound(f"{path} disappeared before it could be opened.") from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Light:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def set_color(
        self,
        red: int,
        green: int,
        blue: int,
        *,
        off: bool = False,
        dim: bool = False,
        flash: bool = False,
    ) -> None:
        speed = 2 if flash else 1
        try:
            report = build_report(
                red,
                green,
                blue,
                off=off,
                dim=dim and not off,
                flash=flash and not off,
                speed=speed,
            )
        except ValueError as exc:
            raise BlynclightError(str(exc)) from exc
        self._write(report)

    def apply(self, state: LightState) -> None:
        self.set_color(
            state.red,
            state.green,
            state.blue,
            off=state.off,
            dim=state.dim,
            flash=state.flash,
        )

    def off(self) -> None:
        self.set_color(0, 0, 0, off=True)

    def _write(self, report: bytes) -> None:
        if self._fd is None:
            raise DeviceIOError(f"{self.path} is closed")
        try:
            written = os.write(self._fd, report)
        except OSError as exc:
            raise DeviceIOError(f"write to {self.path} failed: {exc}") from exc
        if written != len(report):
            raise DeviceIOError(f"short write to {self.path}: {written} of {len(report)} bytes")


class Lights:
    """Every Blynclight currently plugged in."""

    def __init__(self, lights: list[Light]):
        self._lights = lights

    def apply(self, state: LightState) -> None:
        self._each(lambda light: light.apply(state))

    def off(self) -> None:
        self._each(lambda light: light.off())

    def close(self) -> None:
        for light in self._lights:
            light.close()
        self._lights = []

    def __enter__(self) -> Lights:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _each(self, action) -> None:
        failures: list[DeviceIOError] = []
        for light in self._lights:
            try:
                action(light)
            except DeviceIOError as exc:
                failures.append(exc)
        if failures and len(failures) == len(self._lights):
            raise failures[0]


def open_lights(sysfs: Path = Path("/sys/class/hidraw")) -> Lights:
    found = discover(sysfs)
    if not found:
        raise DeviceNotFound(
            "No Embrava Blynclight found. Plug it in and check `lsusb` for 2c0d:000c."
        )
    opened: list[Light] = []
    denied: DevicePermissionError | None = None
    for item in found:
        try:
            opened.append(Light(item.path))
        except DevicePermissionError as exc:
            denied = exc
    if not opened:
        if denied is not None:
            raise denied
        raise DeviceNotFound("A Blynclight was enumerated and then disappeared.")
    return Lights(opened)


def _permission_message(path: Path) -> str:
    rules = project_root() / "udev" / "60-blynclight.rules"
    return (
        f"Cannot open {path}: permission denied.\n"
        "Install the udev rule, then unplug and replug the light:\n"
        f"  sudo install -m 0644 {rules} /etc/udev/rules.d/60-blynclight.rules\n"
        "  sudo udevadm control --reload\n"
        "  sudo udevadm trigger --subsystem-match=hidraw"
    )


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def render_udev_rules() -> str:
    lines = [
        "# Grant the logged-in user write access to Embrava Blynclight hidraw nodes.",
        "# Install: sudo install -m 0644 60-blynclight.rules /etc/udev/rules.d/",
        "# Then: sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw",
        "",
    ]
    for (vendor, product), name in SUPPORTED.items():
        lines.append(f"# {name} {vendor:04x}:{product:04x}")
        lines.append(
            'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", '
            f'ATTRS{{idVendor}}=="{vendor:04x}", ATTRS{{idProduct}}=="{product:04x}", '
            'MODE="0660", TAG+="uaccess"'
        )
    lines.append("")
    return "\n".join(lines)
