"""Login-callback bridge: scheme registration, unregistration, and forwarding.

Registration is owned by ``xdg-mime``; WinBridge only writes the separate
``NoDisplay`` handler and removes its own lines when unregistering. Forwarding
reuses the safe run path, so a callback can never kill a running app and a URL
never reaches a shell. Every external call is either monkeypatched or backed by
the shared fake backend; no Wine and no desktop are required.
"""

from __future__ import annotations

import logging
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge.cli import app, main
from winbridge.core import launcher, urlbridge
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import UnsupportedEnvironment, UsageError, WinBridgeError
from winbridge.core.profile import Profile
from winbridge.core.runner import Catalog
from winbridge.core.state import Paths
from winbridge.core.urlbridge import (
    login_bridge_status,
    open_url,
    register_schemes,
    unregister_schemes,
)

CALLBACK = "example-app://callback?code=x"
HANDLER = "winbridge-example-app-handler.desktop"
SECRET = "example-app://callback?code=secret-token"


def _no_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tool discovery deterministic regardless of the host image."""
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)


def _record_xdg(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, str] | None = None
) -> list[list[str]]:
    """Replace the single ``xdg-mime`` entry point and record every call."""
    calls: list[list[str]] = []
    resolved = dict(answers or {})

    def fake_run(args: list[str]) -> str:
        calls.append(list(args))
        return resolved.get(args[-1], "")

    monkeypatch.setattr(urlbridge, "run", fake_run)
    return calls


def _fail_kill(*args: Any, **kwargs: Any) -> None:
    pytest.fail("a callback killed a running prefix")


def _forward_through(
    monkeypatch: pytest.MonkeyPatch, *, running: bool
) -> list[float]:
    """Keep the real forwarding path but pin prefix discovery and the delay."""
    sleeps: list[float] = []
    real_run_app = urlbridge.run_app
    monkeypatch.setattr("winbridge.core.run.kill_prefix", _fail_kill)
    monkeypatch.setattr("winbridge.core.run.time.sleep", sleeps.append)

    def wrapped(
        profile: Profile,
        paths: Paths,
        backend: Any,
        *,
        url: str | None = None,
        **kwargs: Any,
    ) -> Any:
        probe = (lambda _: {11}) if running else (lambda _: set())
        return real_run_app(profile, paths, backend, url=url, proc_probe=probe)

    monkeypatch.setattr(urlbridge, "run_app", wrapped)
    return sleeps


def _failing_wine(returncode: int = 1) -> Callable[..., subprocess.CompletedProcess[str]]:
    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr="")

    return wine


def _write_handler(paths: Paths, profile: Profile) -> Path:
    return launcher.write_scheme_handler(
        paths, profile, exec_path="/usr/bin/winbridge"
    )


# --- register ----------------------------------------------------------------


def test_register_schemes_writes_handler_and_calls_xdg_mime(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_external_tools(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr("winbridge.core.urlbridge.run", lambda args: calls.append(args))

    register_schemes(profile, paths)

    handler = paths.applications / "winbridge-example-app-handler.desktop"
    assert handler.exists()
    text = handler.read_text(encoding="utf-8")
    assert "NoDisplay=true" in text
    assert "Exec=" in text
    assert "open-url example-app %u" in text
    assert "MimeType=x-scheme-handler/example-app;" in text
    assert stat.S_IMODE(handler.stat().st_mode) == 0o644
    assert [
        "xdg-mime",
        "default",
        "winbridge-example-app-handler.desktop",
        "x-scheme-handler/example-app",
    ] in calls
    assert len(calls) == 1


def test_register_schemes_uses_an_absolute_executable(
    profile: Profile, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    _record_xdg(monkeypatch)

    register_schemes(profile, paths)

    handler = paths.applications / HANDLER
    exec_line = next(
        line
        for line in handler.read_text(encoding="utf-8").splitlines()
        if line.startswith("Exec=")
    )
    assert exec_line != "Exec=open-url example-app %u"
    assert exec_line.startswith(("Exec=/", 'Exec="'))


def test_register_schemes_calls_xdg_mime_once_per_unique_scheme(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)
    profile = make_profile(schemes=["first-app", "second-app", "first-app"])

    schemes = register_schemes(profile, paths)

    assert schemes == ["first-app", "second-app"]
    assert calls == [
        ["xdg-mime", "default", HANDLER, "x-scheme-handler/first-app"],
        ["xdg-mime", "default", HANDLER, "x-scheme-handler/second-app"],
    ]
    text = (paths.applications / HANDLER).read_text(encoding="utf-8")
    assert "MimeType=x-scheme-handler/first-app;x-scheme-handler/second-app;" in text


def test_register_schemes_does_not_touch_mimeapps(
    paths: Paths, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    _record_xdg(monkeypatch)
    mimeapps = paths.config_home / "mimeapps.list"
    original = (
        "# hand-written defaults stay untouched\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
    )
    mimeapps.write_text(original, encoding="utf-8")

    register_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == original


def test_register_schemes_rejects_a_profile_without_schemes(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)
    profile = make_profile(schemes=[])

    with pytest.raises(UsageError, match="no schemes"):
        register_schemes(profile, paths)

    assert calls == []
    assert not (paths.applications / HANDLER).exists()


@pytest.mark.parametrize(
    "schemes",
    [["1bad"], ["bad scheme"], ["bad/scheme"], ["bad\nscheme"], ["a", r"b\c"]],
)
def test_register_schemes_rejects_malformed_schemes(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
    schemes: list[str],
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)

    with pytest.raises(UsageError, match="invalid URL scheme"):
        register_schemes(make_profile(schemes=schemes), paths)

    assert calls == []
    assert not (paths.applications / HANDLER).exists()


def test_register_schemes_rejects_a_reserved_scheme(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)

    with pytest.raises(UsageError, match="reserved"):
        register_schemes(make_profile(schemes=["https"]), paths)

    assert calls == []
    assert not (paths.applications / HANDLER).exists()


def test_cli_login_bridge_rejects_a_reserved_scheme(
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)
    write_profile(paths.root, make_profile(schemes=["https"]))
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr(sys, "argv", ["winbridge", "login-bridge", "install", "example-app"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "reserved" in capsys.readouterr().err
    assert calls == []


def test_register_schemes_surfaces_an_xdg_failure(
    paths: Paths, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)

    def failing(args: list[str]) -> str:
        raise WinBridgeError("xdg-mime default exited 1")

    monkeypatch.setattr(urlbridge, "run", failing)

    with pytest.raises(WinBridgeError, match="xdg-mime default"):
        register_schemes(profile, paths)


# --- unregister --------------------------------------------------------------


def test_unregister_removes_only_our_mimeapps_lines(
    tmp_path: Path, profile: Profile, paths: Paths
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.parent.mkdir(parents=True, exist_ok=True)
    mimeapps.write_text(
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    text = mimeapps.read_text(encoding="utf-8")
    assert "https=firefox.desktop" in text
    assert "example-app" not in text


def test_unregister_removes_the_handler_entry(paths: Paths, profile: Profile) -> None:
    handler = _write_handler(paths, profile)
    assert handler.exists()

    unregister_schemes(profile, paths)

    assert not handler.exists()
    assert handler == paths.applications / HANDLER


def test_unregister_preserves_comments_and_unrelated_entries(
    paths: Paths, profile: Profile
) -> None:
    _write_handler(paths, profile)
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
        "\n"
        "[Added Associations]\n"
        "https=firefox.desktop;\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == (
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
        "\n"
        "[Added Associations]\n"
        "https=firefox.desktop;\n"
    )


def test_unregister_keeps_a_default_owned_by_another_app(
    paths: Paths, profile: Profile
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    original = (
        "[Default Applications]\n"
        "x-scheme-handler/example-app=firefox.desktop\n"
    )
    mimeapps.write_text(original, encoding="utf-8")

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == original


def test_unregister_filters_only_our_handler_from_a_list(
    paths: Paths, profile: Profile
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "[Default Applications]\n"
        "x-scheme-handler/example-app="
        "winbridge-example-app-handler.desktop;other.desktop;\n"
        "[Added Associations]\n"
        "x-scheme-handler/example-app=other.desktop;\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == (
        "[Default Applications]\n"
        "x-scheme-handler/example-app=other.desktop;\n"
        "[Added Associations]\n"
        "x-scheme-handler/example-app=other.desktop;\n"
    )


def test_unregister_accepts_a_handler_without_the_desktop_suffix(
    paths: Paths, profile: Profile
) -> None:
    _write_handler(paths, profile)
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "[Default Applications]\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == "[Default Applications]\n"


def test_unregister_is_idempotent(paths: Paths, profile: Profile) -> None:
    _write_handler(paths, profile)
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "https=firefox.desktop\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)
    once = mimeapps.read_text(encoding="utf-8")
    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == once == "https=firefox.desktop\n"


def test_unregister_without_mimeapps_does_not_create_it(
    paths: Paths, profile: Profile
) -> None:
    _write_handler(paths, profile)
    mimeapps = paths.config_home / "mimeapps.list"
    assert not mimeapps.exists()

    unregister_schemes(profile, paths)

    assert not mimeapps.exists()


def test_unregister_is_safe_when_the_handler_is_already_gone(
    paths: Paths, profile: Profile
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == ""


def test_unregister_preserves_the_file_mode_and_leaves_no_temp_files(
    paths: Paths, profile: Profile
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )
    mimeapps.chmod(0o600)

    unregister_schemes(profile, paths)

    assert stat.S_IMODE(mimeapps.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in paths.config_home.iterdir()) == [
        "mimeapps.list"
    ]


def test_unregister_writes_through_a_symlinked_mimeapps(
    paths: Paths, profile: Profile
) -> None:
    real = paths.config_home / "dotfiles-mimeapps.list"
    real.write_text(
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )
    link = paths.config_home / "mimeapps.list"
    link.symlink_to(real)

    unregister_schemes(profile, paths)

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert real.read_text(encoding="utf-8") == (
        "[Default Applications]\nhttps=firefox.desktop\n"
    )
    assert sorted(entry.name for entry in paths.config_home.iterdir()) == [
        "dotfiles-mimeapps.list",
        "mimeapps.list",
    ]


def test_unregister_removes_associations_for_schemes_no_longer_recorded(
    paths: Paths, profile: Profile
) -> None:
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "# user note\n"
        "[Default Applications]\n"
        "https=winbridge-example-app-handler.desktop\n"
        "x-scheme-handler/old-callback=winbridge-example-app-handler.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
        "[Added Associations]\n"
        "https=firefox.desktop;\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert mimeapps.read_text(encoding="utf-8") == (
        "# user note\n"
        "[Default Applications]\n"
        "x-scheme-handler/other-app=other.desktop\n"
        "[Added Associations]\n"
        "https=firefox.desktop;\n"
    )


def test_unregister_without_schemes_still_removes_handler_and_associations(
    paths: Paths, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile(schemes=[])
    handler = paths.applications / HANDLER
    handler.parent.mkdir(parents=True, exist_ok=True)
    handler.write_text("[Desktop Entry]\n", encoding="utf-8")
    mimeapps = paths.config_home / "mimeapps.list"
    mimeapps.write_text(
        "[Default Applications]\n"
        "x-scheme-handler/old-callback=winbridge-example-app-handler.desktop\n",
        encoding="utf-8",
    )

    unregister_schemes(profile, paths)

    assert not handler.exists()
    assert mimeapps.read_text(encoding="utf-8") == "[Default Applications]\n"


# --- open_url: validation ----------------------------------------------------


def test_open_url_rejects_unregistered_scheme(
    profile: Profile, fake_backend: Any
) -> None:
    with pytest.raises(WinBridgeError):
        open_url(profile, fake_backend, "other-app://callback")


def test_open_url_rejects_without_touching_the_backend(
    profile: Profile, fake_backend: Any, paths: Paths
) -> None:
    with pytest.raises(UsageError, match="unregistered URL scheme"):
        open_url(profile, fake_backend, "other-app://callback", paths=paths)

    assert fake_backend.calls == []
    assert fake_backend.wine_calls == []
    assert fake_backend.runs == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "https://evil.example",
        "example-app://callback?code=x\nsecond",
        " example-app://callback?code=x",
        "example-app://[broken",
        "://callback",
    ],
)
def test_open_url_rejects_malformed_or_foreign_urls(
    profile: Profile, fake_backend: Any, paths: Paths, url: str
) -> None:
    with pytest.raises(UsageError):
        open_url(profile, fake_backend, url, paths=paths)

    assert fake_backend.calls == []


def test_open_url_rejects_a_profile_without_schemes(
    make_profile: Callable[..., Profile], fake_backend: Any, paths: Paths
) -> None:
    with pytest.raises(UsageError, match="unregistered URL scheme"):
        open_url(
            make_profile(schemes=[]),
            fake_backend,
            "example-app://callback?code=x",
            paths=paths,
        )

    assert fake_backend.calls == []


def test_open_url_rejects_a_malformed_recorded_scheme(
    make_profile: Callable[..., Profile], fake_backend: Any, paths: Paths
) -> None:
    with pytest.raises(UsageError, match="invalid URL scheme"):
        open_url(
            make_profile(schemes=["bad scheme"]),
            fake_backend,
            "bad scheme://callback",
            paths=paths,
        )

    assert fake_backend.calls == []


# --- open_url: forwarding ----------------------------------------------------


def test_open_url_warm_forwards_without_starting_or_waiting(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    sleeps = _forward_through(monkeypatch, running=True)

    code = open_url(profile, fake_backend, CALLBACK, paths=paths)

    assert code == 0
    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert fake_backend.runs == []
    assert sleeps == []


def test_open_url_cold_starts_then_forwards_after_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    sleeps = _forward_through(monkeypatch, running=False)

    code = open_url(profile, fake_backend, CALLBACK, paths=paths)

    assert code == 0
    assert fake_backend.calls[0].startswith("run ")
    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert sleeps == [2.0]


def test_open_url_accepts_another_recorded_scheme(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    make_profile: Callable[..., Profile],
    paths: Paths,
) -> None:
    profile = make_profile(schemes=["first-app", "second-app"])
    _forward_through(monkeypatch, running=True)

    open_url(profile, fake_backend, "second-app://callback?code=x", paths=paths)

    assert fake_backend.wine_calls == ["start second-app://callback?code=x"]


def test_open_url_matches_the_scheme_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    _forward_through(monkeypatch, running=True)
    url = "EXAMPLE-APP://callback?code=x"

    open_url(profile, fake_backend, url, paths=paths)

    assert fake_backend.wine_calls == [f"start {url}"]


def test_open_url_keeps_the_url_as_one_argv_argument(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    payload = "example-app://callback?code=x&next=$(touch /tmp/pwned) and spaces"
    seen: list[tuple[Path, tuple[str, ...]]] = []

    def record(prefix: Path, *args: str) -> subprocess.CompletedProcess[str]:
        seen.append((prefix, args))
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    _forward_through(monkeypatch, running=True)
    monkeypatch.setattr(fake_backend, "wine", record)

    open_url(profile, fake_backend, payload, paths=paths)

    assert seen == [(paths.prefix_for(profile.id), ("start", payload))]


@pytest.mark.parametrize("running", [True, False], ids=["warm", "cold"])
def test_open_url_forward_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
    running: bool,
) -> None:
    _forward_through(monkeypatch, running=running)
    monkeypatch.setattr(fake_backend, "wine", _failing_wine())

    with pytest.raises(WinBridgeError) as error:
        open_url(profile, fake_backend, SECRET, paths=paths)

    message = str(error.value)
    assert "secret-token" not in message
    assert "example-app://callback/[redacted]" in message


def test_open_url_logs_only_a_redacted_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_backend: Any,
    profile: Profile,
    paths: Paths,
) -> None:
    _forward_through(monkeypatch, running=True)
    caplog.set_level(logging.INFO, logger="winbridge")

    open_url(profile, fake_backend, SECRET, paths=paths)

    assert "secret-token" not in caplog.text
    assert "example-app://callback/[redacted]" in caplog.text


# --- status ------------------------------------------------------------------


def test_login_bridge_status_reports_one_default_per_scheme(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_xdg(
        monkeypatch, {"x-scheme-handler/example-app": HANDLER}
    )
    profile = make_profile(schemes=["first-app", "example-app"])

    status = login_bridge_status(profile, paths)

    assert status == {"first-app": "", "example-app": HANDLER}
    assert calls == [
        ["xdg-mime", "query", "default", "x-scheme-handler/first-app"],
        ["xdg-mime", "query", "default", "x-scheme-handler/example-app"],
    ]


def test_login_bridge_status_without_schemes_queries_nothing(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_xdg(monkeypatch)

    assert login_bridge_status(make_profile(schemes=[]), paths) == {}
    assert calls == []


def test_login_bridge_status_rejects_malformed_schemes(
    paths: Paths,
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_xdg(monkeypatch)

    with pytest.raises(UsageError, match="invalid URL scheme"):
        login_bridge_status(make_profile(schemes=["bad scheme"]), paths)

    assert calls == []


def test_login_bridge_status_surfaces_a_missing_tool(
    paths: Paths, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(args: list[str]) -> str:
        raise UnsupportedEnvironment("xdg-mime not found; install xdg-utils")

    monkeypatch.setattr(urlbridge, "run", missing)

    with pytest.raises(UnsupportedEnvironment, match="xdg-utils"):
        login_bridge_status(profile, paths)


# --- CLI ---------------------------------------------------------------------


def _context(paths: Paths, catalog: Catalog, backend: Any) -> Context:
    return Context(paths=paths, config=Config(), catalog=catalog, backend=backend)


def _invoke(*args: str, paths: Paths, context: Context | None = None) -> Any:
    payload: dict[str, Any] = {"paths": paths}
    if context is not None:
        payload["context"] = context
    return CliRunner().invoke(app, list(args), obj=payload)


def test_cli_open_url_forwards_to_a_running_app(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _forward_through(monkeypatch, running=True)

    result = _invoke(
        "open-url",
        "example-app",
        CALLBACK,
        paths=paths,
        context=_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert fake_backend.wine_calls == [f"start {CALLBACK}"]
    assert fake_backend.runs == []


def test_cli_open_url_prints_a_redacted_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    _forward_through(monkeypatch, running=True)

    result = _invoke(
        "open-url",
        "example-app",
        SECRET,
        paths=paths,
        context=_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 0, result.output
    assert "secret-token" not in result.output
    assert "example-app://callback/[redacted]" in result.output


def test_cli_open_url_rejects_an_unregistered_scheme(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    result = _invoke(
        "open-url",
        "example-app",
        "other-app://callback",
        paths=paths,
        context=_context(paths, catalog, fake_backend),
    )

    assert isinstance(result.exception, UsageError)
    assert "unregistered URL scheme" in str(result.exception)
    assert fake_backend.calls == []


def test_cli_open_url_exits_2_at_the_process_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr(
        "winbridge.cli._profile_context",
        lambda ctx, profile: _context(paths, catalog, fake_backend),
    )
    monkeypatch.setattr(
        sys, "argv", ["winbridge", "open-url", "example-app", "other-app://callback"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "unregistered URL scheme" in err
    assert fake_backend.calls == []


def test_cli_open_url_unknown_app_exits_1(
    paths: Paths, catalog: Catalog, fake_backend: Any
) -> None:
    result = _invoke(
        "open-url",
        "missing-app",
        CALLBACK,
        paths=paths,
        context=_context(paths, catalog, fake_backend),
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert "missing-app" in str(result.exception)


def test_cli_login_bridge_install_registers_schemes(
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    profile: Profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_external_tools(monkeypatch)
    calls = _record_xdg(monkeypatch)
    write_profile(paths.root, profile)

    result = _invoke("login-bridge", "install", "example-app", paths=paths)

    assert result.exit_code == 0, result.output
    assert (paths.applications / HANDLER).exists()
    assert calls == [
        ["xdg-mime", "default", HANDLER, "x-scheme-handler/example-app"]
    ]
    assert "x-scheme-handler/example-app" in result.output
    assert "experimental" in result.output


def test_cli_login_bridge_status_reports_defaults(
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_xdg(monkeypatch, {"x-scheme-handler/example-app": HANDLER})
    profile = make_profile(schemes=["first-app", "example-app"])
    write_profile(paths.root, profile)

    result = _invoke("login-bridge", "status", "example-app", paths=paths)

    assert result.exit_code == 0, result.output
    assert HANDLER in result.output
    assert "ok" in result.output
    assert "not registered" in result.output
    assert "first-app" in result.output


def test_cli_login_bridge_status_without_schemes(
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_xdg(monkeypatch)
    write_profile(paths.root, make_profile(schemes=[]))

    result = _invoke("login-bridge", "status", "example-app", paths=paths)

    assert result.exit_code == 0, result.output
    assert "no schemes" in result.output
    assert calls == []


def test_cli_login_bridge_install_unknown_app_exits_1(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_xdg(monkeypatch)

    result = _invoke("login-bridge", "install", "missing-app", paths=paths)

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert calls == []
