"""WebView2 Evergreen runtime detection and installation.

The runtime is installed *before* the app because Tauri's NSIS installers do not
bundle it and a missing runtime renders as a blank window. Detection checks both
the 64-bit and ``WOW6432Node`` registry views: a 32-bit prefix, or a runtime
installed by a 32-bit bootstrapper, only appears in one of them.

The checksum-pinned catalog payload is the default: it is reproducible and has
been proven on the default runner. The latest Evergreen bootstrapper is an
opt-in experimental path. It is always the current build, so it cannot carry a
SHA-256; only the catalog's Microsoft HTTPS URL is accepted, and the download
layer independently re-validates the redirect chain. ``latest=False`` uses the
catalog payload (or a caller-supplied file); ``latest=True`` uses the
bootstrapper instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from winbridge.backends.base import Backend, BackendTimeout
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.runner import DEFAULT_CATALOG_PATH, Catalog
from winbridge.core.staging import discard_staged, stage_installer
from winbridge.util.download import download_verified

__all__ = [
    "WEBVIEW2_CLIENT_GUID",
    "apply_win7_override",
    "install",
    "installed_version",
    "kill_edge_update",
]

_LOGGER = logging.getLogger("winbridge")

#: The EdgeUpdate client id that identifies the Evergreen WebView2 runtime.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

_CLIENT_KEY = rf"HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
_WOW6432NODE_KEY = (
    rf"HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
_REGISTRY_KEYS = (_CLIENT_KEY, _WOW6432NODE_KEY)

_OVERRIDE_KEY = r"HKCU\Software\Wine\AppDefaults\msedgewebview2.exe"
_WIN7 = "win7"
_EDGE_UPDATE_IMAGE = "MicrosoftEdgeUpdate.exe"
_BOOTSTRAPPER_HOST = "go.microsoft.com"
_STAGED_NAME = "winbridge-webview2"

_INSTALL_TIMEOUT_S = 600.0  # 10 minutes
_INSTALL_POLL_S = 2.0
_TASKKILL_TIMEOUT_S = 30.0
_PV_LINE = re.compile(
    r"^\s*pv\s+REG_(?:EXPAND_)?SZ\s+(?P<value>.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

Downloader = Callable[[str, str | None, Path], Path]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_payload_matches(path: Path, sha256: str) -> bool:
    """Return whether ``path`` is a regular file with the pinned digest.

    An unreadable or damaged cache entry is a miss, never a reason to abort:
    the caller re-downloads into the same path atomically.
    """
    if not path.is_file():
        return False
    try:
        return _sha256_file(path).lower() == sha256.lower()
    except OSError as exc:
        _LOGGER.warning("[webview2] cannot verify cached payload %s: %s", path, exc)
        return False


def _parse_pv(output: str) -> str | None:
    """Return the non-empty ``pv`` value from ``reg query`` output."""
    match = _PV_LINE.search(output)
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


def _query_pv(backend: Backend, prefix: Path, key: str) -> str | None:
    completed = backend.wine(prefix, "reg", "query", key, "/v", "pv")
    if completed.returncode != 0:
        return None
    return _parse_pv(completed.stdout or "")


def installed_version(backend: Backend, prefix: Path) -> str | None:
    """Return the installed runtime version, or ``None`` when it is absent.

    Both registry views are queried. The 64-bit view wins when both contain a
    value; an empty or missing ``pv`` falls through to the other view.
    """
    found: list[tuple[str, str]] = []
    for key in _REGISTRY_KEYS:
        version = _query_pv(backend, prefix, key)
        if version is not None:
            found.append((key, version))
    if not found:
        _LOGGER.debug("[webview2] runtime not found in either registry view")
        return None
    key, version = found[0]
    _LOGGER.info("[webview2] detected %s in %s", version, key)
    return version


def apply_win7_override(backend: Backend, prefix: Path) -> None:
    """Declare Windows 7 to ``msedgewebview2.exe`` for this prefix.

    WebView2 >= 150 checks the reported Windows version and refuses to start on
    a default Wine prefix. The per-application override is scoped to the runtime
    binary, so it cannot change how the app itself sees Windows.
    """
    completed = backend.wine(
        prefix,
        "reg",
        "add",
        _OVERRIDE_KEY,
        "/v",
        "Version",
        "/d",
        _WIN7,
        "/f",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise WinBridgeError(
            f"cannot apply the msedgewebview2.exe win7 override in {prefix} "
            f"(reg exited {completed.returncode}){suffix}"
        )
    _LOGGER.info("[webview2] applied the msedgewebview2.exe win7 override")


def kill_edge_update(prefix: Path, backend: Backend) -> None:
    """Best-effort stop for a stuck ``MicrosoftEdgeUpdate.exe``.

    The stop goes through Wine's ``taskkill`` inside ``prefix``; there is no
    host PID scan. A missing process is never an error.
    """
    completed = backend.wine(
        prefix,
        "taskkill",
        "/F",
        "/IM",
        _EDGE_UPDATE_IMAGE,
        timeout_s=_TASKKILL_TIMEOUT_S,
    )
    if completed.returncode != 0:
        _LOGGER.debug(
            "[webview2] taskkill for %s exited %s; it may already be gone",
            _EDGE_UPDATE_IMAGE,
            completed.returncode,
        )
    else:
        _LOGGER.info("[webview2] stopped a stuck %s", _EDGE_UPDATE_IMAGE)


def _stop_edge_update(prefix: Path, backend: Backend) -> None:
    """Stop EdgeUpdate without masking the installer's original failure."""
    try:
        kill_edge_update(prefix, backend)
    except WinBridgeError as exc:
        _LOGGER.warning("[webview2] could not stop %s: %s", _EDGE_UPDATE_IMAGE, exc)


def _check_latest_url(url: str) -> None:
    """Refuse a latest-bootstrapper URL that is not Microsoft's HTTPS fwlink."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise WinBridgeError(f"invalid WebView2 bootstrapper URL: {exc}") from exc
    if parsed.scheme != "https" or parsed.hostname != _BOOTSTRAPPER_HOST:
        raise WinBridgeError(
            "the latest WebView2 bootstrapper must be HTTPS on "
            f"{_BOOTSTRAPPER_HOST}; got {url!r}"
        )


def _wait_for_installed(backend: Backend, prefix: Path) -> str | None:
    """Poll ``pv`` every two seconds for up to ten minutes."""
    deadline = time.monotonic() + max(_INSTALL_TIMEOUT_S, 0.0)
    while True:
        version = installed_version(backend, prefix)
        if version is not None:
            return version
        now = time.monotonic()
        if now >= deadline:
            return None
        if _INSTALL_POLL_S > 0:
            time.sleep(min(_INSTALL_POLL_S, deadline - now))


def install(
    backend: Backend,
    prefix: Path,
    *,
    latest: bool = False,
    payload: Path | None = None,
    catalog: Catalog | None = None,
    downloader: Downloader | None = None,
    cache_dir: Path | str | None = None,
) -> str:
    """Ensure the WebView2 runtime is present in ``prefix`` and return its version.

    An existing runtime is adopted without running an installer. ``latest=False``
    (the default) uses ``payload`` or the checksum-pinned catalog installer;
    ``latest=True`` downloads Microsoft's Evergreen bootstrapper (no digest).
    A pinned install
    with ``cache_dir`` reuses ``webview2-<version>.exe`` after verifying its
    SHA-256, and downloads into that path on a miss. The Evergreen bootstrapper
    is deliberately not cached because it has no digest and moves constantly.
    ``downloader`` exists so tests can avoid the network.
    """
    existing = installed_version(backend, prefix)
    if existing is not None:
        _LOGGER.info("[webview2] runtime already installed: %s", existing)
        apply_win7_override(backend, prefix)
        return existing

    # Resolved here, not as a default argument, so tests can monkeypatch the
    # module attribute and callers can inject an offline downloader.
    download = downloader or download_verified
    cache_root = Path(cache_dir) if cache_dir is not None else None

    with tempfile.TemporaryDirectory(prefix="winbridge-webview2-") as raw_workdir:
        workdir = Path(raw_workdir)
        if latest:
            source = catalog or Catalog.load(DEFAULT_CATALOG_PATH)
            url = source.latest_webview2_url
            _check_latest_url(url)
            _LOGGER.info("[webview2] downloading the latest bootstrapper from %s", url)
            installer = Path(download(url, None, workdir / "webview2-installer.exe"))
            description = "latest Evergreen bootstrapper"
        elif payload is not None:
            installer = Path(payload)
            description = f"pinned payload {installer}"
        else:
            source = catalog or Catalog.load(DEFAULT_CATALOG_PATH)
            pinned = source.webview2
            if cache_root is not None:
                cached = cache_root / f"webview2-{pinned.version}.exe"
                if _cached_payload_matches(cached, pinned.sha256):
                    installer = cached
                    description = f"cached pinned runtime {pinned.version}"
                    _LOGGER.info("[webview2] using cached payload %s", installer)
                else:
                    if cached.exists():
                        _LOGGER.warning(
                            "[webview2] cached payload %s failed SHA-256; re-downloading",
                            cached,
                        )
                    _LOGGER.info(
                        "[webview2] downloading pinned runtime %s from %s",
                        pinned.version,
                        pinned.url,
                    )
                    installer = Path(
                        download(pinned.url, pinned.sha256, cached)
                    )
                    description = f"pinned runtime {pinned.version}"
            else:
                _LOGGER.info(
                    "[webview2] downloading pinned runtime %s from %s",
                    pinned.version,
                    pinned.url,
                )
                installer = Path(
                    download(
                        pinned.url,
                        pinned.sha256,
                        workdir / "webview2-installer.exe",
                    )
                )
                description = f"pinned runtime {pinned.version}"

        if not installer.is_file():
            raise UsageError(f"WebView2 payload not found: {installer}")

        _LOGGER.info("[webview2] installing %s", description)
        staged: Path | None = None
        keep_staged = False
        timed_out_version: str | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            staged, guest = stage_installer(
                prefix, installer, name_prefix=_STAGED_NAME
            )
            try:
                completed = backend.wine(
                    prefix,
                    guest,
                    "/silent",
                    "/install",
                    timeout_s=_INSTALL_TIMEOUT_S,
                )
            except BackendTimeout as timeout_error:
                # EdgeUpdate sometimes leaves the installer waiting after the
                # runtime is already registered. That is a slow success, not a
                # failure; only a still-missing runtime is fatal.
                try:
                    timed_out_version = installed_version(backend, prefix)
                except Exception as query_error:  # noqa: BLE001
                    # The registry probe itself failed. Preserve the original
                    # timeout, keep the staged payload, and still try to stop
                    # the updater so the prefix is not left wedged.
                    keep_staged = True
                    _LOGGER.warning(
                        "[webview2] installer timed out and the runtime query "
                        "failed: %s",
                        query_error,
                    )
                    _stop_edge_update(prefix, backend)
                    raise timeout_error from query_error
                if timed_out_version is None:
                    # A wedged installer may still be holding EdgeUpdate open.
                    # Stop it, but keep the staged payload for diagnosis.
                    keep_staged = True
                    _stop_edge_update(prefix, backend)
                    raise
                _LOGGER.warning(
                    "[webview2] installer timed out but runtime %s is installed",
                    timed_out_version,
                )
            if completed is not None and completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                suffix = f": {detail}" if detail else ""
                _LOGGER.warning(
                    "[webview2] installer exited %s%s; waiting for the runtime anyway",
                    completed.returncode,
                    suffix,
                )
            elif completed is not None:
                _LOGGER.info("[webview2] installer exited 0")

            # Ownership rule: setup owns this prefix, so it may stop a wedged
            # updater. The override must be in place before WebView2 is launched.
            _stop_edge_update(prefix, backend)
            apply_win7_override(backend, prefix)

            if timed_out_version is not None:
                version = timed_out_version
            else:
                waited = _wait_for_installed(backend, prefix)
                if waited is None:
                    exit_code = completed.returncode if completed is not None else "unknown"
                    raise WinBridgeError(
                        f"WebView2 runtime is still missing from {prefix} after "
                        f"{_INSTALL_TIMEOUT_S:g}s "
                        f"(installer exited {exit_code}); "
                        "inspect the installer output and run 'winbridge doctor'"
                    )
                version = waited
        finally:
            if not keep_staged:
                discard_staged(staged)

    _LOGGER.info("[webview2] installed %s", version)
    return version
