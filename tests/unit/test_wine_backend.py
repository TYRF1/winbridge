"""Wine backend, prefix lifecycle, and /proc helpers, all without real Wine."""

from __future__ import annotations

import io
import logging
import os
import signal
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from winbridge.backends.base import BackendTimeout
from winbridge.backends.wine import WineBackend, wine_env
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.prefix import PrefixStillRunning, kill_prefix, strip_z_drive
from winbridge.core.state import Paths
from winbridge.util.proc import pid_tree, processes_for_prefix

PREFIX = Path("/state/prefixes/app")


def calls_log(tmp_path: Path) -> str:
    path = tmp_path / "calls.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def run_log(tmp_path: Path, needle: str) -> int:
    return calls_log(tmp_path).count(needle)


def make_backend(tmp_path: Path, fake_runner: Path) -> WineBackend:
    return WineBackend(fake_runner, Paths.from_root(tmp_path / "state"))


# --- environment -------------------------------------------------------------


def test_wine_env_forces_x11_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WINBRIDGE_WINEDEBUG", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    env = wine_env(tmp_path / "prefix", tmp_path / "runner")

    assert env["WINEPREFIX"] == str(tmp_path / "prefix")
    assert "WAYLAND_DISPLAY" not in env
    assert env["WINEDEBUG"] == "-all"


def test_wine_env_removes_the_wayland_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYLAND_SOCKET", "7")

    assert "WAYLAND_SOCKET" not in wine_env(tmp_path / "p", tmp_path / "r")


def test_wine_env_overrides_inherited_winedebug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WINBRIDGE_WINEDEBUG", raising=False)
    monkeypatch.setenv("WINEDEBUG", "+all")

    assert wine_env(tmp_path / "p", tmp_path / "r")["WINEDEBUG"] == "-all"


def test_wine_env_uses_explicit_winedebug_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINBRIDGE_WINEDEBUG", "+cursor")

    assert wine_env(tmp_path / "p", tmp_path / "r")["WINEDEBUG"] == "+cursor"


def test_wine_env_ignores_an_empty_winedebug_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINBRIDGE_WINEDEBUG", "")

    assert wine_env(tmp_path / "p", tmp_path / "r")["WINEDEBUG"] == "-all"


def test_wine_env_sets_winearch_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINEARCH", "win64")

    assert "WINEARCH" not in wine_env(tmp_path / "p", tmp_path / "r")
    assert wine_env(tmp_path / "p", tmp_path / "r", arch="win32")["WINEARCH"] == "win32"


def test_wine_env_rejects_an_unknown_arch(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="WINEARCH"):
        wine_env(tmp_path / "p", tmp_path / "r", arch="win95")


def test_wine_env_prepends_the_runner_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")

    env = wine_env(tmp_path / "p", tmp_path / "runner")

    assert env["PATH"].split(os.pathsep) == [str(tmp_path / "runner" / "bin"), "/usr/bin"]


def test_wine_env_does_not_mutate_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WINEPREFIX", raising=False)

    wine_env(tmp_path / "p", tmp_path / "r")

    assert "WINEPREFIX" not in os.environ


# --- backend construction ----------------------------------------------------


def test_backend_is_runner_bound(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    assert backend.name == "wine"
    assert backend.runner_root == fake_runner
    assert backend.wine_path == fake_runner / "bin" / "wine"


def test_backend_accepts_the_bottles_files_layout(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    (runner / "files" / "bin").mkdir(parents=True)
    (runner / "files" / "bin" / "wine").symlink_to(Path("/bin/false"))

    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))

    assert backend.wine_path == runner / "files" / "bin" / "wine"


def test_backend_requires_a_wine_binary(tmp_path: Path) -> None:
    with pytest.raises(WinBridgeError, match="wine"):
        WineBackend(tmp_path / "empty-runner", Paths.from_root(tmp_path / "state"))


def test_backend_requires_an_absolute_runner_root(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="absolute"):
        WineBackend(Path("relative-runner"), Paths.from_root(tmp_path / "state"))


# --- prefix creation ---------------------------------------------------------


def test_create_prefix_runs_wineboot_once(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    prefix = backend.create_prefix("app")

    assert prefix == tmp_path / "state" / "prefixes" / "app"
    assert prefix.exists()
    assert "wineboot -u" in calls_log(tmp_path)

    backend.create_prefix("app")

    assert run_log(tmp_path, "wineboot -u") == 1


def test_create_prefix_waits_for_wineserver(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    backend.create_prefix("app")

    assert "wineserver -w" in calls_log(tmp_path)


def test_create_prefix_strips_z_drive(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    prefix = backend.create_prefix("app")

    assert not (prefix / "dosdevices" / "z:").exists()


def test_create_prefix_keeps_z_drive_when_requested(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)

    prefix = backend.create_prefix("app", keep_z=True)

    assert (prefix / "dosdevices" / "z:").is_symlink()


def test_create_prefix_strips_z_on_an_initialized_prefix(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.paths.prefix_for("app")
    (prefix / "dosdevices").mkdir(parents=True)
    (prefix / "system.reg").write_text("", encoding="utf-8")
    (prefix / "dosdevices" / "z:").symlink_to("/")

    backend.create_prefix("app")

    assert not (prefix / "dosdevices" / "z:").is_symlink()
    assert run_log(tmp_path, "wineboot") == 0


def test_create_prefix_creates_a_private_directory(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)

    prefix = backend.create_prefix("app")

    assert stat.S_IMODE(prefix.stat().st_mode) == 0o700
    assert stat.S_IMODE(prefix.parent.stat().st_mode) == 0o700


def test_create_prefix_passes_the_prefix_to_the_child(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)

    prefix = backend.create_prefix("app")

    assert f"WINEPREFIX={prefix}" in calls_log(tmp_path)


def test_create_prefix_requires_a_real_marker(
    tmp_path: Path, fake_runner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent fake runner must not be mistaken for an initialized prefix."""
    backend = make_backend(tmp_path, fake_runner)

    def no_boot(prefix: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(backend, "wine", no_boot)

    with pytest.raises(WinBridgeError, match="initialize"):
        backend.create_prefix("app")


@pytest.mark.parametrize("app_id", ["", ".", "..", "a/b", "a\\b", "a\x00b", ".hidden"])
def test_create_prefix_rejects_unsafe_app_ids(
    tmp_path: Path, fake_runner: Path, app_id: str
) -> None:
    backend = make_backend(tmp_path, fake_runner)

    with pytest.raises(UsageError):
        backend.create_prefix(app_id)


def test_create_prefix_never_uses_a_shell(
    tmp_path: Path, fake_runner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, dict[str, Any]]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        recorded.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("winbridge.backends.wine.subprocess.Popen", spy)
    backend = make_backend(tmp_path, fake_runner)

    backend.create_prefix("app")

    assert recorded
    for args, kwargs in recorded:
        assert isinstance(args[0], list)
        assert kwargs.get("shell", False) is False


# --- backend operations ------------------------------------------------------


def _slow_runner(tmp_path: Path) -> Path:
    runner = tmp_path / "slow-runner"
    (runner / "bin").mkdir(parents=True)
    wine = runner / "bin" / "wine"
    wine.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    wine.chmod(0o755)
    return runner


def test_wine_command_has_a_hard_timeout(tmp_path: Path) -> None:
    backend = WineBackend(_slow_runner(tmp_path), Paths.from_root(tmp_path / "state"))
    started = time.monotonic()

    with pytest.raises(BackendTimeout, match="timeout"):
        backend.wine(Path("/prefix"), "winecfg", timeout_s=0.1)

    assert time.monotonic() - started < 5


def test_install_has_a_hard_timeout_and_keeps_the_log(tmp_path: Path) -> None:
    backend = WineBackend(_slow_runner(tmp_path), Paths.from_root(tmp_path / "state"))
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"")
    log = tmp_path / "evidence" / "installer.log"
    started = time.monotonic()

    with pytest.raises(BackendTimeout, match="installer"):
        backend.install(Path("/prefix"), installer, ["/S"], log_path=log, timeout_s=0.1)

    assert time.monotonic() - started < 5
    assert log.is_file()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_wine_runs_the_runner_binary(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    completed = backend.wine(prefix, "winecfg", "-v")

    assert completed.returncode == 0
    assert "winecfg -v" in calls_log(tmp_path)


def test_wine_dispatches_wineserver_to_the_runner_daemon(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    backend.wine(prefix, "wineserver", "-k")

    assert "wineserver -k" in calls_log(tmp_path)


def test_wineserver_falls_back_to_the_wine_multicall(
    tmp_path: Path, fake_runner: Path
) -> None:
    (fake_runner / "bin" / "wineserver").unlink()
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    backend.wine(prefix, "wineserver", "-k")

    assert "wineserver -k" in calls_log(tmp_path)


def test_wine_redacts_callback_urls_in_logs(
    tmp_path: Path,
    fake_runner: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    with caplog.at_level(logging.DEBUG, logger="winbridge"):
        backend.wine(prefix, "start", "example-app://callback/oauth?code=SECRET")

    assert "SECRET" not in caplog.text
    assert "example-app://callback/" in caplog.text


def test_wine_requires_arguments(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    with pytest.raises(UsageError, match="argument"):
        backend.wine(PREFIX)


def test_install_runs_the_installer_with_its_arguments(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    installer = tmp_path / "example-setup.exe"
    installer.write_bytes(b"")

    assert backend.install(prefix, installer, ["/S"]) == 0
    assert "example-setup.exe /S" in calls_log(tmp_path)


def test_install_redirects_stdout_and_stderr_to_the_log(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    (runner / "bin").mkdir(parents=True)
    wine = runner / "bin" / "wine"
    wine.write_text(
        "#!/bin/sh\nprintf 'installer-out\\n'\nprintf 'installer-err\\n' >&2\nexit 3\n",
        encoding="utf-8",
    )
    wine.chmod(0o755)
    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"")
    log = tmp_path / "evidence" / "installer.log"

    assert backend.install(Path("/prefix"), installer, ["/S"], log_path=log) == 3

    captured = log.read_text(encoding="utf-8")
    assert "installer-out" in captured
    assert "installer-err" in captured
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def _script_runner(tmp_path: Path, script: str) -> Path:
    """A runner whose ``bin/wine`` is exactly ``script``."""
    runner = tmp_path / "script-runner"
    (runner / "bin").mkdir(parents=True)
    wine = runner / "bin" / "wine"
    wine.write_text(script, encoding="utf-8")
    wine.chmod(0o755)
    return runner


def _kill_child(pid_file: Path) -> None:
    """Stop the deliberately lingering child a pipe test started."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def test_wine_command_does_not_wait_for_a_child_holding_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured output must not wait for EOF while a grandchild keeps it open."""
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("WINBRIDGE_TEST_CHILD_PID", str(pid_file))
    runner = _script_runner(
        tmp_path,
        "#!/bin/sh\n"
        'sleep 30 &\n'
        'echo $! > "$WINBRIDGE_TEST_CHILD_PID"\n'
        "printf 'done\\n'\n"
        "exit 0\n",
    )
    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))
    started = time.monotonic()

    try:
        completed = backend.wine(Path("/prefix"), "wineboot", timeout_s=5)
    finally:
        _kill_child(pid_file)

    assert completed.returncode == 0
    assert completed.stdout == "done\n"
    assert time.monotonic() - started < 3


def test_wine_command_separates_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    runner = _script_runner(
        tmp_path,
        "#!/bin/sh\nprintf 'out-line\\n'\nprintf 'err-line\\n' >&2\nexit 7\n",
    )
    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))

    completed = backend.wine(Path("/prefix"), "winecfg")

    assert completed.returncode == 7
    assert completed.stdout == "out-line\n"
    assert completed.stderr == "err-line\n"


def test_install_returns_when_a_child_keeps_the_log_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer's completion is its exit, not a log-file EOF."""
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("WINBRIDGE_TEST_CHILD_PID", str(pid_file))
    runner = _script_runner(
        tmp_path,
        "#!/bin/sh\n"
        'sleep 30 &\n'
        'echo $! > "$WINBRIDGE_TEST_CHILD_PID"\n'
        "printf 'installer-out\\n'\n"
        "exit 0\n",
    )
    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"")
    log = tmp_path / "evidence" / "installer.log"
    started = time.monotonic()

    try:
        assert backend.install(
            Path("/prefix"), installer, ["/S"], log_path=log, timeout_s=5
        ) == 0
    finally:
        _kill_child(pid_file)

    assert time.monotonic() - started < 3
    assert "installer-out" in log.read_text(encoding="utf-8")


def test_install_inherits_stdio_without_a_log(
    tmp_path: Path, fake_runner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, dict[str, Any]]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        recorded.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("winbridge.backends.wine.subprocess.Popen", spy)
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"")
    recorded.clear()

    backend.install(prefix, installer, ["/S"])

    assert recorded
    for args, kwargs in recorded:
        assert isinstance(args[0], list)
        assert kwargs.get("shell", False) is False
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs


def test_run_returns_a_process_for_the_app(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    popen = backend.run(prefix, r"C:\Program Files\Example\Example.exe", ["--flag"])

    assert popen.wait() == 0
    assert r"C:\Program Files\Example\Example.exe --flag" in calls_log(tmp_path)


def test_run_does_not_inherit_caller_stdio(
    tmp_path: Path, fake_runner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, dict[str, Any]]] = []
    open_at_spawn: list[bool] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        open_at_spawn.append(not getattr(kwargs.get("stdout"), "closed", True))
        recorded.append((args, dict(kwargs)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("winbridge.backends.wine.subprocess.Popen", spy)
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    recorded.clear()
    open_at_spawn.clear()

    popen = backend.run(prefix, r"C:\Program Files\Example\Example.exe", ["--flag"])
    try:
        popen.wait()
    finally:
        with suppress(subprocess.SubprocessError):
            popen.kill()

    assert recorded
    argv, kwargs = recorded[0]
    assert isinstance(argv[0], list)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs.get("shell") is None
    stdout = kwargs["stdout"]
    assert isinstance(stdout, io.BufferedWriter)
    assert open_at_spawn == [True]
    assert Path(stdout.name) == backend.paths.logs / "run-app.log"
    assert stdout.mode == "ab"


def test_run_writes_app_output_to_a_private_log(tmp_path: Path) -> None:
    runner = _script_runner(
        tmp_path,
        "#!/bin/sh\nprintf 'app-out\\n'\nprintf 'app-err\\n' >&2\nexit 0\n",
    )
    backend = WineBackend(runner, Paths.from_root(tmp_path / "state"))
    prefix = backend.paths.prefix_for("app")

    popen = backend.run(prefix, r"C:\Program Files\Example\Example.exe", [])

    assert popen.wait() == 0
    log = backend.paths.logs / "run-app.log"
    assert log.is_file()
    captured = log.read_text(encoding="utf-8")
    assert "app-out" in captured
    assert "app-err" in captured
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_run_survives_an_unavailable_run_log(
    tmp_path: Path,
    fake_runner: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    backend.paths.logs.parent.mkdir(parents=True)
    backend.paths.logs.write_text("not a directory\n", encoding="utf-8")
    recorded: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        recorded.append(dict(kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("winbridge.backends.wine.subprocess.Popen", spy)

    with caplog.at_level(logging.WARNING, logger="winbridge"):
        popen = backend.run(backend.paths.prefix_for("app"), r"C:\Example\Example.exe", [])
    try:
        popen.wait()
    finally:
        with suppress(subprocess.SubprocessError):
            popen.kill()

    assert recorded[0]["stdout"] is subprocess.DEVNULL
    assert recorded[0]["stderr"] is subprocess.STDOUT
    assert "run-app.log" in caplog.text


def test_expose_symlinks_a_host_folder(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    host = tmp_path / "repos"
    host.mkdir()

    backend.expose(prefix, host, r"C:\users\me\repos")

    target = prefix / "drive_c" / "users" / "me" / "repos"
    assert target.is_symlink()
    assert target.resolve() == host.resolve()


def test_expose_refuses_to_replace_an_existing_path(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    host = tmp_path / "repos"
    host.mkdir()
    (prefix / "drive_c" / "users" / "me" / "repos").mkdir(parents=True)

    with pytest.raises(UsageError, match="exists"):
        backend.expose(prefix, host, r"C:\users\me\repos")


@pytest.mark.parametrize(
    "guest",
    [r"D:\repos", "repos", r"C:\..\escape", r"C:\users\..\..\escape", ""],
)
def test_expose_rejects_unsafe_guest_paths(
    tmp_path: Path, fake_runner: Path, guest: str
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    host = tmp_path / "repos"
    host.mkdir()

    with pytest.raises(UsageError):
        backend.expose(prefix, host, guest)


def test_expose_requires_an_existing_absolute_host(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    with pytest.raises(UsageError):
        backend.expose(prefix, Path("relative"), r"C:\users\me\repos")
    with pytest.raises(UsageError):
        backend.expose(prefix, tmp_path / "missing", r"C:\users\me\repos")


def test_remove_deletes_only_a_prefix_under_state(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    backend.remove(prefix)

    assert not prefix.exists()


def test_remove_refuses_paths_outside_the_prefix_store(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(UsageError, match="refusing"):
        backend.remove(outside)

    assert outside.exists()


def test_remove_refuses_the_prefix_store_itself(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    backend.paths.prefixes.mkdir(parents=True)

    with pytest.raises(UsageError, match="refusing"):
        backend.remove(backend.paths.prefixes)


def test_remove_is_idempotent(tmp_path: Path, fake_runner: Path) -> None:
    backend = make_backend(tmp_path, fake_runner)

    backend.remove(backend.paths.prefix_for("missing"))


# --- Z: handling -------------------------------------------------------------


def test_strip_z_drive_reports_whether_it_removed_anything(tmp_path: Path) -> None:
    (tmp_path / "dosdevices").mkdir()
    (tmp_path / "dosdevices" / "z:").symlink_to("/")

    assert strip_z_drive(tmp_path) is True
    assert strip_z_drive(tmp_path) is False


# --- process matching --------------------------------------------------------


def make_proc_entry(
    proc_root: Path,
    pid: int,
    *,
    environ: bytes | None = None,
    status: str | None = None,
    children: str | None = None,
    comm: str = "wine-preloader",
    cmdline: bytes | None = b"/opt/runner/bin/wine\x00",
) -> Path:
    entry = proc_root / str(pid)
    entry.mkdir(parents=True)
    if environ is not None:
        (entry / "environ").write_bytes(environ)
    if status is not None:
        (entry / "status").write_text(status, encoding="utf-8")
    if children is not None:
        task = entry / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children, encoding="utf-8")
    if comm is not None:
        (entry / "comm").write_text(comm, encoding="utf-8")
    if cmdline is not None:
        (entry / "cmdline").write_bytes(cmdline)
    return entry


def test_processes_for_prefix_matches_only_the_exact_env_value(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    make_proc_entry(proc, 10, environ=b"HOME=/x\x00WINEPREFIX=/state/prefixes/app\x00")
    make_proc_entry(proc, 11, environ=b"WINEPREFIX=/state/prefixes/app-other\x00")
    make_proc_entry(proc, 12, environ=b"WINEPREFIX=/state/prefixes/app\x00EXTRA=1")
    make_proc_entry(proc, 13, environ=b"NOPE=1")
    make_proc_entry(proc, 14, environ=b"WINEPREFIX=/state/prefixes/app")
    make_proc_entry(proc, 15, environ=b"VALUE=WINEPREFIX=/state/prefixes/app\x00")
    (proc / "not-a-pid").mkdir()

    assert processes_for_prefix(Path("/state/prefixes/app"), proc_root=proc) == {
        10,
        12,
        14,
    }


def test_processes_for_prefix_skips_unreadable_pids(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    entry = make_proc_entry(proc, 20)
    (entry / "environ").mkdir()

    assert processes_for_prefix(Path("/p"), proc_root=proc) == set()


def test_processes_for_prefix_ignores_the_cli_and_shell(tmp_path: Path) -> None:
    """An exported WINEPREFIX reaches every child, including WinBridge itself."""
    proc = tmp_path / "proc"
    env = b"WINEPREFIX=/state/prefixes/app\x00"
    make_proc_entry(proc, 10, environ=env, comm="python3", cmdline=b"/usr/bin/python3\x00")
    make_proc_entry(proc, 11, environ=env, comm="bash", cmdline=b"/bin/bash\x00")
    make_proc_entry(proc, 12, environ=env, comm="example.exe", cmdline=b"C:\\App\\example.exe\x00")
    make_proc_entry(proc, 13, environ=env, comm="wineserver", cmdline=b"/r/bin/wineserver\x00")

    assert processes_for_prefix(Path("/state/prefixes/app"), proc_root=proc) == {12, 13}


def test_processes_for_prefix_includes_wine_exe_images(tmp_path: Path) -> None:
    """A reparented Windows image still counts as a prefix process."""
    proc = tmp_path / "proc"
    env = b"WINEPREFIX=/state/prefixes/app\x00"
    make_proc_entry(
        proc,
        30,
        environ=env,
        comm="steam_proton",
        cmdline=b"C:\\Program Files\\Example\\Example.exe\x00",
    )
    make_proc_entry(
        proc,
        31,
        environ=env,
        comm="Example.EXE",
        cmdline=b"C:\\Program Files\\Example\\Example.EXE\x00",
    )
    make_proc_entry(
        proc,
        32,
        environ=env,
        comm="steam_proton",
        cmdline=b"--type=renderer\x00",
    )

    assert processes_for_prefix(Path("/state/prefixes/app"), proc_root=proc) == {30, 31}


def test_processes_for_prefix_excludes_this_process_with_a_shared_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    monkeypatch.setenv("WINEPREFIX", str(prefix))

    found = processes_for_prefix(prefix)

    assert os.getpid() not in found
    assert os.getppid() not in found


def test_processes_for_prefix_tolerates_a_missing_proc(tmp_path: Path) -> None:
    assert processes_for_prefix(Path("/p"), proc_root=tmp_path / "missing") == set()


# --- pid tree ----------------------------------------------------------------


def test_pid_tree_walks_task_children(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    make_proc_entry(proc, 10, children="20 21\n")
    make_proc_entry(proc, 20, children="30\n")
    make_proc_entry(proc, 21, children="")
    make_proc_entry(proc, 30, children="")

    assert pid_tree(10, proc_root=proc) == {10, 20, 21, 30}


def test_pid_tree_falls_back_to_the_parent_map(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid, ppid in ((10, 1), (11, 10), (12, 11), (13, 1)):
        make_proc_entry(proc, pid, status=f"Name:\tapp\nPPid:\t{ppid}\n")

    assert pid_tree(10, proc_root=proc) == {10, 11, 12}


def test_pid_tree_falls_back_to_stat_ppid(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid, ppid in ((10, 1), (11, 10), (12, 11)):
        entry = make_proc_entry(proc, pid)
        (entry / "stat").write_text(
            f"{pid} (app) S {ppid} {pid} 1 0 -1 0\n", encoding="utf-8"
        )

    assert pid_tree(10, proc_root=proc) == {10, 11, 12}


def test_pid_tree_ignores_cycles(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    make_proc_entry(proc, 10, children="20\n")
    make_proc_entry(proc, 20, children="10\n")

    assert pid_tree(10, proc_root=proc) == {10, 20}


def test_pid_tree_keeps_a_missing_root(tmp_path: Path) -> None:
    assert pid_tree(999, proc_root=tmp_path / "proc") == {999}


# --- kill prefix -------------------------------------------------------------


def test_kill_prefix_runs_wineserver_and_waits(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")
    probes = iter([{1}, {1}, set()])
    seen: list[set[int]] = []

    def probe(_prefix: Path) -> set[int]:
        value = next(probes)
        seen.append(value)
        return value

    kill_prefix(backend, prefix, proc_probe=probe, poll_s=0.001)

    assert "wineserver -k" in calls_log(tmp_path)
    assert len(seen) == 3


def test_kill_prefix_returns_immediately_when_empty(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    kill_prefix(backend, prefix, proc_probe=lambda _: set(), poll_s=0.001)

    assert "wineserver -k" in calls_log(tmp_path)


def test_kill_prefix_raises_when_processes_survive(
    tmp_path: Path, fake_runner: Path
) -> None:
    backend = make_backend(tmp_path, fake_runner)
    prefix = backend.create_prefix("app")

    with pytest.raises(PrefixStillRunning, match="7"):
        kill_prefix(
            backend,
            prefix,
            proc_probe=lambda _: {7, 8},
            timeout_s=0.01,
            poll_s=0.001,
        )

    assert PrefixStillRunning.exit_code == 1
