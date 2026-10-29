"""Opt-in cursor force fallback, exercised without X11 or libX11.

The real :class:`X11Cursor` path is manual QA: these tests pin the X11-only
selection rule (XID/pid, never a title or a compositor address), the
preconditions, the detached helper spawn, the per-app helper lock, and the poll
loop against fakes.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from winbridge import cursor_helper as helper_module
from winbridge.core import cursor as cursor_module
from winbridge.core.cursor import (
    X11Cursor,
    require_force_supported,
    run_force,
    select_x11_windows,
    start_force,
)
from winbridge.core.errors import UnsupportedEnvironment
from winbridge.core.locking import file_lock
from winbridge.core.profile import Profile
from winbridge.core.state import Paths
from winbridge.util.log import append_run_log, run_log_path


class FakeX11(X11Cursor):
    """Stand in for the X11 connection without opening a display."""

    def __init__(
        self,
        windows: list[tuple[int, int]] | None = None,
        error: OSError | None = None,
    ) -> None:
        self.windows = list(windows or [])
        self.error = error
        self.forced: list[int | str] = []
        self.top_level_calls = 0
        self.closed = False

    def top_level_windows(self) -> list[tuple[int, int]]:
        self.top_level_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.windows)

    def force_tree(self, window_id: int | str) -> None:
        self.forced.append(window_id)

    def close(self) -> None:
        self.closed = True


class ExplodingLib:
    """Fail if any Xlib member is touched."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Xlib member {name!r} must not be used")


class RecordingLib:
    """Record the Xlib calls a valid XID produces."""

    def __init__(self) -> None:
        self.define_calls: list[tuple[int, int, int]] = []
        self.syncs: list[tuple[int, int]] = []

    def XDefineCursor(self, display: int, window: int, cursor: int) -> int:
        self.define_calls.append((display, window, cursor))
        return 1

    def XQueryTree(
        self,
        display: int,
        window: int,
        root: Any,
        parent: Any,
        children: Any,
        count: Any,
    ) -> int:
        return 0

    def XSync(self, display: int, discard: int) -> int:
        self.syncs.append((display, discard))
        return 1


class PartialX11(X11Cursor):
    """Exercise ``top_level_windows`` without a real Xlib display."""

    def __init__(self) -> None:
        self.display = 1
        self._pid_atom = 39
        self._cursor = 0
        self._lib = None

    def _root_window(self) -> int:
        return 0x100

    def _children(self, window: int) -> list[int]:
        assert window == 0x100
        return [1, 2, 3]

    def _window_pid(self, window: int) -> int | None:
        return {1: 111, 2: None, 3: 222}.get(window)


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- selection and X11 window discovery -------------------------------------


def test_select_x11_windows_filters_by_pid_not_title() -> None:
    matching = (0x1400003, 111)
    unrelated = (0x1400004, 222)

    selected = select_x11_windows([matching, unrelated], {111})

    assert selected == [matching]


def test_top_level_windows_keeps_only_readable_pids() -> None:
    assert PartialX11().top_level_windows() == [(1, 111), (3, 222)]


def test_force_tree_skips_a_64_bit_hyprland_style_address() -> None:
    """A compositor pointer must never reach Xlib as if it were an XID."""
    cursor = object.__new__(X11Cursor)
    cursor.display = 1
    cursor._cursor = 2
    cursor._lib = ExplodingLib()

    cursor.force_tree("0x55d3a4b2c1d0")
    cursor.force_tree(0x55D3A4B2C1D0)
    cursor.force_tree("not-an-xid")
    cursor.force_tree("")


def test_force_tree_accepts_an_integer_xid() -> None:
    cursor = object.__new__(X11Cursor)
    cursor.display = 1
    cursor._cursor = 9
    lib = RecordingLib()
    cursor._lib = lib

    cursor.force_tree(0x1400003)

    assert lib.define_calls == [(1, 0x1400003, 9)]
    assert lib.syncs == [(1, 0)]


# --- preconditions -----------------------------------------------------------


def test_require_force_supported_rejects_missing_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(UnsupportedEnvironment):
        require_force_supported()


def test_require_force_supported_rejects_missing_libx11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(cursor_module, "x11_library_path", lambda: None)

    with pytest.raises(UnsupportedEnvironment):
        require_force_supported()


def test_require_force_supported_rejects_an_unreachable_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(cursor_module, "x11_library_path", lambda: "libX11.so.6")

    def broken() -> X11Cursor:
        raise UnsupportedEnvironment("no X server")

    with pytest.raises(UnsupportedEnvironment, match="cannot open"):
        require_force_supported(broken)


def test_require_force_supported_probes_and_closes_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(cursor_module, "x11_library_path", lambda: "libX11.so.6")
    probe = FakeX11()

    require_force_supported(lambda: probe)

    assert probe.closed is True


# --- helper spawn ------------------------------------------------------------


def test_start_force_spawns_a_detached_helper_without_stdio(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
) -> None:
    recorded: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProc:
        pid = 999

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        recorded.append((list(argv), dict(kwargs)))
        return FakeProc()

    monkeypatch.setattr(cursor_module.subprocess, "Popen", fake_popen)

    proc = start_force(profile, paths, {4242})

    assert proc is not None
    assert proc.pid == 999
    argv, kwargs = recorded[0]
    assert argv[0]
    assert argv[1:3] == ["-m", "winbridge.cursor_helper"]
    assert argv[argv.index("--prefix") + 1] == str(paths.prefix_for(profile.id))
    assert argv[argv.index("--log") + 1] == str(run_log_path(paths, profile.id))
    assert argv[argv.index("--app-id") + 1] == profile.id
    assert argv[argv.index("--lock") + 1] == str(
        paths.locks / f"cursor-{profile.id}.lock"
    )
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs.get("shell") is None

    log = run_log_path(paths, profile.id)
    assert "force helper started (pid 999)" in read_log(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_start_force_spawn_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
) -> None:
    def broken(argv: list[str], **kwargs: Any) -> Any:
        raise OSError("cannot execute the helper")

    monkeypatch.setattr(cursor_module.subprocess, "Popen", broken)

    assert start_force(profile, paths, {4242}) is None

    text = read_log(run_log_path(paths, profile.id))
    assert "cannot execute the helper" in text
    assert "force helper" in text


# --- poll loop ---------------------------------------------------------------


def test_run_force_targets_only_prefix_windows(
    profile: Profile,
    paths: Paths,
) -> None:
    x11 = FakeX11([(0x1400003, 111), (0x1400004, 222)])
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        x11=x11,
        proc_probe=lambda _prefix: {111},
        max_ticks=1,
        sleep=lambda _seconds: pytest.fail("the cap must stop before sleeping"),
    )

    assert code == 0
    assert x11.forced == [0x1400003]
    text = read_log(log)
    assert "force active" in text
    assert "force stopped" in text
    assert "force waiting" not in text


def test_run_force_never_passes_a_desktop_address_to_x11(
    profile: Profile,
    paths: Paths,
) -> None:
    """The loop's only ids come from ``top_level_windows`` as ints."""
    x11 = FakeX11([(0x1400005, 111)])
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        x11=x11,
        proc_probe=lambda _prefix: {111},
        max_ticks=1,
        sleep=lambda _seconds: None,
    )

    assert code == 0
    assert x11.forced == [0x1400005]
    assert all(isinstance(value, int) for value in x11.forced)
    assert x11.forced[0] <= 0xFFFFFFFF


def test_run_force_exits_when_prefix_empty(
    profile: Profile,
    paths: Paths,
) -> None:
    x11 = FakeX11([(0x1400003, 111)])
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        x11=x11,
        proc_probe=lambda _prefix: set(),
        max_ticks=1,
        sleep=lambda _seconds: pytest.fail("an empty prefix must not sleep"),
    )

    assert code == 0
    assert x11.forced == []
    assert x11.top_level_calls == 0
    text = read_log(log)
    assert "force active" not in text
    assert "force stopped" in text


def test_run_force_logs_waiting_once_when_no_x11_window_matches(
    profile: Profile,
    paths: Paths,
) -> None:
    x11 = FakeX11([(0x1400004, 222)])
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        x11=x11,
        proc_probe=lambda _prefix: {111},
        max_ticks=2,
        sleep=lambda _seconds: None,
    )

    assert code == 0
    assert x11.top_level_calls == 2
    assert x11.forced == []
    text = read_log(log)
    assert text.count("force waiting: no matching X11 window yet") == 1
    assert "force active" not in text
    assert "force stopped" in text


def test_run_force_logs_an_x11_error_once_and_keeps_polling(
    profile: Profile,
    paths: Paths,
) -> None:
    x11 = FakeX11(error=OSError("X connection lost"))
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        x11=x11,
        proc_probe=lambda _prefix: {111},
        max_ticks=2,
        sleep=lambda _seconds: None,
    )

    assert code == 0
    assert x11.top_level_calls == 2
    text = read_log(log)
    assert text.count("force failed") == 1
    assert "force waiting" not in text
    assert "force active" not in text
    assert "force stopped" in text


def test_run_force_reports_an_unopenable_display(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
) -> None:
    def broken() -> X11Cursor:
        raise UnsupportedEnvironment("cannot open the X display")

    monkeypatch.setattr(cursor_module, "X11Cursor", broken)
    log = run_log_path(paths, profile.id)

    code = run_force(
        paths.prefix_for(profile.id),
        log,
        proc_probe=lambda _prefix: {111},
        max_ticks=1,
    )

    assert code == 1
    assert "force failed" in read_log(log)


def test_run_force_opens_and_closes_an_owned_cursor(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
) -> None:
    x11 = FakeX11([(0x1400003, 111)])
    monkeypatch.setattr(cursor_module, "X11Cursor", lambda: x11)

    code = run_force(
        paths.prefix_for(profile.id),
        run_log_path(paths, profile.id),
        proc_probe=lambda _prefix: {111},
        max_ticks=1,
        sleep=lambda _seconds: None,
    )

    assert code == 0
    assert x11.forced == [0x1400003]
    assert x11.closed is True


# --- helper singleton --------------------------------------------------------


def test_helper_exits_zero_when_another_helper_owns_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
) -> None:
    log = run_log_path(paths, profile.id)
    lock = paths.locks / f"cursor-{profile.id}.lock"
    monkeypatch.setattr(helper_module, "_LOCK_ATTEMPTS", 1)

    with file_lock(lock, blocking=False):
        code = helper_module.main(
            [
                "--prefix",
                str(paths.prefix_for(profile.id)),
                "--log",
                str(log),
                "--app-id",
                profile.id,
                "--lock",
                str(lock),
            ],
            x11_factory=lambda: pytest.fail("opened X11 while another helper was active"),
        )

    assert code == 0
    text = read_log(log)
    assert "already active" in text
    assert "force failed" not in text


def test_helper_acquires_the_lock_and_stops_with_an_empty_prefix(
    profile: Profile,
    paths: Paths,
) -> None:
    log = run_log_path(paths, profile.id)
    lock = paths.locks / f"cursor-{profile.id}.lock"
    x11 = FakeX11()

    code = helper_module.main(
        [
            "--prefix",
            str(paths.prefix_for(profile.id)),
            "--log",
            str(log),
            "--app-id",
            profile.id,
            "--lock",
            str(lock),
        ],
        x11_factory=lambda: x11,
    )

    assert code == 0
    assert x11.closed is True
    assert "force stopped" in read_log(log)


# --- shared run log ----------------------------------------------------------


def test_run_log_path_uses_the_app_id_component_rule(paths: Paths) -> None:
    assert run_log_path(paths, "example-app") == paths.logs / "run-example-app.log"
    for bad in ("", ".", "..", "../escape", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            run_log_path(paths, bad)


def test_append_run_log_writes_one_private_line(paths: Paths) -> None:
    log = run_log_path(paths, "example-app")

    append_run_log(log, "first\n")
    append_run_log(log, "second")

    assert log.read_text(encoding="utf-8") == "first\nsecond\n"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
