"""X11 desktop adapter: ``wmctrl``, ``xprop``, ``xdotool``, and ``import``.

``wmctrl -lp`` is the window list: id, desktop, pid, host, then the title.
Per window, ``xprop -id <id> WM_CLASS`` supplies the class,
``xdotool getwindowgeometry --shell <id>`` supplies the frame, and
``xprop -id <id> _NET_WM_WINDOW_OPACITY`` supplies the effective opacity when
the property exists (best-effort; a missing property is unknown, not opaque).
Only windows whose geometry can be read are returned; a malformed line is
skipped rather than guessed at.

X11 has no omarchy-shell taskbar, so this adapter never advertises ``pin``.
Capabilities degrade per tool: window discovery needs ``wmctrl``, ``xprop``,
and ``xdotool``; screenshots need ImageMagick's ``import``.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from winbridge.desktops.base import DesktopError, Runner, Window, subprocess_runner

__all__ = ["X11Desktop"]

_LOGGER = logging.getLogger("winbridge")

_WMCTRL_ARGS = ("wmctrl", "-lp")
_POLL_INTERVAL_S = 0.2
#: Absorb float drift so a fake or real clock landing exactly on the deadline
#: does not trigger a zero-length final sleep.
_REMAINING_EPSILON_S = 1e-6
_WINDOW_ID = re.compile(r"0x[0-9a-fA-F]+")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_OPACITY_PROPERTY = "_NET_WM_WINDOW_OPACITY"
_OPACITY_VALUE = re.compile(r"=\s*(0x[0-9a-fA-F]+|[0-9]+)")
_OPACITY_MAX = 0xFFFFFFFF


class X11Desktop:
    """An X11 session observed through ``wmctrl``, ``xprop``, and ``xdotool``."""

    name = "x11"

    def __init__(
        self,
        *,
        runner: Runner = subprocess_runner,
        clock: Callable[[], float] = time.monotonic,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._which = which

    @property
    def capabilities(self) -> set[str]:
        """Report only what the installed X11 tools can actually do.

        Geometry and class are both required to build a :class:`Window`, so
        ``windows`` needs all three commands; ``import`` alone controls
        screenshots. Pinning belongs to omarchy-shell and is never offered.
        """
        window_tools = ("wmctrl", "xprop", "xdotool")
        capabilities: set[str] = set()
        if all(self._which(tool) is not None for tool in window_tools):
            capabilities.add("windows")
        if self._which("import") is not None:
            capabilities.add("screenshot")
        return capabilities

    def windows(self) -> list[Window]:
        """Return windows from ``wmctrl -lp`` enriched with class and geometry.

        A window can disappear between the list and its enrichment calls (a
        splash screen closing, a tooltip expiring). Such a window is skipped
        with a debug log so a live target later in the list still matches. The
        initial ``wmctrl`` failure always propagates; if every listed window
        failed to enrich, the last tool error propagates instead of pretending
        the session has no windows.
        """
        rows = _parse_wmctrl(self._run(list(_WMCTRL_ARGS)))
        windows: list[Window] = []
        last_error: DesktopError | None = None
        for window_id, pid, title in rows:
            try:
                class_name = _parse_wm_class(
                    self._run(["xprop", "-id", window_id, "WM_CLASS"])
                )
                geometry = _parse_geometry(
                    self._run(["xdotool", "getwindowgeometry", "--shell", window_id])
                )
                opacity = _read_opacity(self._run, window_id)
            except DesktopError as exc:
                last_error = exc
                _LOGGER.debug(
                    "[desktop] skipping window %s that vanished during enrichment: %s",
                    window_id,
                    exc,
                )
                continue
            if geometry is None:
                continue
            x, y, width, height = geometry
            window = Window(
                id=window_id,
                pid=pid,
                title=title,
                class_name=class_name,
                x=x,
                y=y,
                width=width,
                height=height,
                opacity=opacity,
            )
            windows.append(window)
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
        if not windows and last_error is not None:
            raise last_error
        return windows

    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None:
        """Poll every 0.2 s for a window owned by ``pid_tree``."""
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
        """Capture exactly the window with ImageMagick ``import -window``."""
        self._run(["import", "-window", window.id, str(dest)])

    def screenshot_screen(self, dest: Path) -> None:
        """Capture the root window with ``import -window root``."""
        self._run(["import", "-window", "root", str(dest)])

    def pin(self, desktop_id: str, match: str) -> bool:
        """Report that X11 cannot pin: omarchy-shell owns the taskbar."""
        return False

    def unpin(self, desktop_id: str) -> bool:
        """Report that X11 had nothing to unpin."""
        return False

    def _run(self, args: list[str]) -> str:
        try:
            return self._runner(args)
        except DesktopError:
            raise
        except OSError as exc:
            raise DesktopError(f"cannot run {args[0]}: {exc}") from exc


def _parse_wmctrl(text: str) -> list[tuple[str, int, str]]:
    """Parse ``wmctrl -lp`` into ``(window id, pid, title)`` tuples.

    The title is everything after the hostname, so spaces in it survive. Lines
    with a bad id, a non-numeric pid, or too few columns are ignored: a window
    manager can print transient or foreign rows, and guessing would attach a
    pid to the wrong window.
    """
    windows: list[tuple[str, int, str]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 4:
            continue
        window_id, _desktop, pid_text, _host = parts[:4]
        if not _WINDOW_ID.fullmatch(window_id):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        title = parts[4] if len(parts) == 5 else ""
        windows.append((window_id, pid, title))
    return windows


def _parse_wm_class(text: str) -> str:
    """Return the class half of ``WM_CLASS``, or ``""`` when it is absent.

    ``WM_CLASS`` carries two strings, instance then class; launchers match the
    class. A single-string or ``not found`` reply is tolerated because some
    override-redirect windows have no class at all.
    """
    values = _QUOTED.findall(text)
    if not values:
        return ""
    class_name = values[1] if len(values) > 1 else values[0]
    return re.sub(r'\\(["\\])', r"\1", class_name)


def _parse_geometry(text: str) -> tuple[int, int, int, int] | None:
    """Parse ``xdotool getwindowgeometry --shell`` into ``(x, y, w, h)``."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip()

    geometry: list[int] = []
    for key in ("X", "Y", "WIDTH", "HEIGHT"):
        try:
            geometry.append(int(values[key]))
        except (KeyError, ValueError):
            return None
    return geometry[0], geometry[1], geometry[2], geometry[3]


def _read_opacity(run: Callable[[list[str]], str], window_id: str) -> float | None:
    """Read ``_NET_WM_WINDOW_OPACITY`` best-effort.

    The property is optional and some window managers or override-redirect
    windows do not expose it, so a failed query is unknown (``None``), never a
    reason to hide an otherwise usable window.
    """
    try:
        text = run(["xprop", "-id", window_id, _OPACITY_PROPERTY])
    except DesktopError:
        return None
    return _parse_opacity(text)


def _parse_opacity(text: str) -> float | None:
    """Convert a 32-bit X11 opacity property to ``0..1``.

    ``_NET_WM_WINDOW_OPACITY`` is a CARDINAL from 0 (fully transparent) to
    ``0xffffffff`` (fully opaque). ``xprop`` may print decimal or hex, and
    prints ``not found`` when the property is absent.
    """
    match = _OPACITY_VALUE.search(text)
    if match is None:
        return None
    try:
        value = int(match.group(1), 0)
    except ValueError:
        return None
    if not 0 <= value <= _OPACITY_MAX:
        return None
    return value / _OPACITY_MAX
