"""One-command update: scan for the newest installer and reinstall in place.

The update path is exercised with the recording fake backend and fake desktop:
the fake installer writes a program tree and a registry diff, so exe/version
re-detection runs for real. Tests pin the version-over-mtime scan, the
"nothing newer" no-op, profile preservation, the tri-state verify contract, and
the CLI flag surface.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.backends.base import BackendTimeout
from winbridge.cli import app
from winbridge.core import update as update_core
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import (
    UnsupportedEnvironment,
    UsageError,
    VerifyFailed,
    WinBridgeError,
)
from winbridge.core.profile import (
    ExposedPath,
    InstallerSpec,
    LastVerified,
    PinSpec,
    Profile,
    WebView2Runtime,
)
from winbridge.core.state import Paths
from winbridge.core.update import find_newest_installer, update_app
from winbridge.core.verify import VerifyResult

MakeInstaller = Callable[..., Path]
MakeProfile = Callable[..., Profile]


@pytest.fixture(autouse=True)
def _fast_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real quiescence/verify code but make its waits test-sized."""
    monkeypatch.setattr(update_core, "_INSTALL_SETTLE_S", 0.0)
    monkeypatch.setattr(update_core, "_INSTALL_POLL_S", 0.0)
    monkeypatch.setattr(update_core, "_VERIFY_POLL_S", 0.001)
    monkeypatch.setattr(update_core, "_VERIFY_CAPTURE_S", 0.01)
    monkeypatch.setattr(update_core, "_VERIFY_TIMEOUT_S", 1.0)


def _versioned(
    make_fake_installer: MakeInstaller,
    directory: Path,
    version: str,
    **installer_kwargs: Any,
) -> Path:
    """Create a fake installer named with its version inside ``directory``."""
    name = f"Example App {version}.exe"
    produced = make_fake_installer(
        filename=name, version=version, **installer_kwargs
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    produced.replace(target)
    return target


def _profile_for(
    make_profile: MakeProfile,
    installer: Path,
    *,
    glob: str = "*Example*.exe",
    **overrides: Any,
) -> Profile:
    spec = InstallerSpec(
        path=str(installer),
        dir=str(installer.parent),
        glob=glob,
        type="nsis",
        silent_args=["/S"],
        extra={"channel": "beta"},
    )
    overrides.setdefault("exe", r"C:\Program Files\Example\Example.exe")
    return make_profile(installer=spec, **overrides)


def _seed_prefix(fake_backend: Any, profile: Profile) -> Path:
    prefix = fake_backend.create_prefix(profile.id)
    (prefix / "sentinel").write_text("keep me", encoding="utf-8")
    return prefix


# --- newest installer scan ---------------------------------------------------


def test_newest_prefers_parsed_version_over_mtime(tmp_path: Path) -> None:
    versioned = tmp_path / "app-1.10.0-setup.exe"
    newer_mtime = tmp_path / "app-1.9.0-setup.exe"
    versioned.write_text("")
    newer_mtime.write_text("")
    future = time.time() + 3600
    os.utime(newer_mtime, (future, future))

    assert find_newest_installer(tmp_path, "*setup*.exe") == versioned


def test_newest_versioned_file_beats_a_much_newer_unversioned_file(
    tmp_path: Path,
) -> None:
    versioned = tmp_path / "app-2.0.0-setup.exe"
    unversioned = tmp_path / "app-setup.exe"
    versioned.write_text("")
    unversioned.write_text("")
    future = time.time() + 3600
    os.utime(versioned, (1_000_000_000, 1_000_000_000))
    os.utime(unversioned, (future, future))

    assert find_newest_installer(tmp_path, "*setup*.exe") == versioned


def test_newest_uses_mtime_for_equal_versions(tmp_path: Path) -> None:
    older = tmp_path / "app-2.0.0-setup.exe"
    newer = tmp_path / "app-2.0.0-setup (1).exe"
    older.write_text("")
    newer.write_text("")
    os.utime(older, (1000000000, 1000000000))
    os.utime(newer, (1000000001, 1000000001))

    assert find_newest_installer(tmp_path, "*setup*.exe") == newer


def test_newest_falls_back_to_mtime_without_a_version(tmp_path: Path) -> None:
    old = tmp_path / "setup.exe"
    new = tmp_path / "setup-latest.exe"
    old.write_text("")
    new.write_text("")
    os.utime(old, (1000000000, 1000000000))
    os.utime(new, (1000000001, 1000000001))

    assert find_newest_installer(tmp_path, "*setup*.exe") == new


def test_newest_ignores_directories_and_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "app-1.0.0-setup.exe"
    real.write_text("")
    (tmp_path / "app-9.0.0-setup.exe").mkdir()
    (tmp_path / "app-8.0.0-setup.exe").symlink_to(real)

    assert find_newest_installer(tmp_path, "*setup*.exe") == real


def test_newest_returns_none_for_a_missing_directory_or_no_match(tmp_path: Path) -> None:
    assert find_newest_installer(tmp_path / "nope", "*setup*.exe") is None
    (tmp_path / "other.exe").write_text("")
    assert find_newest_installer(tmp_path, "*setup*.exe") is None


# --- update ------------------------------------------------------------------


def test_update_reinstalls_in_place_and_keeps_profile(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(
        make_profile,
        old,
        extra={"custom": {"nested": True}},
        pin=PinSpec(match="^Example$", extra={"position": 2}),
        exposed=[
            ExposedPath(
                host="/home/me/docs",
                guest=r"C:\users\me\docs",
                extra={"read_only": True},
            )
        ],
        created_at="2026-01-01T00:00:00Z",
    )
    prefix = _seed_prefix(fake_backend, profile)

    updated = update_app(
        profile, paths, fake_backend, fake_desktop, installer=new, verify=False
    )

    assert updated.id == profile.id
    assert updated.name == profile.name
    assert updated.schema == profile.schema
    assert updated.backend == profile.backend
    assert updated.installer is not None
    assert updated.installer.path == str(new.resolve())
    assert updated.installer.dir == str(new.parent.resolve())
    assert updated.installer.type == "nsis"
    assert updated.installer.silent_args == ["/S"]
    assert updated.installer.extra == {"channel": "beta"}
    assert updated.schemes == profile.schemes
    assert updated.created_at == profile.created_at
    assert updated.extra == {"custom": {"nested": True}}
    assert updated.pin == profile.pin
    assert updated.exposed == profile.exposed
    assert updated.last_verified is None
    # The caller's profile is not mutated.
    assert profile.installer is not None
    assert profile.installer.path == str(old)
    # Update owns the prefix while it works but never deletes it.
    assert prefix.is_dir()
    assert (prefix / "sentinel").read_text(encoding="utf-8") == "keep me"
    assert fake_backend.removed == []
    installed_prefix, program, args = fake_backend.installed[-1]
    assert installed_prefix == prefix
    assert str(program).startswith("C:\\windows\\temp\\winbridge-")
    assert args == ["/S"]
    assert fake_backend.install_payloads[-1] == new.read_bytes()

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(new.resolve())
    assert persisted.installer.extra == {"channel": "beta"}
    assert persisted.extra == {"custom": {"nested": True}}
    assert persisted.webview2 is not None
    assert persisted.webview2.version == "156.0.4314.42"


def test_update_prefers_the_recorded_exe_when_only_a_helper_changed(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    app_guest = r"C:\Program Files\Example\Example.exe"
    profile = _profile_for(make_profile, old, exe=app_guest)
    prefix = _seed_prefix(fake_backend, profile)
    app = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"MZ" + b"\x00" * 4096)

    def helper_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        helper = (
            prefix
            / "drive_c"
            / "Program Files"
            / "Example"
            / "runtimehost.exe"
        )
        helper.write_bytes(b"MZ" + b"\x00" * 8192)

    fake_backend.on_install = helper_only

    updated = update_app(
        profile, paths, fake_backend, fake_desktop, installer=new, verify=False
    )

    assert updated.exe == app_guest


def test_update_reports_a_missing_recorded_exe_with_an_exe_hint(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(
        make_profile,
        old,
        exe=r"C:\Program Files\Example\Missing.exe",
    )
    _seed_prefix(fake_backend, profile)

    def helper_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        helper = (
            prefix
            / "drive_c"
            / "Program Files"
            / "Example"
            / "runtimehost.exe"
        )
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b"MZ" + b"\x00" * 8192)

    fake_backend.on_install = helper_only

    with pytest.raises(WinBridgeError, match=r"recorded executable .*--exe"):
        update_app(
            profile, paths, fake_backend, fake_desktop, installer=new, verify=False
        )


def test_update_accepts_a_local_only_reinstall(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    install_root = r"C:\users\me\AppData\Local\Example"
    new = _versioned(
        make_fake_installer,
        tmp_path / "downloads",
        "2.0.0",
        install_root=install_root,
    )
    profile = _profile_for(
        make_profile,
        old,
        exe=rf"{install_root}\Example.exe",
    )
    profile.dump(paths.profiles / f"{profile.id}.toml")
    _seed_prefix(fake_backend, profile)

    updated = update_app(profile, paths, fake_backend, installer=new, verify=False)

    assert updated.exe == rf"{install_root}\Example.exe"
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.exe == rf"{install_root}\Example.exe"


def test_update_accepts_a_same_size_same_mtime_content_rewrite(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old-build")
    os.utime(exe, ns=(123_456_789, 123_456_789))
    fixed_mtime = exe.stat().st_mtime_ns
    before_ctime = exe.stat().st_ctime_ns

    def rewrite_same_size(prefix: Path, _installer: Path, _args: list[str]) -> None:
        exe.write_bytes(b"new-build")
        os.utime(exe, ns=(fixed_mtime, fixed_mtime))

    fake_backend.on_install = rewrite_same_size

    updated = update_app(profile, paths, fake_backend, installer=new, verify=False)

    after = exe.stat()
    assert after.st_size == len(b"new-build")
    assert after.st_mtime_ns == fixed_mtime
    assert after.st_ctime_ns != before_ctime
    assert updated.exe == r"C:\Program Files\Example\Example.exe"


def test_update_rejects_a_chmod_only_change(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"unchanged-content")
    exe.chmod(0o644)
    before_ctime = exe.stat().st_ctime_ns

    def chmod_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        exe.chmod(0o600)

    fake_backend.on_install = chmod_only

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    after_ctime = exe.stat().st_ctime_ns
    assert after_ctime != before_ctime
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)


def test_update_rejects_a_chmod_only_change_to_an_unreadable_exe(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    guest = r"C:\Program Files\Example\Example.exe"
    profile = _profile_for(make_profile, old, exe=guest)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"unreadable-build")
    exe.chmod(0o000)

    def chmod_only(prefix: Path, _installer: Path, _args: list[str]) -> None:
        exe.chmod(0o600)

    fake_backend.on_install = chmod_only

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)


def test_update_same_build_requires_force(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    guest = r"C:\Program Files\Example\Example.exe"
    profile = _profile_for(make_profile, old, exe=guest)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"same-build")
    os.utime(exe, ns=(123_456_789, 123_456_789))

    def identical_rewrite(prefix: Path, _installer: Path, _args: list[str]) -> None:
        exe.write_bytes(b"same-build")
        os.utime(exe, ns=(123_456_789, 123_456_789))

    fake_backend.on_install = identical_rewrite

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)

    updated = update_app(
        profile, paths, fake_backend, installer=new, verify=False, force=True
    )

    output = capsys.readouterr().out
    assert "no executable change detected" in output
    assert "--force accepted the recorded executable" in output
    assert updated.installer is not None
    assert updated.installer.path == str(new.resolve())
    assert updated.exe == guest


def test_update_force_still_requires_the_recorded_exe(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    _seed_prefix(fake_backend, profile)
    fake_backend.on_install = None

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(
            profile, paths, fake_backend, installer=new, verify=False, force=True
        )

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)


def test_update_force_rejects_a_failed_then_noop_probe(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    installer = downloads / "Example Setup.exe"
    installer.write_bytes(b"MZ" + b"\x00" * 64)
    guest = r"C:\Program Files\Example\Example.exe"
    spec = InstallerSpec(
        path=str(installer),
        dir=str(downloads),
        glob="*Setup*.exe",
        type="unknown",
        silent_args=[],
    )
    profile = make_profile(installer=spec, exe=guest)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"unchanged")
    fake_backend.install_exit_codes = [1, 0]
    fake_backend.on_install = None
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
        update_app(
            profile, paths, fake_backend, installer=installer, verify=False, force=True
        )

    assert [args for _, _, args in fake_backend.installed] == [["/S"], ["/silent"]]
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(installer.resolve())


def test_update_force_rejects_a_symlinked_executable_path(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    guest = r"C:\Program Files\Example\Example.exe"
    profile = _profile_for(make_profile, old, exe=guest)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Example.exe").write_bytes(b"host-file")
    program_dir = prefix / "drive_c" / "Program Files"
    program_dir.mkdir(parents=True)
    (program_dir / "Example").symlink_to(outside, target_is_directory=True)
    fake_backend.on_install = None

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(
            profile, paths, fake_backend, installer=new, verify=False, force=True
        )

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)


def test_update_rejects_an_untouched_prefix(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    prefix = _seed_prefix(fake_backend, profile)
    exe = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"unchanged")
    fake_backend.on_install = None

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)


def test_update_rejects_a_roaming_config_only_install(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    _seed_prefix(fake_backend, profile)

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
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)
    assert persisted.exe == profile.exe


def test_update_keeps_the_profile_when_webview2_detection_fails(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(
        make_profile,
        old,
        webview2=WebView2Runtime(version="156.0.4314.42", source="latest"),
    )
    prefix = _seed_prefix(fake_backend, profile)

    def timeout(*args: Any, **kwargs: Any) -> str | None:
        raise BackendTimeout("registry query exceeded its 300s timeout")

    monkeypatch.setattr(update_core.webview2, "installed_version", timeout)

    updated = update_app(
        profile, paths, fake_backend, fake_desktop, installer=new, verify=False
    )

    # The new installer/exe/version are persisted despite the metadata failure.
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(new.resolve())
    assert persisted.exe == r"C:\Program Files\Example\Example.exe"
    assert updated.webview2 is not None
    assert updated.webview2.version == "156.0.4314.42"
    assert persisted.webview2 is not None
    assert persisted.webview2.version == "156.0.4314.42"
    assert prefix.is_dir()
    assert "metadata query failed" in capsys.readouterr().out


def test_update_kills_before_reinstalling(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    update_app(profile, paths, fake_backend, installer=new, verify=False)

    calls = fake_backend.calls
    kill_index = calls.index("wineserver -k")
    install_index = next(
        index for index, call in enumerate(calls) if call.startswith("install ")
    )
    assert kill_index < install_index


def test_update_scan_uses_the_newest_matching_installer(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    downloads = tmp_path / "downloads"
    old = _versioned(make_fake_installer, downloads, "1.0.0")
    new = _versioned(make_fake_installer, downloads, "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    updated = update_app(profile, paths, fake_backend, verify=False)

    assert fake_backend.install_payloads[-1] == new.read_bytes()
    assert updated.installer is not None
    assert updated.installer.path == str(new.resolve())
    assert updated.installer.glob == "*Example*.exe"


def test_update_reinstalls_a_stable_filename_even_when_it_is_recorded(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    stable = downloads / "setup.exe"
    produced = make_fake_installer(filename="setup.exe", version="1.0.0")
    produced.replace(stable)
    profile = _profile_for(make_profile, stable, glob="*setup*.exe")
    prefix = _seed_prefix(fake_backend, profile)
    # The same filename is replaced by a newer build; nothing about the path
    # changes, so update must reinstall it rather than skip it.
    produced = make_fake_installer(filename="setup.exe", version="2.0.0")
    produced.replace(stable)
    fake_backend.calls.clear()

    updated = update_app(profile, paths, fake_backend, verify=False)

    assert fake_backend.install_payloads[-1] == stable.read_bytes()
    assert "wineserver -k" in fake_backend.calls
    assert prefix.is_dir()
    assert updated.installer is not None
    assert updated.installer.path == str(stable.resolve())
    assert (paths.profiles / f"{profile.id}.toml").is_file()


def test_update_installs_the_newest_scan_match_even_below_the_recorded_version(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    recorded_dir = tmp_path / "previous"
    recorded_dir.mkdir()
    recorded = recorded_dir / "Example App 3.0.0.exe"
    recorded.write_bytes(b"MZ" + b"Nullsoft Install System v3.10")
    downloads = tmp_path / "downloads"
    _versioned(make_fake_installer, downloads, "1.0.0")
    newest = _versioned(make_fake_installer, downloads, "2.0.0")
    spec = InstallerSpec(
        path=str(recorded),
        dir=str(downloads),
        glob="*Example*.exe",
        type="nsis",
        silent_args=["/S"],
    )
    profile = make_profile(
        installer=spec,
        exe=r"C:\Program Files\Example\Example.exe",
    )
    _seed_prefix(fake_backend, profile)

    update_app(profile, paths, fake_backend, verify=False)

    assert fake_backend.install_payloads[-1] == newest.read_bytes()


def test_update_zero_exit_without_an_executable_fails_and_keeps_the_old_profile(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    _seed_prefix(fake_backend, profile)
    # Exit 0 but write nothing: the classic wrong-silent-flag failure.
    fake_backend.on_install = None

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)
    assert paths.prefix_for(profile.id).is_dir()


def test_update_zero_exit_probe_tries_the_next_flag_then_fails(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    unknown = downloads / "Example Setup.exe"
    unknown.write_bytes(b"MZ" + b"\x00" * 64)
    spec = InstallerSpec(
        path=str(unknown),
        dir=str(downloads),
        glob="*Setup*.exe",
        type="unknown",
        silent_args=[],
    )
    profile = make_profile(installer=spec)
    _seed_prefix(fake_backend, profile)
    fake_backend.on_install = None

    with pytest.raises(WinBridgeError, match="no new executable"):
        update_app(profile, paths, fake_backend, installer=unknown, verify=False)

    assert [args for _, _, args in fake_backend.installed] == [
        ["/S"],
        ["/silent"],
    ]


def test_update_explicit_installer_overrides_the_scan(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    downloads = tmp_path / "downloads"
    old = _versioned(make_fake_installer, downloads, "1.0.0")
    _versioned(make_fake_installer, downloads, "3.0.0")
    explicit = _versioned(make_fake_installer, downloads, "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    updated = update_app(
        profile, paths, fake_backend, installer=explicit, verify=False
    )

    assert fake_backend.install_payloads[-1] == explicit.read_bytes()
    assert updated.installer is not None
    assert updated.installer.path == str(explicit.resolve())


def test_update_rejects_a_missing_explicit_installer(
    paths: Paths, profile: Profile, fake_backend: Any
) -> None:
    missing = paths.root / "missing.exe"

    with pytest.raises(UsageError, match="installer not found"):
        update_app(profile, paths, fake_backend, installer=missing, verify=False)


def test_update_without_a_recorded_installer_requires_one(
    paths: Paths, make_profile: MakeProfile, fake_backend: Any
) -> None:
    profile = make_profile(installer=None)

    with pytest.raises(UsageError, match="installer"):
        update_app(profile, paths, fake_backend, verify=False)


def test_update_with_no_matching_installer_fails_clearly(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    spec = InstallerSpec(
        path=str(downloads / "gone.exe"),
        dir=str(downloads),
        glob="*gone*.exe",
        type="nsis",
        silent_args=["/S"],
    )
    profile = make_profile(installer=spec)

    with pytest.raises(WinBridgeError, match="no installer matching"):
        update_app(profile, paths, fake_backend, verify=False)


def test_update_requires_an_existing_prefix(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)

    with pytest.raises(WinBridgeError, match="prefix"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    assert fake_backend.installed == []


def test_update_install_failure_keeps_the_old_profile(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    profile.dump(paths.profiles / f"{profile.id}.toml")
    _seed_prefix(fake_backend, profile)
    fake_backend.install_exit_code = 1

    with pytest.raises(WinBridgeError, match="silent install failed"):
        update_app(profile, paths, fake_backend, installer=new, verify=False)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.installer is not None
    assert persisted.installer.path == str(old)
    assert paths.prefix_for(profile.id).is_dir()


def test_update_no_verify_never_launches(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("verify must not run with --no-verify")

    monkeypatch.setattr(update_core, "verify_app", fail)
    updated = update_app(
        profile, paths, fake_backend, fake_desktop, installer=new, verify=False
    )

    assert updated.last_verified is None
    assert fake_desktop.windows_calls == 0


def test_update_auto_verify_records_the_new_version_and_keeps_extra(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    previous_verified = LastVerified(
        app_version="1.0.0",
        result="pass",
        first_paint_s=1.5,
        at="2026-01-01T00:00:00Z",
        extra={"evidence": "keep"},
    )
    profile = _profile_for(make_profile, old, last_verified=previous_verified)
    _seed_prefix(fake_backend, profile)

    updated = update_app(
        profile, paths, fake_backend, fake_desktop, installer=new, verify=None
    )

    assert updated.last_verified is not None
    assert updated.last_verified.result == "pass"
    assert updated.last_verified.app_version == "2.0.0"
    assert updated.last_verified.extra == {"evidence": "keep"}
    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.last_verified is not None
    assert persisted.last_verified.app_version == "2.0.0"


def test_update_auto_verify_skips_when_the_desktop_cannot_verify(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    updated = update_app(profile, paths, fake_backend, installer=new, verify=None)

    assert updated.last_verified is not None
    assert updated.last_verified.result == "skipped"
    assert updated.last_verified.app_version == "2.0.0"


def test_update_explicit_verify_is_unsupported_without_a_desktop(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    with pytest.raises(UnsupportedEnvironment, match="desktop not supported"):
        update_app(profile, paths, fake_backend, installer=new, verify=True)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.last_verified is not None
    assert persisted.last_verified.result == "skipped"


def test_update_verify_failure_is_exit_four_and_keeps_the_new_version(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    fake_desktop: Any,
    make_fake_installer: MakeInstaller,
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)
    fake_desktop.blank_screenshots = True

    with pytest.raises(VerifyFailed, match="evidence"):
        update_app(profile, paths, fake_backend, fake_desktop, installer=new, verify=None)

    persisted = Profile.load(paths.profiles / f"{profile.id}.toml")
    assert persisted.last_verified is not None
    assert persisted.last_verified.result == "fail"
    assert persisted.last_verified.app_version == "2.0.0"
    assert persisted.installer is not None
    assert persisted.installer.path == str(new.resolve())
    assert list(paths.evidence.rglob("frame-*.png"))


def test_update_background_failure_reports_evidence_without_frames(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
    make_fake_installer: MakeInstaller,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = _versioned(make_fake_installer, tmp_path / "downloads", "1.0.0")
    new = _versioned(make_fake_installer, tmp_path / "downloads", "2.0.0")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)
    run_dir = paths.evidence / "example-app" / "background-run"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        update_core,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(
            False,
            None,
            None,
            [],
            "background",
            evidence_dir=run_dir,
        ),
    )

    with pytest.raises(VerifyFailed, match="background") as error:
        update_app(profile, paths, fake_backend, installer=new, verify=None)

    assert str(run_dir) in str(error.value)
    output = capsys.readouterr().out
    assert f"[update] verify frames: 0 kept in {run_dir}" in output


def test_update_uses_the_new_installer_kind_for_silent_flags(
    tmp_path: Path,
    paths: Paths,
    make_profile: MakeProfile,
    fake_backend: Any,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    old = downloads / "Example App 1.0.0.exe"
    old.write_bytes(b"MZ" + b"Nullsoft Install System v3.10")
    new = downloads / "Example App 2.0.0.msi"
    new.write_bytes(b"msi package")
    profile = _profile_for(make_profile, old)
    _seed_prefix(fake_backend, profile)

    def write_program(prefix: Path, _installer: Path, _args: list[str]) -> None:
        target = prefix / "drive_c" / "Program Files" / "Example" / "Example.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ")

    fake_backend.on_install = write_program
    update_app(profile, paths, fake_backend, installer=new, verify=False)

    prefix, program, args = fake_backend.installed[-1]
    assert prefix == paths.prefix_for(profile.id)
    assert program == Path("msiexec")
    assert args[0] == "/i"
    assert args[1].endswith(".msi")
    assert args[2:] == ["/qn", "/norestart"]


# --- CLI wiring --------------------------------------------------------------


def test_cli_update_passes_flags_and_desktop(
    tmp_path: Path,
    paths: Paths,
    profile: Profile,
    fake_backend: Any,
    fake_desktop: Any,
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.dump(paths.profiles / f"{profile.id}.toml")
    installer = tmp_path / "Example App 2.0.0.exe"
    installer.write_bytes(b"MZ")
    captured: dict[str, Any] = {}

    def fake_update(*args: Any, **kwargs: Any) -> Profile:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return profile

    monkeypatch.setattr(cli_module, "update_app", fake_update)
    context = Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=fake_backend,
        desktop=fake_desktop,
    )
    result = CliRunner().invoke(
        app,
        [
            "update",
            profile.id,
            "--installer",
            str(installer),
            "--no-verify",
            "--force",
        ],
        obj={"paths": paths, "context": context},
    )

    assert result.exit_code == 0, result.output
    assert captured["args"][0] == profile
    assert captured["kwargs"] == {
        "installer": installer,
        "verify": False,
        "force": True,
    }
    assert f"update: {profile.id}" in result.output
