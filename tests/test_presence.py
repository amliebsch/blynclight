import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blynclight_teams.graph import Presence  # noqa: E402
from blynclight_teams.presence import appearance_for, describe  # noqa: E402
from blynclight_teams.service import sync_once  # noqa: E402


class AppearanceTests(unittest.TestCase):
    def test_teams_table(self) -> None:
        cases = {
            ("Available", "Available"): "green",
            ("Available", "OutOfOffice"): "purple",
            ("Busy", "Busy"): "orange",
            ("Busy", "InACall"): "red",
            ("Busy", "InAMeeting"): "red",
            ("Busy", "InAConferenceCall"): "red",
            ("DoNotDisturb", "DoNotDisturb"): "blue",
            ("DoNotDisturb", "Presenting"): "flashing red",
            ("Away", "Away"): "amber",
            ("BeRightBack", "BeRightBack"): "amber",
            ("Offline", "OffWork"): "off",
            ("Offline", "Offline"): "off",
            ("OutOfOffice", "OutOfOffice"): "purple",
            ("AvailableIdle", "Available"): "dim green",
            ("BusyIdle", "Busy"): "dim orange",
            ("AvailableIdle", "Inactive"): "amber",
            ("DoNotDisturb", "Focusing"): "blue",
            ("Focusing", "Focusing"): "blue",
            ("", ""): "off",
        }
        for (availability, activity), expected in cases.items():
            state = appearance_for(availability, activity)
            self.assertEqual(describe(state), expected, f"{availability}/{activity}")

    def test_activity_match_is_case_insensitive(self) -> None:
        self.assertEqual(describe(appearance_for("busy", "inacall")), "red")

    def test_sync_once_pushes_the_mapped_color(self) -> None:
        class Client:
            def get_presence(self) -> Presence:
                return Presence("DoNotDisturb", "Presenting")

        class Lights:
            def __init__(self) -> None:
                self.states = []

            def apply(self, state: object) -> None:
                self.states.append(state)

        lights = Lights()
        presence, state = sync_once(Client(), lights)
        self.assertEqual(presence.label, "DoNotDisturb/Presenting")
        self.assertEqual(describe(state), "flashing red")
        self.assertEqual(lights.states, [state])


if __name__ == "__main__":
    unittest.main()
