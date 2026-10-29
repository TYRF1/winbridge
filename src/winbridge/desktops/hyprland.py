"""Hyprland desktop adapter: ``hyprctl clients -j`` plus ``grim``.

The compositor's JSON is the only window source: window ids are the client
``address`` values, geometry comes from ``at``/``size``, and only mapped clients
count. The adapter never guesses a window from a title.

Pin support is conditional. Omarchy's bar stores its widgets in
``shell.json``; ``pin`` is advertised only while that file contains the taskbar
widget. The actual rewrite is delegated to :mod:`winbridge.core.omarchy`, which
owns the backup and atomic-replace rules.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from winbridge.core import omarchy
from winbridge.core.omarchy import DEFAULT_SHELL_CONFIG
from winbridge.desktops.base import DesktopError, Runner, Window, subprocess_runner

__all__ = ["HyprlandDesktop"]

_LOGGER = logging.getLogger("winbridge")

_CLIENTS_ARGS = ("hyprctl", "clients", "-j")
_POLL_INTERVAL_S = 0.2
#: Absorb float drift so a fake or real clock landing exactly on the deadline
#: does not trigger a zero-length final sleep.
_REMAINING_EPSILON_S = 1e-6


class HyprlandDesktop:
    """A Hyprland session observed through ``hyprctl`` and ``grim``."""

    name = "hyprland"

    def __init__(
        self,
        *,
        runner: Runner = subprocess_runner,
        clock: Callable[[], float] = time.monotonic,
        shell_config: Path = DEFAULT_SHELL_CONFIG,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._shell_config = shell_config
        self._which = which

    @property
    def capabilities(self) -> set[str]:
        """Report only what this session can actually do.

        A missing ``hyprctl`` removes window discovery; a missing ``grim``
        removes screenshots, so setup/verify skips instead of failing hard.
        Pinning is independent of those tools and still needs the omarchy-shell
        taskbar in ``shell.json``.
        """
        capabilities: set[str] = set()
        if self._which("hyprctl") is not None:
            capabilities.add("windows")
        if self._which("grim") is not None:
            capabilities.add("screenshot")
        if _taskbar_present(self._shell_config):
            capabilities.add("pin")
        return capabilities

    def windows(self) -> list[Window]:
        """Return mapped clients from ``hyprctl clients -j``."""
        windows = _parse_clients(self._run(list(_CLIENTS_ARGS)))
        for window in windows:
            _LOGGER.debug(
                "[desktop] window %s pid=%s class=%r %dx%d+%d+%d",
                window.id,
                window.pid,
                window.class_name,
                window.width,
                window.height,
                window.x,
                window.y,
            )
        return windows

    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None:
        """Poll every 0.2 s for a mapped window owned by ``pid_tree``."""
        deadline = self._clock() + max(0.0, timeout)
        while True:
            for window in self.windows():
                if window.pid in pid_tree:
                    return window
            remaining = deadline - self._clock()
            if remaining <= _REMAINING_EPSILON_S:
                _LOGGER.debug(
                    "[desktop] no window for %d process(es) after %.2fs",
                    len(pid_tree),
                    max(0.0, timeout),
                )
                return None
            time.sleep(min(_POLL_INTERVAL_S, remaining))

    def screenshot(self, window: Window, dest: Path) -> None:
        """Capture exactly the window region with ``grim -g``."""
        geometry = f"{window.x},{window.y} {window.width}x{window.height}"
        self._run(["grim", "-g", geometry, str(dest)])

    def screenshot_screen(self, dest: Path) -> None:
        """Capture the whole composited output with ``grim``.

        Verify uses this pre-launch background to tell a rendered window from
        wallpaper showing through a fully transparent one.
        """
        self._run(["grim", str(dest)])

    def pin(self, desktop_id: str, match: str) -> bool:
        """Delegate the taskbar pin to :mod:`winbridge.core.omarchy`."""
        return omarchy.pin_app(self._shell_config, desktop_id, match)

    def unpin(self, desktop_id: str) -> bool:
        """Delegate the taskbar unpin to :mod:`winbridge.core.omarchy`."""
        return omarchy.unpin_app(self._shell_config, desktop_id)

    def _run(self, args: list[str]) -> str:
        try:
            return self._runner(args)
        except DesktopError:
            raise
        except OSError as exc:
            raise DesktopError(f"cannot run {args[0]}: {exc}") from exc


def _parse_clients(text: str) -> list[Window]:
    """Parse ``hyprctl clients -j``; keep mapped clients with usable geometry."""
    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesktopError(f"hyprctl clients returned invalid JSON: {exc}") from exc
    if not isinstance(document, list):
        raise DesktopError(
            "hyprctl clients returned JSON that is not an array; expected a JSON array"
        )

    windows: list[Window] = []
    for entry in document:
        window = _window(entry)
        if window is not None:
            windows.append(window)
    return windows


def _window(entry: Any) -> Window | None:
    """Convert one client object, or ``None`` when it is not a visible window."""
    if not isinstance(entry, dict) or entry.get("mapped") is not True:
        return None
    # Hyprland keeps invisible clients mapped with stale geometry. Selecting
    # one makes wait_for_window return an invisible target and grim capture
    # unrelated pixels.
    if entry.get("hidden") is True:
        return None

    address = entry.get("address")
    pid = _integer(entry.get("pid"))
    at = entry.get("at")
    size = entry.get("size")
    if not isinstance(address, str) or pid is None:
        return None
    if not isinstance(at, list) or len(at) != 2:
        return None
    if not isinstance(size, list) or len(size) != 2:
        return None

    x = _integer(at[0])
    y = _integer(at[1])
    width = _integer(size[0])
    height = _integer(size[1])
    if x is None or y is None or width is None or height is None:
        return None

    title = entry.get("title")
    class_name = entry.get("class")
    return Window(
        id=address,
        pid=pid,
        title=title if isinstance(title, str) else "",
        class_name=class_name if isinstance(class_name, str) else "",
        x=x,
        y=y,
        width=width,
        height=height,
        opacity=_opacity(entry),
    )


def _opacity(entry: dict[str, Any]) -> float | None:
    """Read the compositor's effective opacity, tolerating old clients output.

    ``opacity`` is the current value. ``opacityOverride`` is the rule override;
    Hyprland reports ``0.0`` when no override is active, which is not a
    transparent window, so it is only a fallback when non-zero. An unreadable
    or out-of-range value is unknown rather than guessed.
    """
    for key in ("opacity", "opacityOverride"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        opacity = float(value)
        if key == "opacityOverride" and opacity == 0.0:
            continue
        if 0.0 <= opacity <= 1.0:
            return opacity
    return None


def _integer(value: Any) -> int | None:
    """Accept JSON integers, plus integral floats some clients emit."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _taskbar_present(path: Path) -> bool:
    """Return whether ``path`` is a readable shell config with the taskbar widget.

    An absent, unreadable, or invalid config simply means no pin capability:
    capability detection must never make ``doctor`` or ``list`` crash. The
    schema check is shared with :mod:`winbridge.core.omarchy`, so both the
    current ``bar.layout.<section>`` shape and the legacy ``bar.<section>``
    shape report the same capability.
    """
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    return omarchy.taskbar_present(document)
