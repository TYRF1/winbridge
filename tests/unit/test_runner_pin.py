"""Runner re-pinning: record-only by default, prefix rebuild on request.

Re-pinning is the only automatic path that discards an initialized prefix, so
the tests pin the safe default, the explicit confirmation gate, the missing
installer failure, and everything a rebuild must preserve: profile, launcher,
pin, schemes, and exposed folders.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.cli import app
from winbridge.core import omarchy
from winbridge.core import update as update_core
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.launcher import write_desktop_entry
from winbridge.core.omarchy import pin_app
from winbridge.core.profile import (
    ExposedPath,
    InstallerSpec,
    LastVerified,
    Profile,
)
from winbridge.core.state import Paths
from winbridge.core.update import pin_runner

MakeInstaller = Callable[..., Path]
MakeProfile = Callable[..., Profile]

DESKTOP_ID = "winbridge-example-app"


@pytest.fixture(autouse=True)
def _fast_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the installer quiescence wait test-sized."""
    monkeypatch.setattr(update_core, "_INSTALL_SETTLE_S", 0.0)
    monkeypatch.setattr(update_core, "_INSTALL_POLL_S", 0.0)


def _shell(paths: Paths) -> Path:
    target = paths.root / "shell.json"
    target.write_text(
        json.dumps(
            {
                "bar": {"left": [{"id": omarchy.TASKBAR_ID, "apps": []}]},
                "unrelated": {"keep": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _apps(shell: Path) -> list[Any]:
    document = json.loads(shell.read_text(encoding="utf-8"))
    return document["bar"]["left"][0]["apps"]


def _prepared(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    *,
    effect: bool = True,
    silent_args: list[str] | None = None,
) -> tuple[Profile, Path, Path, Path, Path, Path]:
    """Write the profile, launcher, pin, prefix, and a real installer."""
    installer = make_fake_installer(effect=effect)
    host = paths.root / "docs"
    host.mkdir()
    profile = make_profile(
        installer=InstallerSpec(
            path=str(installer),
            dir=str(installer.parent),
            glob="*Example*.exe",
            type="nsis",
            silent_args=list(silent_args or ["/S"]),
            extra={"channel": "beta"},
        ),
        exe=r"C:\Program Files\Example\Example.exe",
        exposed=[
            ExposedPath(
                host=str(host),
                guest=r"C:\users\me\docs",
                extra={"read_only": True},
            )
        ],
        last_verified=LastVerified(
            app_version="1.0.0",
            result="pass",
            first_paint_s=1.0,
            at="2026-01-01T00:00:00Z",
        ),
        extra={"custom": 1},
    )
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = fake_backend.create_prefix(profile.id)
    (prefix / "sentinel").write_text("old prefix", encoding="utf-8")
    launcher = write_desktop_entry(paths, profile, exec_path="/usr/bin/winbridge")
    shell = _shell(paths)
    pin_app(shell, DESKTOP_ID, "^Example$")
    return profile, installer, prefix, launcher, shell, host


# --- record-only -------------------------------------------------------------


def test_runner_pin_is_record_only_by_default(
    profile: Profile,
    paths: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix = paths.prefix_for(profile.id)
    prefix.mkdir(parents=True)
    (prefix / "sentinel").write_text("keep me", encoding="utf-8")

    updated = pin_runner(profile, "example@1.0", rebuild=False, paths=paths)

    assert updated.runner == "example@1.0"
    assert profile.runner == "soda@11.0-10"
    assert prefix.is_dir()
    assert (prefix / "sentinel").read_text(encoding="utf-8") == "keep me"
    output = capsys.readouterr().out
    assert "record-only" in output
    assert "soda@11.0-10" in output
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.runner == "example@1.0"


def test_record_only_pin_without_a_prefix_says_so(
    profile: Profile,
    paths: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pin_runner(profile, "example@1.0", paths=paths)

    output = capsys.readouterr().out
    assert "no prefix" in output
    assert paths.prefix_for(profile.id).exists() is False


def test_record_only_pin_without_paths_uses_the_default_layout(
    profile: Profile,
) -> None:
    updated = pin_runner(profile, "example@1.0", rebuild=False)

    assert updated.runner == "example@1.0"


def test_pin_rejects_an_invalid_runner_reference(
    profile: Profile, paths: Paths
) -> None:
    with pytest.raises(UsageError, match="runner reference"):
        pin_runner(profile, "not-a-reference", paths=paths)


# --- rebuild -----------------------------------------------------------------


def test_runner_rebuild_requires_confirmation(
    make_profile: MakeProfile,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    profile, installer, prefix, *_ = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )
    assert installer.is_file()

    with pytest.raises(WinBridgeError, match="confirmation"):
        pin_runner(
            profile,
            "example@1.0",
            rebuild=True,
            backend=fake_backend,
            paths=paths,
            catalog=catalog,
            config=Config(),
            confirm=True,
        )

    assert prefix.is_dir()
    assert fake_backend.removed == []
    assert fake_backend.installed == []


def test_runner_rebuild_fails_clearly_when_the_installer_is_missing(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
) -> None:
    prefix = fake_backend.create_prefix(profile.id)
    missing = paths.root / "gone.exe"
    profile.installer = InstallerSpec(
        path=str(missing),
        dir=str(missing.parent),
        glob="*gone*.exe",
        type="nsis",
        silent_args=["/S"],
    )

    with pytest.raises(WinBridgeError, match="installer"):
        pin_runner(
            profile,
            "example@1.0",
            rebuild=True,
            backend=fake_backend,
            paths=paths,
            catalog=catalog,
            config=Config(),
            confirm=False,
        )

    assert prefix.is_dir()
    assert fake_backend.removed == []


def test_runner_rebuild_without_a_recorded_installer_fails_clearly(
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    catalog: Any,
) -> None:
    profile = make_profile(installer=None)

    with pytest.raises(WinBridgeError, match="installer"):
        pin_runner(
            profile,
            "example@1.0",
            rebuild=True,
            backend=fake_backend,
            paths=paths,
            catalog=catalog,
            config=Config(),
            confirm=False,
        )


def test_runner_rebuild_keeps_a_recorded_keep_z_drive(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
) -> None:
    profile, _installer, _old_prefix, _launcher, _shell, _host = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )
    profile.keep_z_drive = True
    profile.dump(paths.profiles / f"{profile.id}.toml")

    pin_runner(
        profile,
        "example@1.0",
        rebuild=True,
        backend=fake_backend,
        paths=paths,
        catalog=catalog,
        config=Config(),
        desktop=fake_desktop,
        confirm=False,
    )

    z_drive = paths.prefix_for(profile.id) / "dosdevices" / "z:"
    assert z_drive.is_symlink()
    assert any("keep_z=True" in call for call in fake_backend.calls)


def test_runner_rebuild_keeps_launcher_and_pin(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile, installer, old_prefix, launcher, shell, host = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )
    before_launcher = launcher.read_text(encoding="utf-8")

    updated = pin_runner(
        profile,
        "example@1.0",
        rebuild=True,
        backend=fake_backend,
        paths=paths,
        catalog=catalog,
        config=Config(),
        desktop=fake_desktop,
        confirm=False,
    )

    new_prefix = paths.prefix_for(profile.id)
    assert updated.id == profile.id
    assert updated.name == profile.name
    assert updated.runner == "example@1.0"
    assert updated.schemes == profile.schemes
    assert updated.exposed == profile.exposed
    assert updated.pin == profile.pin
    assert updated.extra == profile.extra
    assert updated.created_at == profile.created_at
    assert updated.last_verified is None
    assert updated.installer is not None
    assert updated.installer.path == str(installer.resolve())
    assert updated.installer.extra == {"channel": "beta"}
    assert updated.webview2 is not None
    assert updated.webview2.version == "156.0.4314.42"

    # Only the prefix was discarded and recreated.
    assert fake_backend.removed == [old_prefix]
    assert new_prefix.is_dir()
    assert not (new_prefix / "sentinel").exists()
    # Wine's Z: host-root link is stripped exactly like setup does.
    assert not (new_prefix / "dosdevices" / "z:").exists()
    assert "z drive: stripped" in capsys.readouterr().out
    calls = fake_backend.calls
    remove_index = calls.index(f"remove {old_prefix}")
    create_index = max(
        index
        for index, call in enumerate(calls)
        if call.startswith("create_prefix ")
    )
    assert remove_index < create_index

    # WebView2 was ensured before the app was reinstalled.
    assert any(call.startswith("reg query") for call in fake_backend.wine_calls)
    assert fake_backend.installed[-1][0] == new_prefix
    assert str(fake_backend.installed[-1][1]).startswith(
        "C:\\windows\\temp\\winbridge-"
    )
    assert fake_backend.installed[-1][2] == ["/S"]
    assert fake_backend.install_payloads[-1] == installer.read_bytes()

    # The exe is re-detected in the new prefix.
    assert updated.exe == r"C:\Program Files\Example\Example.exe"

    # Exposed host folders are recorded and re-linked in the new prefix.
    assert fake_backend.exposed == [
        (new_prefix, host, r"C:\users\me\docs")
    ]

    # The launcher and the bar pin are untouched.
    assert launcher.is_file()
    assert launcher.read_text(encoding="utf-8") == before_launcher
    apps = _apps(shell)
    assert [entry for entry in apps if entry.get("desktopId") == DESKTOP_ID]
    assert json.loads(shell.read_text(encoding="utf-8"))["unrelated"] == {"keep": True}
    assert fake_desktop.pins == []

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.runner == "example@1.0"
    assert persisted.pin is not None
    assert persisted.schemes == profile.schemes
    assert persisted.exposed == profile.exposed
    assert persisted.extra == {"custom": 1}
    assert persisted.created_at == profile.created_at
    assert persisted.last_verified is None


def test_runner_rebuild_reuses_the_recorded_silent_args(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
) -> None:
    profile, *_ = _prepared(
        make_profile,
        make_fake_installer,
        paths,
        fake_backend,
        silent_args=["/S", "/norestart"],
    )

    pin_runner(
        profile,
        "soda@11.0-10",
        rebuild=True,
        backend=fake_backend,
        paths=paths,
        catalog=catalog,
        config=Config(),
        confirm=False,
    )

    assert fake_backend.installed[-1][2] == ["/S", "/norestart"]


def test_runner_rebuild_accepts_an_imported_path_reference(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
) -> None:
    profile, *_ = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )

    updated = pin_runner(
        profile,
        "path:/opt/runners/custom",
        rebuild=True,
        backend=fake_backend,
        paths=paths,
        catalog=catalog,
        config=Config(),
        confirm=False,
    )

    assert updated.runner == "path:/opt/runners/custom"
    assert Profile.load(paths.profiles / f"{profile.id}.toml").runner == (
        "path:/opt/runners/custom"
    )


def test_runner_rebuild_zero_exit_without_an_executable_never_records_success(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
) -> None:
    profile, *_ = _prepared(
        make_profile,
        make_fake_installer,
        paths,
        fake_backend,
        effect=False,
    )
    assert profile.exe
    profile_path = paths.profiles / f"{profile.id}.toml"
    before_profile = profile_path.read_text(encoding="utf-8")

    with pytest.raises(WinBridgeError, match="no new executable"):
        pin_runner(
            profile,
            "example@1.0",
            rebuild=True,
            backend=fake_backend,
            paths=paths,
            catalog=catalog,
            config=Config(),
            confirm=False,
        )

    # The old profile still names the old runner and the old exe; a missing app
    # is never recorded as a successful rebuild.
    assert profile_path.read_text(encoding="utf-8") == before_profile
    persisted = Profile.load(profile_path)
    assert persisted.runner == "soda@11.0-10"
    assert persisted.exe == profile.exe


def test_runner_rebuild_without_a_detectable_exe_does_not_fall_back(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
) -> None:
    profile, *_ = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )
    assert profile.exe

    def write_uninstaller_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        target = prefix / "drive_c" / "Program Files" / "Example" / "unins000.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not the main executable")

    fake_backend.on_install = write_uninstaller_only

    with pytest.raises(WinBridgeError, match="main executable"):
        pin_runner(
            profile,
            "example@1.0",
            rebuild=True,
            backend=fake_backend,
            paths=paths,
            catalog=catalog,
            config=Config(),
            confirm=False,
        )


def test_runner_rebuild_does_not_verify(
    make_profile: MakeProfile,
    make_fake_installer: MakeInstaller,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
) -> None:
    profile, *_ = _prepared(
        make_profile, make_fake_installer, paths, fake_backend
    )

    pin_runner(
        profile,
        "example@1.0",
        rebuild=True,
        backend=fake_backend,
        paths=paths,
        catalog=catalog,
        config=Config(),
        desktop=fake_desktop,
        confirm=False,
    )

    assert fake_desktop.windows_calls == 0
    assert fake_desktop.screenshots == []


# --- CLI wiring --------------------------------------------------------------


def _invoke(
    args: list[str],
    paths: Paths,
    *,
    context: Context | None = None,
    input_text: str | None = None,
) -> Any:
    payload: dict[str, Any] = {"paths": paths}
    if context is not None:
        payload["context"] = context
    return CliRunner().invoke(app, args, obj=payload, input=input_text)


def test_cli_runner_pin_is_record_only_by_default(
    profile: Profile,
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.dump(paths.profiles / f"{profile.id}.toml")
    captured: dict[str, Any] = {}

    def fake_pin(*args: Any, **kwargs: Any) -> Profile:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return profile

    monkeypatch.setattr(cli_module, "pin_runner", fake_pin)
    result = _invoke(
        ["runners", "pin", profile.id, "example@1.0"], paths
    )

    assert result.exit_code == 0, result.output
    assert captured["kwargs"] == {"paths": paths, "confirm": False}
    assert "record" in result.output


def test_cli_runner_pin_rebuild_prompts_and_uses_the_new_backend(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.dump(paths.profiles / f"{profile.id}.toml")
    captured: dict[str, Any] = {}

    def fake_pin(*args: Any, **kwargs: Any) -> Profile:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return profile

    monkeypatch.setattr(cli_module, "pin_runner", fake_pin)
    context = Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=fake_backend,
        desktop=fake_desktop,
    )
    result = _invoke(
        ["runners", "pin", profile.id, "example@1.0", "--rebuild"],
        paths,
        context=context,
        input_text="y\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["args"][1] == "example@1.0"
    assert captured["kwargs"]["rebuild"] is True
    assert captured["kwargs"]["backend"] is fake_backend
    assert captured["kwargs"]["catalog"] is catalog
    assert captured["kwargs"]["desktop"] is fake_desktop
    assert captured["kwargs"]["confirm"] is False


def test_cli_runner_pin_rebuild_yes_skips_the_prompt(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.dump(paths.profiles / f"{profile.id}.toml")
    called: list[bool] = []

    def fake_pin(*args: Any, **kwargs: Any) -> Profile:
        called.append(True)
        return profile

    monkeypatch.setattr(cli_module, "pin_runner", fake_pin)
    context = Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=fake_backend,
        desktop=None,
    )
    result = _invoke(
        ["runners", "pin", profile.id, "example@1.0", "--rebuild", "--yes"],
        paths,
        context=context,
    )

    assert result.exit_code == 0, result.output
    assert called == [True]


def test_cli_runner_pin_rebuild_aborts_without_confirmation(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.dump(paths.profiles / f"{profile.id}.toml")
    called: list[bool] = []

    def fake_pin(*args: Any, **kwargs: Any) -> Profile:
        called.append(True)
        return profile

    monkeypatch.setattr(cli_module, "pin_runner", fake_pin)
    context = Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=fake_backend,
        desktop=None,
    )
    result = _invoke(
        ["runners", "pin", profile.id, "example@1.0", "--rebuild"],
        paths,
        context=context,
        input_text="n\n",
    )

    assert result.exit_code != 0
    assert called == []
