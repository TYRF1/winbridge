"""Internal backend contract.

Backends are deliberately a closed set: there is no plugin API and no dynamic
loading. A backend executes Windows payloads inside one managed prefix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from winbridge.core.errors import WinBridgeError

__all__ = ["Backend", "BackendError", "BackendTimeout"]


class BackendError(WinBridgeError):
    """A runner cannot execute the requested operation."""


class BackendTimeout(BackendError):
    """A runner command exceeded its hard timeout and was stopped."""


class Backend(Protocol):
    """The operations every backend must provide to the core services."""

    name: str

    def create_prefix(self, app_id: str, *, keep_z: bool = False) -> Path:
        """Create (or adopt) the prefix for ``app_id`` and return its path."""
        ...

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a Wine tool or Windows program and capture its output.

        ``timeout_s`` is a hard bound; the backend must stop the process group
        and raise :class:`BackendTimeout` when it expires.
        """
        ...

    def install(
        self,
        prefix: Path,
        installer: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        """Run a Windows installer synchronously and return its exit code.

        Contract: ``install(prefix, installer, args, *, log_path=None,
        timeout_s=None)``. When ``log_path`` is set, the installer's stdout and
        stderr are written there and the call must not inherit stdio. When it is
        ``None``, the installer inherits stdio so a GUI run stays usable.
        ``timeout_s`` is a hard bound enforced by the backend.
        """
        ...

    def run(self, prefix: Path, exe: str, argv: list[str]) -> subprocess.Popen[bytes]:
        """Start an installed program and return its live process handle."""
        ...

    def expose(self, prefix: Path, host: Path, guest: str) -> None:
        """Link a host folder into the prefix without replacing anything."""
        ...

    def remove(self, prefix: Path) -> None:
        """Remove a prefix owned by WinBridge; never user content."""
        ...
