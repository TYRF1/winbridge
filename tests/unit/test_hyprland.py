"""Hyprland adapter: recorded ``hyprctl`` output, injected runner and clock.

No compositor, ``hyprctl``, or ``grim`` is required. Every subprocess is a
recording fake, and the clock is advanced by a fake sleep so the 0.2 s poll
cadence is asserted deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageStat

from winbridge.desktops import hyprland
from winbridge.desktops.base import DesktopError, Window, subprocess_runner
from winbridge.desktops.hyprland import HyprlandDesktop

FIXTURES = Path(__file__).parent.parent / "fixtures"
CLIENTS = FIXTURES / "hyprctl-clients.json"
WINDOW = Window("0x1", 1, "T", "C", 10, 20, 800, 600)


def _fixture_runner(args: list[str]) -> str:
    assert args == ["hyprctl", "clients", "-j"]
    return CLIENTS.read_text(encoding="utf-8")


def _json_runner(payload: Any) -> Any:
    return lambda args: json.dumps(payload)


def _which(name: str) -> str | None:
    """Pretend both compositor tools are installed, wherever the test runs."""
    return f"/usr/bin/{name}" if name in {"hyprctl", "grim"} else None


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
    monkeypatch.setattr(hyprland.time, "sleep", fake.sleep)
    return fake


def _shell_config(path: Path, widgets: list[Any], section: str = "left") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bar": {section: widgets}}), encoding="utf-8")
    return path


def _layout_shell_config(
    path: Path, widgets: list[Any], section: str = "left"
) -> Path:
    """The current Omarchy 4 shape: widgets under ``bar.layout.<section>``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"bar": {"layout": {section: widgets}}}), encoding="utf-8"
    )
    return path


# --- windows ----------------------------------------------------------------


def test_parses_hyprctl_clients_and_matches_pid() -> None:
    desktop = HyprlandDesktop(runner=_fixture_runner)

    windows = desktop.windows()

    assert windows[0].class_name == "Example"
    matched = desktop.wait_for_window({4242}, timeout=0.1)
    assert matched is not None
    assert matched.pid == 4242


def test_windows_filters_unmapped_and_hidden_clients() -> None:
    windows = HyprlandDesktop(runner=_fixture_runner).windows()

    assert [window.pid for window in windows] == [4242, 777]
    assert [window.title for window in windows] == ["Example - Main", "Other - Notes"]


def test_windows_skips_hidden_mapped_clients() -> None:
    windows = HyprlandDesktop(runner=_fixture_runner).windows()

    assert all(window.pid != 4244 for window in windows)
    assert all(window.title != "Example - Hidden" for window in windows)


def test_windows_reads_geometry_from_at_and_size() -> None:
    first, second = HyprlandDesktop(runner=_fixture_runner).windows()

    assert (first.x, first.y, first.width, first.height) == (10, 20, 800, 600)
    assert (second.x, second.y, second.width, second.height) == (-100, 40, 640, 480)


def test_windows_uses_the_client_address_as_the_id() -> None:
    first = HyprlandDesktop(runner=_fixture_runner).windows()[0]

    assert first.id == "0x55d3d8e2a2a0"


def test_windows_defaults_missing_title_and_class_to_empty() -> None:
    payload = [{"address": "0x1", "pid": 7, "at": [0, 0], "size": [10, 10], "mapped": True}]

    window = HyprlandDesktop(runner=_json_runner(payload)).windows()[0]

    assert window.title == ""
    assert window.class_name == ""


def test_windows_accepts_integral_floats_for_geometry() -> None:
    payload = [
        {
            "address": "0x1",
            "pid": 7,
            "at": [10.0, 20.0],
            "size": [800.0, 600.0],
            "mapped": True,
        }
    ]

    window = HyprlandDesktop(runner=_json_runner(payload)).windows()[0]

    assert (window.x, window.y, window.width, window.height) == (10, 20, 800, 600)


def test_windows_reads_opacity_and_override() -> None:
    def entry(**extra: Any) -> dict[str, Any]:
        return {
            "address": "0x1",
            "pid": 7,
            "at": [0, 0],
            "size": [800, 600],
            "mapped": True,
            **extra,
        }

    payload = [
        entry(opacity=0.35, opacityOverride=0.5),
        entry(opacity=1.0, opacityOverride=0.25),
    ]

    windows = HyprlandDesktop(runner=_json_runner(payload)).windows()

    assert [window.opacity for window in windows] == [0.35, 1.0]


def test_windows_falls_back_to_a_nonzero_opacity_override() -> None:
    payload = [
        {
            "address": "0x1",
            "pid": 7,
            "at": [0, 0],
            "size": [800, 600],
            "mapped": True,
            "opacityOverride": 0.25,
        }
    ]

    assert HyprlandDesktop(runner=_json_runner(payload)).windows()[0].opacity == 0.25


def test_windows_treats_a_zero_opacity_override_as_no_override() -> None:
    payload = [
        {
            "address": "0x1",
            "pid": 7,
            "at": [0, 0],
            "size": [800, 600],
            "mapped": True,
            "opacityOverride": 0.0,
        }
    ]

    assert HyprlandDesktop(runner=_json_runner(payload)).windows()[0].opacity is None


@pytest.mark.parametrize("value", [None, True, "0.5", -0.1, 1.1])
def test_windows_ignores_unusable_opacity_values(value: Any) -> None:
    payload = [
        {
            "address": "0x1",
            "pid": 7,
            "at": [0, 0],
            "size": [800, 600],
            "mapped": True,
            "opacity": value,
        }
    ]

    assert HyprlandDesktop(runner=_json_runner(payload)).windows()[0].opacity is None


@pytest.mark.parametrize(
    "entry",
    [
        "not-an-object",
        {"address": "0x1", "pid": True, "at": [0, 0], "size": [10, 10], "mapped": True},
        {"address": "0x1", "pid": 1, "at": [0], "size": [10, 10], "mapped": True},
        {"address": "0x1", "pid": 1, "at": [0, 0], "size": [10, "10"], "mapped": True},
        {"address": "0x1", "pid": 1, "at": [0, 0], "size": [10, 10], "mapped": False},
        {"address": "0x1", "pid": 1, "at": [0, 0], "size": [10, 10]},
    ],
)
def test_windows_skips_malformed_or_unmapped_clients(entry: Any) -> None:
    assert HyprlandDesktop(runner=_json_runner([entry])).windows() == []


def test_windows_raises_for_invalid_json() -> None:
    desktop = HyprlandDesktop(runner=lambda args: "not json")

    with pytest.raises(DesktopError, match="invalid JSON"):
        desktop.windows()


def test_windows_raises_when_hyprctl_returns_a_non_array() -> None:
    desktop = HyprlandDesktop(runner=_json_runner({"clients": []}))

    with pytest.raises(DesktopError, match="JSON array"):
        desktop.windows()


def test_windows_wraps_runner_os_errors() -> None:
    def failing(args: list[str]) -> str:
        raise OSError("hyprctl missing")

    with pytest.raises(DesktopError, match="hyprctl"):
        HyprlandDesktop(runner=failing).windows()


# --- wait_for_window --------------------------------------------------------


def test_wait_for_window_returns_immediately_without_sleeping(
    fake_sleep: FakeSleep,
) -> None:
    desktop = HyprlandDesktop(runner=_fixture_runner, clock=fake_sleep.clock)

    window = desktop.wait_for_window({4242}, timeout=1.0)

    assert window is not None
    assert window.pid == 4242
    assert fake_sleep.sleeps == []


def test_wait_for_window_polls_every_200ms_and_returns_none_on_timeout(
    fake_sleep: FakeSleep,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return "[]"

    desktop = HyprlandDesktop(runner=runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({9999}, timeout=0.5) is None
    assert fake_sleep.sleeps == pytest.approx([0.2, 0.2, 0.1])
    assert len(calls) == 4


def test_wait_for_window_returns_a_late_window(fake_sleep: FakeSleep) -> None:
    responses = ["[]", "[]", CLIENTS.read_text(encoding="utf-8")]

    def runner(args: list[str]) -> str:
        return responses.pop(0)

    desktop = HyprlandDesktop(runner=runner, clock=fake_sleep.clock)

    window = desktop.wait_for_window({4242}, timeout=1.0)

    assert window is not None
    assert window.pid == 4242
    assert fake_sleep.sleeps == pytest.approx([0.2, 0.2])


def test_wait_for_window_ignores_unmapped_clients_with_a_matching_pid(
    fake_sleep: FakeSleep,
) -> None:
    desktop = HyprlandDesktop(runner=_fixture_runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({4243}, timeout=0.0) is None
    assert fake_sleep.sleeps == []


def test_wait_for_window_ignores_hidden_mapped_clients_with_a_matching_pid(
    fake_sleep: FakeSleep,
) -> None:
    desktop = HyprlandDesktop(runner=_fixture_runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({4244}, timeout=0.0) is None
    assert fake_sleep.sleeps == []


def test_wait_for_window_tolerates_a_negative_timeout(fake_sleep: FakeSleep) -> None:
    desktop = HyprlandDesktop(runner=_fixture_runner, clock=fake_sleep.clock)

    assert desktop.wait_for_window({9999}, timeout=-1.0) is None
    assert fake_sleep.sleeps == []


# --- screenshot -------------------------------------------------------------


def test_screenshot_uses_grim_geometry(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    desktop = HyprlandDesktop(runner=lambda args: calls.append(args) or "")

    desktop.screenshot(WINDOW, tmp_path / "s.png")

    assert calls[-1] == ["grim", "-g", "10,20 800x600", str(tmp_path / "s.png")]


def test_screenshot_keeps_negative_coordinates(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    desktop = HyprlandDesktop(runner=lambda args: calls.append(args) or "")

    desktop.screenshot(Window("0x2", 2, "T", "C", -100, 40, 640, 480), tmp_path / "s.png")

    assert calls[-1] == ["grim", "-g", "-100,40 640x480", str(tmp_path / "s.png")]


def test_screenshot_screen_uses_plain_grim(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    desktop = HyprlandDesktop(runner=lambda args: calls.append(args) or "")

    desktop.screenshot_screen(tmp_path / "background.png")

    assert calls[-1] == ["grim", str(tmp_path / "background.png")]


# --- capabilities -----------------------------------------------------------


def test_capabilities_without_a_shell_config(tmp_path: Path) -> None:
    desktop = HyprlandDesktop(shell_config=tmp_path / "shell.json", which=_which)

    assert desktop.capabilities == {"windows", "screenshot"}


def test_capabilities_adds_pin_with_the_taskbar_widget(tmp_path: Path) -> None:
    config = _shell_config(
        tmp_path / "shell.json",
        [{"id": "io.github.joeyvigil.taskbar", "apps": []}],
    )

    desktop = HyprlandDesktop(shell_config=config, which=_which)

    assert desktop.capabilities == {"windows", "screenshot", "pin"}


def test_capabilities_accepts_the_taskbar_in_any_bar_section(tmp_path: Path) -> None:
    config = _shell_config(
        tmp_path / "shell.json",
        [{"id": "io.github.joeyvigil.taskbar"}],
        section="right",
    )

    assert "pin" in HyprlandDesktop(shell_config=config, which=_which).capabilities


def test_capabilities_accepts_the_current_nested_layout_schema(tmp_path: Path) -> None:
    """Omarchy 4 stores sections under ``bar.layout``, not directly on ``bar``."""
    config = _layout_shell_config(
        tmp_path / "shell.json",
        [{"id": "io.github.joeyvigil.taskbar", "apps": []}],
    )

    assert "pin" in HyprlandDesktop(shell_config=config, which=_which).capabilities


def test_capabilities_accepts_the_layout_taskbar_outside_left(tmp_path: Path) -> None:
    config = _layout_shell_config(
        tmp_path / "shell.json",
        [{"id": "io.github.joeyvigil.taskbar"}],
        section="right",
    )

    assert "pin" in HyprlandDesktop(shell_config=config, which=_which).capabilities


def test_capabilities_ignores_other_widgets(tmp_path: Path) -> None:
    config = _shell_config(tmp_path / "shell.json", [{"id": "clock"}, {"id": "tray"}])

    assert "pin" not in HyprlandDesktop(shell_config=config, which=_which).capabilities


def test_capabilities_ignore_invalid_shell_json(tmp_path: Path) -> None:
    config = tmp_path / "shell.json"
    config.write_text("{not json", encoding="utf-8")

    assert "pin" not in HyprlandDesktop(shell_config=config, which=_which).capabilities


def test_capabilities_are_recomputed_after_the_config_appears(tmp_path: Path) -> None:
    config = tmp_path / "shell.json"
    desktop = HyprlandDesktop(shell_config=config, which=_which)
    assert "pin" not in desktop.capabilities

    _shell_config(config, [{"id": "io.github.joeyvigil.taskbar"}])

    assert "pin" in desktop.capabilities


def test_capabilities_degrade_when_hyprctl_is_missing(tmp_path: Path) -> None:
    desktop = HyprlandDesktop(
        shell_config=tmp_path / "shell.json",
        which=lambda name: "/usr/bin/grim" if name == "grim" else None,
    )

    assert desktop.capabilities == {"screenshot"}


def test_capabilities_degrade_when_grim_is_missing(tmp_path: Path) -> None:
    desktop = HyprlandDesktop(
        shell_config=tmp_path / "shell.json",
        which=lambda name: "/usr/bin/hyprctl" if name == "hyprctl" else None,
    )

    assert desktop.capabilities == {"windows"}


def test_capabilities_are_empty_without_either_tool(tmp_path: Path) -> None:
    desktop = HyprlandDesktop(
        shell_config=tmp_path / "shell.json",
        which=lambda name: None,
    )

    assert desktop.capabilities == set()


def test_capabilities_keep_pin_when_window_tools_are_missing(tmp_path: Path) -> None:
    config = _shell_config(
        tmp_path / "shell.json",
        [{"id": "io.github.joeyvigil.taskbar"}],
    )
    desktop = HyprlandDesktop(shell_config=config, which=lambda name: None)

    assert desktop.capabilities == {"pin"}


# --- pin/unpin --------------------------------------------------------------


def test_pin_delegates_to_omarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str, str]] = []

    def fake_pin(config: Path, desktop_id: str, match: str) -> bool:
        calls.append((config, desktop_id, match))
        return True

    monkeypatch.setattr(hyprland.omarchy, "pin_app", fake_pin)
    config = tmp_path / "shell.json"
    desktop = HyprlandDesktop(shell_config=config)

    assert desktop.pin("winbridge-example-app", "^Example$") is True
    assert calls == [(config, "winbridge-example-app", "^Example$")]


def test_unpin_delegates_to_omarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_unpin(config: Path, desktop_id: str) -> bool:
        calls.append((config, desktop_id))
        return False

    monkeypatch.setattr(hyprland.omarchy, "unpin_app", fake_unpin)
    config = tmp_path / "shell.json"
    desktop = HyprlandDesktop(shell_config=config)

    assert desktop.unpin("winbridge-example-app") is False
    assert calls == [(config, "winbridge-example-app")]


def test_pin_reports_false_without_a_taskbar(tmp_path: Path) -> None:
    desktop = HyprlandDesktop(shell_config=tmp_path / "missing.json")

    assert desktop.pin("winbridge-example-app", "^Example$") is False
    assert desktop.unpin("winbridge-example-app") is False


# --- subprocess runner ------------------------------------------------------


def test_subprocess_runner_returns_stdout() -> None:
    assert subprocess_runner([sys.executable, "-c", "print('hello')"]) == "hello\n"


def test_subprocess_runner_reports_a_nonzero_exit() -> None:
    with pytest.raises(DesktopError, match="exited 3"):
        subprocess_runner(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('boom\\n'); sys.exit(3)",
            ]
        )


def test_subprocess_runner_wraps_a_missing_tool() -> None:
    with pytest.raises(DesktopError, match="cannot run"):
        subprocess_runner(["winbridge-tool-that-does-not-exist"])


# --- shared fixture ---------------------------------------------------------


def test_fake_desktop_records_calls_and_writes_a_non_blank_png(
    fake_desktop: Any, tmp_path: Path
) -> None:
    window = fake_desktop.windows()[0]
    assert fake_desktop.wait_for_window({window.pid}, timeout=0.1) == window

    destination = tmp_path / "evidence" / "frame.png"
    fake_desktop.screenshot(window, destination)

    assert fake_desktop.screenshots == [destination]
    assert fake_desktop.captures == [(window, destination)]
    assert destination.is_file()
    with Image.open(destination) as image:
        assert image.size == (window.width, window.height)
        deviation = ImageStat.Stat(image.convert("L")).stddev[0]
    assert deviation > 3.0

    screen = tmp_path / "background.png"
    fake_desktop.screenshot_screen(screen)
    assert fake_desktop.screen_screenshots == [screen]
    with Image.open(screen) as image:
        assert image.size == (1920, 1080)
        assert ImageStat.Stat(image.convert("L")).stddev[0] > 3.0

    assert fake_desktop.pin("winbridge-example-app", "^Example$") is True
    assert fake_desktop.pins == [("winbridge-example-app", "^Example$")]
    assert fake_desktop.pinned == "winbridge-example-app"

    assert fake_desktop.unpin("winbridge-example-app") is True
    assert fake_desktop.unpinned == ["winbridge-example-app"]
    assert fake_desktop.pinned is None
