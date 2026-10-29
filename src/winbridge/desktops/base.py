"""Internal desktop adapter contract.

Adapters are a deliberately closed set: there is no plugin API and no dynamic
loading. An adapter observes windows and captures frames so ``verify`` can
prove that an app rendered; the pin methods only exist on desktops that can
host the omarchy-shell taskbar.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from winbridge.core.errors import WinBridgeError

__all__ = ["Desktop", "DesktopError", "Runner", "Window", "subprocess_runner"]

#: A command runner: receives one argument list and returns captured stdout.
#: Implementations must never invoke a shell.
Runner = Callable[[list[str]], str]


class DesktopError(WinBridgeError):
    """A desktop tool is missing or returned output the adapter cannot read."""


@dataclass(frozen=True, slots=True)
class Window:
    """One mapped toplevel window, in compositor coordinates.

    ``opacity`` is the compositor's effective window opacity in ``0..1`` when
    the adapter can read it. ``None`` means unknown, not fully opaque: verify
    must not infer transparency from a missing value.
    """

    id: str
    pid: int
    title: str
    class_name: str
    x: int
    y: int
    width: int
    height: int
    opacity: float | None = None


class Desktop(Protocol):
    """The operations every desktop adapter must provide to the core services."""

    name: str

    @property
    def capabilities(self) -> set[str]:
        """Capability names (``windows``, ``screenshot``, ``pin``) actually available."""
        ...

    def windows(self) -> list[Window]:
        """Return the mapped windows in compositor order."""
        ...

    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None:
        """Wait for a window owned by any pid in ``pid_tree``; ``None`` on timeout."""
        ...

    def screenshot(self, window: Window, dest: Path) -> None:
        """Capture ``window`` to ``dest`` as an image the verifier can read."""
        ...

    def screenshot_screen(self, dest: Path) -> None:
        """Capture the whole desktop to ``dest`` for a background comparison.

        Best-effort: ``verify`` continues without the diff when this fails or an
        adapter does not implement it.
        """
        ...

    def pin(self, desktop_id: str, match: str) -> bool:
        """Pin a bare desktop id; return whether the bar configuration changed."""
        ...

    def unpin(self, desktop_id: str) -> bool:
        """Remove the pin for a bare desktop id; return whether anything changed."""
        ...


_TOOL_TIMEOUT_S = 30.0


def subprocess_runner(args: list[str]) -> str:
    """Run one command without a shell and return its stdout.

    A non-zero exit or a missing binary is a :class:`DesktopError` carrying the
    tool's own diagnostics: a desktop tool that fails must be visible in a bug
    report, never silently treated as empty output. A wedged compositor query is
    bounded by ``_TOOL_TIMEOUT_S``.
    """
    if not args:
        raise DesktopError("cannot run an empty command")
    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopError(
            f"{Path(args[0]).name} exceeded its {_TOOL_TIMEOUT_S:g}s timeout"
        ) from exc
    except OSError as exc:
        raise DesktopError(f"cannot run {args[0]}: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        message = f"{Path(args[0]).name} exited {completed.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise DesktopError(message)
    return completed.stdout
