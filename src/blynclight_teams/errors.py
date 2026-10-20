class BlynclightError(Exception):
    """A failure the command line can print and exit on."""


class DeviceNotFound(BlynclightError):
    """No supported Blynclight is plugged in."""


class DevicePermissionError(BlynclightError):
    """The hidraw node exists, but this user cannot write it."""


class DeviceIOError(BlynclightError):
    """A write to the light failed."""


class NotSignedIn(BlynclightError):
    """Graph credentials are missing or no longer accepted."""


class GraphTemporaryError(BlynclightError):
    """A retryable Graph or network failure."""


class ConfigError(BlynclightError):
    """The local configuration is missing something required."""
