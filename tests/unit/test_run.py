"""Safe launching: ``run`` must never destroy unsaved work.

The normal path is exercised against the shared fake backend and a fake
``/proc`` probe, so no Wine prefix and no real process is needed. The three
tests quoted in the plan are kept verbatim; the rest pin the behaviour around
them: the already-running handle, the restart-only kill, URL forwarding on a
cold and a warm prefix, argument passthrough, and the composition of the pid
tree verify later consumes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.backends.base import BackendError
from winbridge.cli import app, main
from winbridge.core import run as run_module
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import UnsupportedEnvironment, UsageError, WinBridgeError
from winbridge.core.profile import Profile
from winbridge.core.run import Launch, run_app
from winbridge.core.runner import Catalog
from winbridge.core.state import Paths
from winbridge.core.verify import BackgroundUnavailable
from winbridge.desktops.base import DesktopError, Window

CALLBACK = "example-app://callback?code=x"


def _fail_kill(*args: Any, **kwargs: Any) -> None:
    pytest.fail("run killed a prefix on the normal path")


def failing_wine(returncode: int = 1) -> Callable[..., subprocess.CompletedProcess[str]]:
    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr="")

    return wine


def make_context(
    paths: Paths, catalog: Catalog, backend: Any, desktop: Any = None
) -> Context:
    return Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=backend,
        desktop=desktop,
    )


def invoke(
    *args: str,
    paths: Paths,
    context: Context | None = None,
    obj: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = dict(obj or {})
    payload.setdefault("paths", paths)
    if context is not None:
        payload.setdefault("context", context)
    result = CliRunner().invoke(app, list(args), obj=payload)
    # ``main`` maps WinBridgeError to its documented exit code. CliRunner calls
    # the Typer command directly, so mirror that boundary for these assertions;
    # ``test_cli_run_wait_failure_exits_4_at_the_console_boundary`` proves the
    # real ``main`` path.
    if isinstance(result.exception, WinBridgeError):
        result.exit_code = result.exception.exit_code
    return result


def _freeze_verify_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Make stage timestamps and the wait loop deterministic and instant."""
    clock = {"now": 0.0}
    monkeypatch.setattr("winbridge.core.verify.time.monotonic", lambda: clock["now"])

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr("winbridge.core.verify.time.sleep", sleep)
    return clock


def _allow_cursor_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy the force preconditions without a real X11 session."""
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr("winbridge.core.cursor.x11_library_path", lambda: "libX11.so.6")

    class Probe:
        def close(self) -> None:
            pass

    monkeypatch.setattr("winbridge.core.cursor.X11Cursor", lambda: Probe())


# --- core: normal start ------------------------------------------------------


def test_normal_run_never_kills(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(profile, paths, fake_backend)

    assert fake_backend.calls[-1].startswith("run")
    assert launch.pid_tree == {4242}


def test_normal_run_returns_the_backend_handle(
    fake_backend: Any, profile: Profile, paths: Paths
) -> None:
    launch = run_app(profile, paths, fake_backend)

    assert launch.popen is fake_backend.popen
    assert launch == Launch(popen=fake_backend.popen, pid_tree={4242})


def test_normal_run_uses_the_profile_prefix_and_exe(
    fake_backend: Any, profile: Profile, paths: Paths
) -> None:
    run_app(profile, paths, fake_backend)

    assert fake_backend.runs == [(paths.prefix_for(profile.id), profile.exe, [])]
    assert fake_backend.wine_calls == []


def test_normal_run_passes_app_arguments_through(
    fake_backend: Any, profile: Profile, paths: Paths
) -> None:
    run_app(profile, paths, fake_backend, argv=["--open", "file name.txt"])

    assert fake_backend.runs[0][2] == ["--open", "file name.txt"]


def test_child_processes_join_the_pid_tree(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr(run_module, "pid_tree", lambda pid: {pid, 7, 8})

    launch = run_app(profile, paths, fake_backend)

    assert launch.pid_tree == {4242, 7, 8}


def test_missing_exe_fails_before_spawning_or_killing(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    make_profile: Callable[..., Profile],
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    with pytest.raises(WinBridgeError, match="no exe"):
        run_app(make_profile(exe=""), paths, fake_backend)

    assert fake_backend.runs == []


def test_restart_never_kills_when_the_exe_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    make_profile: Callable[..., Profile],
    paths: Paths,
) -> None:
    """A malformed profile must fail before restart destroys the running app."""
    kills: list[str] = []
    monkeypatch.setattr(
        "winbridge.core.run.kill_prefix", lambda backend, prefix: kills.append("kill")
    )

    with pytest.raises(WinBridgeError, match="no exe"):
        run_app(make_profile(exe=""), paths, fake_backend, restart=True)

    assert kills == []
    assert fake_backend.runs == []


def test_concurrent_runs_serialize_probe_and_spawn(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    """Two probes that both start empty must still produce one instance."""
    state = {"spawned": False}
    popen = fake_backend.popen

    def probe(_prefix: Path) -> set[int]:
        return {4242} if state["spawned"] else set()

    def slow_run(prefix: Path, exe: str, argv: list[str]) -> Any:
        time.sleep(0.1)
        state["spawned"] = True
        fake_backend.runs.append((prefix, exe, list(argv)))
        return popen

    monkeypatch.setattr(fake_backend, "run", slow_run)
    launches: list[Launch] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            launches.append(run_app(profile, paths, fake_backend, proc_probe=probe))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(fake_backend.runs) == 1
    assert sum(1 for launch in launches if launch.popen is not None) == 1


# --- core: already running ---------------------------------------------------


def test_already_running_is_reported_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(profile, paths, fake_backend, proc_probe=lambda _: {11, 12})

    assert launch.popen is None
    assert launch.pid_tree == {11, 12}
    assert fake_backend.runs == []
    output = capsys.readouterr().out
    assert "already running" in output
    assert "use --restart" in output


def test_run_reports_the_pids_blocking_a_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(profile, paths, fake_backend, proc_probe=lambda _: {12, 3})

    assert launch.popen is None
    assert fake_backend.runs == []
    assert "pids 3, 12" in capsys.readouterr().out


def test_already_running_with_arguments_still_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(
        profile, paths, fake_backend, argv=["--second"], proc_probe=lambda _: {5}
    )

    assert launch.popen is None
    assert fake_backend.runs == []


# --- core: restart is the only kill path ------------------------------------


def test_restart_is_the_only_kill_path(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "winbridge.core.run.kill_prefix",
        lambda backend, prefix: calls.append("kill"),
    )

    run_app(profile, paths, fake_backend, restart=True)

    assert calls == ["kill"]


def test_restart_kills_then_starts_even_when_nothing_was_running(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        "winbridge.core.run.kill_prefix",
        lambda backend, prefix: order.append("kill"),
    )
    monkeypatch.setattr(
        fake_backend,
        "run",
        lambda prefix, exe, argv: order.append("run") or fake_backend.popen,
    )

    run_app(profile, paths, fake_backend, restart=True, proc_probe=lambda _: set())

    assert order == ["kill", "run"]


def test_restart_replaces_a_running_instance(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        "winbridge.core.run.kill_prefix",
        lambda backend, prefix: order.append("kill"),
    )
    monkeypatch.setattr(
        fake_backend,
        "run",
        lambda prefix, exe, argv: order.append("run") or fake_backend.popen,
    )

    launch = run_app(profile, paths, fake_backend, restart=True, proc_probe=lambda _: {1})

    assert order == ["kill", "run"]
    assert launch.popen is fake_backend.popen


# --- core: lifecycle callbacks -----------------------------------------------


def test_restart_reports_stages_and_prestart_hook_order(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    order: list[str] = []
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "winbridge.core.run.kill_prefix",
        lambda backend, prefix: order.append("kill"),
    )
    monkeypatch.setattr(
        fake_backend,
        "run",
        lambda prefix, exe, argv: order.append("run") or fake_backend.popen,
    )

    run_app(
        profile,
        paths,
        fake_backend,
        restart=True,
        proc_probe=lambda _: {1},
        before_start=lambda: order.append("before_start"),
        on_stage=lambda stage, status: events.append((stage, status)),
    )

    assert order == ["kill", "before_start", "run"]
    assert events == [
        ("stop", "stopping"),
        ("stop", "stopped"),
        ("launch", "starting"),
        ("launch", "started"),
    ]


def test_before_start_is_not_called_for_a_running_instance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(
        profile,
        paths,
        fake_backend,
        proc_probe=lambda _: {11},
        before_start=lambda: pytest.fail("before_start ran for a live app"),
        on_stage=lambda stage, status: events.append((stage, status)),
    )

    assert launch.popen is None
    assert events == [("launch", "already-running")]
    assert capsys.readouterr().out == ""


def test_before_start_failure_prevents_spawn(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", lambda *args: None)

    with pytest.raises(RuntimeError, match="no background"):
        run_app(
            profile,
            paths,
            fake_backend,
            restart=True,
            proc_probe=lambda _: set(),
            before_start=lambda: (_ for _ in ()).throw(RuntimeError("no background")),
        )

    assert fake_backend.runs == []


# --- core: URL forwarding ----------------------------------------------------


def test_url_run_forwards_without_killing(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    run_app(
        profile,
        paths,
        fake_backend,
        url=CALLBACK,
        proc_probe=lambda _: {1},
    )

    assert fake_backend.calls[-1].startswith("start ")


def test_url_on_a_running_app_is_one_argv_argument(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    launch = run_app(profile, paths, fake_backend, url=CALLBACK, proc_probe=lambda _: {1})

    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert fake_backend.runs == []
    assert launch.popen is None


def test_url_on_a_running_app_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(run_module.time, "sleep", sleeps.append)

    run_app(profile, paths, fake_backend, url=CALLBACK, proc_probe=lambda _: {1})

    assert sleeps == []


def test_url_stays_one_argv_argument_through_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    """No shell: metacharacters and spaces cannot be re-tokenized."""
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)
    seen: list[tuple[Path, tuple[str, ...]]] = []
    payload = "example-app://callback?code=x&next=$(touch /tmp/pwned) and spaces"

    def record(prefix: Path, *args: str) -> subprocess.CompletedProcess[str]:
        seen.append((prefix, args))
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(fake_backend, "wine", record)

    run_app(profile, paths, fake_backend, url=payload, proc_probe=lambda _: {1})

    assert seen == [(paths.prefix_for(profile.id), ("start", payload))]


def test_url_on_a_cold_prefix_starts_then_forwards(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(run_module.time, "sleep", sleeps.append)

    launch = run_app(profile, paths, fake_backend, url=CALLBACK, proc_probe=lambda _: set())

    assert fake_backend.calls[0].startswith("run ")
    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert sleeps == [2.0]
    assert launch.popen is fake_backend.popen
    assert launch.pid_tree == {4242}


def test_url_with_restart_is_rejected_before_killing(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    with pytest.raises(UsageError, match="--restart"):
        run_app(
            profile,
            paths,
            fake_backend,
            url=CALLBACK,
            restart=True,
            proc_probe=lambda _: {1},
        )

    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []


@pytest.mark.parametrize("running", [True, False], ids=["warm", "cold"])
def test_url_forward_failure_raises_with_a_redacted_url(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
    running: bool,
) -> None:
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)
    monkeypatch.setattr(run_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(fake_backend, "wine", failing_wine())
    url = "example-app://callback?code=secret-token"

    with pytest.raises(WinBridgeError) as error:
        run_app(
            profile,
            paths,
            fake_backend,
            url=url,
            proc_probe=lambda _: {1} if running else set(),
        )

    message = str(error.value)
    assert "example-app://callback/[redacted]" in message
    assert "secret-token" not in message
    assert "wine start exited 1" in message


# --- CLI ---------------------------------------------------------------------


def test_cli_run_starts_the_app(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run", "example-app", paths=paths, context=make_context(paths, catalog, fake_backend)
    )

    assert result.exit_code == 0, result.output
    assert fake_backend.runs == [(paths.prefix_for("example-app"), profile.exe, [])]


def test_cli_run_forwards_url(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    sleeps: list[float] = []
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)
    monkeypatch.setattr(run_module.time, "sleep", sleeps.append)

    result = invoke(
        "run",
        "example-app",
        "--url",
        CALLBACK,
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert sleeps == [2.0]


def test_cli_run_rejects_url_with_restart(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)

    result = invoke(
        "run",
        "example-app",
        "--url",
        CALLBACK,
        "--restart",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert isinstance(result.exception, UsageError)
    assert "--restart" in str(result.exception)
    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []


def test_cli_run_url_restart_exits_2_before_building_a_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
) -> None:
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr(
        "winbridge.cli._profile_context",
        lambda *args, **kwargs: pytest.fail("built a context for an invalid combination"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["winbridge", "run", "example-app", "--url", CALLBACK, "--restart"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "--url" in err
    assert "--restart" in err


def test_cli_run_passes_arguments_after_double_dash(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--",
        "--flag",
        "value two",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert fake_backend.runs[0][2] == ["--flag", "value two"]


def test_cli_run_unknown_option_before_double_dash_is_a_usage_error(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--flag",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 2
    assert fake_backend.runs == []


def test_cli_run_missing_profile_exits_1(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    result = invoke(
        "run", "missing-app", paths=paths, context=make_context(paths, catalog, fake_backend)
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert "missing-app" in str(result.exception)
    assert fake_backend.runs == []


def test_cli_run_already_running_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        "winbridge.cli.run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree=set()),
    )

    result = invoke(
        "run", "example-app", paths=paths, context=make_context(paths, catalog, fake_backend)
    )

    assert result.exit_code == 0, result.output
    assert fake_backend.runs == []


def test_cli_run_uses_the_profile_runner(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    seen: dict[str, Any] = {}

    def fake_build_context(
        build_paths: Paths,
        config: Config,
        build_catalog: Catalog,
        backend_name: str = "wine",
        desktop: Any = None,
    ) -> Context:
        seen["runner"] = config.default_runner
        seen["backend_name"] = backend_name
        return Context(
            paths=build_paths, config=config, catalog=build_catalog, backend=fake_backend
        )

    monkeypatch.setattr("winbridge.cli.build_context", fake_build_context)

    result = invoke("run", "example-app", paths=paths)

    assert result.exit_code == 0, result.output
    assert seen == {"runner": profile.runner, "backend_name": profile.backend}


# --- CLI: staged progress and wait ------------------------------------------


def test_cli_run_default_prints_launch_stages_and_does_not_wait(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert "launch: starting" in result.output
    assert "launch: started" in result.output
    assert "result: launched" in result.output
    assert "window:" not in result.output


def test_cli_run_timeout_requires_wait(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--timeout",
        "5",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 2
    assert fake_backend.runs == []


def test_cli_run_wait_passes_after_two_visible_frames(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _freeze_verify_clock(monkeypatch)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert "window: found" in result.output
    assert "paint: visible" in result.output
    assert "paint: stable" in result.output
    assert "result: PASS" in result.output
    assert result.output.index("launch: started") < result.output.index("window: found")
    assert result.output.index("window: found") < result.output.index("paint: stable")
    assert fake_backend.runs == [(paths.prefix_for("example-app"), profile.exe, [])]
    assert len(list(paths.evidence.rglob("background.png"))) == 1
    # ``run --wait`` is a launch check; it must not rewrite verify state.
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.first_paint_s == 95.0


def test_cli_run_wait_result_parenthetical_is_elapsed(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """The parenthetical is the run clock, not the launch-relative paint time.

    The reporter and the watcher get deliberately different clocks: the stage
    prefix is 50 s while first paint is 0 s, so a parenthetical that used
    ``first_paint_s`` would print ``(0.00s)`` and fail.
    """
    write_profile(paths.root, profile)
    _freeze_verify_clock(monkeypatch)
    clock = {"now": 50.0}
    real_reporter = cli_module._RunReporter

    def make_reporter(**kwargs: Any) -> Any:
        return real_reporter(clock=lambda: clock["now"], **kwargs)

    monkeypatch.setattr(cli_module, "_RunReporter", make_reporter)
    spawn = fake_backend.run

    def record_run(prefix: Path, exe: str, argv: list[str]) -> Any:
        clock["now"] = 100.0
        return spawn(prefix, exe, argv)

    monkeypatch.setattr(fake_backend, "run", record_run)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0
    line = next(line for line in result.output.splitlines() if "result: PASS" in line)
    match = re.search(r"\[run \+(\d+\.\d+)s\] result: PASS \((\d+\.\d+)s\)", line)
    assert match is not None
    assert match.group(1) == "50.00"
    assert match.group(2) == match.group(1)


def test_cli_run_wait_timeout_budget_starts_at_launch(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """Lock wait and pre-launch work must not consume ``--timeout``.

    The lock is held for longer than the whole budget before the launch. If the
    clock started before the lock, the watcher would give up immediately; a
    launch-relative origin still waits the full 100 s after the spawn.
    """
    write_profile(paths.root, profile)
    fake_desktop.wait_result = None
    fake_desktop.windows_result = []
    clock = _freeze_verify_clock(monkeypatch)
    delay = 150.0

    @contextmanager
    def delayed(lock: Path, blocking: bool = True) -> Any:
        clock["now"] += delay
        yield

    monkeypatch.setattr(cli_module, "file_lock", delayed)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    final = json.loads(result.stdout.splitlines()[-1])
    assert final["event"] == "result"
    assert final["status"] == "fail"
    assert final["reason"] == "timeout"
    assert final["elapsed_s"] >= delay + 100.0


def test_cli_run_wait_first_paint_is_launch_relative(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """``first_paint_s`` is measured from the spawn, not from reporter creation."""
    write_profile(paths.root, profile)
    clock = _freeze_verify_clock(monkeypatch)
    delay = 30.0

    @contextmanager
    def delayed(lock: Path, blocking: bool = True) -> Any:
        clock["now"] += delay
        yield

    monkeypatch.setattr(cli_module, "file_lock", delayed)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    final = json.loads(result.stdout.splitlines()[-1])
    assert final["status"] == "pass"
    assert final["first_paint_s"] == 0.0
    assert final["elapsed_s"] >= delay


def test_cli_run_wait_launch_failure_emits_json_result(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    def broken(*args: Any, **kwargs: Any) -> Launch:
        raise BackendError("cannot start wine")

    monkeypatch.setattr(cli_module, "run_app", broken)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 1
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[-1]["event"] == "result"
    assert lines[-1]["status"] == "fail"
    assert lines[-1]["reason"] == "launch"
    assert sum(1 for line in lines if line["event"] == "result") == 1
    assert fake_backend.runs == []


def test_cli_run_json_launch_failure_emits_json_result(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """The non-wait path also ends with a result event on failure."""
    write_profile(paths.root, profile)

    def broken(*args: Any, **kwargs: Any) -> Launch:
        raise BackendError("cannot start wine")

    monkeypatch.setattr(cli_module, "run_app", broken)

    result = invoke(
        "run",
        "example-app",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 1
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[-1]["event"] == "result"
    assert lines[-1]["status"] == "fail"
    assert lines[-1]["reason"] == "launch"


def test_cli_run_wait_pre_loop_desktop_failure_exits_4_with_result(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """A compositor failure before the first capture is a failed wait, not a crash."""
    write_profile(paths.root, profile)

    def broken_windows() -> list[Window]:
        raise DesktopError("no compositor windows")

    monkeypatch.setattr(fake_desktop, "windows", broken_windows)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    final = json.loads(result.stdout.splitlines()[-1])
    assert final["event"] == "result"
    assert final["status"] == "fail"
    assert "desktop tool failure" in final["reason"]


def test_cli_run_wait_url_on_a_live_instance_tailors_the_message(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """``--restart --wait`` is illegal with ``--url``; do not recommend it."""
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree=set()),
    )

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--url",
        CALLBACK,
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "already-running" in result.output
    message = str(result.exception)
    assert "URL was forwarded" in message
    assert "--restart --wait" not in message


def test_cli_run_wait_timeout_keeps_observed_first_paint(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    """A timeout after one visible frame still reports when paint was seen."""
    write_profile(paths.root, profile)
    _freeze_verify_clock(monkeypatch)
    fake_desktop.wait_result = None
    windows = [
        Window("0x1", 4242, "Example - Main", "Example", 10, 20, 800, 600),
        Window("0x2", 4242, "Example - Main", "Example", 10, 20, 800, 600),
    ]
    calls = {"count": 0}

    def alternating() -> list[Window]:
        window = windows[calls["count"] % len(windows)]
        calls["count"] += 1
        return [window]

    monkeypatch.setattr(fake_desktop, "windows", alternating)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    final = json.loads(result.stdout.splitlines()[-1])
    assert final["status"] == "fail"
    assert final["reason"] == "timeout"
    assert final["first_paint_s"] == 0.0


def test_cli_run_negative_timeout_is_a_usage_error(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "-1",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 2
    assert isinstance(result.exception, UsageError)
    assert fake_backend.runs == []


def test_cli_run_timeout_without_wait_exits_2_at_the_console_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
) -> None:
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(
        cli_module,
        "_profile_context",
        lambda *args, **kwargs: pytest.fail("built a context for a usage error"),
    )
    monkeypatch.setattr(
        sys, "argv", ["winbridge", "run", "example-app", "--timeout", "5"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "--wait" in capsys.readouterr().err


def test_cli_run_wait_unsupported_desktop_exits_3_at_the_console_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(sys, "argv", ["winbridge", "run", "example-app", "--wait"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 3
    assert "desktop not supported" in capsys.readouterr().err
    assert fake_backend.runs == []


def test_cli_run_wait_json_pass_has_first_paint_and_evidence(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines
    final = lines[-1]
    assert final["event"] == "result"
    assert final["status"] == "pass"
    assert final["reason"] == "passed"
    assert final["first_paint_s"] is not None
    assert isinstance(final["evidence"], str)
    assert final["evidence"]
    assert "[run" not in result.stdout


def test_cli_run_json_default_is_ndjson(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[0]["event"] == "stage"
    assert lines[0]["stage"] == "launch"
    assert lines[-1]["event"] == "result"
    assert lines[-1]["status"] == "launched"
    assert "[run" not in result.stdout


def test_cli_run_wait_transparent_frames_exit_4(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    fake_desktop.blank_screenshots = True
    _freeze_verify_clock(monkeypatch)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "transparent" in result.output
    assert "result: FAIL" in result.output


def test_cli_run_wait_json_failure_carries_reason(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    fake_desktop.blank_screenshots = True
    _freeze_verify_clock(monkeypatch)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[-1]["event"] == "result"
    assert lines[-1]["status"] == "fail"
    assert lines[-1]["reason"] == "transparent"
    assert sum(1 for line in lines if line["event"] == "result") == 1


def test_cli_run_wait_timeout_exit_4(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    fake_desktop.wait_result = None
    fake_desktop.windows_result = []
    _freeze_verify_clock(monkeypatch)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "timeout" in result.output


def test_cli_run_wait_background_failure_exits_4_without_launching(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    def unavailable(desktop: Any, run_dir: Path) -> Path:
        raise BackgroundUnavailable("screen")

    monkeypatch.setattr(cli_module, "capture_background", unavailable)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "background" in result.output
    assert fake_backend.runs == []
    assert fake_desktop.screen_screenshots == []


def test_cli_run_wait_unsupported_desktop_exits_3_without_touching_the_prefix(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 3
    assert isinstance(result.exception, UnsupportedEnvironment)
    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []
    assert not (paths.locks / f"app-{profile.id}.lock").exists()
    assert list(paths.evidence.rglob("background.png")) == []


def test_cli_run_wait_json_unsupported_desktop_emits_result(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 3
    assert isinstance(result.exception, UnsupportedEnvironment)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(lines) == 1
    assert lines[-1]["event"] == "result"
    assert lines[-1]["status"] == "fail"
    assert lines[-1]["reason"] == "unsupported"
    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []


def test_cli_run_default_already_running_prints_the_state(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree=set()),
    )

    result = invoke(
        "run",
        "example-app",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    assert fake_backend.runs == []


def test_cli_run_default_already_running_prints_blocking_pids(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree={12, 3}),
    )

    result = invoke(
        "run",
        "example-app",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert "pids 3, 12" in result.output
    assert "use --restart" in result.output
    assert fake_backend.runs == []


def test_cli_run_wait_already_running_exits_4(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree=set()),
    )

    result = invoke(
        "run",
        "example-app",
        "--wait",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "already-running" in result.output
    assert fake_backend.runs == []
    assert fake_desktop.screen_screenshots == []
    # Version detection and evidence creation belong after the live-instance
    # check, so an unobserved wait touches neither the registry nor evidence:
    # only the log directory the CLI bound before the wait exists.
    assert fake_backend.wine_calls == []
    versions = {path.name for path in (paths.evidence / profile.id).iterdir()}
    assert versions == {profile.last_verified.app_version}


def test_cli_run_wait_already_running_names_the_blocking_pids(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree={12, 3}),
    )

    result = invoke(
        "run",
        "example-app",
        "--wait",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "pids 3, 12" in str(result.exception)
    assert fake_backend.runs == []


def test_cli_run_wait_holds_the_app_lock_across_launch_and_watch(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _freeze_verify_clock(monkeypatch)
    events: list[str] = []
    seen: list[tuple[Path, bool]] = []

    @contextmanager
    def recording(lock: Path, blocking: bool = True) -> Any:
        seen.append((Path(lock), blocking))
        events.append("lock:enter")
        try:
            yield
        finally:
            events.append("lock:exit")

    monkeypatch.setattr(cli_module, "file_lock", recording)
    run = fake_backend.run

    def record_run(prefix: Path, exe: str, argv: list[str]) -> Any:
        events.append("spawn")
        return run(prefix, exe, argv)

    shot = fake_desktop.screenshot

    def record_shot(window: Any, dest: Path) -> None:
        events.append("capture")
        shot(window, dest)

    monkeypatch.setattr(fake_backend, "run", record_run)
    monkeypatch.setattr(fake_desktop, "screenshot", record_shot)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert seen == [(paths.locks / f"app-{profile.id}.lock", True)]
    assert events[0] == "lock:enter"
    assert events[-1] == "lock:exit"
    assert events.index("spawn") < events.index("lock:exit")
    assert "capture" in events
    assert events.index("capture") < events.index("lock:exit")


def test_cli_run_wait_failure_exits_4_at_the_console_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    fake_desktop.blank_screenshots = True
    context = make_context(paths, catalog, fake_backend, fake_desktop)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    _freeze_verify_clock(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["winbridge", "run", "example-app", "--wait", "--timeout", "100"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 4
    captured = capsys.readouterr()
    assert "result: FAIL (transparent)" in captured.out
    assert "run failed: transparent" in captured.err


# --- CLI: opt-in cursor force ------------------------------------------------


def test_cli_run_cursor_force_starts_the_helper(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _allow_cursor_force(monkeypatch)
    calls: list[tuple[str, set[int]]] = []

    def record(target: Profile, target_paths: Paths, pids: set[int]) -> None:
        assert target_paths is paths
        calls.append((target.id, set(pids)))

    monkeypatch.setattr(cli_module, "start_force", record)

    result = invoke(
        "run",
        "example-app",
        "--cursor=force",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert calls == [("example-app", {4242})]


def test_cli_run_cursor_force_rejects_missing_display(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        cli_module,
        "start_force",
        lambda *args, **kwargs: pytest.fail("started the helper without a display"),
    )

    result = invoke(
        "run",
        "example-app",
        "--cursor=force",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 3
    assert isinstance(result.exception, UnsupportedEnvironment)
    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []


def test_cli_run_cursor_invalid_is_usage_error(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    # No profile on disk: validation must run before the profile is loaded.
    result = invoke(
        "run",
        "example-app",
        "--cursor=weird",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 2
    assert isinstance(result.exception, UsageError)
    assert fake_backend.runs == []


def test_cli_run_cursor_off_does_not_start_the_helper(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "start_force",
        lambda *args, **kwargs: pytest.fail("started the helper with --cursor=off"),
    )

    result = invoke(
        "run",
        "example-app",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert len(fake_backend.runs) == 1


def test_cli_run_wait_cursor_force_starts_the_helper_after_launch(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _allow_cursor_force(monkeypatch)
    _freeze_verify_clock(monkeypatch)
    events: list[str] = []
    spawn = fake_backend.run

    def record_spawn(prefix: Path, exe: str, argv: list[str]) -> Any:
        events.append("spawn")
        return spawn(prefix, exe, argv)

    def record_helper(target: Profile, target_paths: Paths, pids: set[int]) -> None:
        events.append("helper")
        assert target.id == profile.id
        assert target_paths is paths
        assert set(pids) == {4242}

    monkeypatch.setattr(fake_backend, "run", record_spawn)
    monkeypatch.setattr(cli_module, "start_force", record_helper)

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--timeout",
        "100",
        "--cursor=force",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert events == ["spawn", "helper"]


def test_cli_run_cursor_force_starts_the_helper_for_a_live_instance(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _allow_cursor_force(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree={77, 88}),
    )
    calls: list[set[int]] = []
    monkeypatch.setattr(
        cli_module,
        "start_force",
        lambda target, target_paths, pids: calls.append(set(pids)),
    )

    result = invoke(
        "run",
        "example-app",
        "--cursor=force",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    assert calls == [{77, 88}]


def test_cli_run_wait_cursor_force_does_not_start_the_helper_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _allow_cursor_force(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "run_app",
        lambda *args, **kwargs: Launch(popen=None, pid_tree={77}),
    )
    monkeypatch.setattr(
        cli_module,
        "start_force",
        lambda *args, **kwargs: pytest.fail("started a wait helper for a live instance"),
    )

    result = invoke(
        "run",
        "example-app",
        "--wait",
        "--cursor=force",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 4
    assert "already-running" in result.output


def test_cli_run_cursor_force_json_preflight_failure_emits_result(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    def unsupported() -> None:
        raise UnsupportedEnvironment("desktop not supported: no X display")

    monkeypatch.setattr(cli_module, "require_force_supported", unsupported)

    result = invoke(
        "run",
        "example-app",
        "--cursor=force",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 3
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(lines) == 1
    assert lines[0]["event"] == "result"
    assert lines[0]["status"] == "fail"
    assert lines[0]["reason"] == "unsupported"
    assert fake_backend.runs == []
