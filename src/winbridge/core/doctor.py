"""Environment checks and a redacted diagnostic bundle.

``doctor`` answers one question: why is this machine not running the app? Every
check is side-effect free — no state directory is created, no lock is taken, no
process is started, and no file is written by a check — so it is safe to run
while a setup or an app is already running. (The CLI's own command log and an
explicit ``--bundle`` are the only writers.) A probe that cannot run (missing
tool, unreadable registry, unsupported session) degrades to a warning instead of
aborting the report.

``write_bundle`` is the only writer. It produces an owner-only ``tar.gz`` under
the XDG state directory by default, never the current working directory, and
sanitizes every log line with :func:`~winbridge.util.log.redact_url` before it
goes in. The archive is meant to be attached to an issue; callback tokens,
authorization headers, home paths, and Windows user paths never survive that
step.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from winbridge import __version__
from winbridge.core.config import Config
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.launcher import desktop_id
from winbridge.core.profile import Profile
from winbridge.core.runner import (
    INSTALL_MANIFEST_NAME,
    Catalog,
    Runner,
    runner_wine_path,
)
from winbridge.core.state import Paths
from winbridge.core.webview2 import WEBVIEW2_CLIENT_GUID
from winbridge.desktops import Desktop, detect_desktop
from winbridge.util.log import redact_url
from winbridge.util.reg import parse_reg

__all__ = ["BundleIdentity", "CheckResult", "Status", "run_checks", "write_bundle"]

Status = Literal["ok", "warn", "fail"]

_STATUSES = frozenset({"ok", "warn", "fail"})
_MIN_PYTHON = (3, 11)
_REQUIRED_PACKAGES = (
    ("typer", "typer"),
    ("rich", "rich"),
    ("tomli_w", "tomli-w"),
    ("PIL", "Pillow"),
)
_SCREENSHOT_TOOLS = {"hyprland": "grim", "x11": "import"}
_DISK_MIN_BYTES = 2 * 1024**3
_WEBVIEW2_PIN_MAX_AGE_DAYS = 90
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_MAX_LOG_BYTES = 1_000_000
_MAX_RUN_LOGS = 20
_MAX_EVIDENCE_LOGS = 20
_MAX_INSTALLER_LOGS = 20
_GIB = 1024**3
_WEBVIEW2_KEYS = (
    rf"software\microsoft\edgeupdate\clients\{WEBVIEW2_CLIENT_GUID.lower()}",
    rf"software\wow6432node\microsoft\edgeupdate\clients\{WEBVIEW2_CLIENT_GUID.lower()}",
)

_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_SECRET_PATTERN = re.compile(
    r"\b(access_token|refresh_token|id_token|token|secret|password|authorization|code)"
    r"(\s*[:=]\s*)(?:bearer\s+)?([^\s&,;]+)",
    re.IGNORECASE,
)
_WINDOWS_USER_PATTERN = re.compile(r"([A-Za-z]:\\users\\)[^\\\s\"']+", re.IGNORECASE)
_WINDOWS_PROGRAM_FILES_PATTERN = re.compile(
    r"([A-Za-z]:\\Program Files(?: \(x86\))?\\)[^\r\n\"']*",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"
_NO_APP_LOG_NOTE = (
    "No app selected: logs are omitted because they can contain app identity. "
    "App-scoped bundles include logs; run 'winbridge doctor <app> --bundle'."
)


@dataclass(frozen=True, slots=True)
class BundleIdentity:
    """The app identity a diagnostic bundle must not disclose."""

    app_id: str
    app_name: str = ""
    app_version: str = ""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One diagnostic finding. ``status`` is exactly ``ok``, ``warn``, or ``fail``."""

    name: str
    status: Status
    detail: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("check name must not be empty")
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {', '.join(sorted(_STATUSES))}")


def run_checks(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    app: Profile | None = None,
    *,
    desktop: Desktop | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[CheckResult]:
    """Run every environment check without mutating state.

    ``app`` adds prefix, WebView2, launcher, and scheme checks. ``desktop`` and
    ``which`` are injection seams for tests and embedders; production calls
    detect the session and resolve tools from ``PATH``.
    """
    probe = which if which is not None else shutil.which
    session, session_error = _detect_session(desktop)

    return [
        _guard("python", _check_python),
        _guard("python.deps", _check_dependencies),
        _guard("config", lambda: _check_config(config, catalog)),
        _guard("paths.xdg", lambda: _check_paths(paths)),
        _guard("session.adapter", lambda: _check_session(session, session_error)),
        _guard("desktop.screenshot", lambda: _check_screenshot(session, probe)),
        _guard("runners.store", lambda: _check_runner_store(paths, catalog, config, app)),
        _guard("prefix", lambda: _check_prefix(paths, app)),
        _guard("webview2", lambda: _check_webview2(paths, app)),
        _guard("webview2.pin", lambda: _check_webview2_pin(catalog)),
        _guard("launcher.entry", lambda: _check_launcher_entry(paths, app)),
        _guard("launcher.scheme", lambda: _check_scheme_registration(paths, app)),
        _guard("disk", lambda: _check_disk(paths)),
        _guard("locks", lambda: _check_locks(paths)),
    ]


def write_bundle(
    paths: Paths,
    checks: Iterable[CheckResult],
    *,
    dest: Path | str | None = None,
    identity: BundleIdentity | None = None,
) -> Path:
    """Write a redacted diagnostic archive and return its path.

    The default target is ``paths.bundles/winbridge-<timestamp>.tar.gz``; the
    current working directory is never used. The archive is mode ``0600`` and
    contains ``manifest.json`` plus sanitized copies of the global log, the
    command logs, and the newest evidence and installer logs **when an app
    identity is supplied**. A no-app bundle omits every log because command and
    global logs can themselves contain app identity; its manifest says where
    app-scoped logs are available. When ``identity`` is provided the app id,
    name, and version are anonymized in both the file contents and the tar
    member names, and ``C:\\Program Files\\...`` paths are replaced. No source
    log is modified.
    """
    target = Path(dest) if dest is not None else _default_bundle_path(paths)
    redactions = _redaction_roots(paths)
    if identity is not None:
        redactions = _identity_redactions(identity) + redactions
    results = list(checks)

    log_entries: list[tuple[str, bytes]] = []
    if identity is not None:
        for name, source in _log_sources(
            paths,
            app_id=identity.app_id,
            include_evidence=True,
        ):
            member = _safe_member_name(name)
            if member is None:
                continue
            member = _anonymize_member_name(member, identity)
            log_entries.append(
                (member, _redact_text(_read_limited(source), redactions).encode("utf-8"))
            )

    manifest: dict[str, Any] = {
        "created_at": _now_iso(),
        "winbridge": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "checks": [
            {
                "name": result.name,
                "status": result.status,
                "detail": _redact_text(result.detail, redactions),
            }
            for result in results
        ],
        "files": ["manifest.json", *(name for name, _ in log_entries)],
    }
    if identity is None:
        manifest["log_note"] = _NO_APP_LOG_NOTE
    entries = [
        ("manifest.json", json.dumps(manifest, indent=2).encode("utf-8")),
        *log_entries,
    ]
    _write_archive(target, entries)
    return target


# --- individual checks -------------------------------------------------------


def _check_python() -> CheckResult:
    version = platform.python_version()
    if sys.version_info[:2] < _MIN_PYTHON:
        required = ".".join(str(part) for part in _MIN_PYTHON)
        return CheckResult("python", "fail", f"Python {version}; WinBridge requires >= {required}")
    return CheckResult("python", "ok", f"Python {version}")


def _check_dependencies() -> CheckResult:
    missing: list[str] = []
    for module, label in _REQUIRED_PACKAGES:
        try:
            present = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            missing.append(label)
    if missing:
        return CheckResult(
            "python.deps", "fail", f"missing required package(s): {', '.join(missing)}"
        )
    labels = ", ".join(label for _, label in _REQUIRED_PACKAGES)
    return CheckResult("python.deps", "ok", f"present: {labels}")


def _check_config(config: Config, catalog: Catalog) -> CheckResult:
    problems: list[str] = []
    if config.backend != "wine":
        problems.append(
            f"backend {config.backend!r} is not available in v0.1; only 'wine' runs"
        )
    if (
        isinstance(config.verify_timeout_s, bool)
        or not isinstance(config.verify_timeout_s, int)
        or config.verify_timeout_s <= 0
    ):
        problems.append(f"verify_timeout_s must be positive; got {config.verify_timeout_s!r}")
    if config.default_runner is not None:
        try:
            _check_runner_reference(config.default_runner, catalog)
        except WinBridgeError as exc:
            problems.append(str(exc))

    detail = (
        f"backend {config.backend}; "
        f"default runner {_default_runner_ref(config, catalog)}; "
        f"verify timeout {config.verify_timeout_s}s"
    )
    if problems:
        return CheckResult("config", "warn", f"{'; '.join(problems)} ({detail})")
    return CheckResult("config", "ok", detail)


def _check_runner_reference(reference: str, catalog: Catalog) -> None:
    if reference.startswith("path:"):
        root = Path(reference[len("path:") :])
        if not root.is_absolute():
            raise UsageError(f"runner path must be absolute; got {reference!r}")
        if not root.is_dir():
            raise WinBridgeError(f"runner path not found: {root}")
        return
    catalog.get(reference)


def _check_paths(paths: Paths) -> CheckResult:
    problems: list[str] = []
    for label, directory in (("config", paths.config_home), ("data", paths.data_home)):
        if not directory.is_absolute():
            problems.append(f"{label} home is not absolute: {directory}")
        elif directory.exists() and not directory.is_dir():
            problems.append(f"{label} home is not a directory: {directory}")
        elif not os.access(_existing_ancestor(directory), os.W_OK):
            problems.append(f"{label} home is not writable: {directory}")
    if paths.config.exists() and not paths.config.is_file():
        problems.append(f"config path is not a file: {paths.config}")
    elif paths.config.is_file() and not os.access(paths.config, os.R_OK):
        problems.append(f"config file is not readable: {paths.config}")

    detail = f"config home {paths.config_home}; data home {paths.data_home}"
    if problems:
        return CheckResult("paths.xdg", "warn", f"{'; '.join(problems)} ({detail})")
    return CheckResult("paths.xdg", "ok", detail)


def _check_session(session: Desktop | None, error: str | None) -> CheckResult:
    if error is not None:
        return CheckResult("session.adapter", "warn", f"desktop detection failed: {error}")
    if session is None:
        return CheckResult("session.adapter", "warn", "no desktop adapter detected")
    try:
        capabilities = sorted(session.capabilities)
    except Exception as exc:  # noqa: BLE001 - a broken adapter is a finding, not a crash
        return CheckResult("session.adapter", "warn", f"cannot read adapter capabilities: {exc}")

    detail = f"desktop {session.name}; capabilities: {', '.join(capabilities) or 'none'}"
    if session.name == "unsupported":
        return CheckResult(
            "session.adapter",
            "warn",
            f"{detail}; window discovery and verify are unavailable",
        )
    if "windows" not in capabilities:
        return CheckResult(
            "session.adapter",
            "warn",
            f"{detail}; window discovery is unavailable",
        )
    return CheckResult("session.adapter", "ok", detail)


def _check_screenshot(
    session: Desktop | None, which: Callable[[str], str | None]
) -> CheckResult:
    if session is None:
        return CheckResult("desktop.screenshot", "warn", "no desktop adapter to capture with")
    tool = _SCREENSHOT_TOOLS.get(session.name)
    if tool is None:
        return CheckResult(
            "desktop.screenshot",
            "warn",
            f"no screenshot tool is defined for desktop {session.name!r}; "
            "verify cannot capture frames",
        )
    found = which(tool)
    if found is None:
        return CheckResult(
            "desktop.screenshot",
            "warn",
            f"{tool} not found on PATH; verify cannot capture frames",
        )
    return CheckResult("desktop.screenshot", "ok", f"{tool} at {found}")


def _check_runner_store(
    paths: Paths, catalog: Catalog, config: Config, app: Profile | None = None
) -> CheckResult:
    default_ref = _default_runner_ref(config, catalog)
    app_problem, app_note = _app_runner_status(paths, catalog, app)
    store = paths.runners
    if not store.exists():
        if app_problem is not None:
            return CheckResult("runners.store", "fail", app_problem)
        detail = f"no runner store yet; default runner {default_ref} is not installed"
        if app_note is not None:
            detail = f"{detail}; {app_note}"
        return CheckResult("runners.store", "ok", detail)
    if not store.is_dir():
        detail = f"runner store is not a directory: {store}"
        if app_problem is not None:
            return CheckResult("runners.store", "fail", f"{app_problem}; {detail}")
        return CheckResult("runners.store", "warn", detail)
    try:
        entries = sorted(store.iterdir())
    except OSError as exc:
        return CheckResult("runners.store", "warn", f"cannot read {store}: {exc}")

    catalog_ids = {runner.id for runner in catalog.runners}
    installed: list[str] = []
    problems: list[str] = [app_problem] if app_problem is not None else []
    unverifiable: list[str] = []
    unmanaged: list[str] = []

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_dir() or entry.name not in catalog_ids:
            unmanaged.append(entry.name)
            continue
        try:
            versions = sorted(entry.iterdir())
        except OSError as exc:
            problems.append(f"cannot read {entry}: {exc}")
            continue
        for version_dir in versions:
            if version_dir.name.startswith("."):
                continue
            runner = next(
                (
                    candidate
                    for candidate in catalog.runners
                    if candidate.id == entry.name and candidate.version == version_dir.name
                ),
                None,
            )
            if runner is None:
                unmanaged.append(f"{entry.name}/{version_dir.name}")
                continue
            try:
                wine = runner_wine_path(version_dir, runner)
                if not wine.is_file():
                    problems.append(f"{runner.ref} is missing {runner.wine_relpath}")
                    continue
                digest = _sha256_file(wine)
            except (OSError, WinBridgeError) as exc:
                problems.append(f"{runner.ref}: {exc}")
                continue

            problem, note, unverifiable_note = _verify_install_manifest(
                version_dir, runner, digest
            )
            if problem is not None:
                problems.append(problem)
                continue
            if unverifiable_note is not None:
                unverifiable.append(unverifiable_note)
            else:
                cached = paths.cache / f"{runner.id}-{runner.version}.archive"
                if _is_regular(cached):
                    try:
                        cached_digest = _sha256_file(cached)
                    except OSError as exc:
                        problems.append(f"{runner.ref}: cannot hash cached archive: {exc}")
                        continue
                    if cached_digest.lower() != runner.sha256.lower():
                        problems.append(
                            f"{runner.ref} cached archive sha256 mismatch: "
                            f"catalog {runner.sha256}, got {cached_digest}"
                        )
                        continue
                    note = f"{note} (cached archive verified)"
                if note is not None:
                    installed.append(note)

    if problems:
        detail = "; ".join(problems)
        if unverifiable:
            detail = f"{detail}; not verifiable: {'; '.join(unverifiable)}"
        if unmanaged:
            detail = f"{detail}; unmanaged entries: {', '.join(unmanaged)}"
        return CheckResult("runners.store", "fail", detail)
    if unverifiable:
        detail = f"not verifiable: {'; '.join(unverifiable)}"
        if installed:
            detail = f"{detail}; verified: {'; '.join(installed)}"
        if unmanaged:
            detail = f"{detail}; unmanaged entries: {', '.join(unmanaged)}"
        if app_note is not None:
            detail = f"{detail}; {app_note}"
        return CheckResult("runners.store", "warn", f"{detail}; default runner {default_ref}")
    if unmanaged:
        detail = f"unmanaged store entries: {', '.join(unmanaged)}"
        if installed:
            detail = f"{detail}; installed: {'; '.join(installed)}"
        if app_note is not None:
            detail = f"{detail}; {app_note}"
        return CheckResult("runners.store", "warn", f"{detail}; default runner {default_ref}")
    if installed:
        detail = f"installed: {'; '.join(installed)}"
        if app_note is not None:
            detail = f"{detail}; {app_note}"
        return CheckResult(
            "runners.store", "ok", f"{detail}; default runner {default_ref}"
        )
    detail = f"no catalog runners installed; default runner {default_ref}"
    if app_note is not None:
        detail = f"{detail}; {app_note}"
    return CheckResult("runners.store", "ok", detail)


def _app_runner_status(
    paths: Paths, catalog: Catalog, app: Profile | None
) -> tuple[str | None, str | None]:
    """Return ``(problem, note)`` for the runner recorded in ``app``'s profile."""
    if app is None:
        return None, None
    reference = app.runner.strip()
    if not reference:
        return f"profile {app.id!r} has no runner; re-run setup", None
    if reference.startswith("path:"):
        raw = reference[len("path:") :]
        root = Path(raw)
        if not raw or not root.is_absolute():
            return f"profile runner path must be absolute: {reference!r}", None
        if not root.is_dir():
            return f"profile runner path not found: {root}; re-pin the app", None
        return None, f"app runner {reference} is an imported path"

    try:
        runner = catalog.get(reference)
    except WinBridgeError:
        return (
            f"profile runner {reference} is not in the catalog; "
            f"run 'winbridge runners install {reference}' or re-pin the app",
            None,
        )
    try:
        wine = runner_wine_path(paths.runners / runner.id / runner.version, runner)
    except WinBridgeError as exc:
        return f"profile runner {reference}: {exc}", None
    if not wine.is_file():
        return (
            f"profile runner {reference} is not installed; "
            f"run 'winbridge runners install {reference}'",
            None,
        )
    return None, f"app runner {reference} is installed"


def _verify_install_manifest(
    version_dir: Path, runner: Runner, wine_digest: str
) -> tuple[str | None, str | None, str | None]:
    """Compare the installed wine digest with the install-time manifest.

    Returns ``(problem, note, unverifiable)``. A mismatch is a failure; a
    missing, unreadable, or digest-less manifest is only unverifiable, never a
    false ``ok``.
    """
    manifest_path = version_dir / INSTALL_MANIFEST_NAME
    if not manifest_path.is_file():
        return (
            None,
            None,
            f"{runner.ref}: install manifest is missing; wine hash not verifiable "
            "(reinstall the runner)",
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            None,
            None,
            f"{runner.ref}: install manifest is unreadable ({exc}); wine hash not verifiable",
        )
    if not isinstance(data, dict):
        return (
            None,
            None,
            f"{runner.ref}: install manifest is not a JSON object; wine hash not verifiable",
        )
    manifest: dict[str, Any] = data

    pinned = manifest.get("wine_sha256")
    if not isinstance(pinned, str) or not pinned.strip():
        return (
            None,
            None,
            f"{runner.ref}: install manifest has no wine_sha256; wine hash not verifiable",
        )
    pinned_digest = pinned.strip().lower()
    if pinned_digest != wine_digest.lower():
        return (
            f"{runner.ref}: installed wine sha256 mismatch "
            f"(manifest {pinned_digest}, current {wine_digest}); remove and reinstall the runner",
            None,
            None,
        )

    archive = manifest.get("archive_sha256")
    if not isinstance(archive, str) or not archive.strip():
        return (
            None,
            None,
            f"{runner.ref}: install manifest has no archive_sha256; not fully verifiable",
        )
    if archive.strip().lower() != runner.sha256.lower():
        return (
            f"{runner.ref}: install manifest archive sha256 mismatch "
            f"(manifest {archive.strip()}, catalog {runner.sha256})",
            None,
            None,
        )
    return (
        None,
        f"{runner.ref} (verified wine sha256 {wine_digest[:16]}..., "
        f"archive sha256 {runner.sha256[:16]}...)",
        None,
    )


def _check_prefix(paths: Paths, app: Profile | None) -> CheckResult:
    if app is None:
        return _check_all_prefixes(paths)

    prefix = paths.prefix_for(app.id)
    if prefix.is_symlink():
        return CheckResult(
            "prefix", "fail", f"refusing a symlinked prefix: {prefix}; remove it and re-run setup"
        )
    if not prefix.exists():
        return CheckResult(
            "prefix", "warn", f"prefix missing: {prefix}; re-run setup to recreate it"
        )
    if not prefix.is_dir():
        return CheckResult("prefix", "fail", f"prefix is not a directory: {prefix}")

    problems: list[str] = []
    warnings: list[str] = []
    if not (prefix / "system.reg").is_file():
        problems.append("system.reg is missing (this is not a Wine prefix)")
    if not (prefix / "drive_c").is_dir():
        warnings.append("drive_c is missing")
    z_drive = prefix / "dosdevices" / "z:"
    if z_drive.is_symlink() or z_drive.exists():
        warnings.append("the Z: host-root link is present; re-run setup or remove it")

    if problems:
        detail = "; ".join(problems + warnings)
        return CheckResult("prefix", "fail", f"{prefix}: {detail}")
    if warnings:
        return CheckResult("prefix", "warn", f"{prefix}: {'; '.join(warnings)}")
    return CheckResult("prefix", "ok", f"{prefix} has system.reg and drive_c")


def _check_all_prefixes(paths: Paths) -> CheckResult:
    if not paths.prefixes.is_dir():
        return CheckResult("prefix", "ok", "skipped: no app selected and no prefixes exist")
    try:
        prefixes = sorted(
            entry
            for entry in paths.prefixes.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )
    except OSError as exc:
        return CheckResult("prefix", "warn", f"cannot read {paths.prefixes}: {exc}")
    if not prefixes:
        return CheckResult("prefix", "ok", "skipped: no app selected and no prefixes exist")
    broken = [prefix.name for prefix in prefixes if not (prefix / "system.reg").is_file()]
    if broken:
        return CheckResult(
            "prefix",
            "fail",
            f"prefix(es) without system.reg: {', '.join(broken)}",
        )
    return CheckResult(
        "prefix",
        "ok",
        f"{len(prefixes)} prefix(es) look healthy: {', '.join(prefix.name for prefix in prefixes)}",
    )


def _check_webview2(paths: Paths, app: Profile | None) -> CheckResult:
    if app is None:
        return CheckResult("webview2", "ok", "skipped: no app selected")
    recorded = app.webview2.version if app.webview2 is not None else None
    recorded_note = f"; recorded {recorded}" if recorded else ""
    system_reg = paths.prefix_for(app.id) / "system.reg"
    if not system_reg.is_file():
        return CheckResult(
            "webview2",
            "warn",
            f"no {system_reg} to inspect; WebView2 version is unknown{recorded_note}",
        )
    try:
        text = system_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult("webview2", "warn", f"cannot read {system_reg}: {exc}")
    versions = _webview2_versions(parse_reg(text))
    if not versions:
        return CheckResult(
            "webview2",
            "warn",
            "WebView2 runtime not found in either the 64-bit or WOW6432Node "
            f"registry view{recorded_note}",
        )
    distinct = list(dict.fromkeys(version for _, version in versions))
    detail = "; ".join(f"{version} ({view})" for view, version in versions)
    if len(distinct) > 1:
        return CheckResult(
            "webview2", "warn", f"registry views disagree: {detail}{recorded_note}"
        )
    if recorded is not None and recorded not in distinct:
        return CheckResult(
            "webview2",
            "warn",
            f"registry reports {detail} but the profile recorded {recorded}; "
            "re-run setup to refresh the metadata",
        )
    return CheckResult("webview2", "ok", f"WebView2 runtime {detail}{recorded_note}")


def _webview2_versions(parsed: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    versions: list[tuple[str, str]] = []
    for key, values in parsed.items():
        normalized = key.replace("\\\\", "\\").lower()
        if not any(candidate in normalized for candidate in _WEBVIEW2_KEYS):
            continue
        version = values.get("pv", "").strip()
        if not version:
            continue
        view = "32-bit" if "wow6432node" in normalized else "64-bit"
        versions.append((view, version))
    return versions


def _check_webview2_pin(catalog: Catalog) -> CheckResult:
    payload = catalog.webview2
    version = payload.version
    if not payload.released:
        return CheckResult(
            "webview2.pin",
            "warn",
            f"catalog has no release date for pinned WebView2 {version}; "
            "staleness cannot be checked",
        )
    released_date = _parse_release_date(payload.released)
    if released_date is None:
        return CheckResult(
            "webview2.pin",
            "warn",
            f"cannot parse release metadata {payload.released!r} for pinned "
            f"WebView2 {version}",
        )
    age = (_utcnow().date() - released_date).days
    if age > _WEBVIEW2_PIN_MAX_AGE_DAYS:
        return CheckResult(
            "webview2.pin",
            "warn",
            f"pinned WebView2 {version} was released {released_date.isoformat()} "
            f"({age} days ago); update the catalog payload or opt into "
            "experimental --webview2=latest",
        )
    return CheckResult(
        "webview2.pin",
        "ok",
        f"pinned WebView2 {version} released {released_date.isoformat()} ({age} days ago)",
    )


def _check_launcher_entry(paths: Paths, app: Profile | None) -> CheckResult:
    if app is None:
        return CheckResult("launcher.entry", "ok", "skipped: no app selected")
    try:
        name = desktop_id(app.id)
    except UsageError as exc:
        return CheckResult("launcher.entry", "fail", str(exc))
    target = paths.applications / f"{name}.desktop"
    if not target.is_file():
        return CheckResult(
            "launcher.entry", "warn", f"launcher entry missing: {target}; re-run setup"
        )
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult("launcher.entry", "warn", f"cannot read {target}: {exc}")
    if f"run {app.id}" not in text:
        return CheckResult(
            "launcher.entry", "warn", f"{target} does not launch {app.id}; re-run setup"
        )
    return CheckResult("launcher.entry", "ok", str(target))


def _check_scheme_registration(paths: Paths, app: Profile | None) -> CheckResult:
    if app is None:
        return CheckResult("launcher.scheme", "ok", "skipped: no app selected")
    schemes = list(dict.fromkeys(app.schemes))
    if not schemes:
        return CheckResult("launcher.scheme", "ok", "no URL schemes are recorded")
    try:
        name = desktop_id(app.id)
    except UsageError as exc:
        return CheckResult("launcher.scheme", "fail", str(exc))

    handler_name = f"{name}-handler.desktop"
    handler = paths.applications / handler_name
    if not handler.is_file():
        return CheckResult(
            "launcher.scheme",
            "warn",
            f"scheme handler entry is missing: {handler}; "
            f"run 'winbridge login-bridge install {app.id}'",
        )

    defaults = _read_default_applications(paths.config_home / "mimeapps.list")
    accepted = {handler_name, f"{name}-handler"}
    missing: list[str] = []
    mismatched: list[str] = []
    for scheme in schemes:
        current = defaults.get(f"x-scheme-handler/{scheme.lower()}")
        if current is None:
            missing.append(scheme)
        elif current not in accepted:
            mismatched.append(f"{scheme} -> {current}")

    if missing:
        return CheckResult(
            "launcher.scheme",
            "warn",
            f"no default handler recorded for: {', '.join(missing)}; "
            f"run 'winbridge login-bridge install {app.id}'",
        )
    if mismatched:
        return CheckResult(
            "launcher.scheme",
            "warn",
            f"default handler differs from {handler_name}: {'; '.join(mismatched)}",
        )
    return CheckResult(
        "launcher.scheme",
        "ok",
        "; ".join(f"{scheme} -> {handler_name}" for scheme in schemes),
    )


def _check_disk(paths: Paths) -> CheckResult:
    location = _existing_ancestor(paths.root)
    try:
        usage = shutil.disk_usage(location)
    except OSError as exc:
        return CheckResult("disk", "warn", f"cannot read disk usage at {location}: {exc}")
    free = usage.free
    detail = f"{free / _GIB:.1f} GiB free at {location} (minimum {_DISK_MIN_BYTES / _GIB:.1f} GiB)"
    if free < _DISK_MIN_BYTES:
        return CheckResult("disk", "warn", f"{detail}; setup and updates may fail")
    return CheckResult("disk", "ok", detail)


def _check_locks(paths: Paths) -> CheckResult:
    if not paths.locks.exists():
        return CheckResult("locks", "ok", "no lock directory; nothing is locked")
    if not paths.locks.is_dir():
        return CheckResult("locks", "warn", f"lock path is not a directory: {paths.locks}")
    try:
        locks = sorted(paths.locks.glob("*.lock"))
    except OSError as exc:
        return CheckResult("locks", "warn", f"cannot read {paths.locks}: {exc}")

    held: list[str] = []
    unknown: list[str] = []
    for lock in locks:
        if lock.is_symlink() or not lock.is_file():
            continue
        state = _lock_state(lock)
        if state == "held":
            held.append(lock.name)
        elif state == "unknown":
            unknown.append(lock.name)

    if held:
        return CheckResult(
            "locks",
            "warn",
            f"lock file(s) held by a running process: {', '.join(held)}; "
            "another command may be running, or a process is stuck",
        )
    if unknown:
        return CheckResult(
            "locks", "warn", f"cannot probe lock file(s): {', '.join(unknown)}"
        )
    if locks:
        return CheckResult(
            "locks",
            "ok",
            f"{len(locks)} lock file(s) present, none held (normal after a completed command)",
        )
    return CheckResult("locks", "ok", "no lock files")


# --- probe helpers -----------------------------------------------------------


def _guard(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    """Turn an unexpected probe failure into a fail finding, never a crash."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 - diagnostics must always produce a report
        return CheckResult(name, "fail", f"check raised {type(exc).__name__}: {exc}")


def _detect_session(desktop: Desktop | None) -> tuple[Desktop | None, str | None]:
    if desktop is not None:
        return desktop, None
    try:
        return detect_desktop(), None
    except Exception as exc:  # noqa: BLE001 - detection failure is a finding
        return None, f"{type(exc).__name__}: {exc}"


def _default_runner_ref(config: Config, catalog: Catalog) -> str:
    if config.default_runner:
        return config.default_runner
    try:
        return catalog.default_runner().ref
    except Exception:  # noqa: BLE001 - a broken catalog is reported by its own check
        return "unavailable"


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lock_state(lock: Path) -> str:
    """Return ``held``, ``free``, or ``unknown`` without creating anything."""
    try:
        descriptor = os.open(lock, os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return "free"
    finally:
        os.close(descriptor)


def _read_default_applications(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    defaults: dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != "Default Applications":
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        first = value.split(";")[0].strip()
        if first:
            defaults[key.strip().lower()] = first
    return defaults


def _parse_release_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- bundle helpers ----------------------------------------------------------


def _timestamp() -> str:
    return _utcnow().strftime(_TIMESTAMP_FORMAT)


def _default_bundle_path(paths: Paths) -> Path:
    stamp = _timestamp()
    candidate = paths.bundles / f"winbridge-{stamp}.tar.gz"
    index = 1
    while candidate.exists():
        candidate = paths.bundles / f"winbridge-{stamp}-{index}.tar.gz"
        index += 1
    return candidate


def _log_sources(
    paths: Paths, *, app_id: str | None = None, include_evidence: bool = True
) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    global_log = paths.logs.parent / "winbridge.log"
    if _is_regular(global_log):
        sources.append(("logs/winbridge.log", global_log))

    if paths.logs.is_dir():
        for path in sorted(_newest(paths.logs.glob("*.log"), _MAX_RUN_LOGS), key=str):
            sources.append((f"logs/{path.name}", path))

    if not include_evidence:
        return sources

    evidence_root = paths.evidence
    if (
        app_id
        and app_id not in {".", ".."}
        and not any(char in app_id for char in ("/", "\\", "\x00"))
    ):
        evidence_root = paths.evidence / app_id
    if evidence_root.is_dir():
        for path in sorted(
            _newest(evidence_root.rglob("winbridge.log"), _MAX_EVIDENCE_LOGS), key=str
        ):
            try:
                relative = path.relative_to(paths.evidence)
            except ValueError:
                continue
            sources.append((f"evidence/{relative.as_posix()}", path))
        for path in sorted(
            _newest(evidence_root.rglob("installer.log"), _MAX_INSTALLER_LOGS), key=str
        ):
            try:
                relative = path.relative_to(paths.evidence)
            except ValueError:
                continue
            sources.append((f"evidence/{relative.as_posix()}", path))
    return sources


def _is_regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _newest(candidates: Iterable[Path], limit: int) -> list[Path]:
    regular = [path for path in candidates if _is_regular(path)]
    regular.sort(key=_mtime, reverse=True)
    return regular[:limit]


def _mtime(path: Path) -> float:
    with contextlib.suppress(OSError):
        return path.stat().st_mtime
    return 0.0


def _read_limited(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _MAX_LOG_BYTES:
                handle.seek(size - _MAX_LOG_BYTES)
                data = handle.read()
                prefix = f"[truncated: showing the last {_MAX_LOG_BYTES} bytes]\n"
            else:
                data = handle.read()
                prefix = ""
    except OSError as exc:
        return f"[cannot read {path.name}: {exc}]\n"
    return prefix + data.decode("utf-8", errors="replace")


def _redact_text(text: str, redactions: Iterable[tuple[str, str]] = ()) -> str:
    redacted = _URL_PATTERN.sub(_redact_url_token, text)
    redacted = _SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        redacted,
    )
    redacted = _WINDOWS_USER_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        redacted,
    )
    redacted = _WINDOWS_PROGRAM_FILES_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        redacted,
    )
    for needle, replacement in redactions:
        if needle:
            redacted = redacted.replace(needle, replacement)
    return redacted


def _identity_redactions(identity: BundleIdentity) -> list[tuple[str, str]]:
    """Map app identity tokens to stable placeholders, longest token first."""
    candidates = (
        (identity.app_version, "$APP_VERSION"),
        (identity.app_name, "$APP_NAME"),
        (identity.app_id, "$APP_ID"),
    )
    unique: dict[str, str] = {}
    for needle, replacement in candidates:
        value = needle.strip()
        if value and value not in unique:
            unique[value] = replacement
    return sorted(unique.items(), key=lambda item: len(item[0]), reverse=True)


def _anonymize_member_name(name: str, identity: BundleIdentity) -> str:
    """Replace identity tokens in an archive member name and keep it relative."""
    for needle, replacement in _identity_redactions(identity):
        name = name.replace(needle, replacement)
    return _safe_member_name(name) or "evidence/[redacted].log"


def _redaction_roots(paths: Paths) -> list[tuple[str, str]]:
    """Map every state root and the home directory to a stable placeholder."""
    candidates = (
        (str(Path.home()), "~"),
        (str(paths.root), "$XDG_DATA_HOME/winbridge"),
        (str(paths.config_home), "$XDG_CONFIG_HOME"),
        (str(paths.data_home), "$XDG_DATA_HOME"),
    )
    unique: dict[str, str] = {}
    for needle, replacement in candidates:
        if needle and needle not in unique:
            unique[needle] = replacement
    return sorted(unique.items(), key=lambda item: len(item[0]), reverse=True)


def _redact_url_token(match: re.Match[str]) -> str:
    value = match.group(0)
    trailing = ""
    while value and value[-1] in ").,;:]}\"'":
        trailing = value[-1] + trailing
        value = value[:-1]
    return f"{redact_url(value)}{trailing}"


def _safe_member_name(name: str) -> str | None:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return None
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _write_archive(target: Path, entries: Iterable[tuple[str, bytes]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                for name, payload in entries:
                    _add_member(archive, name, payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _add_member(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = int(time.time())
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(payload))
