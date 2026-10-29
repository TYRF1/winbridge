"""Error taxonomy and process exit codes.

Exit codes are part of the command contract:

* ``0`` success
* ``1`` failure
* ``2`` usage error
* ``3`` unsupported environment
* ``4`` verify failed
"""

from __future__ import annotations

__all__ = [
    "UnsupportedEnvironment",
    "UsageError",
    "VerifyFailed",
    "WinBridgeError",
]


class WinBridgeError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code: int = 1


class UsageError(WinBridgeError):
    """The caller asked for something invalid."""

    exit_code = 2


class UnsupportedEnvironment(WinBridgeError):
    """The host cannot support the requested operation."""

    exit_code = 3


class VerifyFailed(WinBridgeError):
    """The smoke harness proved the app did not render."""

    exit_code = 4
