import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blynclight_teams.device import (  # noqa: E402
    SUPPORTED,
    build_report,
    discover,
    parse_hid_uevent,
    project_root,
    render_udev_rules,
)
from blynclight_teams.service import render_systemd_unit  # noqa: E402


class ReportTests(unittest.TestCase):
    def test_solid_red_matches_the_published_layout(self) -> None:
        # Leading 0 is the hidraw report number. The light sees the next 8 bytes:
        # red, blue, green, control (speed 1 == 0x08), pad, pad, 0xff, 0x22.
        self.assertEqual(build_report(255, 0, 0).hex(), "00ff0000080000ff22")

    def test_green_and_blue_are_not_swapped_on_the_wire(self) -> None:
        self.assertEqual(build_report(0, 255, 0).hex(), "000000ff080000ff22")
        self.assertEqual(build_report(0, 0, 255).hex(), "0000ff00080000ff22")
        # Call order is red, green, blue. Byte order is red, blue, green.
        self.assertEqual(build_report(1, 2, 3).hex(), "00010302080000ff22")

    def test_off_dim_and_flash_bits(self) -> None:
        self.assertEqual(build_report(255, 0, 0, off=True).hex(), "00ff0000090000ff22")
        self.assertEqual(build_report(0, 255, 0, dim=True).hex(), "000000ff0a0000ff22")
        self.assertEqual(
            build_report(255, 0, 0, flash=True, speed=4).hex(),
            "00ff0000240000ff22",
        )

    def test_speed_must_be_a_firmware_value(self) -> None:
        for speed in (0, 3, 8):
            with self.assertRaises(ValueError):
                build_report(0, 0, 0, speed=speed)

    def test_parse_uevent(self) -> None:
        text = "DRIVER=hid-generic\nHID_ID=0003:00002C0D:0000000C\nHID_NAME=Blynclight Standard\n"
        self.assertEqual(parse_hid_uevent(text), (0x2C0D, 0x000C, "Blynclight Standard"))

    def test_discover_filters_other_hid_devices(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp)
            light = sysfs / "hidraw7" / "device"
            light.mkdir(parents=True)
            (light / "uevent").write_text(
                "HID_ID=0003:00002C0D:0000000C\nHID_NAME=Blynclight Standard\n"
            )
            other = sysfs / "hidraw0" / "device"
            other.mkdir(parents=True)
            (other / "uevent").write_text("HID_ID=0003:0000046D:0000C52B\nHID_NAME=Logitech\n")
            found = discover(sysfs)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, Path("/dev/hidraw7"))
        self.assertEqual((found[0].vendor_id, found[0].product_id), (0x2C0D, 0x000C))
        self.assertIn("Blynclight", found[0].label)

    def test_udev_rules_cover_every_supported_id(self) -> None:
        text = (project_root() / "udev" / "60-blynclight.rules").read_text()
        generated = render_udev_rules()
        for vendor, product in SUPPORTED:
            needle_vendor = f'ATTRS{{idVendor}}=="{vendor:04x}"'
            needle_product = f'ATTRS{{idProduct}}=="{product:04x}"'
            self.assertIn(needle_vendor, text)
            self.assertIn(needle_product, text)
            self.assertIn(needle_vendor, generated)
            self.assertIn(needle_product, generated)

    def test_systemd_unit_points_at_the_package(self) -> None:
        text = render_systemd_unit(Path("/proj"))
        self.assertIn("Environment=PYTHONPATH=/proj/src", text)
        self.assertIn("ExecStart=/usr/bin/python3 -m blynclight_teams run", text)
        self.assertIn("WantedBy=default.target", text)


if __name__ == "__main__":
    unittest.main()
