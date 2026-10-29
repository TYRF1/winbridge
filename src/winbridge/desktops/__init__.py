"""Desktop adapters and session detection.

The set is closed: there is no plugin API. :func:`detect_desktop` looks at the
session environment only — it never probes a socket, launches a tool, or reads a
window — so importing this package stays side-effect free. Tool availability
degrades each adapter's capabilities instead of selecting a different adapter.

Detection order is Hyprland signature, then a plain X11 session, then the
unsupported fallback. A Wayland session that is not Hyprland is unsupported on
purpose: claiming X11 through its XWayland display would produce misleading
window ids and screenshots.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn

from winbridge.core.errors import UnsupportedEnvironment
from winbridge.core.omarchy import DEFAULT_SHELL_CONFIG
from winbridge.desktops.base import Desktop, DesktopError, Window
from winbridge.desktops.hyprland import HyprlandDesktop
from winbridge.desktops.x11 import X11Desktop

__all__ = [
    "Desktop",
    "DesktopError",
    "HyprlandDesktop",
    "UnsupportedDesktop",
    "Window",
    "X11Desktop",
    "detect_desktop",
]


class UnsupportedDesktop:
    """A session with no window or screenshot adapter.

    ``setup``/``run``/``update``/``expose``/``unbridge`` still work. Verification
    cannot observe a window here, so every observation method raises
    :class:`UnsupportedEnvironment` (exit 3) rather than returning an empty list
    that would look like an app that never appeared. Pins are merely absent.
    """

    name = "unsupported"

    @property
    def capabilities(self) -> set[str]:
        """Offer nothing: there is no tool contract for this session."""
        return set()

    def windows(self) -> list[Window]:
        """Refuse window discovery; the session has no supported adapter."""
        _refuse()

    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None:
        """Refuse waiting; the session has no supported adapter."""
        _refuse()

    def screenshot(self, window: Window, dest: Path) -> None:
        """Refuse capture; the session has no supported adapter."""
        _refuse()

    def screenshot_screen(self, dest: Path) -> None:
        """Refuse background capture; the session has no supported adapter."""
        _refuse()

    def pin(self, desktop_id: str, match: str) -> bool:
        """Report that an unsupported session never pins."""
        return False

    def unpin(self, desktop_id: str) -> bool:
        """Report that an unsupported session had nothing to unpin."""
        return False


def _refuse() -> NoReturn:
    raise UnsupportedEnvironment(
        "desktop not supported: expected Hyprland or an X11 session"
    )


def detect_desktop(
    env: Mapping[str, str] = os.environ,
    shell_config: Path = DEFAULT_SHELL_CONFIG,
    which: Callable[[str], str | None] = shutil.which,
) -> Desktop:
    """Choose the adapter for the current session environment.

    ``HYPRLAND_INSTANCE_SIGNATURE`` is authoritative even when XWayland also
    exports ``DISPLAY``. A non-empty ``WAYLAND_DISPLAY`` without that signature
    is some other compositor: there is no adapter for it in v0.1, so detection
    returns :class:`UnsupportedDesktop` instead of guessing through XWayland.
    """
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return HyprlandDesktop(shell_config=shell_config, which=which)
    if env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return X11Desktop(which=which)
    return UnsupportedDesktop()
