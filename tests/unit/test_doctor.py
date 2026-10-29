"""Doctor environment checks and the redacted diagnostic bundle.

Every external probe is injected or redirected: ``shutil.which``,
``shutil.disk_usage``, the desktop adapter, and the state paths all live inside
``tmp_path``. No test needs Wine, a compositor, a display, or the network, and no
test reads real user state.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import stat
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge import __version__
from winbridge.cli import app
from winbridge.core import launcher
from winbridge.core.config import Config
from winbridge.core.doctor import BundleIdentity, CheckResult, run_checks, write_bundle
from winbridge.core.locking import file_lock
from winbridge.core.profile import Profile, WebView2Runtime
from winbridge.core.runner import (
    INSTALL_MANIFEST_NAME,
    Catalog,
    Runner,
    install_runner,
)
from winbridge.core.state import Paths
from winbridge.core.webview2 import WEBVIEW2_CLIENT_GUID

APP_ID = "example-app"
SODA_REF = "soda@11.0-10"
SODA_ARCHIVE_SHA = "14633fabd180d308d2c1ebcb84a16f36d0484dded5809c64f8a7c93e654d7296"


class _Session:
    """The smallest desktop adapter doctor needs: a name and capabilities."""

    def __init__(self, name: str, capabilities: set[str]) -> None:
        self.name = name
        self.capabilities = capabilities


@pytest.fixture(autouse=True)
def _no_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop database refreshes must not depend on the host image."""
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)


def _session(name: str = "x11", capabilities: set[str] | None = None) -> _Session:
    return _Session(name, {"windows", "screenshot"} if capabilities is None else capabilities)


def _check(checks: list[CheckResult], name: str) -> CheckResult:
    matches = [check for check in checks if check.name == name]
    assert matches, f"missing check {name!r}; got {[check.name for check in checks]}"
    return matches[0]


def _system_reg(*versions: str) -> str:
    """Build a Wine system.reg fragment for the 64- and 32-bit runtime views."""
    keys = (
        rf"Software\\Microsoft\\EdgeUpdate\\Clients\\{WEBVIEW2_CLIENT_GUID}",
        rf"Software\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{WEBVIEW2_CLIENT_GUID}",
    )
    sections = [
        f"[{key}]\n" + "".join(f'"pv"="{version}"\n' for version in versions)
        for key in keys
    ]
    return "Windows Registry Editor Version 5.00\n\n" + "\n".join(sections)


def _make_prefix(paths: Paths, app_id: str = APP_ID, *, system_reg: str | None = None) -> Path:
    prefix = paths.prefix_for(app_id)
    prefix.mkdir(parents=True)
    (prefix / "drive_c").mkdir()
    (prefix / "system.reg").write_text(
        system_reg if system_reg is not None else _system_reg("156.0.4314.42"),
        encoding="utf-8",
    )
    return prefix


def _installed_runner(
    paths: Paths,
    wine: bytes | None = b"#!/bin/sh\n",
    *,
    manifest: bool = True,
    wine_sha256: str | None = None,
) -> Path:
    root = paths.runners / "soda" / "11.0-10"
    (root / "bin").mkdir(parents=True)
    if wine is not None:
        (root / "bin" / "wine").write_bytes(wine)
        if manifest:
            (root / INSTALL_MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "ref": SODA_REF,
                        "archive_sha256": SODA_ARCHIVE_SHA,
                        "wine_relpath": "bin/wine",
                        "wine_sha256": wine_sha256 or hashlib.sha256(wine).hexdigest(),
                        "installed_at": "2026-10-27T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
    return root


def _runner_archive(tmp_path: Path, wine: bytes) -> tuple[Path, str]:
    archive = tmp_path / "runner.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("bin/wine")
        info.size = len(wine)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(wine))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def _write_launcher(paths: Paths, profile: Profile) -> None:
    launcher.write_desktop_entry(paths, profile, exec_path="/usr/bin/winbridge")
    launcher.write_scheme_handler(paths, profile, exec_path="/usr/bin/winbridge")
    (paths.config_home / "mimeapps.list").write_text(
        "[Default Applications]\n"
        f"x-scheme-handler/{profile.schemes[0]}="
        f"{launcher.desktop_id(profile.id)}-handler.desktop\n",
        encoding="utf-8",
    )


# --- the three contract tests from the plan ----------------------------------


def test_missing_screenshot_tool_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: Paths, catalog: Catalog
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    checks = run_checks(paths, Config(), catalog)
    assert any(c.name == "desktop.screenshot" and c.status == "warn" for c in checks)


def test_bundle_redacts_urls(tmp_path: Path, paths: Paths) -> None:
    checks = [CheckResult("x", "ok", "https://example.com/cb?code=SECRET")]
    bundle = write_bundle(paths, checks, dest=tmp_path / "b.tar.gz")
    with tarfile.open(bundle) as tar:
        text = "\n".join(
            tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()
        )
    assert "SECRET" not in text


def test_bundle_defaults_under_state_not_cwd(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    bundle = write_bundle(paths, [])
    assert paths.bundles in bundle.parents
    assert not (tmp_path / bundle.name).exists()


# --- check inventory and environment probes ---------------------------------


def test_all_checks_report_a_known_status(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert checks
    assert all(check.status in {"ok", "warn", "fail"} for check in checks)
    assert len({check.name for check in checks}) == len(checks)


def test_no_app_selected_skips_app_scoped_checks(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    for name in ("prefix", "webview2", "launcher.entry", "launcher.scheme"):
        assert _check(checks, name).status == "ok"
        assert "skipped" in _check(checks, name).detail


def test_python_and_dependencies_are_ok(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "python").status == "ok"
    assert _check(checks, "python.deps").status == "ok"


def test_unsupported_session_is_a_warning(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(), catalog, desktop=_session("unsupported", set()))
    assert _check(checks, "session.adapter").status == "warn"
    assert _check(checks, "desktop.screenshot").status == "warn"


def test_bottles_backend_is_a_warning(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(backend="bottles"), catalog, desktop=_session())
    assert _check(checks, "config").status == "warn"


def test_unknown_default_runner_is_a_warning(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(
        paths, Config(default_runner="missing@1.0"), catalog, desktop=_session()
    )
    assert _check(checks, "config").status == "warn"


def test_screenshot_tool_present_is_ok(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "desktop.screenshot").status == "ok"
    assert "/usr/bin/import" in _check(checks, "desktop.screenshot").detail


def test_xdg_dirs_are_reported(paths: Paths, catalog: Catalog) -> None:
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "paths.xdg").status == "ok"


def test_low_disk_space_is_a_warning(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = SimpleNamespace(total=100, used=1, free=1024)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "disk").status == "warn"
    assert "minimum" in _check(checks, "disk").detail


def test_disk_space_above_minimum_is_ok(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = SimpleNamespace(total=100 * 1024**3, used=1, free=10 * 1024**3)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "disk").status == "ok"


def test_disk_probe_failure_is_a_warning(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: str) -> Any:
        raise OSError("no disk probe here")

    monkeypatch.setattr(shutil, "disk_usage", fail)
    checks = run_checks(paths, Config(), catalog, desktop=_session())
    assert _check(checks, "disk").status == "warn"


# --- runner store ------------------------------------------------------------


def test_runner_store_reports_the_installed_digest(paths: Paths, catalog: Catalog) -> None:
    _installed_runner(paths)

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "ok"
    assert SODA_REF in store.detail
    assert "verified wine sha256" in store.detail
    assert "sha256" in store.detail


def test_runner_store_missing_manifest_is_a_warning(
    paths: Paths, catalog: Catalog
) -> None:
    _installed_runner(paths, manifest=False)

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "warn"
    assert "not verifiable" in store.detail
    assert SODA_REF in store.detail


def test_runner_store_wine_digest_mismatch_fails(
    paths: Paths, catalog: Catalog
) -> None:
    _installed_runner(paths, wine_sha256="0" * 64)

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert "mismatch" in store.detail
    assert SODA_REF in store.detail


def test_runner_store_missing_wine_fails(paths: Paths, catalog: Catalog) -> None:
    _installed_runner(paths, wine=None)

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert SODA_REF in store.detail


def test_runner_store_unmanaged_entry_is_a_warning(paths: Paths, catalog: Catalog) -> None:
    (paths.runners / "unknown" / "1.0" / "bin").mkdir(parents=True)
    (paths.runners / "unknown" / "1.0" / "bin" / "wine").write_bytes(b"x")

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    assert _check(checks, "runners.store").status == "warn"


def test_runner_store_cached_archive_hash_mismatch_fails(
    paths: Paths, catalog: Catalog
) -> None:
    _installed_runner(paths)
    paths.cache.mkdir(parents=True)
    (paths.cache / "soda-11.0-10.archive").write_bytes(b"not the catalog payload")

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert "mismatch" in store.detail


def test_app_runner_unknown_fails(
    paths: Paths, catalog: Catalog, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile(runner="missing@1.0")

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert "winbridge runners install missing@1.0" in store.detail


def test_app_runner_not_installed_fails(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert f"winbridge runners install {SODA_REF}" in store.detail


def test_app_runner_installed_is_ok(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    _installed_runner(paths)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "ok"
    assert f"app runner {SODA_REF} is installed" in store.detail


def test_fresh_install_manifest_detects_wine_tampering(
    paths: Paths, catalog: Catalog, profile: Profile, tmp_path: Path
) -> None:
    archive, digest = _runner_archive(tmp_path, b"#!/bin/sh\necho runner\n")
    runner = Runner(
        "soda",
        "11.0-10",
        "soda",
        archive.as_uri(),
        digest,
        "bin/wine",
        default=True,
    )
    custom = Catalog((runner,), catalog.webview2, catalog.latest_webview2_url)
    install_runner(runner, paths)

    checks = run_checks(paths, Config(), custom, profile, desktop=_session())
    assert _check(checks, "runners.store").status == "ok"

    (paths.runners / "soda" / "11.0-10" / "bin" / "wine").write_bytes(b"tampered")

    checks = run_checks(paths, Config(), custom, profile, desktop=_session())
    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert "mismatch" in store.detail


def test_app_runner_missing_path_fails(
    paths: Paths, catalog: Catalog, make_profile: Callable[..., Profile], tmp_path: Path
) -> None:
    profile = make_profile(runner=f"path:{tmp_path / 'missing-runner'}")

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "fail"
    assert "not found" in store.detail


def test_app_runner_existing_path_is_ok(
    paths: Paths, catalog: Catalog, make_profile: Callable[..., Profile], tmp_path: Path
) -> None:
    imported = tmp_path / "imported-runner"
    imported.mkdir()
    profile = make_profile(runner=f"path:{imported}")

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    store = _check(checks, "runners.store")
    assert store.status == "ok"
    assert "imported path" in store.detail


# --- prefix and WebView2 -----------------------------------------------------


def test_healthy_prefix_is_ok(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    _make_prefix(paths)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    assert _check(checks, "prefix").status == "ok"
    assert _check(checks, "webview2").status == "ok"
    assert "156.0.4314.42" in _check(checks, "webview2").detail


def test_webview2_reads_a_wine_system_reg_with_timestamped_sections(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    """Wine's system.reg headers carry a timestamp; doctor must still see ``pv``."""
    guid = WEBVIEW2_CLIENT_GUID
    system_reg = (
        "WINE REGISTRY Version 2\n"
        ";; All keys relative to \\\\Machine\n"
        "\n"
        f"[Software\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\{guid}] 1761619200\n"
        "#time=1dc4c0a1b2c3d4e\n"
        '"pv"="156.0.4314.42"\n'
        "\n"
        f"[Software\\\\WOW6432Node\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\{guid}]"
        " 1761619200\n"
        '"pv"="156.0.4314.42"\n'
    )
    _make_prefix(paths, system_reg=system_reg)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    check = _check(checks, "webview2")
    assert check.status == "ok", check.detail
    assert "156.0.4314.42" in check.detail


def test_missing_prefix_is_a_warning(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())
    assert _check(checks, "prefix").status == "warn"
    assert _check(checks, "webview2").status == "warn"


def test_prefix_without_system_reg_fails(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    prefix = paths.prefix_for(profile.id)
    prefix.mkdir(parents=True)
    (prefix / "drive_c").mkdir()

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    assert _check(checks, "prefix").status == "fail"
    assert "system.reg" in _check(checks, "prefix").detail


def test_prefix_with_z_drive_is_a_warning(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    prefix = _make_prefix(paths)
    (prefix / "dosdevices").mkdir()
    (prefix / "dosdevices" / "z:").symlink_to("/")

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    assert _check(checks, "prefix").status == "warn"
    assert "Z:" in _check(checks, "prefix").detail


def test_webview2_missing_from_registry_is_a_warning(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    _make_prefix(paths, system_reg="Windows Registry Editor Version 5.00\n")

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    assert _check(checks, "webview2").status == "warn"


def test_webview2_check_shows_the_recorded_version(
    paths: Paths,
    catalog: Catalog,
    make_profile: Callable[..., Profile],
) -> None:
    profile = make_profile(
        webview2=WebView2Runtime(version="156.0.4314.42", source="latest")
    )
    _make_prefix(paths)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    check = _check(checks, "webview2")
    assert check.status == "ok"
    assert "recorded 156.0.4314.42" in check.detail


def test_webview2_recorded_version_mismatch_is_a_warning(
    paths: Paths,
    catalog: Catalog,
    make_profile: Callable[..., Profile],
) -> None:
    profile = make_profile(webview2=WebView2Runtime(version="1.0.0"))
    _make_prefix(paths)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    check = _check(checks, "webview2")
    assert check.status == "warn"
    assert "profile recorded 1.0.0" in check.detail


def test_webview2_pin_without_release_metadata_is_a_warning(
    paths: Paths, catalog: Catalog
) -> None:
    payload = SimpleNamespace(
        version="100.0.1",
        released="",
        url="https://example.invalid/runtime.exe",
        sha256="0" * 64,
    )
    fake_catalog = SimpleNamespace(
        runners=(),
        webview2=payload,
        latest_webview2_url="https://example.invalid/latest",
    )

    checks = run_checks(paths, Config(), fake_catalog, desktop=_session())  # type: ignore[arg-type]

    pin = _check(checks, "webview2.pin")
    assert pin.status == "warn"
    assert "no release date" in pin.detail


def test_webview2_pin_age_comes_from_the_catalog(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "winbridge.core.doctor._utcnow",
        lambda: datetime(2027, 3, 1, tzinfo=UTC),
    )

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    pin = _check(checks, "webview2.pin")
    assert pin.status == "warn"
    assert catalog.webview2.version in pin.detail
    assert "released 2026-10-23" in pin.detail


def test_webview2_pin_is_ok_when_the_catalog_date_is_fresh(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "winbridge.core.doctor._utcnow",
        lambda: datetime(2026, 10, 28, tzinfo=UTC),
    )

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    pin = _check(checks, "webview2.pin")
    assert pin.status == "ok"
    assert "released 2026-10-23" in pin.detail


def test_webview2_pin_warns_when_older_than_ninety_days(
    paths: Paths, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = SimpleNamespace(
        version="100.0.1",
        released="2026-01-01",
        url="https://example.invalid/runtime.exe",
        sha256="0" * 64,
    )
    fake_catalog = SimpleNamespace(
        runners=(),
        webview2=payload,
        latest_webview2_url="https://example.invalid/latest",
    )
    monkeypatch.setattr(
        "winbridge.core.doctor._utcnow",
        lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )

    checks = run_checks(paths, Config(), fake_catalog, desktop=_session())  # type: ignore[arg-type]

    pin = _check(checks, "webview2.pin")
    assert pin.status == "warn"
    assert "100.0.1" in pin.detail


# --- launcher and scheme registration ---------------------------------------


def test_missing_launcher_entry_is_a_warning(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())
    assert _check(checks, "launcher.entry").status == "warn"
    assert _check(checks, "launcher.scheme").status == "warn"


def test_launcher_and_scheme_registration_are_ok(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    _write_launcher(paths, profile)

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    assert _check(checks, "launcher.entry").status == "ok"
    assert _check(checks, "launcher.scheme").status == "ok"


def test_scheme_default_mismatch_is_a_warning(
    paths: Paths, catalog: Catalog, profile: Profile
) -> None:
    _write_launcher(paths, profile)
    (paths.config_home / "mimeapps.list").write_text(
        "[Default Applications]\n"
        f"x-scheme-handler/{profile.schemes[0]}=other-handler.desktop\n",
        encoding="utf-8",
    )

    checks = run_checks(paths, Config(), catalog, profile, desktop=_session())

    scheme = _check(checks, "launcher.scheme")
    assert scheme.status == "warn"
    assert profile.schemes[0] in scheme.detail


# --- locks -------------------------------------------------------------------


def test_released_lock_file_is_not_treated_as_stale(
    paths: Paths, catalog: Catalog
) -> None:
    paths.locks.mkdir(parents=True)
    lock = paths.locks / "app-example.lock"
    with file_lock(lock):
        pass

    checks = run_checks(paths, Config(), catalog, desktop=_session())

    assert _check(checks, "locks").status == "ok"


def test_held_lock_is_reported(paths: Paths, catalog: Catalog) -> None:
    paths.locks.mkdir(parents=True)
    lock = paths.locks / "app-example.lock"

    with file_lock(lock):
        checks = run_checks(paths, Config(), catalog, desktop=_session())

    locks = _check(checks, "locks")
    assert locks.status == "warn"
    assert lock.name in locks.detail


# --- mutability --------------------------------------------------------------


def test_checks_do_not_create_state(paths: Paths, catalog: Catalog) -> None:
    run_checks(paths, Config(), catalog, desktop=_session())

    assert not paths.bundles.exists()
    assert not paths.profiles.exists()
    assert not paths.prefixes.exists()
    assert not paths.locks.exists()
    assert not paths.logs.exists()


def test_checks_do_not_change_existing_state(paths: Paths, catalog: Catalog) -> None:
    paths.logs.mkdir(parents=True)
    log = paths.logs / "existing.log"
    log.write_text("keep me\n", encoding="utf-8")
    before = {
        path.relative_to(paths.root): path.read_bytes()
        for path in paths.root.rglob("*")
        if path.is_file()
    }

    run_checks(paths, Config(), catalog, desktop=_session())

    after = {
        path.relative_to(paths.root): path.read_bytes()
        for path in paths.root.rglob("*")
        if path.is_file()
    }
    assert after == before


# --- bundle ------------------------------------------------------------------


def test_bundle_is_private_and_named_under_state(paths: Paths) -> None:
    bundle = write_bundle(paths, [CheckResult("x", "ok", "fine")])

    assert bundle.name.startswith("winbridge-")
    assert bundle.name.endswith(".tar.gz")
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert paths.bundles in bundle.parents


def test_bundle_writes_a_manifest(paths: Paths) -> None:
    bundle = write_bundle(paths, [CheckResult("disk", "warn", "only 1 GiB free")])

    with tarfile.open(bundle) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))

    assert manifest["winbridge"] == __version__
    assert manifest["checks"] == [
        {"name": "disk", "status": "warn", "detail": "only 1 GiB free"}
    ]
    assert manifest["files"] == ["manifest.json"]


def test_bundle_includes_redacted_logs(paths: Paths) -> None:
    paths.logs.mkdir(parents=True)
    (paths.logs / "20260101T000000Z-doctor.log").write_text(
        "[doctor] callback https://example.com/cb?code=SECRET\n"
        "Authorization: Bearer TOPSECRET\n"
        f"state {Path.home()}/.local/share/winbridge\n"
        r"app C:\users\someone\AppData\Local\App" "\n",
        encoding="utf-8",
    )

    bundle = write_bundle(
        paths, [], identity=BundleIdentity(app_id="redaction-test")
    )

    with tarfile.open(bundle) as tar:
        assert "logs/20260101T000000Z-doctor.log" in tar.getnames()
        text = "\n".join(
            member.name + "\n" + tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
    assert "SECRET" not in text
    assert "TOPSECRET" not in text
    assert str(Path.home()) not in text
    assert "someone" not in text
    assert "[redacted]" in text


def test_bundle_redacts_state_paths(paths: Paths) -> None:
    checks = [CheckResult("x", "ok", f"state at {paths.root / 'private'}")]

    bundle = write_bundle(paths, checks)

    with tarfile.open(bundle) as tar:
        manifest = tar.extractfile("manifest.json").read().decode("utf-8")
    assert str(paths.root) not in manifest
    assert "$XDG_DATA_HOME" in manifest


def test_bundle_anonymizes_app_identity_and_vendor_paths(paths: Paths) -> None:
    paths.logs.mkdir(parents=True)
    (paths.logs / "20260101T000000Z-setup.log").write_text(
        "app example-app (Example App) version 1.2.3\n"
        r"exe C:\Program Files\Vendor Name\Example.exe" "\n",
        encoding="utf-8",
    )
    identity = BundleIdentity(
        app_id="example-app", app_name="Example App", app_version="1.2.3"
    )

    bundle = write_bundle(paths, [], identity=identity)

    with tarfile.open(bundle) as tar:
        names = "\n".join(tar.getnames())
        text = "\n".join(
            tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
    combined = names + "\n" + text
    assert "example-app" not in combined
    assert "Example App" not in combined
    assert "1.2.3" not in combined
    assert "Vendor Name" not in combined
    assert "$APP_ID" in combined
    assert "$APP_NAME" in combined
    assert "$APP_VERSION" in combined


def test_bundle_includes_installer_and_evidence_logs(paths: Paths) -> None:
    evidence = paths.evidence / APP_ID / "1.2.3" / "20260101T000000Z"
    evidence.mkdir(parents=True)
    (evidence / "installer.log").write_text("silent installer output\n", encoding="utf-8")
    (evidence / "winbridge.log").write_text("evidence log\n", encoding="utf-8")

    bundle = write_bundle(
        paths,
        [],
        identity=BundleIdentity(app_id=APP_ID, app_version="1.2.3"),
    )

    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        text = "\n".join(
            tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
    assert any(name.endswith("installer.log") for name in names)
    assert "silent installer output" in text
    assert "evidence log" in text
    assert APP_ID not in "\n".join(names)


def test_bundle_without_an_app_omits_all_logs_and_explains_why(paths: Paths) -> None:
    paths.logs.mkdir(parents=True)
    (paths.logs / "winbridge.log").write_text(
        "[cli] setup app vendor-secret-app\n", encoding="utf-8"
    )
    (paths.logs / "20260101T000000Z-setup.log").write_text(
        "[setup] app id: vendor-secret-app\n", encoding="utf-8"
    )
    evidence = paths.evidence / "vendor-secret-app" / "9.9.9" / "run"
    evidence.mkdir(parents=True)
    (evidence / "winbridge.log").write_text(
        "[setup] app id: vendor-secret-app\n", encoding="utf-8"
    )
    (evidence / "installer.log").write_text(
        "vendor installer output\n", encoding="utf-8"
    )
    checks = [CheckResult("disk", "ok", "space available")]

    bundle = write_bundle(paths, checks)

    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        text = "\n".join(
            tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
    combined = "\n".join(names) + "\n" + text
    assert "vendor-secret-app" not in combined
    assert "vendor installer output" not in combined
    assert not any(name.startswith(("logs/", "evidence/")) for name in names)
    assert names == ["manifest.json"]
    assert manifest["files"] == ["manifest.json"]
    assert manifest["checks"] == [
        {"name": "disk", "status": "ok", "detail": "space available"}
    ]
    assert "app-scoped bundles include logs" in manifest["log_note"].lower()


def test_bundle_creates_a_nested_destination(tmp_path: Path, paths: Paths) -> None:
    bundle = write_bundle(
        paths, [], dest=tmp_path / "nested" / "issue.tar.gz"
    )
    assert bundle == tmp_path / "nested" / "issue.tar.gz"
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600


def test_bundles_do_not_overwrite_each_other(paths: Paths) -> None:
    first = write_bundle(paths, [])
    second = write_bundle(paths, [])

    assert first != second
    assert first.exists()
    assert second.exists()


def test_bundle_members_are_relative(paths: Paths) -> None:
    paths.logs.mkdir(parents=True)
    (paths.logs / "run.log").write_text("hello\n", encoding="utf-8")

    bundle = write_bundle(
        paths, [], identity=BundleIdentity(app_id="relative-test")
    )

    with tarfile.open(bundle) as tar:
        for member in tar.getmembers():
            assert not member.name.startswith("/")
            assert ".." not in Path(member.name).parts


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def cli(
    tmp_path: Path,
    paths: Paths,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Any]:
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr("winbridge.cli._catalog", lambda ctx: catalog)

    def invoke(*args: str) -> Any:
        return CliRunner().invoke(app, list(args))

    return invoke


def test_cli_doctor_prints_every_check(cli: Callable[..., Any]) -> None:
    result = cli("doctor")

    assert result.exit_code == 0
    assert "[doctor]" in result.output
    assert "desktop.screenshot" in result.output
    assert "summary" in result.output


def test_cli_doctor_accepts_an_app(
    cli: Callable[..., Any],
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    tmp_path: Path,
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id=APP_ID))
    _installed_runner(paths)

    result = cli("doctor", APP_ID)

    assert result.exit_code == 0
    assert "prefix" in result.output


def test_cli_doctor_app_writes_its_evidence_log(
    cli: Callable[..., Any],
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    tmp_path: Path,
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id=APP_ID))

    cli("doctor", APP_ID)

    logs = list(paths.evidence.glob(f"{APP_ID}/*/*/winbridge.log"))
    assert len(logs) == 1
    assert APP_ID in logs[0].read_text(encoding="utf-8")


def test_cli_doctor_missing_profile_fails(cli: Callable[..., Any]) -> None:
    result = cli("doctor", "missing-app")

    assert result.exit_code == 1
    assert "profile" in result.output
    assert "no profile" in result.output


def test_cli_doctor_corrupt_profile_fails(
    cli: Callable[..., Any], paths: Paths
) -> None:
    paths.profiles.mkdir(parents=True)
    (paths.profiles / f"{APP_ID}.toml").write_text("schema = [", encoding="utf-8")

    result = cli("doctor", APP_ID)

    assert result.exit_code == 1
    assert "profile" in result.output


def test_cli_doctor_unreadable_config_is_a_failed_check(
    cli: Callable[..., Any], paths: Paths
) -> None:
    paths.config.mkdir(parents=True)

    result = cli("doctor")

    assert result.exit_code == 1
    assert "config" in result.output


def test_cli_doctor_failing_check_exits_one(
    cli: Callable[..., Any],
    write_profile: Callable[[Path, Profile], Path],
    paths: Paths,
    tmp_path: Path,
    make_profile: Callable[..., Profile],
) -> None:
    profile = make_profile(id=APP_ID)
    write_profile(tmp_path, profile)
    paths.prefix_for(APP_ID).mkdir(parents=True)

    result = cli("doctor", APP_ID)

    assert result.exit_code == 1
    assert "fail" in result.output


def test_cli_doctor_bundle_writes_under_state(
    cli: Callable[..., Any], paths: Paths
) -> None:
    result = cli("doctor", "--bundle")

    assert result.exit_code == 0
    bundles = list(paths.bundles.glob("winbridge-*.tar.gz"))
    assert len(bundles) == 1
    assert str(bundles[0]) in result.output
    assert stat.S_IMODE(bundles[0].stat().st_mode) == 0o600
    with tarfile.open(bundles[0]) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
    assert names == ["manifest.json"]
    assert manifest["files"] == ["manifest.json"]
    assert "app-scoped bundles include logs" in manifest["log_note"].lower()


def test_cli_doctor_bundle_without_app_excludes_evidence_and_logs(
    cli: Callable[..., Any], paths: Paths
) -> None:
    paths.logs.mkdir(parents=True)
    (paths.logs.parent / "winbridge.log").write_text(
        "[cli] setup app vendor-secret-app\n", encoding="utf-8"
    )
    (paths.logs / "20260101T000000Z-setup.log").write_text(
        "[setup] app id: vendor-secret-app\n", encoding="utf-8"
    )
    evidence = paths.evidence / "vendor-secret-app" / "9.9.9" / "run"
    evidence.mkdir(parents=True)
    (evidence / "winbridge.log").write_text(
        "[setup] app id: vendor-secret-app\n", encoding="utf-8"
    )

    cli("doctor", "--bundle")

    bundles = list(paths.bundles.glob("winbridge-*.tar.gz"))
    assert len(bundles) == 1
    with tarfile.open(bundles[0]) as tar:
        names = tar.getnames()
        text = "\n".join(
            tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
    combined = "\n".join(names) + "\n" + text
    assert "vendor-secret-app" not in combined
    assert names == ["manifest.json"]
    assert "app-scoped bundles include logs" in manifest["log_note"].lower()


def test_cli_doctor_bundle_redacts_the_app_identity(
    cli: Callable[..., Any],
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
    tmp_path: Path,
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id=APP_ID))

    cli("doctor", APP_ID, "--bundle")

    bundles = list(paths.bundles.glob("winbridge-*.tar.gz"))
    assert len(bundles) == 1
    with tarfile.open(bundles[0]) as tar:
        names = "\n".join(tar.getnames())
        text = "\n".join(
            tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
        )
    combined = names + "\n" + text
    assert APP_ID not in combined
    assert "$APP_ID" in combined
