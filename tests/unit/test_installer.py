"""Installer detection, silent flags, and quiescence, all without Wine.

The installer engine is the one place that decides how a Windows setup program
is asked to run silently and when its prefix has stopped changing. Both are
tested with a fake backend and a monkeypatched ``/proc`` probe, so no Wine
prefix and no real subprocess is required.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path
from typing import Any

import pytest

from winbridge.backends.base import BackendTimeout
from winbridge.core import installer as installer_module
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.installer import (
    InstallerTimeout,
    InstallResult,
    detect_type,
    install,
    silent_args,
)

HEADER_LIMIT = 256 * 1024


def make_installer(tmp_path: Path, body: bytes = b"", name: str = "setup.exe") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A writable prefix root; staging creates ``drive_c/windows/temp``."""
    target = tmp_path / "prefix"
    target.mkdir()
    return target


def probe_factory(
    monkeypatch: pytest.MonkeyPatch,
    values: list[set[int]],
    *,
    default: set[int] | None = None,
) -> list[Path]:
    """Install a fake process probe that records every call."""
    calls: list[Path] = []

    def probe(prefix: Path) -> set[int]:
        index = len(calls)
        calls.append(prefix)
        if index < len(values):
            return set(values[index])
        return set() if default is None else set(default)

    monkeypatch.setattr(installer_module, "processes_for_prefix", probe)
    return calls


# --- type detection ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body", "expected"),
    [
        ("app-setup.exe", b"Nullsoft Install System v3.09", "nsis"),
        ("app-installer.exe", b"Inno Setup Setup Data (6.2.0)", "inno"),
        ("app-bundle.exe", b"WixBundle Burn engine", "burn"),
        ("app.msi", b"", "msi"),
        ("APP.MSI", b"", "msi"),
        ("mystery.exe", b"no recognizable setup header", "unknown"),
        ("empty.exe", b"", "unknown"),
    ],
)
def test_detects_installer_types(
    tmp_path: Path, name: str, body: bytes, expected: str
) -> None:
    assert detect_type(make_installer(tmp_path, body, name)) == expected


def test_msi_extension_wins_over_a_pe_header(tmp_path: Path) -> None:
    installer = make_installer(tmp_path, b"Nullsoft Install System", "app.msi")

    assert detect_type(installer) == "msi"


def test_detection_reads_only_the_first_256_kib(tmp_path: Path) -> None:
    marker = b"Nullsoft"

    inside = make_installer(
        tmp_path, b"\0" * (HEADER_LIMIT - len(marker)) + marker, "inside.exe"
    )
    beyond = make_installer(tmp_path, b"\0" * HEADER_LIMIT + marker, "beyond.exe")

    assert detect_type(inside) == "nsis"
    assert detect_type(beyond) == "unknown"


def test_detection_refuses_a_missing_installer(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="installer"):
        detect_type(tmp_path / "missing.exe")


def test_detection_refuses_a_directory(tmp_path: Path) -> None:
    directory = tmp_path / "setup.exe"
    directory.mkdir()

    with pytest.raises(UsageError, match="installer"):
        detect_type(directory)


# --- silent flags ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("nsis", ["/S"]),
        ("inno", ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]),
        ("msi", ["msiexec", "/i", "/qn", "/norestart"]),
        ("burn", ["/quiet", "/norestart"]),
        ("unknown", []),
    ],
)
def test_silent_args_are_exact(kind: str, expected: list[str]) -> None:
    assert silent_args(kind) == expected


def test_silent_args_returns_a_fresh_list() -> None:
    first = silent_args("nsis")
    first.append("--mutated")

    assert silent_args("nsis") == ["/S"]


# --- install and quiescence --------------------------------------------------


def test_installs_and_runs_the_staged_installer(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 0
    assert result.duration_s >= 0
    installed_prefix, program, args = fake_backend.installed[-1]
    assert installed_prefix == prefix
    assert str(program).startswith("C:\\windows\\temp\\winbridge-")
    assert str(program).endswith(".exe")
    assert args == ["/S"]
    assert isinstance(result, InstallResult)
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*"))


def test_waits_for_installer_children_that_outlive_the_launcher(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    probe_factory(monkeypatch, [{101}, {101}, set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 0


def test_brief_sample_waits_for_installer_children(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's sample: three probes, the last one empty."""
    probes = iter([{101}, {101}, set()])
    monkeypatch.setattr(installer_module, "processes_for_prefix", lambda _: next(probes))
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path)

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.1,
        poll_s=0.01,
    )

    assert result.exit_code == 0


def test_observes_the_settle_window_before_the_first_probe(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher can exit before its children become visible."""
    installer = make_installer(tmp_path)
    elapsed: list[float] = []
    started = time.monotonic()

    def probe(_prefix: Path) -> set[int]:
        elapsed.append(time.monotonic() - started)
        return set()

    monkeypatch.setattr(installer_module, "processes_for_prefix", probe)

    install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.05,
        poll_s=0.005,
    )

    assert elapsed
    assert elapsed[0] >= 0.045


def test_reports_a_nonzero_installer_exit_code(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    fake_backend.install_exit_code = 7
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 7


def test_quiescence_runs_even_when_the_installer_failed(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    fake_backend.install_exit_code = 1
    calls = probe_factory(monkeypatch, [{303}, set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 1
    assert calls


def test_installer_process_timeout_is_reported_with_the_log(
    fake_backend: Any, tmp_path: Path
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path, b"Nullsoft", "app-setup.exe")
    fake_backend.install_timeout_error = BackendTimeout("installer exceeded its 900s timeout")

    log = tmp_path / "evidence" / "installer.log"
    with pytest.raises(InstallerTimeout, match="exceeded") as error:
        install(fake_backend, prefix, installer, ["/S"], log_path=log)

    assert str(log) in str(error.value)
    assert log.is_file()
    staged = list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*"))
    assert len(staged) == 1


def test_timeout_names_the_log_path_and_the_surviving_pids(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    log = tmp_path / "evidence" / "installer.log"
    probe_factory(monkeypatch, [], default={404, 405})

    with pytest.raises(WinBridgeError, match="did not settle") as caught:
        install(
            fake_backend,
            prefix,
            installer,
            ["/S"],
            timeout_s=0.05,
            settle_s=0.05,
            poll_s=0.01,
            log_path=log,
        )

    message = str(caught.value)
    assert str(log) in message
    assert "404" in message


def test_creates_the_log_before_the_installer_runs(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert fake_backend.install_saw_log == [True]
    assert all(path is not None for path in fake_backend.install_logs)


def test_uses_the_evidence_directory_by_default(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = make_installer(tmp_path)
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        fake_backend.paths.prefix_for("example-app"),
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.log_path.name == "installer.log"
    assert result.log_path.is_relative_to(fake_backend.paths.evidence)
    assert result.log_path.is_file()


def test_explicit_log_path_is_created_privately(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    log = tmp_path / "evidence" / "nested" / "installer.log"
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0.01,
        poll_s=0.005,
        log_path=log,
    )

    assert result.log_path == log
    assert fake_backend.install_logs == [log]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.read_text(encoding="utf-8")


def test_msi_installers_run_through_msiexec(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path, b"msi-payload", "app.msi")
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        silent_args("msi"),
        settle_s=0.01,
        poll_s=0.005,
    )

    installed_prefix, program, argv = fake_backend.installed[-1]
    assert installed_prefix == prefix
    assert program == Path("msiexec")
    assert argv[0] == "/i"
    assert argv[1].startswith("C:\\windows\\temp\\winbridge-")
    assert argv[1].endswith(".msi")
    assert argv[2:] == ["/qn", "/norestart"]
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.msi"))


def test_msi_flags_without_the_msiexec_prefix_are_still_routed(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path, name="app.msi")
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        ["/qn", "/norestart"],
        settle_s=0.01,
        poll_s=0.005,
    )

    _, program, argv = fake_backend.installed[-1]
    assert program == Path("msiexec")
    assert argv[0] == "/i"
    assert argv[1].startswith("C:\\windows\\temp\\winbridge-")
    assert argv[2:] == ["/qn", "/norestart"]


def test_msi_package_is_staged_inside_the_prefix_during_install(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    payload = b"msi-package-bytes"
    installer = make_installer(tmp_path, payload, "app.msi")
    seen: dict[str, Any] = {}
    original = fake_backend.install

    def install_spy(
        install_prefix: Path,
        program: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        temp_dir = install_prefix / "drive_c" / "windows" / "temp"
        staged = sorted(temp_dir.glob("winbridge-*.msi"))
        seen["names"] = [item.name for item in staged]
        seen["content"] = staged[0].read_bytes() if staged else b""
        seen["argv"] = list(args)
        return original(
            install_prefix, program, args, log_path=log_path, timeout_s=timeout_s
        )

    monkeypatch.setattr(fake_backend, "install", install_spy)
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        silent_args("msi"),
        settle_s=0.01,
        poll_s=0.005,
    )

    assert seen["names"]
    assert seen["content"] == payload
    assert seen["argv"][1] == f"C:\\windows\\temp\\{seen['names'][0]}"
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.msi"))


def test_staged_msi_is_removed_after_a_failed_install(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path, name="app.msi")
    fake_backend.install_exit_code = 3
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        silent_args("msi"),
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 3
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.msi"))


def test_timeout_keeps_the_staged_package_for_evidence(
    fake_backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    installer = make_installer(tmp_path, name="app.msi")
    probe_factory(monkeypatch, [], default={909})

    with pytest.raises(WinBridgeError, match="did not settle"):
        install(
            fake_backend,
            prefix,
            installer,
            silent_args("msi"),
            timeout_s=0.05,
            settle_s=0.01,
            poll_s=0.01,
        )

    staged = list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.msi"))
    assert len(staged) == 1


def test_exe_installer_is_staged_inside_the_prefix_during_install(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"Nullsoft Install System v3.10"
    installer = make_installer(tmp_path, payload, "Example-setup.exe")
    seen: dict[str, Any] = {}
    original = fake_backend.install

    def install_spy(
        install_prefix: Path,
        program: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        temp_dir = install_prefix / "drive_c" / "windows" / "temp"
        staged = sorted(temp_dir.glob("winbridge-*.exe"))
        seen["names"] = [item.name for item in staged]
        seen["content"] = staged[0].read_bytes() if staged else b""
        seen["program"] = str(program)
        seen["argv"] = list(args)
        return original(
            install_prefix, program, args, log_path=log_path, timeout_s=timeout_s
        )

    monkeypatch.setattr(fake_backend, "install", install_spy)
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        silent_args("nsis"),
        settle_s=0.01,
        poll_s=0.005,
    )

    assert seen["names"]
    assert seen["content"] == payload
    assert seen["program"] == f"C:\\windows\\temp\\{seen['names'][0]}"
    assert seen["argv"] == ["/S"]
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.exe"))


def test_staged_exe_is_removed_after_a_failed_install(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path, name="Example-setup.exe")
    fake_backend.install_exit_code = 2
    probe_factory(monkeypatch, [set()])

    result = install(
        fake_backend,
        prefix,
        installer,
        silent_args("nsis"),
        settle_s=0.01,
        poll_s=0.005,
    )

    assert result.exit_code == 2
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*.exe"))


def test_non_msi_arguments_are_passed_through_verbatim(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path, b"Nullsoft Install System")
    probe_factory(monkeypatch, [set()])

    install(
        fake_backend,
        prefix,
        installer,
        silent_args("nsis"),
        settle_s=0.01,
        poll_s=0.005,
    )

    installed_prefix, program, args = fake_backend.installed[-1]
    assert installed_prefix == prefix
    assert str(program).startswith("C:\\windows\\temp\\winbridge-")
    assert args == ["/S"]


def test_symlinked_temp_refuses_app_installer_staging(
    fake_backend: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (prefix / "drive_c" / "windows" / "temp").symlink_to(
        outside, target_is_directory=True
    )
    installer = make_installer(tmp_path)

    with pytest.raises(WinBridgeError, match="symlink"):
        install(
            fake_backend,
            prefix,
            installer,
            ["/S"],
            settle_s=0.01,
            poll_s=0.005,
        )

    assert fake_backend.installed == []
    assert list(outside.iterdir()) == []


def test_zero_settle_window_skips_waiting(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = make_installer(tmp_path)
    calls = probe_factory(monkeypatch, [], default={505})

    result = install(
        fake_backend,
        prefix,
        installer,
        ["/S"],
        settle_s=0,
        poll_s=0.005,
        timeout_s=5,
    )

    assert result.exit_code == 0
    assert calls == []
