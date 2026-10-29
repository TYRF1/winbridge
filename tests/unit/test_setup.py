"""Zero-touch setup pipeline.

The pipeline is exercised end to end with the recording fake backend and fake
desktop: the fake installer writes a program tree and a registry diff, so every
detection stage runs for real. Tests pin the spec's order, the tri-state verify
contract, failure persistence, re-run safety, and the CLI flag surface.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.cli import app, main
from winbridge.core import launcher
from winbridge.core import setup as setup_core
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import (
    UnsupportedEnvironment,
    UsageError,
    VerifyFailed,
    WinBridgeError,
)
from winbridge.core.expose import expose as expose_path
from winbridge.core.expose import guest_user_dir
from winbridge.core.locking import file_lock
from winbridge.core.profile import Profile
from winbridge.core.setup import SetupOptions, setup_installer
from winbridge.core.state import Paths
from winbridge.core.verify import VerifyResult
from winbridge.desktops.base import Window

MakeInstaller = Callable[..., Path]


@pytest.fixture(autouse=True)
def _fast_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real quiescence/verify code but make its waits test-sized."""
    monkeypatch.setattr(setup_core, "_INSTALL_SETTLE_S", 0.0)
    monkeypatch.setattr(setup_core, "_INSTALL_POLL_S", 0.0)
    monkeypatch.setattr(setup_core, "_VERIFY_POLL_S", 0.001)
    monkeypatch.setattr(setup_core, "_VERIFY_CAPTURE_S", 0.01)


class _NoCaptureDesktop:
    """A supported launcher target that cannot observe or capture a window."""

    name = "no-capture"
    capabilities: set[str] = set()


class _PinOnlyDesktop:
    """A bar that can pin but cannot verify a window."""

    name = "pin-only"
    capabilities = {"pin"}

    def __init__(self) -> None:
        self.pins: list[tuple[str, str]] = []

    def pin(self, desktop_id: str, match: str) -> bool:
        self.pins.append((desktop_id, match))
        return True


def _run_setup(
    installer: Path,
    paths: Paths,
    catalog: Any,
    backend: Any,
    desktop: Any,
    options: SetupOptions | None = None,
    config: Config | None = None,
) -> Profile:
    return setup_installer(
        installer,
        paths,
        config or Config(),
        catalog,
        backend,
        desktop,
        options or SetupOptions(yes=True),
    )


# --- end to end --------------------------------------------------------------


def test_setup_end_to_end_with_fakes(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    profile = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert profile.id == "example-app"
    assert profile.name == "Example"
    assert profile.exe.endswith(r"Example.exe")
    assert profile.schemes == ["example-app"]
    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"
    assert (paths.applications / "winbridge-example-app.desktop").exists()
    assert (paths.applications / "winbridge-example-app-handler.desktop").exists() is False
    assert fake_desktop.pinned == "winbridge-example-app"
    assert profile.runner == "soda@11.0-10"
    assert profile.backend == "wine"
    assert profile.installer is not None
    assert profile.installer.path == str(installer)
    assert profile.installer.dir == str(installer.parent)
    assert profile.installer.glob == "*Example*.exe"
    assert profile.installer.type == "nsis"
    assert profile.installer.silent_args == ["/S"]
    assert profile.webview2 is not None
    assert profile.webview2.version == "156.0.4314.42"
    assert profile.webview2.source == "pinned"
    assert profile.webview2.installed_at
    assert profile.keep_z_drive is False


def test_setup_accepts_a_local_only_per_user_install(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    install_root = r"C:\users\me\AppData\Local\Example"
    installer = make_fake_installer(install_root=install_root)

    profile = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert profile.exe == rf"{install_root}\Example.exe"
    assert paths.prefix_for(profile.id).is_dir()
    assert (paths.profiles / f"{profile.id}.toml").is_file()


def test_setup_rejects_a_roaming_config_only_install(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer(effect=False)

    def roaming_config_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        config = (
            prefix
            / "drive_c"
            / "users"
            / "me"
            / "AppData"
            / "Roaming"
            / "Example"
            / "config.json"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{}", encoding="utf-8")

    fake_backend.on_install = roaming_config_only

    with pytest.raises(WinBridgeError, match="no new executable"):
        _run_setup(installer, paths, catalog, fake_backend, fake_desktop)
    assert not (paths.profiles / "example-app.toml").exists()


def test_scheme_is_detected_from_a_per_user_hive_only(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    """Tauri's currentUser NSIS writes to HKCU; HKCR alone must not be enough."""
    installer = make_fake_installer()
    original = fake_backend.on_install
    assert original is not None

    def install_effect(prefix: Path, installer_path: Path, args: list[str]) -> None:
        original(prefix, installer_path, args)
        machine_text = fake_backend.reg_export_text or ""
        fake_backend.reg_export_text = None
        fake_backend.reg_exports = {
            r"HKEY_CURRENT_USER\Software\Classes": machine_text.replace(
                "HKEY_CLASSES_ROOT", r"HKEY_CURRENT_USER\Software\Classes"
            )
        }

    fake_backend.on_install = install_effect

    profile = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert profile.schemes == ["example-app"]


def test_launcher_is_written_before_verify_and_refreshed_after(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_fake_installer()
    launcher = paths.applications / "winbridge-example-app.desktop"
    seen: list[str] = []
    real_verify = setup_core.verify_app

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(launcher.read_text(encoding="utf-8"))
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(setup_core, "verify_app", spy)
    _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert len(seen) == 1
    assert "StartupWMClass" not in seen[0]
    final = launcher.read_text(encoding="utf-8")
    assert "StartupWMClass=Example" in final
    assert "Exec=" in final
    assert " run example-app" in final


def test_pin_uses_the_verified_window_class(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    assert fake_desktop.pins == [("winbridge-example-app", "^Example$")]
    assert profile.pin is not None
    assert profile.pin.match == "^Example$"


def test_generic_runner_window_class_falls_back_to_the_exe_stem(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    generic = Window(
        "0x1", 4242, "Example App", "steam_proton", 0, 0, 800, 600
    )
    fake_desktop.window = generic
    fake_desktop.windows_result = [generic]
    fake_desktop.wait_result = generic

    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    launcher_path = paths.applications / "winbridge-example-app.desktop"
    assert "StartupWMClass=Example" in launcher_path.read_text(encoding="utf-8")
    assert profile.pin is not None
    assert profile.pin.match == "^Example$"
    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"


def test_unchanged_pin_keeps_the_carried_pin_and_does_not_restart(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarts: list[bool] = []

    def fake_restart(*, enabled: bool = True) -> bool:
        restarts.append(enabled)
        return False

    monkeypatch.setattr(setup_core.omarchy, "restart_shell", fake_restart)
    installer = make_fake_installer()
    first = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)
    assert first.pin is not None
    assert first.pin.match == "^Example$"
    assert restarts == [True]

    restarts.clear()
    fake_desktop.pin_result = False
    again = _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, force=True),
    )

    assert restarts == []
    assert again.pin is not None
    assert again.pin.match == first.pin.match


def test_missing_taskbar_does_not_record_a_phantom_pin(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    restarts: list[bool] = []

    def fake_restart(*, enabled: bool = True) -> bool:
        restarts.append(enabled)
        return False

    monkeypatch.setattr(setup_core.omarchy, "restart_shell", fake_restart)
    fake_desktop.pin_result = False
    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    assert profile.pin is None
    assert restarts == []
    assert "already present or unavailable" in capsys.readouterr().out


def test_skipped_verify_pins_with_the_exe_stem(
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    desktop = _PinOnlyDesktop()
    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, desktop
    )

    assert profile.last_verified is not None
    assert profile.last_verified.result == "skipped"
    assert desktop.pins == [("winbridge-example-app", "^Example$")]
    assert profile.pin is not None
    assert profile.pin.match == "^Example$"


def test_pin_happens_after_the_launcher_refresh(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = paths.applications / "winbridge-example-app.desktop"
    refreshed: list[bool] = []

    def spy(desktop_id: str, match: str) -> bool:
        refreshed.append("StartupWMClass=Example" in launcher.read_text(encoding="utf-8"))
        return True

    monkeypatch.setattr(fake_desktop, "pin", spy)
    _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    assert refreshed == [True]


def test_shell_restart_can_be_disabled(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def fake_restart(*, enabled: bool = True) -> bool:
        calls.append(enabled)
        return False

    monkeypatch.setattr(setup_core.omarchy, "restart_shell", fake_restart)
    _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, shell_restart=False),
    )

    assert calls == [False]


def test_pin_failure_does_not_discard_a_verified_profile(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_pin(desktop_id: str, match: str) -> bool:
        raise WinBridgeError("invalid omarchy-shell config")

    monkeypatch.setattr(fake_desktop, "pin", broken_pin)
    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"
    assert profile.pin is None
    assert (paths.applications / "winbridge-example-app.desktop").is_file()


def test_pin_oserror_does_not_abort_an_otherwise_complete_setup(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_pin(desktop_id: str, match: str) -> bool:
        raise OSError("shell.json is on a read-only mount")

    monkeypatch.setattr(fake_desktop, "pin", broken_pin)
    profile = _run_setup(
        make_fake_installer(), paths, catalog, fake_backend, fake_desktop
    )

    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"
    assert profile.pin is None
    assert (paths.applications / "winbridge-example-app.desktop").is_file()


def test_pipeline_runs_webview2_before_the_app_and_verifies_last(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    calls = fake_backend.calls
    prefix_index = next(i for i, call in enumerate(calls) if call.startswith("create_prefix"))
    install_index = next(i for i, call in enumerate(calls) if call.startswith("install "))
    run_index = next(i for i, call in enumerate(calls) if call.startswith("run "))

    assert prefix_index < install_index < run_index
    override_index = next(
        i for i, call in enumerate(calls) if "msedgewebview2.exe" in call
    )
    assert override_index < install_index
    # The app installer is the only Windows payload after the runtime override,
    # and it runs from the staged guest path rather than the host path.
    installed_prefix, program, args = fake_backend.installed[-1]
    assert installed_prefix == paths.prefix_for("example-app")
    assert str(program).startswith("C:\\windows\\temp\\winbridge-")
    assert args == ["/S"]
    assert fake_backend.install_payloads[-1] == installer.read_bytes()


def test_setup_rejects_a_runner_without_wow64_before_webview2(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_backend.wow64 = False

    with pytest.raises(WinBridgeError) as raised:
        _run_setup(
            make_fake_installer(), paths, catalog, fake_backend, fake_desktop
        )

    message = str(raised.value)
    assert "32-bit" in message
    assert "WoW64" in message
    assert "soda@11.0-10" in message
    assert "syswow64" in message
    assert fake_backend.installed == []
    assert not any("msedgewebview2.exe" in call for call in fake_backend.calls)
    assert "wow64: missing" in capsys.readouterr().out


def test_existing_prefix_is_stopped_before_it_is_adopted(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    paths.prefix_for("example-app").mkdir(parents=True)
    _run_setup(make_fake_installer(), paths, catalog, fake_backend, fake_desktop)

    kill_index = fake_backend.calls.index("wineserver -k")
    create_index = next(
        i for i, call in enumerate(fake_backend.calls) if call.startswith("create_prefix")
    )
    assert kill_index < create_index


def test_symlinked_prefix_is_refused_before_backend_work(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.prefixes.mkdir(parents=True)
    paths.prefix_for("example-app").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WinBridgeError, match="symlinked prefix"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )
    assert fake_backend.calls == []


def test_setup_prints_every_detected_value(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_setup(make_fake_installer(), paths, catalog, fake_backend, fake_desktop)

    output = capsys.readouterr().out
    for value in (
        "example-app",
        "Example",
        r"Example.exe",
        "example-app",
        "1.2.3",
        "156.0.4314.42",
        "pass",
    ):
        assert value in output


@pytest.mark.parametrize(
    ("choice", "expected_latest"),
    [("latest", True), ("pinned", False)],
)
def test_webview2_mode_is_forwarded_with_the_catalog(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    expected_latest: bool,
) -> None:
    calls: list[tuple[bool, Any, Path | None]] = []

    def fake_install(
        backend: Any,
        prefix: Path,
        *,
        latest: bool = True,
        payload: Path | None = None,
        catalog: Any = None,
        downloader: Any = None,
        cache_dir: Path | None = None,
    ) -> str:
        calls.append((latest, catalog, cache_dir))
        return "156.0.4314.42"

    monkeypatch.setattr(setup_core.webview2, "install", fake_install)
    _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, webview2=choice),
    )

    assert calls == [(expected_latest, catalog, paths.cache)]


def test_z_drive_is_stripped_unless_keep_z_drive(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    _run_setup(make_fake_installer(), paths, catalog, fake_backend, fake_desktop)
    stripped = paths.prefix_for("example-app") / "dosdevices" / "z:"
    assert not stripped.exists()
    assert not stripped.is_symlink()

    kept_options = SetupOptions(yes=True, app_id="keep-z-app", keep_z_drive=True)
    _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        kept_options,
    )
    kept = paths.prefix_for("keep-z-app") / "dosdevices" / "z:"
    assert kept.is_symlink()
    assert any("keep_z=True" in call for call in fake_backend.calls)
    kept_profile = Profile.load(paths.profiles / "keep-z-app.toml")
    assert kept_profile.keep_z_drive is True


# --- preflight ---------------------------------------------------------------


def test_missing_installer_is_refused_before_any_backend_work(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
) -> None:
    with pytest.raises(UsageError, match="installer not found"):
        _run_setup(
            tmp_path / "Missing.exe",
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )
    assert fake_backend.calls == []


def test_low_disk_space_is_refused_before_prefix_creation(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_core, "_MIN_FREE_BYTES", 10**18)
    with pytest.raises(WinBridgeError, match="disk space"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )
    assert fake_backend.calls == []


# --- identity and overrides --------------------------------------------------


def test_app_id_override_wins_over_the_installer_slug(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, app_id="custom-id"),
    )

    assert profile.id == "custom-id"
    assert paths.prefix_for("custom-id").is_dir()
    assert (paths.applications / "winbridge-custom-id.desktop").is_file()


def test_explicit_exe_and_scheme_are_recorded(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(
            yes=True,
            exe=r"C:\Program Files\Example\Example.exe",
            schemes=["custom-scheme"],
        ),
    )

    assert profile.exe == r"C:\Program Files\Example\Example.exe"
    # Detected schemes stay first; explicit additions follow.
    assert profile.schemes == ["example-app", "custom-scheme"]


def test_runner_override_is_recorded(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, runner="example@1.0"),
    )
    assert profile.runner == "example@1.0"


def test_silent_args_override_the_detected_flags(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, silent_args=["/quiet", "/norestart"]),
    )

    assert fake_backend.installed[0][2] == ["/quiet", "/norestart"]
    assert profile.installer is not None
    assert profile.installer.silent_args == ["/quiet", "/norestart"]


# --- install failures and fallbacks -----------------------------------------


def test_unknown_installer_probes_slash_s_then_silent(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    fake_backend.install_exit_codes = [1, 0]
    _run_setup(
        make_fake_installer(kind="unknown"),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
    )

    assert [args for _, _, args in fake_backend.installed] == [["/S"], ["/silent"]]


def test_unknown_installer_retries_when_a_flag_installs_nothing(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer(kind="unknown")
    real_effect = fake_backend.on_install
    calls: list[str] = []

    def second_attempt_only(prefix: Path, raw: Path, args: list[str]) -> None:
        calls.append("attempt")
        if len(calls) >= 2:
            assert real_effect is not None
            real_effect(prefix, raw, args)

    fake_backend.on_install = second_attempt_only
    fake_backend.install_exit_codes = [0, 0]
    _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert [args for _, _, args in fake_backend.installed] == [["/S"], ["/silent"]]


def test_failed_probe_executable_is_not_evidence_for_a_later_attempt(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_fake_installer(kind="unknown")
    fake_backend.on_install = None
    fake_backend.install_exit_codes = [1, 0]
    real_install = fake_backend.install

    def install_with_failed_probe(
        prefix: Path,
        program: Path,
        args: list[str],
        **kwargs: Any,
    ) -> int:
        code = real_install(prefix, program, args, **kwargs)
        if code != 0:
            stray = prefix / "drive_c" / "Program Files" / "Stray" / "Stray.exe"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b"failed-probe")
        return code

    monkeypatch.setattr(fake_backend, "install", install_with_failed_probe)

    with pytest.raises(WinBridgeError, match="no new executable"):
        _run_setup(installer, paths, catalog, fake_backend, fake_desktop)


def test_manual_mode_runs_the_gui_installer_after_silent_failure(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    fake_backend.install_exit_codes = [1, 0]
    _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, manual=True),
    )

    assert [args for _, _, args in fake_backend.installed] == [["/S"], []]


def test_manual_mode_runs_gui_when_silent_exits_zero_but_writes_nothing(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    real_effect = fake_backend.on_install
    attempts: list[int] = []

    def effect_from_second_attempt(
        prefix: Path, raw: Path, args: list[str]
    ) -> None:
        attempts.append(1)
        if len(attempts) >= 2:
            assert real_effect is not None
            real_effect(prefix, raw, args)

    fake_backend.on_install = effect_from_second_attempt
    fake_backend.install_exit_codes = [0, 0]
    _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, manual=True),
    )

    assert [args for _, _, args in fake_backend.installed] == [["/S"], []]


def test_silent_failure_without_manual_points_at_the_installer_log(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    fake_backend.install_exit_codes = [1]
    with pytest.raises(WinBridgeError, match=r"installer\.log"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )


def test_install_verification_requires_a_new_executable(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    with pytest.raises(WinBridgeError, match="no new executable"):
        _run_setup(
            make_fake_installer(effect=False),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )


def test_setup_with_only_a_sidecar_exe_requires_an_explicit_exe(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    with pytest.raises(WinBridgeError, match=r"no app executable.*--exe"):
        _run_setup(
            make_fake_installer(exe_name="node.exe"),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
        )


# --- re-run safety -----------------------------------------------------------


def test_existing_profile_requires_force_and_force_backs_up_first(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    profile_path = paths.profiles / "example-app.toml"
    loaded = Profile.load(profile_path)
    loaded.extra["custom_key"] = "kept"
    loaded.dump(profile_path)
    installs_before = len(fake_backend.installed)

    with pytest.raises(UsageError, match="--force"):
        _run_setup(installer, paths, catalog, fake_backend, fake_desktop)
    assert len(fake_backend.installed) == installs_before

    again = _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, force=True),
    )
    backup = paths.profiles / "example-app.toml.bak"
    assert backup.is_file()
    assert 'custom_key = "kept"' in backup.read_text(encoding="utf-8")
    assert again.extra["custom_key"] == "kept"
    # The adopted prefix already contains the scheme registration, so the
    # pre/post diff is empty; the old profile's schemes must survive --force.
    assert again.schemes == ["example-app"]


def test_force_replaces_an_unreadable_profile_after_backing_it_up(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    profile_path = paths.profiles / "example-app.toml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("this is not TOML", encoding="utf-8")

    with pytest.raises(UsageError, match="--force"):
        _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    profile = _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, force=True),
    )
    backup = paths.profiles / "example-app.toml.bak"
    assert backup.read_text(encoding="utf-8") == "this is not TOML"
    assert profile.id == "example-app"


def test_force_setup_recreates_recorded_exposures(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    first = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)
    prefix = paths.prefix_for("example-app")
    user = guest_user_dir(prefix)
    host = tmp_path / "repos"
    host.mkdir()
    expose_path(first, paths, host)
    first.dump(paths.profiles / "example-app.toml")

    shutil.rmtree(prefix)
    again = _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, force=True),
    )

    link = paths.prefix_for("example-app") / "drive_c" / "users" / user.name / "repos"
    assert fake_backend.exposed == [(paths.prefix_for("example-app"), host, r"C:\users\me\repos")]
    assert link.parent.is_dir()
    assert [item.host for item in again.exposed] == [str(host)]


def test_force_setup_refreshes_an_existing_scheme_handler(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    first = _run_setup(installer, paths, catalog, fake_backend, fake_desktop)
    handler = launcher.write_scheme_handler(
        paths, first, exec_path="/usr/bin/winbridge"
    )

    _run_setup(
        installer,
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, force=True, schemes=["second-scheme"]),
    )

    text = handler.read_text(encoding="utf-8")
    assert "x-scheme-handler/example-app" in text
    assert "x-scheme-handler/second-scheme" in text


def test_concurrent_setup_is_refused_before_any_prefix_work(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    installer = make_fake_installer()
    paths.ensure()

    with file_lock(paths.locks / "app-example-app.lock"):
        with pytest.raises(WinBridgeError, match="already running"):
            _run_setup(installer, paths, catalog, fake_backend, fake_desktop)

    assert fake_backend.calls == []


# --- tri-state verify --------------------------------------------------------


def test_unsupported_desktop_skips_verify_and_records_skipped(
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        _NoCaptureDesktop(),
    )

    assert profile.last_verified is not None
    assert profile.last_verified.result == "skipped"
    launcher = (paths.applications / "winbridge-example-app.desktop").read_text(
        encoding="utf-8"
    )
    assert "StartupWMClass" not in launcher
    assert profile.pin is None


def test_explicit_verify_on_unsupported_desktop_exits_three_but_keeps_profile(
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    with pytest.raises(UnsupportedEnvironment, match="desktop not supported"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            _NoCaptureDesktop(),
            SetupOptions(yes=True, verify=True),
        )

    profile = Profile.load(paths.profiles / "example-app.toml")
    assert profile.last_verified is not None
    assert profile.last_verified.result == "skipped"
    assert (paths.applications / "winbridge-example-app.desktop").is_file()


def test_no_verify_skips_the_harness_entirely(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    fake_desktop.blank_screenshots = True
    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        SetupOptions(yes=True, verify=False),
    )

    assert profile.last_verified is None
    assert fake_desktop.windows_calls == 0
    assert fake_desktop.screenshots == []


def test_verify_retries_once_after_a_cold_start_timeout(
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            return VerifyResult(False, None, None, [], "timeout")
        return VerifyResult(True, 14.36, "Example", [], "passed")

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        config=Config(verify_timeout_s=7),
    )

    assert calls == [7.0, 7.0]
    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"
    assert profile.last_verified.first_paint_s == 14.36
    output = capsys.readouterr().out
    assert "[setup] verify retry:" in output
    assert "relaunching once" in output
    launcher = paths.applications / "winbridge-example-app.desktop"
    assert "StartupWMClass=Example" in launcher.read_text(encoding="utf-8")


def test_verify_retries_once_after_a_transparent_first_attempt(
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            return VerifyResult(False, None, None, [], "transparent", transparent_frames=4)
        return VerifyResult(True, 21.0, "Example", [], "passed")

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    profile = _run_setup(
        make_fake_installer(),
        paths,
        catalog,
        fake_backend,
        fake_desktop,
        config=Config(verify_timeout_s=5),
    )

    assert calls == [5.0, 5.0]
    assert profile.last_verified is not None
    assert profile.last_verified.result == "pass"
    assert profile.last_verified.first_paint_s == 21.0
    output = capsys.readouterr().out
    assert "[setup] verify retry:" in output
    assert "relaunching once" in output


def test_verify_transparent_twice_fails_after_the_single_retry(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(kwargs["timeout"])
        return VerifyResult(False, None, None, [], "transparent", transparent_frames=4)

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    with pytest.raises(VerifyFailed, match="transparent"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
            config=Config(verify_timeout_s=2),
        )

    assert calls == [2.0, 2.0]
    profile = Profile.load(paths.profiles / "example-app.toml")
    assert profile.last_verified is not None
    assert profile.last_verified.result == "fail"


def test_verify_timeout_twice_still_exits_four_and_keeps_both_evidence_dirs(
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_frame = paths.evidence / "example-app" / "first" / "frame-0001.png"
    retry_frame = paths.evidence / "example-app" / "retry" / "frame-0001.png"
    for frame in (first_frame, retry_frame):
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b"png")
    calls: list[float] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            return VerifyResult(False, None, None, [first_frame], "timeout")
        return VerifyResult(False, None, None, [retry_frame], "timeout")

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    with pytest.raises(VerifyFailed, match="timeout") as error:
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
            config=Config(verify_timeout_s=3),
        )

    assert calls == [3.0, 3.0]
    assert str(retry_frame.parent) in str(error.value)
    output = capsys.readouterr().out
    assert str(first_frame.parent) in output
    assert first_frame.is_file()
    assert retry_frame.is_file()
    profile = Profile.load(paths.profiles / "example-app.toml")
    assert profile.last_verified is not None
    assert profile.last_verified.result == "fail"
    assert (paths.applications / "winbridge-example-app.desktop").is_file()


def test_explicit_verify_does_not_retry_a_timeout(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(args)
        return VerifyResult(False, None, None, [], "timeout")

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    with pytest.raises(VerifyFailed, match="timeout"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
            SetupOptions(yes=True, verify=True),
        )

    assert len(calls) == 1


def test_verify_background_failure_is_not_retried_and_reports_evidence(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = paths.evidence / "example-app" / "background"
    run_dir.mkdir(parents=True)
    calls: list[Any] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(args)
        return VerifyResult(
            False, None, None, [], "background", evidence_dir=run_dir
        )

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    with pytest.raises(VerifyFailed, match="background") as error:
        _run_setup(make_fake_installer(), paths, catalog, fake_backend, fake_desktop)

    assert len(calls) == 1
    assert str(run_dir) in str(error.value)


def test_verify_non_timeout_result_is_not_retried(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def fake_verify(*args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(args)
        return VerifyResult(False, None, "Example", [], "desktop tool failure: import exited 1")

    monkeypatch.setattr(setup_core, "verify_app", fake_verify)

    with pytest.raises(VerifyFailed, match="desktop tool failure"):
        _run_setup(make_fake_installer(), paths, catalog, fake_backend, fake_desktop)

    assert len(calls) == 1


def test_verify_tool_failure_is_exit_four_and_keeps_the_profile(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def broken_verify(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise WinBridgeError("window capture failed")

    monkeypatch.setattr(setup_core, "verify_app", broken_verify)
    with pytest.raises(VerifyFailed, match="window capture failed"):
        _run_setup(
            make_fake_installer(), paths, catalog, fake_backend, fake_desktop
        )

    assert len(calls) == 1
    profile = Profile.load(paths.profiles / "example-app.toml")
    assert profile.last_verified is not None
    assert profile.last_verified.result == "fail"
    assert (paths.applications / "winbridge-example-app.desktop").is_file()


def test_verify_failure_keeps_profile_launcher_and_evidence(
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    fake_desktop.blank_screenshots = True

    with pytest.raises(VerifyFailed, match="evidence"):
        _run_setup(
            make_fake_installer(),
            paths,
            catalog,
            fake_backend,
            fake_desktop,
            config=Config(verify_timeout_s=1),
        )

    profile = Profile.load(paths.profiles / "example-app.toml")
    assert profile.last_verified is not None
    assert profile.last_verified.result == "fail"
    assert (paths.applications / "winbridge-example-app.desktop").is_file()
    assert list(paths.evidence.rglob("frame-*.png"))


# --- option validation -------------------------------------------------------


def test_unknown_webview2_mode_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="webview2"):
        SetupOptions(webview2="nightly")


def test_default_webview2_mode_is_pinned() -> None:
    assert SetupOptions().webview2 == "pinned"


def test_bottles_backend_is_unsupported_in_v01() -> None:
    with pytest.raises(UnsupportedEnvironment, match="v0.1"):
        SetupOptions(backend="bottles")


def test_unknown_backend_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="unknown backend"):
        SetupOptions(backend="not-a-backend")


def test_invalid_runner_reference_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="runner"):
        SetupOptions(runner="not-a-reference")


def test_invalid_app_id_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="app id"):
        SetupOptions(app_id="../escape")


def test_reserved_scheme_is_refused_by_setup() -> None:
    with pytest.raises(UsageError, match="reserved"):
        SetupOptions(schemes=["https"])


# --- CLI wiring --------------------------------------------------------------


def _cli_context(
    paths: Paths, catalog: Any, backend: Any, desktop: Any
) -> Context:
    return Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=backend,
        desktop=desktop,
    )


def test_cli_setup_requests_runner_auto_install(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)
    captured: dict[str, Any] = {}

    def fake_build_context(*args: Any, **kwargs: Any) -> Context:
        captured.update(kwargs)
        return context

    monkeypatch.setattr(cli_module, "build_context", fake_build_context)

    def fake_setup(*args: Any) -> Profile:
        return Profile(schema=1, id="example-app", name="Example")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)

    result = CliRunner().invoke(
        app,
        ["setup", str(installer), "--yes"],
        obj={"paths": paths, "catalog": catalog},
    )

    assert result.exit_code == 0, result.output
    assert captured["install_missing_runner"] is True


def test_cli_setup_missing_installer_does_not_download_a_runner(
    tmp_path: Path,
    paths: Paths,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "Missing.exe"
    installs: list[object] = []
    monkeypatch.setattr(
        "winbridge.core.context.install_runner",
        lambda *args, **kwargs: installs.append(args),
    )

    def unexpected(*args: Any, **kwargs: Any) -> Context:
        pytest.fail("setup built a runner context before installer preflight")

    monkeypatch.setattr(cli_module, "_setup_context", unexpected)

    result = CliRunner().invoke(
        app,
        ["setup", str(missing), "--yes"],
        obj={"paths": paths, "catalog": catalog},
    )

    assert result.exit_code != 0
    assert "installer not found" in str(result.exception)
    assert installs == []


def test_cli_setup_defaults_to_the_pinned_webview2(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    captured: dict[str, SetupOptions] = {}

    def fake_setup(*args: Any) -> Profile:
        captured["options"] = args[-1]
        return Profile(schema=1, id="my-app", name="My App", runner="example@1.0")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)

    result = CliRunner().invoke(
        app,
        [
            "setup",
            str(installer),
            "--id",
            "my-app",
            "--runner",
            "example@1.0",
            "--yes",
        ],
        obj={"paths": paths, "catalog": catalog, "context": context},
    )

    assert result.exit_code == 0, result.output
    assert captured["options"].webview2 == "pinned"


def test_cli_setup_passes_every_flag(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    captured: dict[str, SetupOptions] = {}

    def fake_setup(*args: Any) -> Profile:
        captured["options"] = args[-1]
        return Profile(schema=1, id="my-app", name="My App", runner="example@1.0")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)
    result = CliRunner().invoke(
        app,
        [
            "setup",
            str(installer),
            "--id",
            "my-app",
            "--runner",
            "example@1.0",
            "--exe",
            r"C:\X\Y.exe",
            "--scheme",
            "one",
            "--scheme",
            "two",
            "--silent-args",
            "/S /X",
            "--backend",
            "wine",
            "--webview2",
            "pinned",
            "--keep-z-drive",
            "--manual",
            "--verify",
            "--no-shell-restart",
            "--yes",
            "--force",
        ],
        obj={"paths": paths, "catalog": catalog, "context": context},
    )

    assert result.exit_code == 0, result.output
    options = captured["options"]
    assert options.app_id == "my-app"
    assert options.runner == "example@1.0"
    assert options.exe == r"C:\X\Y.exe"
    assert options.schemes == ["one", "two"]
    assert options.silent_args == ["/S", "/X"]
    assert options.backend == "wine"
    assert options.webview2 == "pinned"
    assert options.keep_z_drive is True
    assert options.manual is True
    assert options.verify is True
    assert options.shell_restart is False
    assert options.yes is True
    assert options.force is True
    assert "my-app" in result.output


def test_cli_setup_preserves_windows_backslashes_in_silent_args(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    captured: dict[str, SetupOptions] = {}

    def fake_setup(*args: Any) -> Profile:
        captured["options"] = args[-1]
        return Profile(schema=1, id="example-app", name="Example")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)

    result = CliRunner().invoke(
        app,
        [
            "setup",
            str(installer),
            "--silent-args",
            r"/S /D=C:\Tools\Example",
            "--yes",
        ],
        obj={"paths": paths, "catalog": catalog, "context": context},
    )

    assert result.exit_code == 0, result.output
    assert captured["options"].silent_args == ["/S", r"/D=C:\Tools\Example"]


def test_cli_setup_keeps_quoted_windows_paths_whole(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    captured: dict[str, SetupOptions] = {}

    def fake_setup(*args: Any) -> Profile:
        captured["options"] = args[-1]
        return Profile(schema=1, id="example-app", name="Example")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)

    result = CliRunner().invoke(
        app,
        [
            "setup",
            str(installer),
            "--silent-args",
            r'/S /D="C:\Program Files\Example"',
            "--yes",
        ],
        obj={"paths": paths, "catalog": catalog, "context": context},
    )

    assert result.exit_code == 0, result.output
    assert captured["options"].silent_args == [
        "/S",
        r"/D=C:\Program Files\Example",
    ]


def test_cli_setup_tri_state_verify_defaults(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    captured: list[SetupOptions] = []

    def fake_setup(*args: Any) -> Profile:
        captured.append(args[-1])
        return Profile(schema=1, id="example-app", name="Example")

    monkeypatch.setattr(cli_module, "setup_installer", fake_setup)
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)
    runner = CliRunner()

    for flag, expected in (([], None), (["--verify"], True), (["--no-verify"], False)):
        result = runner.invoke(
            app,
            ["setup", str(installer), *flag],
            obj={"paths": paths, "catalog": catalog, "context": context},
        )
        assert result.exit_code == 0, result.output
        assert captured[-1].verify is expected


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (VerifyFailed("verify failed"), 4),
        (UnsupportedEnvironment("desktop not supported"), 3),
    ],
)
def test_cli_maps_setup_failures_to_exit_codes(
    tmp_path: Path,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: WinBridgeError,
    exit_code: int,
) -> None:
    installer = tmp_path / "Example App.exe"
    installer.write_bytes(b"MZ")
    context = _cli_context(paths, catalog, fake_backend, fake_desktop)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_catalog", lambda ctx: catalog)
    monkeypatch.setattr(cli_module, "_setup_context", lambda *args, **kwargs: context)

    def fail(*args: Any) -> Profile:
        raise error

    monkeypatch.setattr(cli_module, "setup_installer", fail)
    monkeypatch.setattr(sys, "argv", ["winbridge", "setup", str(installer)])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == exit_code
    assert str(error) in capsys.readouterr().err
