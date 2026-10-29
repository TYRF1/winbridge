"""WebView2 detection and Evergreen installation, without Wine or a network."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from winbridge.backends.base import BackendTimeout
from winbridge.core import webview2
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.runner import Catalog
from winbridge.core.webview2 import (
    WEBVIEW2_CLIENT_GUID,
    apply_win7_override,
    install,
    installed_version,
    kill_edge_update,
)

CLIENT_KEY = rf"HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
WOW6432_KEY = (
    rf"HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
OVERRIDE_KEY = r"HKCU\Software\Wine\AppDefaults\msedgewebview2.exe"


def mark_installed_on_override(
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "156.0.1",
) -> None:
    """Emulate the runtime appearing once the win7 override is written."""
    original = fake_backend.wine

    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        completed = original(prefix, *args, timeout_s=timeout_s)
        if len(args) > 2 and args[:2] == ("reg", "add") and args[2] == OVERRIDE_KEY:
            fake_backend.reg_values.setdefault(WOW6432_KEY, {})["pv"] = version
        return completed

    monkeypatch.setattr(fake_backend, "wine", wine)


def write_payload(tmp_path: Path) -> Path:
    payload = tmp_path / "webview2.exe"
    payload.write_bytes(b"x")
    return payload


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A writable prefix skeleton; staging needs ``drive_c/windows/temp``."""
    target = tmp_path / "prefix"
    target.mkdir()
    (target / "drive_c" / "windows" / "temp").mkdir(parents=True)
    return target


# --- detection ---------------------------------------------------------------


def test_detects_version_from_32bit_view(fake_backend: Any) -> None:
    fake_backend.reg_values = {WOW6432_KEY: {"pv": "156.0.1"}}

    assert installed_version(fake_backend, Path("/prefix")) == "156.0.1"


def test_detects_version_from_64bit_view(fake_backend: Any) -> None:
    fake_backend.reg_values = {CLIENT_KEY: {"pv": "156.0.2"}}

    assert installed_version(fake_backend, Path("/prefix")) == "156.0.2"


def test_detection_checks_both_registry_views(fake_backend: Any) -> None:
    fake_backend.reg_values = {CLIENT_KEY: {"pv": "156.0.2"}}

    installed_version(fake_backend, Path("/prefix"))

    queries = [call for call in fake_backend.wine_calls if call.startswith("reg query")]
    assert any(CLIENT_KEY in query for query in queries)
    assert any(WOW6432_KEY in query for query in queries)


def test_64bit_view_takes_precedence(fake_backend: Any) -> None:
    fake_backend.reg_values = {
        CLIENT_KEY: {"pv": "156.0.2"},
        WOW6432_KEY: {"pv": "156.0.1"},
    }

    assert installed_version(fake_backend, Path("/prefix")) == "156.0.2"


def test_missing_runtime_returns_none(fake_backend: Any) -> None:
    assert installed_version(fake_backend, Path("/prefix")) is None


def test_empty_pv_falls_through_to_the_other_view(fake_backend: Any) -> None:
    fake_backend.reg_values = {
        CLIENT_KEY: {"pv": ""},
        WOW6432_KEY: {"pv": "156.0.1"},
    }

    assert installed_version(fake_backend, Path("/prefix")) == "156.0.1"


def test_detection_logs_the_version(
    fake_backend: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fake_backend.reg_values = {WOW6432_KEY: {"pv": "156.0.1"}}

    with caplog.at_level("INFO", logger="winbridge"):
        installed_version(fake_backend, Path("/prefix"))

    assert "156.0.1" in caplog.text


# --- installation ------------------------------------------------------------


def test_install_runs_silent_and_applies_win7_override(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    mark_installed_on_override(fake_backend, monkeypatch)

    version = install(
        fake_backend, prefix, latest=False, payload=payload, catalog=catalog
    )

    assert version == "156.0.1"
    assert "MicrosoftEdgeUpdate.exe" in fake_backend.killed
    assert any("/silent" in call and "/install" in call for call in fake_backend.wine_calls)
    assert fake_backend.reg_writes[-1] == ("Version", OVERRIDE_KEY, "win7")

    calls = fake_backend.wine_calls
    installer_index = next(
        index for index, call in enumerate(calls) if call.endswith("/silent /install")
    )
    kill_index = next(index for index, call in enumerate(calls) if call.startswith("taskkill"))
    override_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("reg add") and OVERRIDE_KEY in call
    )
    assert installer_index < kill_index < override_index
    assert fake_backend.wine_timeouts[installer_index] == 600.0


def test_staged_installer_is_inside_the_prefix_and_removed_after_success(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "webview2.exe"
    payload.write_bytes(b"installer-bytes")
    temp_dir = prefix / "drive_c" / "windows" / "temp"
    seen: list[tuple[str, bytes]] = []
    mark_installed_on_override(fake_backend, monkeypatch)
    original = fake_backend.wine

    def wine(
        current: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0].startswith("C:\\windows\\temp\\winbridge-webview2-"):
            staged = next(temp_dir.glob("winbridge-webview2-*.exe"))
            seen.append((args[0], staged.read_bytes()))
        return original(current, *args, timeout_s=timeout_s)

    monkeypatch.setattr(fake_backend, "wine", wine)

    install(fake_backend, prefix, latest=False, payload=payload, catalog=catalog)

    assert len(seen) == 1
    guest, content = seen[0]
    assert guest.startswith("C:\\windows\\temp\\winbridge-webview2-")
    assert guest.endswith(".exe")
    assert content == b"installer-bytes"
    assert not list(temp_dir.glob("winbridge-webview2-*.exe"))


def test_staged_installer_is_kept_after_an_installer_timeout(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    temp_dir = prefix / "drive_c" / "windows" / "temp"
    original = fake_backend.wine

    def wine(
        current: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0].startswith("C:\\windows\\temp\\winbridge-webview2-"):
            raise BackendTimeout("installer exceeded its 600s timeout")
        return original(current, *args, timeout_s=timeout_s)

    monkeypatch.setattr(fake_backend, "wine", wine)

    with pytest.raises(BackendTimeout):
        install(fake_backend, prefix, latest=False, payload=payload, catalog=catalog)

    staged = list(temp_dir.glob("winbridge-webview2-*.exe"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"x"
    assert "taskkill /F /IM MicrosoftEdgeUpdate.exe" in fake_backend.wine_calls


def test_timeout_but_installed_runtime_is_a_success(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EdgeUpdate can finish after the installer process stops responding."""
    payload = write_payload(tmp_path)
    versions = iter([None, "156.0.4314.42"])
    monkeypatch.setattr(webview2, "installed_version", lambda *_args: next(versions))
    original = fake_backend.wine

    def wine(
        current: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0].startswith("C:\\windows\\temp\\winbridge-webview2-"):
            raise BackendTimeout("installer exceeded its 600s timeout")
        return original(current, *args, timeout_s=timeout_s)

    monkeypatch.setattr(fake_backend, "wine", wine)

    version = install(fake_backend, prefix, latest=False, payload=payload, catalog=catalog)

    assert version == "156.0.4314.42"
    assert "taskkill /F /IM MicrosoftEdgeUpdate.exe" in fake_backend.wine_calls
    assert fake_backend.reg_writes[-1] == ("Version", OVERRIDE_KEY, "win7")
    assert not list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-*"))


def test_timeout_query_failure_keeps_staged_and_raises_the_timeout(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    calls = {"count": 0}

    def installed(_backend: Any, _prefix: Path) -> str | None:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        raise RuntimeError("registry query failed")

    monkeypatch.setattr(webview2, "installed_version", installed)
    original = fake_backend.wine

    def wine(
        current: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0].startswith("C:\\windows\\temp\\winbridge-webview2-"):
            raise BackendTimeout("installer exceeded its 600s timeout")
        return original(current, *args, timeout_s=timeout_s)

    monkeypatch.setattr(fake_backend, "wine", wine)

    with pytest.raises(BackendTimeout, match="installer exceeded"):
        install(fake_backend, prefix, latest=False, payload=payload, catalog=catalog)

    staged = list((prefix / "drive_c" / "windows" / "temp").glob("winbridge-webview2-*"))
    assert len(staged) == 1
    assert "taskkill /F /IM MicrosoftEdgeUpdate.exe" in fake_backend.wine_calls


def test_staging_refuses_a_symlinked_windows_temp(
    fake_backend: Any,
    tmp_path: Path,
    catalog: Catalog,
) -> None:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (prefix / "drive_c" / "windows" / "temp").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(WinBridgeError, match="symlink"):
        install(
            fake_backend,
            prefix,
            latest=False,
            payload=write_payload(tmp_path),
            catalog=catalog,
        )

    assert list(outside.iterdir()) == []


def test_install_is_idempotent_when_the_runtime_is_present(
    fake_backend: Any,
    catalog: Catalog,
) -> None:
    fake_backend.reg_values = {CLIENT_KEY: {"pv": "156.0.0"}}

    def fail_download(*_args: object) -> Path:
        raise AssertionError("the downloader must not run for an installed runtime")

    version = install(
        fake_backend, Path("/prefix"), latest=True, catalog=catalog, downloader=fail_download
    )

    assert version == "156.0.0"
    assert not any("/silent" in call for call in fake_backend.wine_calls)
    assert fake_backend.killed == []
    assert fake_backend.reg_writes[-1] == ("Version", OVERRIDE_KEY, "win7")


def test_latest_uses_the_bootstrapper_without_a_hash(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None, Path]] = []

    def fake_download(url: str, sha256: str | None, dest: Path) -> Path:
        seen.append((url, sha256, Path(dest)))
        Path(dest).write_bytes(b"x")
        return Path(dest)

    monkeypatch.setattr("winbridge.core.webview2.download_verified", fake_download)
    mark_installed_on_override(fake_backend, monkeypatch)

    version = install(fake_backend, prefix, latest=True, catalog=catalog)

    assert version == "156.0.1"
    assert seen
    assert seen[0][0] == catalog.latest_webview2_url
    assert seen[0][1] is None
    assert "go.microsoft.com" in seen[0][0]
    assert any(
        call.endswith("/silent /install") and "C:\\windows\\temp\\winbridge-webview2-"
        in call
        for call in fake_backend.wine_calls
    )


def test_downloader_argument_is_used_when_injected(
    fake_backend: Any,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_download(url: str, _sha256: str | None, dest: Path) -> Path:
        seen.append(url)
        Path(dest).write_bytes(b"x")
        return Path(dest)

    mark_installed_on_override(fake_backend, monkeypatch)

    install(
        fake_backend,
        prefix,
        latest=True,
        catalog=catalog,
        downloader=fake_download,
    )

    assert seen == [catalog.latest_webview2_url]


def test_pinned_uses_the_catalog_payload_and_hash(
    fake_backend: Any,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def fake_download(url: str, sha256: str | None, dest: Path) -> Path:
        seen.append((url, sha256))
        Path(dest).write_bytes(b"x")
        return Path(dest)

    mark_installed_on_override(fake_backend, monkeypatch)

    install(fake_backend, prefix, latest=False, catalog=catalog, downloader=fake_download)

    assert seen == [(catalog.webview2.url, catalog.webview2.sha256)]


def test_install_defaults_to_the_pinned_catalog_payload(
    fake_backend: Any,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def fake_download(url: str, sha256: str | None, dest: Path) -> Path:
        seen.append((url, sha256))
        Path(dest).write_bytes(b"x")
        return Path(dest)

    mark_installed_on_override(fake_backend, monkeypatch)

    install(fake_backend, prefix, catalog=catalog, downloader=fake_download)

    assert seen == [(catalog.webview2.url, catalog.webview2.sha256)]


def _pinned_catalog(catalog: Catalog, content: bytes) -> Catalog:
    return replace(
        catalog,
        webview2=replace(
            catalog.webview2,
            sha256=hashlib.sha256(content).hexdigest(),
        ),
    )


def test_pinned_cache_hit_reuses_the_verified_payload(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"cached pinned runtime"
    cache_dir = tmp_path / "downloads"
    cache_dir.mkdir()
    cached = cache_dir / f"webview2-{catalog.webview2.version}.exe"
    cached.write_bytes(content)
    pinned_catalog = _pinned_catalog(catalog, content)

    def fail_download(*_args: object) -> Path:
        raise AssertionError("a verified cache hit must not download")

    mark_installed_on_override(fake_backend, monkeypatch)

    version = install(
        fake_backend,
        prefix,
        latest=False,
        catalog=pinned_catalog,
        downloader=fail_download,
        cache_dir=cache_dir,
    )

    assert version == "156.0.1"
    assert any(
        call.endswith("/silent /install") and "C:\\windows\\temp\\winbridge-webview2-"
        in call
        for call in fake_backend.wine_calls
    )


def test_pinned_cache_miss_downloads_into_the_cache(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"fresh pinned runtime"
    cache_dir = tmp_path / "downloads"
    cache_dir.mkdir()
    cached = cache_dir / f"webview2-{catalog.webview2.version}.exe"
    pinned_catalog = _pinned_catalog(catalog, content)
    seen: list[tuple[str, str | None, Path]] = []

    def fake_download(url: str, sha256: str | None, dest: Path) -> Path:
        seen.append((url, sha256, Path(dest)))
        Path(dest).write_bytes(content)
        return Path(dest)

    mark_installed_on_override(fake_backend, monkeypatch)

    install(
        fake_backend,
        prefix,
        latest=False,
        catalog=pinned_catalog,
        downloader=fake_download,
        cache_dir=cache_dir,
    )

    assert seen == [(pinned_catalog.webview2.url, pinned_catalog.webview2.sha256, cached)]
    assert cached.read_bytes() == content


def test_pinned_cache_corruption_is_replaced(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"replacement pinned runtime"
    cache_dir = tmp_path / "downloads"
    cache_dir.mkdir()
    cached = cache_dir / f"webview2-{catalog.webview2.version}.exe"
    cached.write_bytes(b"truncated")
    pinned_catalog = _pinned_catalog(catalog, content)
    seen: list[Path] = []

    def fake_download(_url: str, _sha256: str | None, dest: Path) -> Path:
        seen.append(Path(dest))
        Path(dest).write_bytes(content)
        return Path(dest)

    mark_installed_on_override(fake_backend, monkeypatch)

    install(
        fake_backend,
        prefix,
        latest=False,
        catalog=pinned_catalog,
        downloader=fake_download,
        cache_dir=cache_dir,
    )

    assert seen == [cached]
    assert cached.read_bytes() == content


def test_pinned_payload_file_is_used_without_downloading(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)

    def fail_download(*_args: object) -> Path:
        raise AssertionError("a supplied payload must not be downloaded")

    mark_installed_on_override(fake_backend, monkeypatch)

    install(
        fake_backend,
        prefix,
        latest=False,
        payload=payload,
        catalog=catalog,
        downloader=fail_download,
    )

    assert any(
        "C:\\windows\\temp\\winbridge-webview2-" in call
        and call.endswith("/silent /install")
        for call in fake_backend.wine_calls
    )


def test_missing_pinned_payload_is_a_usage_error(
    fake_backend: Any,
    tmp_path: Path,
    catalog: Catalog,
) -> None:
    with pytest.raises(UsageError, match="payload"):
        install(
            fake_backend,
            Path("/prefix"),
            latest=False,
            payload=tmp_path / "missing.exe",
            catalog=catalog,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://go.microsoft.com/fwlink/p/?LinkId=2124703",
        "https://example.invalid/bootstrapper.exe",
        "https://msedge.sf.dl.delivery.mp.microsoft.com/bootstrapper.exe",
    ],
)
def test_latest_refuses_an_unapproved_bootstrapper_url(
    fake_backend: Any,
    catalog: Catalog,
    url: str,
) -> None:
    bad_catalog = replace(catalog, latest_webview2_url=url)
    called: list[str] = []

    def fake_download(target: str, _sha256: str | None, dest: Path) -> Path:
        called.append(target)
        Path(dest).write_bytes(b"x")
        return Path(dest)

    with pytest.raises(WinBridgeError, match="Microsoft|go.microsoft.com"):
        install(
            fake_backend,
            Path("/prefix"),
            latest=True,
            catalog=bad_catalog,
            downloader=fake_download,
        )

    assert called == []


def test_a_nonzero_installer_exit_is_tolerated_when_the_runtime_appears(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    original = fake_backend.wine

    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        completed = original(prefix, *args)
        if args and args[0].startswith("C:\\windows\\temp\\winbridge-webview2-"):
            return subprocess.CompletedProcess([], 3010, stdout="", stderr="reboot required\n")
        if len(args) > 2 and args[:2] == ("reg", "add") and args[2] == OVERRIDE_KEY:
            fake_backend.reg_values.setdefault(WOW6432_KEY, {})["pv"] = "156.0.1"
        return completed

    monkeypatch.setattr(fake_backend, "wine", wine)

    version = install(
        fake_backend, prefix, latest=False, payload=payload, catalog=catalog
    )

    assert version == "156.0.1"


def test_install_polls_until_the_runtime_appears(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    versions = iter([None, None, "156.0.1"])
    monkeypatch.setattr(webview2, "installed_version", lambda *_args: next(versions))
    sleeps: list[float] = []
    monkeypatch.setattr(webview2.time, "sleep", sleeps.append)

    version = install(
        fake_backend, prefix, latest=False, payload=payload, catalog=catalog
    )

    assert version == "156.0.1"
    assert sleeps == [2.0]


def test_install_times_out_with_a_clear_error(
    fake_backend: Any,
    tmp_path: Path,
    prefix: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = write_payload(tmp_path)
    monkeypatch.setattr(webview2, "installed_version", lambda *_args: None)
    monkeypatch.setattr(webview2, "_INSTALL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(webview2.time, "sleep", lambda _seconds: pytest.fail("slept"))

    with pytest.raises(WinBridgeError, match="WebView2.*still missing"):
        install(fake_backend, prefix, latest=False, payload=payload, catalog=catalog)

    assert fake_backend.reg_writes[-1] == ("Version", OVERRIDE_KEY, "win7")
    assert not list(
        (prefix / "drive_c" / "windows" / "temp").glob("winbridge-webview2-*.exe")
    )


# --- helpers -----------------------------------------------------------------


def test_apply_win7_override_raises_when_the_registry_write_fails(
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake_backend.wine

    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("reg", "add"):
            return subprocess.CompletedProcess([], 5, stdout="", stderr="denied\n")
        return original(prefix, *args)

    monkeypatch.setattr(fake_backend, "wine", wine)

    with pytest.raises(WinBridgeError, match="win7"):
        apply_win7_override(fake_backend, Path("/prefix"))


def test_kill_edge_update_uses_wine_taskkill(fake_backend: Any) -> None:
    kill_edge_update(Path("/prefix"), fake_backend)

    assert "taskkill /F /IM MicrosoftEdgeUpdate.exe" in fake_backend.wine_calls
    assert fake_backend.killed == ["MicrosoftEdgeUpdate.exe"]


def test_kill_edge_update_scopes_taskkill_to_the_prefix(
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Path, tuple[str, ...]]] = []
    original = fake_backend.wine

    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        seen.append((prefix, args))
        return original(prefix, *args)

    monkeypatch.setattr(fake_backend, "wine", wine)

    kill_edge_update(Path("/scoped-prefix"), fake_backend)

    assert seen == [
        (
            Path("/scoped-prefix"),
            ("taskkill", "/F", "/IM", "MicrosoftEdgeUpdate.exe"),
        )
    ]


def test_kill_edge_update_has_a_short_timeout(fake_backend: Any) -> None:
    kill_edge_update(Path("/prefix"), fake_backend)

    assert fake_backend.wine_timeouts[-1] == 30.0


def test_kill_edge_update_requires_a_backend() -> None:
    with pytest.raises(TypeError):
        kill_edge_update(Path("/prefix"))  # type: ignore[call-arg]


def test_kill_edge_update_has_no_host_pid_fallback() -> None:
    assert "_host_edge_update_pids" not in webview2.__dict__
    assert "processes_for_prefix" not in webview2.__dict__


def test_kill_edge_update_tolerates_a_missing_process(
    fake_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake_backend.wine

    def wine(
        prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "taskkill":
            return subprocess.CompletedProcess([], 128, stdout="", stderr="not found\n")
        return original(prefix, *args)

    monkeypatch.setattr(fake_backend, "wine", wine)

    kill_edge_update(Path("/prefix"), fake_backend)
