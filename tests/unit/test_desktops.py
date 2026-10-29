"""Desktop detection and the unsupported fallback adapter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from winbridge.core.errors import UnsupportedEnvironment
from winbridge.core.omarchy import TASKBAR_ID
from winbridge.desktops import (
    Desktop,
    HyprlandDesktop,
    UnsupportedDesktop,
    Window,
    X11Desktop,
    detect_desktop,
)
from winbridge.desktops.base import DesktopError, subprocess_runner


def _which(*names: str) -> Callable[[str], str | None]:
    installed = set(names)

    def lookup(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in installed else None

    return lookup


def _env(**values: str) -> dict[str, str]:
    return dict(values)


def test_subprocess_runner_times_out_a_wedged_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timing_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["hyprctl"], 30)

    monkeypatch.setattr("winbridge.desktops.base.subprocess.run", timing_out)

    with pytest.raises(DesktopError, match="timeout"):
        subprocess_runner(["hyprctl", "clients", "-j"])


def test_subprocess_runner_passes_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    monkeypatch.setattr("winbridge.desktops.base.subprocess.run", fake_run)

    assert subprocess_runner(["hyprctl", "clients", "-j"]) == "[]"
    assert seen["timeout"] == 30.0


def _shell_config(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"bar": {"left": [{"id": TASKBAR_ID, "apps": []}]}}),
        encoding="utf-8",
    )
    return path


# --- detection precedence ---------------------------------------------------


def test_detect_desktop_prefers_hyprland_over_any_display() -> None:
    desktop = detect_desktop(
        env=_env(
            HYPRLAND_INSTANCE_SIGNATURE="signature",
            DISPLAY=":0",
            WAYLAND_DISPLAY="wayland-1",
        )
    )

    assert isinstance(desktop, HyprlandDesktop)
    assert desktop.name == "hyprland"


def test_detect_desktop_ignores_an_empty_hyprland_signature() -> None:
    desktop = detect_desktop(env=_env(HYPRLAND_INSTANCE_SIGNATURE="", DISPLAY=":0"))

    assert desktop.name == "x11"


def test_detect_desktop_falls_back_to_x11() -> None:
    desktop = detect_desktop(env=_env(DISPLAY=":0"))

    assert isinstance(desktop, X11Desktop)
    assert desktop.name == "x11"


def test_detect_desktop_treats_an_empty_wayland_display_as_unset() -> None:
    desktop = detect_desktop(env=_env(DISPLAY=":0", WAYLAND_DISPLAY=""))

    assert desktop.name == "x11"


def test_detect_desktop_does_not_claim_x11_under_wayland() -> None:
    desktop = detect_desktop(env=_env(DISPLAY=":0", WAYLAND_DISPLAY="wayland-1"))

    assert isinstance(desktop, UnsupportedDesktop)
    assert desktop.name == "unsupported"


def test_detect_desktop_without_a_display_is_unsupported() -> None:
    desktop = detect_desktop(env=_env())

    assert isinstance(desktop, UnsupportedDesktop)
    assert desktop.name == "unsupported"
    assert desktop.capabilities == set()


def test_detect_desktop_uses_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "signature")

    assert detect_desktop().name == "hyprland"


def test_detect_desktop_reads_the_live_environment_for_x11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    assert detect_desktop().name == "x11"


# --- injected collaborators -------------------------------------------------


def test_detect_desktop_forwards_the_shell_config_to_hyprland(tmp_path: Path) -> None:
    config = _shell_config(tmp_path / "shell.json")

    desktop = detect_desktop(
        env=_env(HYPRLAND_INSTANCE_SIGNATURE="signature"),
        shell_config=config,
        which=_which("hyprctl", "grim"),
    )

    assert isinstance(desktop, HyprlandDesktop)
    assert desktop.capabilities == {"windows", "screenshot", "pin"}


def test_detect_desktop_forwards_which_to_x11() -> None:
    desktop = detect_desktop(
        env=_env(DISPLAY=":0"),
        which=_which("wmctrl", "xdotool", "xprop"),
    )

    assert desktop.capabilities == {"windows"}


def test_detect_desktop_can_report_an_adapter_without_any_tools() -> None:
    desktop = detect_desktop(env=_env(DISPLAY=":0"), which=_which())

    assert desktop.capabilities == set()


def test_detect_desktop_returns_a_desktop_protocol_instance() -> None:
    desktop: Desktop = detect_desktop(env=_env(DISPLAY=":0"))

    assert desktop.name == "x11"


# --- unsupported adapter ----------------------------------------------------


def test_unsupported_adapter_exposes_no_capabilities() -> None:
    desktop = UnsupportedDesktop()

    assert desktop.name == "unsupported"
    assert desktop.capabilities == set()


@pytest.mark.parametrize(
    "operation", ["windows", "wait_for_window", "screenshot", "screenshot_screen"]
)
def test_unsupported_adapter_refuses_verification(operation: str) -> None:
    desktop = UnsupportedDesktop()

    with pytest.raises(UnsupportedEnvironment, match="desktop not supported") as exc_info:
        if operation == "windows":
            desktop.windows()
        elif operation == "wait_for_window":
            desktop.wait_for_window({1}, timeout=0.0)
        elif operation == "screenshot":
            desktop.screenshot(
                Window("0x1", 1, "T", "C", 0, 0, 10, 10),
                Path("/nonexistent/frame.png"),
            )
        else:
            desktop.screenshot_screen(Path("/nonexistent/background.png"))

    assert exc_info.value.exit_code == 3


def test_unsupported_adapter_does_not_pretend_to_pin() -> None:
    desktop = UnsupportedDesktop()

    assert desktop.pin("winbridge-example-app", "^Example$") is False
    assert desktop.unpin("winbridge-example-app") is False
