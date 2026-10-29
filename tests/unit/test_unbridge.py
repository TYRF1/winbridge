"""Clean removal of everything WinBridge created for one app.

``unbridge`` owns no payload of its own: it stops and deletes the prefix,
removes the profile, both desktop entries, the scheme associations, and the
bar pin. Every artifact is addressed from the app id, so a missing profile or a
missing entry is a warning rather than a failure and a second run is a no-op.
No Wine and no desktop are required; the backend and the desktop adapter are
fakes, and every external path is redirected into ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.cli import app
from winbridge.core import launcher, omarchy
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.launcher import write_desktop_entry, write_scheme_handler
from winbridge.core.omarchy import pin_app
from winbridge.core.prefix import PrefixStillRunning
from winbridge.core.profile import Profile
from winbridge.core.runner import Catalog
from winbridge.core.state import Paths
from winbridge.core.unbridge import NoopBackend, unbridge
from winbridge.desktops.x11 import X11Desktop

LAUNCHER = "winbridge-example-app.desktop"
HANDLER = "winbridge-example-app-handler.desktop"
PIN_ID = "winbridge-example-app"


class FakeDesktop:
    """Record unpin requests; the real adapters are Task 15/16."""

    name = "fake"
    capabilities = {"pin"}

    def __init__(self, removed: bool = True) -> None:
        self.removed = removed
        self.unpinned: list[str] = []

    def unpin(self, desktop_id: str) -> bool:
        self.unpinned.append(desktop_id)
        return self.removed


@pytest.fixture(autouse=True)
def _no_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop database refreshes must not depend on the host image."""
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)


@pytest.fixture(autouse=True)
def shell_restarts(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> list[bool]:
    """Redirect the omarchy config and record best-effort shell restarts."""
    monkeypatch.setattr(omarchy, "DEFAULT_SHELL_CONFIG", paths.root / "shell.json")
    calls: list[bool] = []

    def fake_restart(*, enabled: bool = True) -> bool:
        calls.append(enabled)
        return True

    monkeypatch.setattr(omarchy, "restart_shell", fake_restart)
    return calls


def _write_shell(paths: Paths, apps: list[dict[str, str]] | None = None) -> Path:
    """Write a minimal omarchy-shell config with the taskbar widget."""
    target = paths.root / "shell.json"
    document = {
        "bar": {
            "left": [
                {
                    "id": omarchy.TASKBAR_ID,
                    "apps": list(apps or []),
                }
            ]
        },
        "unrelated": {"keep": True},
    }
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target


def _apps(target: Path) -> list[Any]:
    document = json.loads(target.read_text(encoding="utf-8"))
    return document["bar"]["left"][0]["apps"]


def _write_mimeapps(paths: Paths) -> Path:
    target = paths.config_home / "mimeapps.list"
    target.write_text(
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/example-app=winbridge-example-app-handler.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n",
        encoding="utf-8",
    )
    return target


def _seed(
    paths: Paths,
    profile: Profile,
    *,
    prefix: bool = True,
    entries: bool = True,
    profile_file: bool = True,
) -> Path:
    """Create the prefix, profile, and desktop entries for one app."""
    target = paths.prefix_for(profile.id)
    if prefix:
        (target / "drive_c" / "users" / "me").mkdir(parents=True)
        (target / "system.reg").write_text("[registry]\n", encoding="utf-8")
    if profile_file:
        profile.dump(paths.profiles / f"{profile.id}.toml")
    if entries:
        write_desktop_entry(paths, profile, exec_path="/usr/bin/winbridge")
        write_scheme_handler(paths, profile, exec_path="/usr/bin/winbridge")
    return target


def _host_file(tmp_path: Path) -> Path:
    host = tmp_path / "host" / "keep.txt"
    host.parent.mkdir(parents=True)
    host.write_text("user content\n", encoding="utf-8")
    return host


def test_unbridge_removes_all_owned_artifacts(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
) -> None:
    """The brief's headline case: every artifact WinBridge wrote is gone."""
    prefix = _seed(paths, profile)
    shell = _write_shell(paths, [{"desktopId": "other-app", "match": "^Other$"}])
    pin_app(shell, PIN_ID, "^Example$")
    mimeapps = _write_mimeapps(paths)
    host = _host_file(tmp_path)
    exposed = prefix / "drive_c" / "users" / "me" / "repos"
    exposed.symlink_to(host.parent, target_is_directory=True)

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert not (paths.applications / LAUNCHER).exists()
    assert not (paths.applications / HANDLER).exists()
    assert all(entry["desktopId"] != PIN_ID for entry in _apps(shell))
    assert [entry["desktopId"] for entry in _apps(shell)] == ["other-app"]
    assert json.loads(shell.read_text(encoding="utf-8"))["unrelated"] == {"keep": True}
    assert mimeapps.read_text(encoding="utf-8") == (
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
    )
    assert host.read_text(encoding="utf-8") == "user content\n"


def test_unbridge_removes_the_extracted_icon_and_profile_backup(
    profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """Setup's icon and --force backup are exact app-id-owned artifacts."""
    _seed(paths, profile)
    icon = paths.applications / f"{PIN_ID}.png"
    icon.write_bytes(b"icon")
    backup = paths.profiles / f"{profile.id}.toml.bak"
    backup.write_text("previous profile\n", encoding="utf-8")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not icon.exists()
    assert not backup.exists()


def test_unbridge_preserves_unrelated_png_and_profile_backup(
    profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """Only the exact app-id-named icon and backup are ours to remove."""
    _seed(paths, profile)
    other_icon = paths.applications / "other.png"
    other_icon.write_bytes(b"not ours")
    other_backup = paths.profiles / "other.toml.bak"
    other_backup.write_text("not ours\n", encoding="utf-8")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert other_icon.read_bytes() == b"not ours"
    assert other_backup.read_text(encoding="utf-8") == "not ours\n"


def test_unbridge_is_idempotent(profile: Profile, paths: Paths, fake_backend: Any) -> None:
    """A second run must succeed and must not delete or spawn anything again."""
    prefix = _seed(paths, profile)
    _write_shell(paths)
    pin_app(paths.root / "shell.json", PIN_ID, "^Example$")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)
    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not prefix.exists()
    assert fake_backend.removed == [prefix]
    assert fake_backend.wine_calls == ["wineserver -k"]


def test_unbridge_requires_confirmation(profile: Profile, paths: Paths, fake_backend: Any) -> None:
    """The default is safe: refuse before touching any artifact."""
    prefix = _seed(paths, profile)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")

    with pytest.raises(UsageError, match="--yes"):
        unbridge(profile.id, paths, fake_backend)

    assert prefix.exists()
    assert (paths.profiles / f"{profile.id}.toml").exists()
    assert (paths.applications / LAUNCHER).exists()
    assert (paths.applications / HANDLER).exists()
    assert any(entry["desktopId"] == PIN_ID for entry in _apps(shell))
    assert fake_backend.calls == []


def test_unbridge_removes_only_the_named_app(
    make_profile: Callable[..., Profile], paths: Paths, fake_backend: Any
) -> None:
    """A neighbouring app keeps its prefix, profile, entries, and pin."""
    profile = make_profile()
    other = make_profile(id="other-app", name="Other App")
    prefix = _seed(paths, profile)
    other_prefix = _seed(paths, other)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")
    pin_app(shell, "winbridge-other-app", "^Other$")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not prefix.exists()
    assert other_prefix.exists()
    assert (paths.profiles / f"{other.id}.toml").exists()
    assert (paths.applications / "winbridge-other-app.desktop").exists()
    assert (paths.applications / "winbridge-other-app-handler.desktop").exists()
    assert [entry["desktopId"] for entry in _apps(shell)] == ["winbridge-other-app"]


def test_unbridge_leaves_exposed_host_files_untouched(
    tmp_path: Path, profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """Removing the prefix must unlink an exposed symlink, never its target."""
    prefix = _seed(paths, profile)
    host = _host_file(tmp_path)
    link = prefix / "drive_c" / "users" / "me" / "host"
    link.symlink_to(host.parent, target_is_directory=True)

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not link.exists()
    assert host.read_text(encoding="utf-8") == "user content\n"


def test_unbridge_refuses_a_symlinked_prefix(
    tmp_path: Path, profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """A prefix that resolves outside the store is not ours to delete."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "keep.txt").write_text("keep\n", encoding="utf-8")
    prefix = paths.prefix_for(profile.id)
    prefix.parent.mkdir(parents=True)
    prefix.symlink_to(host, target_is_directory=True)
    profile.dump(paths.profiles / f"{profile.id}.toml")

    with pytest.raises(UsageError, match="symlink"):
        unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert (host / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (paths.profiles / f"{profile.id}.toml").exists()
    assert fake_backend.calls == []


def test_unbridge_missing_artifacts_are_warnings(
    paths: Paths, fake_backend: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing to remove is not an error; the backend is not touched."""
    caplog.set_level(logging.WARNING, logger="winbridge")

    unbridge("example-app", paths, fake_backend, confirm=False, restart_shell=False)

    assert fake_backend.calls == []
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "no profile" in messages
    assert "no prefix" in messages
    assert "no launcher" in messages
    assert "no scheme handler" in messages
    assert "no pin" in messages


def test_unbridge_stops_the_prefix_before_removing_it(
    profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """Kill and wait first; only then is the tree deleted."""
    prefix = _seed(paths, profile)

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert fake_backend.wine_calls == ["wineserver -k"]
    assert fake_backend.removed == [prefix]
    assert fake_backend.calls.index("wineserver -k") < fake_backend.calls.index(
        f"remove {prefix}"
    )


def test_unbridge_aborts_before_deleting_when_the_prefix_will_not_stop(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live process means the user still has unsaved work; stop and report."""
    prefix = _seed(paths, profile)

    def still_running(backend: Any, target: Path) -> None:
        raise PrefixStillRunning(f"prefix {target} still has processes")

    monkeypatch.setattr("winbridge.core.unbridge.kill_prefix", still_running)

    with pytest.raises(PrefixStillRunning):
        unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert prefix.exists()
    assert (paths.profiles / f"{profile.id}.toml").exists()
    assert (paths.applications / LAUNCHER).exists()
    assert (paths.applications / HANDLER).exists()
    assert fake_backend.removed == []


def test_unbridge_cleans_the_rest_when_the_prefix_is_already_gone(
    profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """A missing prefix skips the kill path but does not block cleanup."""
    prefix = _seed(paths, profile, prefix=False)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert not (paths.applications / LAUNCHER).exists()
    assert not (paths.applications / HANDLER).exists()
    assert _apps(shell) == []
    assert fake_backend.calls == []


def test_unbridge_removes_a_corrupt_profile_and_continues(
    paths: Paths,
    fake_backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile WinBridge cannot read must not make the app unremovable."""
    paths.profiles.mkdir(parents=True)
    profile_file = paths.profiles / "example-app.toml"
    profile_file.write_text("schema = ", encoding="utf-8")
    prefix = paths.prefix_for("example-app")
    (prefix / "drive_c").mkdir(parents=True)
    caplog.set_level(logging.WARNING, logger="winbridge")

    unbridge("example-app", paths, fake_backend, confirm=False, restart_shell=False)

    assert not profile_file.exists()
    assert not prefix.exists()
    assert "cannot read profile" in caplog.text


def test_unbridge_pin_failure_is_a_warning(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable bar config leaves a stale pin, not an unremoved app."""
    prefix = _seed(paths, profile)
    (paths.root / "shell.json").write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="winbridge")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert "could not remove pin" in caplog.text


def test_unbridge_delegates_pin_removal_to_the_desktop(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied adapter owns the pin; the omarchy default is not touched."""
    _seed(paths, profile)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")
    original = shell.read_text(encoding="utf-8")
    omarchy_calls: list[tuple[Path, str]] = []

    def record_unpin(config: Path, desktop_id: str) -> bool:
        omarchy_calls.append((config, desktop_id))
        return True

    monkeypatch.setattr(omarchy, "unpin_app", record_unpin)
    desktop = FakeDesktop()

    unbridge(
        profile.id,
        paths,
        fake_backend,
        desktop=desktop,
        confirm=False,
        restart_shell=False,
    )

    assert desktop.unpinned == [PIN_ID]
    assert omarchy_calls == []
    assert shell.read_text(encoding="utf-8") == original


def test_unbridge_removes_the_pin_with_an_adapter_that_cannot_pin(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    shell_restarts: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An X11 adapter cannot unpin, so cleanup must fall back to omarchy."""
    _seed(paths, profile)
    shell = _write_shell(paths, [{"desktopId": "other-app", "match": "^Other$"}])
    pin_app(shell, PIN_ID, "^Example$")
    caplog.set_level(logging.WARNING, logger="winbridge")

    unbridge(
        profile.id,
        paths,
        fake_backend,
        desktop=X11Desktop(which=lambda name: None),
        confirm=False,
    )

    assert [entry["desktopId"] for entry in _apps(shell)] == ["other-app"]
    assert shell_restarts == [True]
    assert "no pin" not in caplog.text


def test_unbridge_unpins_the_default_shell_config_without_a_desktop(
    profile: Profile, paths: Paths, fake_backend: Any
) -> None:
    """Without an adapter, the exact bare id is removed from omarchy's file."""
    _seed(paths, profile)
    shell = _write_shell(paths, [{"desktopId": "other-app", "match": "^Other$"}])
    pin_app(shell, PIN_ID, "^Example$")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert [entry["desktopId"] for entry in _apps(shell)] == ["other-app"]
    assert (paths.root / "shell.json.bak").exists()


def test_unbridge_removes_scheme_registration_without_a_profile(
    paths: Paths, fake_backend: Any
) -> None:
    """The handler and mimeapps lines are derived from the id, not the profile."""
    handler = paths.applications / HANDLER
    handler.parent.mkdir(parents=True)
    handler.write_text("[Desktop Entry]\n", encoding="utf-8")
    mimeapps = _write_mimeapps(paths)

    unbridge("example-app", paths, fake_backend, confirm=False, restart_shell=False)

    assert not handler.exists()
    assert mimeapps.read_text(encoding="utf-8") == (
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
    )
    assert not (paths.profiles / "example-app.toml").exists()


def test_unbridge_does_not_create_a_missing_mimeapps(
    paths: Paths, fake_backend: Any
) -> None:
    """Cleanup reads the default list; it never materialises one."""
    mimeapps = paths.config_home / "mimeapps.list"
    assert not mimeapps.exists()

    unbridge("example-app", paths, fake_backend, confirm=False, restart_shell=False)

    assert not mimeapps.exists()


def test_unbridge_restarts_the_shell_after_removing_a_pin(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    shell_restarts: list[bool],
) -> None:
    """A pin change is the one reason to disrupt the bar."""
    _seed(paths, profile)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")

    unbridge(profile.id, paths, fake_backend, confirm=False)

    assert shell_restarts == [True]


def test_unbridge_skips_the_shell_restart_when_disabled(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    shell_restarts: list[bool],
) -> None:
    """The programmatic opt-out must not invoke the shell at all."""
    _seed(paths, profile)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")

    unbridge(profile.id, paths, fake_backend, confirm=False, restart_shell=False)

    assert shell_restarts == []


def test_unbridge_does_not_restart_without_a_pin_change(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    shell_restarts: list[bool],
) -> None:
    """No pin, no rewrite, no restart; removal does not disturb the session."""
    _seed(paths, profile)
    _write_shell(paths)

    unbridge(profile.id, paths, fake_backend, confirm=False)

    assert shell_restarts == []


def test_unbridge_does_not_restart_when_the_desktop_had_no_pin(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    shell_restarts: list[bool],
) -> None:
    """The adapter's False means no change, so the bar stays untouched."""
    _seed(paths, profile)
    desktop = FakeDesktop(removed=False)

    unbridge(
        profile.id,
        paths,
        fake_backend,
        desktop=desktop,
        confirm=False,
    )

    assert desktop.unpinned == [PIN_ID]
    assert shell_restarts == []


# --- CLI ---------------------------------------------------------------------


def _context(
    paths: Paths,
    catalog: Catalog,
    backend: Any,
    desktop: FakeDesktop | None = None,
) -> Context:
    return Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=backend,
        desktop=desktop,
    )


def _invoke(
    *args: str,
    paths: Paths,
    context: Context | None = None,
    input_text: str | None = None,
) -> Any:
    payload: dict[str, Any] = {"paths": paths}
    if context is not None:
        payload["context"] = context
    return CliRunner().invoke(app, list(args), obj=payload, input=input_text)


def test_cli_unbridge_with_yes_removes_everything(
    profile: Profile,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    prefix = _seed(paths, profile)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")
    icon = paths.applications / f"{PIN_ID}.png"
    icon.write_bytes(b"icon")
    backup = paths.profiles / f"{profile.id}.toml.bak"
    backup.write_text("previous profile\n", encoding="utf-8")
    desktop = FakeDesktop()

    result = _invoke(
        "unbridge",
        profile.id,
        "--yes",
        paths=paths,
        context=_context(paths, catalog, fake_backend, desktop),
    )

    assert result.exit_code == 0, result.output
    assert profile.id in result.output
    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert not (paths.applications / LAUNCHER).exists()
    assert not (paths.applications / HANDLER).exists()
    assert not icon.exists()
    assert not backup.exists()
    assert desktop.unpinned == [PIN_ID]


def test_cli_unbridge_prompts_and_aborts_on_no(
    profile: Profile,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    prefix = _seed(paths, profile)
    desktop = FakeDesktop()

    result = _invoke(
        "unbridge",
        profile.id,
        paths=paths,
        context=_context(paths, catalog, fake_backend, desktop),
        input_text="n\n",
    )

    assert result.exit_code != 0
    assert "Aborted" in result.output
    assert prefix.exists()
    assert (paths.profiles / f"{profile.id}.toml").exists()
    assert fake_backend.calls == []
    assert desktop.unpinned == []


def test_cli_unbridge_prompts_and_proceeds_on_yes(
    profile: Profile,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    prefix = _seed(paths, profile)

    result = _invoke(
        "unbridge",
        profile.id,
        paths=paths,
        context=_context(paths, catalog, fake_backend, FakeDesktop()),
        input_text="y\n",
    )

    assert result.exit_code == 0, result.output
    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert fake_backend.removed == [prefix]


def test_cli_unbridge_without_a_profile_cleans_stale_artifacts(
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
) -> None:
    """A lost profile must not block deleting the rest of the app."""
    prefix = paths.prefix_for("example-app")
    (prefix / "drive_c").mkdir(parents=True)
    paths.applications.mkdir(parents=True)
    (paths.applications / LAUNCHER).write_text("[Desktop Entry]\n", encoding="utf-8")
    (paths.applications / HANDLER).write_text("[Desktop Entry]\n", encoding="utf-8")

    result = _invoke(
        "unbridge",
        "example-app",
        "--yes",
        paths=paths,
        context=_context(paths, catalog, fake_backend, FakeDesktop()),
    )

    assert result.exit_code == 0, result.output
    assert not prefix.exists()
    assert not (paths.applications / LAUNCHER).exists()
    assert not (paths.applications / HANDLER).exists()


def test_unbridge_noop_backend_refuses_a_prefix_that_appeared(
    profile: Profile,
    paths: Paths,
) -> None:
    """A prefix created after the CLI check must never be deleted unguarded."""
    prefix = _seed(paths, profile)
    marker = prefix / "drive_c" / "users" / "me" / "keep.txt"
    marker.write_text("user content\n", encoding="utf-8")

    with pytest.raises(WinBridgeError, match="runner missing"):
        unbridge(
            profile.id,
            paths,
            NoopBackend(),
            confirm=False,
            restart_shell=False,
        )

    assert marker.read_text(encoding="utf-8") == "user content\n"
    assert (paths.profiles / f"{profile.id}.toml").exists()
    assert (paths.applications / LAUNCHER).exists()
    assert (paths.applications / HANDLER).exists()


def test_cli_unbridge_without_context_or_runner_cleans_metadata(
    profile: Profile,
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No prefix means no backend is needed, even when no runner is installed."""
    prefix = _seed(paths, profile, prefix=False)
    shell = _write_shell(paths)
    pin_app(shell, PIN_ID, "^Example$")
    mimeapps = _write_mimeapps(paths)
    calls: list[str] = []
    original = cli_module._unbridge_context

    def record_context(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[2])
        return original(*args, **kwargs)

    def fail_global_state(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("metadata-only cleanup loaded global config or a runner")

    monkeypatch.setattr(cli_module, "_unbridge_context", record_context)
    monkeypatch.setattr(cli_module, "build_context", fail_global_state)
    monkeypatch.setattr(cli_module.Config, "load", fail_global_state)
    monkeypatch.setattr(cli_module, "_catalog", fail_global_state)

    result = _invoke("unbridge", profile.id, "--yes", paths=paths)

    assert result.exit_code == 0, result.output
    assert calls == [profile.id]
    assert not prefix.exists()
    assert not (paths.profiles / f"{profile.id}.toml").exists()
    assert not (paths.applications / LAUNCHER).exists()
    assert not (paths.applications / HANDLER).exists()
    assert _apps(shell) == []
    assert mimeapps.read_text(encoding="utf-8") == (
        "# user note: keep this\n"
        "[Default Applications]\n"
        "https=firefox.desktop\n"
        "x-scheme-handler/other-app=other.desktop\n"
    )


@pytest.mark.parametrize("with_profile", [True, False], ids=["profile", "no-profile"])
def test_cli_unbridge_with_prefix_and_missing_runner_fails_before_deletion(
    with_profile: bool,
    profile: Profile,
    paths: Paths,
) -> None:
    """A prefix that exists needs a runner; fail before deleting anything."""
    prefix = paths.prefix_for(profile.id)
    (prefix / "drive_c").mkdir(parents=True)
    paths.applications.mkdir(parents=True)
    launcher_entry = paths.applications / LAUNCHER
    launcher_entry.write_text("[Desktop Entry]\n", encoding="utf-8")
    if with_profile:
        profile.dump(paths.profiles / f"{profile.id}.toml")

    result = _invoke("unbridge", profile.id, "--yes", paths=paths)

    assert result.exit_code == 1
    message = str(result.exception)
    assert "runner missing" in message
    assert "nothing was removed" in message
    assert prefix.exists()
    assert launcher_entry.exists()
    if with_profile:
        assert (paths.profiles / f"{profile.id}.toml").exists()
