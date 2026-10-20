"""Map Microsoft Graph presence to a Blynclight color.

Available is green, a plain busy status is orange, and an active call or
meeting is red. That keeps scheduled focus time distinct from time on the
phone. Do not disturb is purple, and away is a darker amber than the busy
orange. Offline turns the light off. Presenting flashes red. Idle
availability dims the same color.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LightState:
    red: int
    green: int
    blue: int
    off: bool = False
    dim: bool = False
    flash: bool = False

    def key(self) -> tuple[int, int, int, bool, bool, bool]:
        return (self.red, self.green, self.blue, self.off, self.dim, self.flash)


GREEN = LightState(0, 255, 0)
DIM_GREEN = LightState(0, 255, 0, dim=True)
RED = LightState(255, 0, 0)
FLASH_RED = LightState(255, 0, 0, flash=True)
ORANGE = LightState(255, 140, 0)
DIM_ORANGE = LightState(255, 140, 0, dim=True)
PURPLE = LightState(170, 0, 255)
AMBER = LightState(255, 96, 0)
OFF = LightState(0, 0, 0, off=True)

# Activity is the specific signal (in a call, presenting, out of office).
# Generic activities such as Available and Busy are left out so the
# availability value, including the Idle variants, can decide.
BY_ACTIVITY: dict[str, LightState] = {
    "InACall": RED,
    "InAConferenceCall": RED,
    "InAMeeting": RED,
    "Presenting": FLASH_RED,
    "OutOfOffice": PURPLE,
    "OffWork": OFF,
    "Inactive": AMBER,
    "Focusing": PURPLE,
    "UrgentInterruptionsOnly": PURPLE,
}

BY_AVAILABILITY: dict[str, LightState] = {
    "Available": GREEN,
    "AvailableIdle": DIM_GREEN,
    "Busy": ORANGE,
    "BusyIdle": DIM_ORANGE,
    "DoNotDisturb": PURPLE,
    "Focusing": PURPLE,
    "Away": AMBER,
    "BeRightBack": AMBER,
    "Offline": OFF,
    "OutOfOffice": PURPLE,
    "PresenceUnknown": OFF,
    "InACall": RED,
    "InAMeeting": RED,
    "Presenting": FLASH_RED,
}

_NAMES = {
    (0, 255, 0): "green",
    (255, 0, 0): "red",
    (255, 140, 0): "orange",
    (255, 96, 0): "amber",
    (170, 0, 255): "purple",
    (0, 0, 255): "blue",
    (255, 255, 255): "white",
}


def appearance_for(availability: str, activity: str) -> LightState:
    """Choose the light state for one Graph presence reading."""
    specific = _lookup(BY_ACTIVITY, activity)
    if specific is not None:
        return specific
    general = _lookup(BY_AVAILABILITY, availability)
    if general is not None:
        return general
    return OFF


def describe(state: LightState) -> str:
    if state.off:
        return "off"
    name = _NAMES.get(
        (state.red, state.green, state.blue),
        f"#{state.red:02x}{state.green:02x}{state.blue:02x}",
    )
    if state.flash:
        return f"flashing {name}"
    if state.dim:
        return f"dim {name}"
    return name


def named_state(name: str) -> LightState | None:
    """States a person can ask for from the command line."""
    key = name.strip().lower().replace("-", "").replace("_", "")
    table = {
        "available": appearance_for("Available", "Available"),
        "busy": appearance_for("Busy", "Busy"),
        "call": appearance_for("Busy", "InACall"),
        "meeting": appearance_for("Busy", "InAMeeting"),
        "dnd": appearance_for("DoNotDisturb", "DoNotDisturb"),
        "donotdisturb": appearance_for("DoNotDisturb", "DoNotDisturb"),
        "away": appearance_for("Away", "Away"),
        "brb": appearance_for("BeRightBack", "BeRightBack"),
        "presenting": appearance_for("DoNotDisturb", "Presenting"),
        "ooo": appearance_for("Available", "OutOfOffice"),
        "outofoffice": appearance_for("Available", "OutOfOffice"),
        "offline": appearance_for("Offline", "OffWork"),
        "off": OFF,
        "red": RED,
        "green": GREEN,
        "blue": LightState(0, 0, 255),
        "orange": ORANGE,
        "amber": AMBER,
        "purple": PURPLE,
        "white": LightState(255, 255, 255),
    }
    return table.get(key)


def _lookup(table: dict[str, LightState], key: str) -> LightState | None:
    if not key:
        return None
    if key in table:
        return table[key]
    folded = key.lower()
    for name, state in table.items():
        if name.lower() == folded:
            return state
    return None
