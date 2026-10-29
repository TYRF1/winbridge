"""Experimental X11/XWayland cursor fallback for Wine bug 58922.

Wine never calls ``XDefineCursor`` for a WebView2 app: the cursor handles are
created by a child process, so the window's own process cannot resolve them.
Upstream ``cursoricon.c`` is unchanged through Wine 11.15. This module is the
opt-in workaround: a detached helper reads X11's own root children and their
``_NET_WM_PID`` values, selects windows owned by the prefix's processes, and
applies the standard X11 arrow cursor to each window and its descendants. It is
never part of the default ``run`` path, and it changes nothing about
``verify``'s cursor policy.

Window ids come from Xlib directly, never from a desktop adapter: compositor
APIs such as ``hyprctl`` expose their own client addresses (64-bit pointers),
which are not XIDs and must never be passed to Xlib.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from winbridge.core.errors import UnsupportedEnvironment, WinBridgeError
from winbridge.core.profile import Profile
from winbridge.core.state import Paths
from winbridge.util.log import append_run_log, run_log_path
from winbridge.util.proc import processes_for_prefix

__all__ = [
    "CURSOR_MODES",
    "X11Cursor",
    "require_force_supported",
    "run_force",
    "select_x11_windows",
    "start_force",
    "x11_library_path",
]

_LOGGER = logging.getLogger("winbridge")

#: ``off`` keeps the launched instance untouched; ``force`` opts into the
#: helper. Anything else is a usage error at the CLI boundary.
CURSOR_MODES = frozenset({"off", "force"})

#: ``XC_left_ptr``: the conventional arrow cursor.
_XC_LEFT_PTR = 68
#: ``XA_CARDINAL``: the property type ``_NET_WM_PID`` is specified to use.
_XA_CARDINAL = 6
_NET_WM_PID = b"_NET_WM_PID"
_XID_MASK = 0xFFFFFFFF
_FORCE_INTERVAL_S = 2.0
_PREFIX = "[cursor]"
_HELPER_MODULE = "winbridge.cursor_helper"


def x11_library_path() -> str | None:
    """Return the shared-library name for Xlib, or ``None`` when unavailable."""
    try:
        return ctypes.util.find_library("X11")
    except OSError:
        return None


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _ignore_x_error(_display: int | None, _event: int | None) -> int:
    """Swallow asynchronous X errors caused by windows disappearing mid-walk."""
    return 0


#: Keep the callback alive for the whole process; Xlib stores a bare pointer.
_NOOP_X_ERROR_HANDLER = _XErrorHandler(_ignore_x_error)


class X11Cursor:
    """Read X11 windows and apply one standard arrow cursor through Xlib.

    ``ctypes`` is used directly so the feature adds no runtime dependency. An
    invalid window id is skipped rather than passed to Xlib; a child that
    vanishes between ``XQueryTree`` and ``XDefineCursor`` produces an
    asynchronous error, which the installed no-op handler absorbs.
    """

    def __init__(self) -> None:
        library = x11_library_path()
        if library is None:
            raise UnsupportedEnvironment("cursor force requires libX11")
        self._lib = ctypes.CDLL(library)
        self.display: int | None = None
        self._cursor = 0
        self._pid_atom = 0
        self._configure()
        display = self._lib.XOpenDisplay(None)
        if not display:
            raise UnsupportedEnvironment(
                "cursor force cannot open the X display; check DISPLAY"
            )
        self.display = int(display)
        cursor = int(self._lib.XCreateFontCursor(self.display, _XC_LEFT_PTR))
        if not cursor:
            self._lib.XCloseDisplay(self.display)
            self.display = None
            raise UnsupportedEnvironment(
                "cursor force cannot create the standard arrow cursor"
            )
        self._cursor = cursor
        self._pid_atom = int(self._lib.XInternAtom(self.display, _NET_WM_PID, 0))

    def _configure(self) -> None:
        """Declare Xlib signatures before the first call.

        Without ``argtypes`` ctypes truncates 64-bit display and window handles
        on some platforms. ``XSetErrorHandler`` is installed here so a race
        during ``XQueryTree`` or a property read cannot terminate the helper.
        """
        xid = ctypes.c_ulong
        display = ctypes.c_void_p
        self._lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._lib.XOpenDisplay.restype = display
        self._lib.XCloseDisplay.argtypes = [display]
        self._lib.XCloseDisplay.restype = ctypes.c_int
        self._lib.XCreateFontCursor.argtypes = [display, ctypes.c_uint]
        self._lib.XCreateFontCursor.restype = xid
        self._lib.XFreeCursor.argtypes = [display, xid]
        self._lib.XFreeCursor.restype = ctypes.c_int
        self._lib.XDefineCursor.argtypes = [display, xid, xid]
        self._lib.XDefineCursor.restype = ctypes.c_int
        self._lib.XDefaultRootWindow.argtypes = [display]
        self._lib.XDefaultRootWindow.restype = xid
        self._lib.XInternAtom.argtypes = [display, ctypes.c_char_p, ctypes.c_int]
        self._lib.XInternAtom.restype = xid
        self._lib.XQueryTree.argtypes = [
            display,
            xid,
            ctypes.POINTER(xid),
            ctypes.POINTER(xid),
            ctypes.POINTER(ctypes.POINTER(xid)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._lib.XQueryTree.restype = ctypes.c_int
        self._lib.XGetWindowProperty.argtypes = [
            display,
            xid,
            xid,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            xid,
            ctypes.POINTER(xid),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self._lib.XGetWindowProperty.restype = ctypes.c_int
        self._lib.XFree.argtypes = [display]
        self._lib.XFree.restype = ctypes.c_int
        self._lib.XSync.argtypes = [display, ctypes.c_int]
        self._lib.XSync.restype = ctypes.c_int
        self._lib.XSetErrorHandler.argtypes = [_XErrorHandler]
        self._lib.XSetErrorHandler.restype = display
        self._lib.XSetErrorHandler(_NOOP_X_ERROR_HANDLER)

    def top_level_windows(self) -> list[tuple[int, int]]:
        """Return root children that publish a readable ``_NET_WM_PID``.

        This is the force loop's only window source. Desktop adapters are not
        consulted: a compositor may report its own client address instead of an
        X11 XID, and passing that value to Xlib would silently target nothing.
        """
        display = self.display
        if display is None or not self._pid_atom:
            return []
        windows: list[tuple[int, int]] = []
        for window in self._children(self._root_window()):
            pid = self._window_pid(window)
            if pid is not None:
                windows.append((window, pid))
        return windows

    def _root_window(self) -> int:
        display = self.display
        if display is None:
            return 0
        return int(self._lib.XDefaultRootWindow(display))

    def _window_pid(self, window: int) -> int | None:
        """Read one window's ``_NET_WM_PID`` as a 32-bit CARDINAL."""
        display = self.display
        atom = self._pid_atom
        if display is None or not atom:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        items = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        status = self._lib.XGetWindowProperty(
            display,
            window,
            atom,
            0,
            1,
            0,
            _XA_CARDINAL,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(items),
            ctypes.byref(bytes_after),
            ctypes.byref(prop),
        )
        pid = 0
        try:
            if status != 0 or actual_format.value != 32 or items.value < 1:
                return None
            if actual_type.value != _XA_CARDINAL or not prop:
                return None
            pid = int(ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong)).contents.value)
        finally:
            if prop:
                self._lib.XFree(prop)
        return pid if pid > 0 else None

    def force_tree(self, window_id: int | str) -> None:
        """Apply the arrow to ``window_id`` and every descendant X window.

        ``window_id`` is the integer XID from :meth:`top_level_windows`, or a
        hex string for direct callers. A 64-bit compositor address is rejected
        by :func:`_parse_xid` before any Xlib call.
        """
        xid = _parse_xid(window_id)
        display = self.display
        if xid is None or display is None:
            return

        pending = [xid]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            self._lib.XDefineCursor(display, current, self._cursor)
            pending.extend(self._children(current))
        self._lib.XSync(display, 0)

    def _children(self, window: int) -> list[int]:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint(0)
        display = self.display
        if display is None:
            return []
        status = self._lib.XQueryTree(
            display,
            window,
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(children),
            ctypes.byref(count),
        )
        if not status:
            return []
        try:
            return [children[index] for index in range(count.value)]
        finally:
            if children:
                self._lib.XFree(children)

    def close(self) -> None:
        """Free the cursor and close the display; safe to call twice."""
        display = self.display
        if display is None:
            return
        self.display = None
        try:
            if self._cursor:
                self._lib.XFreeCursor(display, self._cursor)
                self._cursor = 0
        finally:
            self._lib.XCloseDisplay(display)


def _parse_xid(value: int | str) -> int | None:
    """Parse a 32-bit XID from an int or hex string; reject anything else."""
    if isinstance(value, int):
        xid = value
    else:
        text = value.strip()
        if text.lower().startswith("0x"):
            text = text[2:]
        if not text:
            return None
        try:
            xid = int(text, 16)
        except ValueError:
            return None
    if xid <= 0 or xid > _XID_MASK:
        return None
    return xid


def require_force_supported(
    factory: Callable[[], X11Cursor] | None = None,
) -> None:
    """Refuse ``--cursor=force`` unless X11 can be reached directly.

    The check is intentionally independent of the desktop adapter. A compositor
    that advertises window discovery (for example through ``hyprctl``) still
    does not provide usable XIDs, so only ``DISPLAY``, libX11, and an actual
    ``XOpenDisplay`` probe decide support.
    """
    if not os.environ.get("DISPLAY"):
        raise UnsupportedEnvironment(
            "cursor force requires an X11 or XWayland DISPLAY; it is unset"
        )
    if x11_library_path() is None:
        raise UnsupportedEnvironment("cursor force requires libX11")
    opener = factory or X11Cursor
    try:
        probe = opener()
    except (WinBridgeError, OSError) as exc:
        raise UnsupportedEnvironment(
            f"cursor force cannot open the X display: {exc}"
        ) from exc
    with suppress(OSError):
        probe.close()


def select_x11_windows(
    windows: list[tuple[int, int]],
    pids: set[int],
) -> list[tuple[int, int]]:
    """Keep ``(xid, pid)`` pairs owned by a prefix process; no title matching."""
    return [(xid, pid) for xid, pid in windows if pid in pids]


def start_force(
    profile: Profile,
    paths: Paths,
    pids: set[int],
) -> subprocess.Popen[bytes] | None:
    """Spawn the detached cursor helper; a failure never fails the launch.

    The helper gets ``/dev/null`` on every stdio stream and its own session, so
    it can neither hold the caller's pipe open nor be killed with the caller's
    process group. Activation and spawn failures are appended to the per-app
    run log for ``doctor``/``verify`` to inspect later. The lock path is passed
    explicitly so the helper can enforce one active helper per app.
    """
    log = run_log_path(paths, profile.id)
    lock = paths.locks / f"cursor-{profile.id}.lock"
    command = [
        sys.executable,
        "-m",
        _HELPER_MODULE,
        "--prefix",
        str(paths.prefix_for(profile.id)),
        "--log",
        str(log),
        "--app-id",
        profile.id,
        "--lock",
        str(lock),
    ]
    _LOGGER.debug("%s targeting pids %s", _PREFIX, sorted(pids))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        message = f"{_PREFIX} force helper failed to start: {exc}"
        _LOGGER.warning(message)
        _append_best_effort(log, message)
        return None

    message = f"{_PREFIX} force helper started (pid {process.pid})"
    _LOGGER.info(message)
    _append_best_effort(log, message)
    return process


def run_force(
    prefix: Path,
    log_path: Path,
    *,
    x11: X11Cursor | None = None,
    proc_probe: Callable[[Path], set[int]] = processes_for_prefix,
    interval_s: float = _FORCE_INTERVAL_S,
    max_ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll X11 and force the arrow until the prefix is empty.

    Selection uses only X11's own ``(xid, pid)`` pairs from
    :meth:`X11Cursor.top_level_windows`, filtered by prefix PIDs. A transient
    Xlib or property-read failure is recorded once and polling continues; a
    window is never inferred from a title, class, or desktop-adapter address.
    The return value is ``0`` for a normal stop and ``1`` when the helper could
    not start at all.
    """
    owned = x11 is None
    cursor = x11
    if cursor is None:
        try:
            cursor = X11Cursor()
        except (WinBridgeError, OSError) as exc:
            _record(log_path, f"{_PREFIX} force failed: {exc}")
            return 1
    assert cursor is not None

    active = False
    waiting = False
    failed = False
    ticks = 0
    try:
        while max_ticks is None or ticks < max_ticks:
            pids = set(proc_probe(prefix))
            if not pids:
                _record(log_path, f"{_PREFIX} force stopped")
                return 0
            try:
                windows = select_x11_windows(cursor.top_level_windows(), pids)
            except (WinBridgeError, OSError) as exc:
                if not failed:
                    _record(log_path, f"{_PREFIX} force failed: {exc}")
                    failed = True
            else:
                if windows:
                    try:
                        for xid, _pid in windows:
                            cursor.force_tree(xid)
                    except (WinBridgeError, OSError) as exc:
                        if not failed:
                            _record(log_path, f"{_PREFIX} force failed: {exc}")
                            failed = True
                    else:
                        if not active:
                            _record(log_path, f"{_PREFIX} force active")
                            active = True
                elif not active and not waiting:
                    _record(
                        log_path,
                        f"{_PREFIX} force waiting: no matching X11 window yet",
                    )
                    waiting = True
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep(max(interval_s, 0.0))
        _record(log_path, f"{_PREFIX} force stopped")
        return 0
    finally:
        if owned:
            with suppress(OSError):
                cursor.close()


def _record(log_path: Path, message: str) -> None:
    """Write one activation line to the run log and the normal logger."""
    _LOGGER.info(message)
    _append_best_effort(log_path, message)


def _append_best_effort(log_path: Path, message: str) -> None:
    try:
        append_run_log(log_path, message)
    except OSError as exc:
        _LOGGER.warning("%s cannot write run log %s: %s", _PREFIX, log_path, exc)
