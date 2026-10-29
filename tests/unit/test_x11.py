"""X11 adapter: recorded ``wmctrl``/``xprop``/``xdotool`` output only.

No X server or desktop tool is required. Every subprocess is a recording fake,
and the clock is advanced by a fake sleep so the poll cadence is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from winbridge.desktops import x11
from winbridge.desktops.base import DesktopError, Window
from winbridge.desktops.x11 import X11Desktop

FIRST_ID = "0x00400007"
SECOND_ID = "0x02a00003"

WMCTRL = (
    f"{FIRST_ID}  0 77 host Example - Main\n"
    f"{SECOND_ID} -1 4242 host Other - Notes\n"
)
XPROP = {
    FIRST_ID: 'WM_CLASS(STRING) = "example", "Example"\n',
    SECOND_ID: 'WM_CLASS(STRING) = "other", "Other"\n',
}
OPACITY = {
    FIRST_ID: "_NET_WM_WINDOW_OPACITY(CARDINAL) = 4294967295\n",
    SECOND_ID: "_NET_WM_WINDOW_OPACITY(CARDINAL) = 2147483648\n",
}
GEOMETRY = {
    FIRST_ID: (
        f"WINDOW={int(FIRST_ID, 16)}\n"
        "X=10\n"
        "Y=20\n"
        "WIDTH=800\n"
        "HEIGHT=600\n"
        "SCREEN=0\n"
    ),
    SECOND_ID: (
        f"WINDOW={int(SECOND_ID, 16)}\n"
        "X=-100\n"
        "Y=40\n"
        "WIDTH=640\n"
        "HEIGHT=480\n"
        "SCREEN=0\n"
    ),
}

WINDOW = Window("0x1", 1, "T", "C", 0, 0, 10, 10)


def _fake_x11_runner(args: list[str]) -> str:
    """Answer the four commands ``windows()`` issues, in any order."""
    if args == ["wmctrl", "-lp"]:
        return WMCTRL
    if args[:2] == ["xprop", "-id"] and len(args) == 4 and args[3] == "WM_CLASS":
        return XPROP[args[2]]
    if (
        args[:2] == ["xprop", "-id"]
        and len(args) == 4
        and args[3] == "_NET_WM_WINDOW_OPACITY"
    ):
        return OPACITY[args[2]]
    if args[:3] == ["xdotool", "getwindowgeometry", "--shell"] and len(args) == 4:
        return GEOMETRY[args[3]]
    raise AssertionError(f"unexpected command: {args!r}")


def _which(*names: str) -> Callable[[str], str | None]:
    """Pretend exactly ``names`` are installed, wherever the test runs."""
    installed = set(names)

    def lookup(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in installed else None

    return lookup


class FakeSleep:
    """``time.sleep`` replacement that advances a monotonic fake clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> FakeSleep:
    fake = FakeSleep()
    monkeypatch.setattr(x11.time, "sleep", fake.sleep)
    return fake


# --- windows ----------------------------------------------------------------


def test_parses_wmctrl_wm_class_and_geometry() -> None:
    windows = X11Desktop(runner=_fake_x11_runner).windows()

    assert [window.id for window in windows] == [FIRST_ID, SECOND_ID]
    first, second = windows
    assert first.pid == 77
    assert first.title == "Example - Main"
    assert first.class_name == "Example"
    assert (first.x, first.y, first.width, first.height) == (10, 20, 800, 600)
    assert second.pid == 4242
    assert second.title == "Other - Notes"
    assert second.class_name == "Other"
    assert (second.x, second.y, second.width, second.height) == (-100, 40, 640, 480)
    assert first.opacity == 1.0
    assert second.opacity == pytest.approx(0.5)


def test_windows_issues_one_command_per_tool_per_window() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return _fake_x11_runner(args)

    X11Desktop(runner=runner).windows()

    assert calls == [
        ["wmctrl", "-lp"],
        ["xprop", "-id", FIRST_ID, "WM_CLASS"],
        ["xdotool", "getwindowgeometry", "--shell", FIRST_ID],
        ["xprop", "-id", FIRST_ID, "_NET_WM_WINDOW_OPACITY"],
        ["xprop", "-id", SECOND_ID, "WM_CLASS"],
        ["xdotool", "getwindowgeometry", "--shell", SECOND_ID],
        ["xprop", "-id", SECOND_ID, "_NET_WM_WINDOW_OPACITY"],
    ]


def test_windows_keeps_titles_with_inner_spaces() -> None:
    line = f"{FIRST_ID} 0 77 host Example  -  Main window\n"

    window = _single_window(line)

    assert window.title == "Example  -  Main window"


def test_windows_defaults_a_missing_title_to_empty() -> None:
    line = f"{FIRST_ID} 0 77 host\n"

    window = _single_window(line)

    assert window.title == ""


def test_windows_skips_malformed_wmctrl_lines() -> None:
    text = (
        "\n"
        "0xzz 0 1 host bad id\n"
        f"{FIRST_ID} 0 not-a-pid host bad pid\n"
        f"{FIRST_ID} 0\n"
        "garbage\n"
        f"{FIRST_ID} 0 77 host Example - Main\n"
    )

    windows = X11Desktop(runner=_runner_with_wmctrl(text)).windows()

    assert [window.id for window in windows] == [FIRST_ID]


def test_windows_uses_the_second_quoted_wm_class_as_the_class() -> None:
    desktop = X11Desktop(
        runner=_runner_with_xprop('WM_CLASS(STRING) = "instance-name", "Class Name"\n')
    )

    assert desktop.windows()[0].class_name == "Class Name"


def test_windows_falls_back_to_the_first_quoted_wm_class() -> None:
    desktop = X11Desktop(runner=_runner_with_xprop('WM_CLASS(STRING) = "OnlyClass"\n'))

    assert desktop.windows()[0].class_name == "OnlyClass"


def test_windows_defaults_a_missing_wm_class_to_empty() -> None:
    desktop = X11Desktop(runner=_runner_with_xprop('WM_CLASS:  not found.\n'))

    assert desktop.windows()[0].class_name == ""


def test_windows_unescapes_quotes_in_wm_class() -> None:
    desktop = X11Desktop(
        runner=_runner_with_xprop('WM_CLASS(STRING) = "instance", "Say \\"Hi\\""\n')
    )

    assert desktop.windows()[0].class_name == 'Say "Hi"'


def test_windows_skips_a_window_without_complete_geometry() -> None:
    desktop = X11Desktop(
        runner=_runner_with_geometry("WINDOW=1\nX=10\nY=20\nWIDTH=800\n")
    )

    assert desktop.windows() == []


def test_windows_skips_non_integer_geometry() -> None:
    desktop = X11Desktop(
        runner=_runner_with_geometry(
            "WINDOW=1\nX=ten\nY=20\nWIDTH=800\nHEIGHT=600\n"
        )
    )

    assert desktop.windows() == []


def test_windows_tolerates_extra_geometry_keys() -> None:
    desktop = X11Desktop(
        runner=_runner_with_geometry(
            "WINDOW=1\nX=10\nY=20\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n"
        )
    )

    window = desktop.windows()[0]

    assert (window.x, window.y, window.width, window.height) == (10, 20, 800, 600)


def test_windows_accepts_hex_opacity() -> None:
    def runner(args: list[str]) -> str:
        if args[:2] == ["xprop", "-id"] and args[3] == "_NET_WM_WINDOW_OPACITY":
            return "_NET_WM_WINDOW_OPACITY(CARDINAL) = 0x80000000\n"
        return _fake_x11_runner(args)

    assert X11Desktop(runner=runner).windows()[0].opacity == pytest.approx(0.5)


def test_windows_leaves_opacity_unknown_when_the_property_is_missing() -> None:
    def runner(args: list[str]) -> str:
        if args[:2] == ["xprop", "-id"] and args[3] == "_NET_WM_WINDOW_OPACITY":
            return "_NET_WM_WINDOW_OPACITY:  not found.\n"
        return _fake_x11_runner(args)

    windows = X11Desktop(runner=runner).windows()

    assert [window.opacity for window in windows] == [None, None]


def test_windows_keeps_the_window_when_the_opacity_query_fails() -> None:
    """Opacity is a diagnostic: losing it must not hide a usable window."""

    def runner(args: list[str]) -> str:
        if args[:2] == ["xprop", "-id"] and args[3] == "_NET_WM_WINDOW_OPACITY":
            raise DesktopError("xprop exited 1: no such atom")
        return _fake_x11_runner(args)

    windows = X11Desktop(runner=runner).windows()

    assert [window.id for window in windows] == [FIRST_ID, SECOND_ID]
    assert [window.opacity for window in windows] == [None, None]


def test_windows_propagates_tool_failures() -> None:
    def failing(args: list[str]) -> str:
        raise DesktopError("wmctrl exited 1: Cannot open display")

    with pytest.raises(DesktopError, match="Cannot open display"):
        X11Desktop(runner=failing).windows()


@pytest.mark.parametrize("tool", ["xprop", "xdotool"])
def test_windows_skips_a_window_that_vanishes_during_enrichment(tool: str) -> None:
    """A closing splash/tooltip must not abort the whole window list."""

    def runner(args: list[str]) -> str:
        if args == ["wmctrl", "-lp"]:
            return WMCTRL
        if args[0] == tool and SECOND_ID in args:
            raise DesktopError(f"{tool} exited 1: no such window")
        return _fake_x11_runner(args)

    windows = X11Desktop(runner=runner).windows()

    assert [window.id for window in windows] == [FIRST_ID]


def test_windows_raises_when_no_window_can_be_enriched() -> None:
    """A dead display must still fail loudly instead of looking like no windows."""

    def runner(args: list[str]) -> str:
        if args == ["wmctrl", "-lp"]:
            return WMCTRL
        if args[:2] == ["xprop", "-id"]:
            raise DesktopError("xprop exited 1: no such window")
        return _fake_x11_runner(args)

    with pytest.raises(DesktopError, match="no such window"):
        X11Desktop(runner=runner).windows()


def test_windows_wraps_runner_os_errors() -> None:
    def failing(args: list[str]) -> str:
        raise OSError("wmctrl missing")

    with pytest.raises(DesktopError, match="wmctrl"):
        X11Desktop(runner=failing).windows()


def _single_window(wmctrl_line: str) -> Window:
    return X11Desktop(runner=_runner_with_wmctrl(wmctrl_line)).windows()[0]


def _runner_with_wmctrl(text: str) -> Callable[[list[str]], str]:
    def runner(args: list[str]) -> str:
        if args == ["wmctrl", "-lp"]:
            return text
        return _fake_x11_runner(args)

    return runner


def _runner_with_xprop(text: str) -> Callable[[list[str]], str]:
    def runner(args: list[str]) -> str:
        if args[:3] == ["xprop", "-id", FIRST_ID]:
            return text
        return _fake_x11_runner(args)

    return runner


def _runner_with_geometry(text: str) -> Callable[[list[str]], str]:
    def runner(args: list[str]) -> str:
        if args[:3] == ["xdotool", "getwindowgeometry", "--shell"]:
            return text
        return _fake_x11_runner(args)

    return runner


# --- wait_for_window --------------------------------------------------------


def test_wait_for_window_returns_immediately_without_sleeping(
    fake_sleep: FakeSleep,
) -> None:
    desktop = X11Desktop(runner=_fake_x11_runner, clock=fake_sleep.clock)

    window = desktop.wait_for_window({77}, timeout=1.0)

    assert window is not None
    assert window.pid == 77
    assert fake_sleep.sleeps == []


def test_wait_for_window_matches_any_pid_in_the_tree(fake_sleep: FakeSleep) -> None:
    desktop = X11Desktop(runner=_fake_x11_runner, clock=fake_sleep.clock)

    window = desktop.wait_for_window({1, 4242}, timeout=1.0)

    assert window is not None
    assert window.id == SECOND_ID


def test_wait_for_window_polls_every_200ms_and_returns_none_on_timeout(
    fake_sleep: FakeSleep,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        if args == ["wmctrl", "-lp"]:
            return ""
        raise AssertionError(f"unexpected command: {args!r}")

    desktop = X11Desktop(runner=runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({9999}, timeout=0.5) is None
    assert fake_sleep.sleeps == pytest.approx([0.2, 0.2, 0.1])
    assert len(calls) == 4


def test_wait_for_window_returns_a_late_window(fake_sleep: FakeSleep) -> None:
    responses = ["", "", WMCTRL]

    def runner(args: list[str]) -> str:
        if args == ["wmctrl", "-lp"]:
            return responses.pop(0)
        return _fake_x11_runner(args)

    desktop = X11Desktop(runner=runner, clock=fake_sleep.clock)

    window = desktop.wait_for_window({77}, timeout=1.0)

    assert window is not None
    assert window.pid == 77
    assert fake_sleep.sleeps == pytest.approx([0.2, 0.2])


def test_wait_for_window_tolerates_a_negative_timeout(fake_sleep: FakeSleep) -> None:
    desktop = X11Desktop(runner=_fake_x11_runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({9999}, timeout=-1.0) is None
    assert fake_sleep.sleeps == []


# --- screenshot -------------------------------------------------------------


def test_screenshot_uses_import_window(tmp_path) -> None:
    calls: list[list[str]] = []
    desktop = X11Desktop(runner=lambda args: calls.append(args) or "")

    destination = tmp_path / "s.png"
    desktop.screenshot(WINDOW, destination)

    assert calls[-1] == ["import", "-window", "0x1", str(destination)]


def test_screenshot_screen_uses_import_root(tmp_path) -> None:
    calls: list[list[str]] = []
    desktop = X11Desktop(runner=lambda args: calls.append(args) or "")

    destination = tmp_path / "background.png"
    desktop.screenshot_screen(destination)

    assert calls[-1] == ["import", "-window", "root", str(destination)]


# --- capabilities -----------------------------------------------------------


def test_capabilities_require_all_window_tools_and_import() -> None:
    desktop = X11Desktop(which=_which("wmctrl", "xdotool", "xprop", "import"))

    assert desktop.capabilities == {"windows", "screenshot"}


def test_capabilities_never_include_pin() -> None:
    desktop = X11Desktop(which=_which("wmctrl", "xdotool", "xprop", "import"))

    assert "pin" not in desktop.capabilities


@pytest.mark.parametrize("missing", ["wmctrl", "xdotool", "xprop"])
def test_capabilities_drop_windows_when_one_tool_is_missing(missing: str) -> None:
    tools = {"wmctrl", "xdotool", "xprop", "import"} - {missing}
    desktop = X11Desktop(which=_which(*tools))

    assert desktop.capabilities == {"screenshot"}


def test_capabilities_drop_screenshot_without_import() -> None:
    desktop = X11Desktop(which=_which("wmctrl", "xdotool", "xprop"))

    assert desktop.capabilities == {"windows"}


def test_capabilities_are_empty_without_any_tool() -> None:
    desktop = X11Desktop(which=_which())

    assert desktop.capabilities == set()


# --- pin --------------------------------------------------------------------


def test_pin_and_unpin_are_unsupported_no_ops() -> None:
    calls: list[list[str]] = []
    desktop = X11Desktop(runner=lambda args: calls.append(args) or "")

    assert desktop.pin("winbridge-example-app", "^Example$") is False
    assert desktop.unpin("winbridge-example-app") is False
    assert calls == []
