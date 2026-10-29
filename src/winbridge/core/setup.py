r"""Zero-touch setup pipeline.

One call takes a Windows installer from preflight to a verified, pinned desktop
entry. The order is fixed by the design spec because every later stage depends
on the earlier one: the prefix exists before WebView2, the snapshot brackets the
silent install, and detection reads the post-install filesystem and registry.

Verification is tri-state. ``None`` runs the harness and records ``skipped``
when the session cannot capture a window; ``True`` treats that as an
unsupported environment (exit 3); ``False`` does not launch at all. A failed
check keeps the profile, launcher, evidence, and pin, then raises
:class:`~winbridge.core.errors.VerifyFailed` (exit 4), so re-pinning a runner
never requires reinstalling the app.

Every detected or decided value is printed. Setup runs entirely through the
injected backend and desktop adapter; this module never reads real user state.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from winbridge.backends.base import Backend
from winbridge.core import detect, launcher, omarchy, webview2
from winbridge.core import installer as installer_engine
from winbridge.core.config import Config
from winbridge.core.errors import (
    UnsupportedEnvironment,
    UsageError,
    VerifyFailed,
    WinBridgeError,
)
from winbridge.core.expose import relink_exposed
from winbridge.core.installer import InstallResult
from winbridge.core.locking import file_lock
from winbridge.core.prefix import kill_prefix, strip_z_drive
from winbridge.core.profile import (
    ExposedPath,
    InstallerSpec,
    LastVerified,
    PinSpec,
    Profile,
    ProfileError,
    WebView2Runtime,
)
from winbridge.core.runner import Catalog
from winbridge.core.schemes import is_reserved_scheme, reject_reserved_scheme
from winbridge.core.state import Paths
from winbridge.core.verify import TIMEOUT_REASON, TRANSPARENT_REASON, verify_app
from winbridge.desktops.base import Desktop
from winbridge.util.reg import export_hive
from winbridge.util.slug import slugify

__all__ = ["SetupOptions", "app_id_for", "preflight_setup", "setup_installer"]

_LOGGER = logging.getLogger("winbridge")

#: Installer quiescence: five seconds of no new children, fifteen minutes total.
_INSTALL_TIMEOUT_S = 900.0
_INSTALL_SETTLE_S = 5.0
_INSTALL_POLL_S = 0.5

#: Verify cadence; the CLI passes the configured timeout separately.
_VERIFY_POLL_S = 2.0
_VERIFY_CAPTURE_S = 10.0
#: A cold launch can miss its window or paint a transparent surface. Both are
#: retried once with a fresh relaunch before setup fails.
_VERIFY_RETRY_REASONS = frozenset({TIMEOUT_REASON, TRANSPARENT_REASON})

#: A prefix plus a WebView2 runtime and one app needs room to grow.
_MIN_FREE_BYTES = 512 * 1024 * 1024

_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_GUEST_PATH = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$")
_UNKNOWN_FALLBACKS: tuple[tuple[str, ...], ...] = (("/S",), ("/silent",))
#: Wine runners can report a runner-generated class for every window. Recording
#: one as StartupWMClass would group/pin unrelated apps; fall back to the exe.
_GENERIC_WINDOW_CLASSES = frozenset(
    {"steam_proton", "wine", "wine64", "wine-preloader", "explorer.exe"}
)
#: Tauri's NSIS installer defaults to per-user and writes the scheme under
#: ``HKCU\Software\Classes``. Wine dumps that view separately from HKCR, so both
#: are exported and merged before the before/after diff.
_SCHEME_HIVES = (
    "HKEY_CLASSES_ROOT",
    r"HKEY_CURRENT_USER\Software\Classes",
)
_BACKUP_SUFFIX = ".bak"
_RESULT_PASS = "pass"
_RESULT_FAIL = "fail"
_RESULT_SKIPPED = "skipped"


@dataclass(slots=True)
class SetupOptions:
    """Every ``winbridge setup`` flag, with the spec's defaults."""

    app_id: str | None = None
    runner: str | None = None
    exe: str | None = None
    schemes: list[str] | None = None
    silent_args: list[str] | None = None
    backend: str = "wine"
    webview2: str = "pinned"
    keep_z_drive: bool = False
    manual: bool = False
    verify: bool | None = None
    shell_restart: bool = True
    yes: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        if self.backend == "bottles":
            raise UnsupportedEnvironment(
                "the bottles backend is not available in v0.1; "
                "only 'wine' is supported"
            )
        if self.backend != "wine":
            raise UsageError(f"unknown backend: {self.backend!r}")
        if self.webview2 not in {"latest", "pinned"}:
            raise UsageError(
                f"--webview2 must be 'latest' or 'pinned'; got {self.webview2!r}"
            )
        if self.app_id is not None:
            _check_app_id(self.app_id)
        if self.schemes is not None:
            normalized: list[str] = []
            for scheme in self.schemes:
                _check_scheme(scheme)
                lowered = scheme.casefold()
                if lowered not in normalized:
                    normalized.append(lowered)
            self.schemes = normalized
        if self.runner is not None and not _is_runner_ref(self.runner):
            raise UsageError(f"invalid runner reference: {self.runner!r}")


@dataclass(frozen=True, slots=True)
class _CarryOver:
    """Schema-1 state a ``--force`` re-run must not silently discard."""

    extra: dict[str, Any]
    installer_extra: dict[str, Any]
    pin_extra: dict[str, Any]
    verified_extra: dict[str, Any]
    exposed: list[ExposedPath]
    schemes: list[str]
    pin: PinSpec | None
    webview2_extra: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class _Verification:
    """The verify stage's result plus any error to raise after persistence."""

    result: str | None
    window_class: str | None = None
    first_paint_s: float | None = None
    evidence: Path | None = None
    error: WinBridgeError | None = None


def preflight_setup(
    installer: Path | str, paths: Paths, options: SetupOptions
) -> str:
    """Validate the cheap, no-download preconditions before runner install.

    Setup is zero-touch, but installing a missing catalog runner downloads a
    large archive. A missing installer, unreadable package, low disk, or an
    existing profile without ``--force`` must fail first, while the command log
    alone is the only side effect. The pipeline repeats these checks under its
    per-app lock; this pass exists to order the network work after them.
    """
    installer_path = Path(installer)
    if not installer_path.is_file():
        raise UsageError(f"installer not found: {installer_path}")
    installer_engine.detect_type(installer_path)
    app_id = app_id_for(installer_path, options.app_id)
    _check_app_id(app_id)
    paths.ensure()
    profile_path = paths.profiles / f"{app_id}.toml"
    if profile_path.is_file() and not options.force:
        raise UsageError(
            f"a profile already exists at {profile_path}; "
            "re-run with --force to replace it (the old profile is backed up)"
        )
    _check_disk(paths, report=False)
    return app_id


def setup_installer(
    installer: Path | str,
    paths: Paths,
    config: Config,
    catalog: Catalog,
    backend: Backend,
    desktop: Desktop | None,
    options: SetupOptions,
) -> Profile:
    """Install, verify, and record one app; return its finalized profile."""
    installer_path = Path(installer)
    if not installer_path.is_file():
        raise UsageError(f"installer not found: {installer_path}")

    kind = installer_engine.detect_type(installer_path)
    app_id = app_id_for(installer_path, options.app_id)
    _check_app_id(app_id)
    runner = _runner_reference(config, catalog, options)

    paths.ensure()
    _print("installer", f"{installer_path} ({kind})")
    _print("app id", app_id)
    _print("runner", runner)
    _print("backend", options.backend)

    lock = paths.locks / f"app-{app_id}.lock"
    try:
        with file_lock(lock, blocking=False):
            return _pipeline(
                installer_path=installer_path,
                kind=kind,
                app_id=app_id,
                runner=runner,
                paths=paths,
                config=config,
                catalog=catalog,
                backend=backend,
                desktop=desktop,
                options=options,
            )
    except BlockingIOError as exc:
        raise WinBridgeError(
            f"another operation is already running for {app_id!r}; "
            "wait for it to finish and retry"
        ) from exc


def _pipeline(
    *,
    installer_path: Path,
    kind: str,
    app_id: str,
    runner: str,
    paths: Paths,
    config: Config,
    catalog: Catalog,
    backend: Backend,
    desktop: Desktop | None,
    options: SetupOptions,
) -> Profile:
    profile_path = paths.profiles / f"{app_id}.toml"
    carry = _prepare_profile_slot(profile_path, options)
    _check_disk(paths)

    # A re-run adopts an existing prefix. Stop it first: create_prefix waits on
    # wineserver, which would otherwise block on a live app from the last run.
    existing = paths.prefix_for(app_id)
    if existing.is_symlink():
        raise WinBridgeError(
            f"refusing to use a symlinked prefix: {existing}; remove it and retry"
        )
    prefix_existed = existing.is_dir()
    if prefix_existed:
        kill_prefix(backend, existing)

    prefix = backend.create_prefix(app_id, keep_z=options.keep_z_drive)
    _print("prefix", prefix)
    if options.keep_z_drive:
        _print("z drive", "kept (--keep-z-drive)")
    else:
        removed = strip_z_drive(prefix)
        _print("z drive", "stripped" if removed else "already absent")

    _require_wow64(prefix, runner)

    # Setup owns the prefix while it works; only run keeps a live instance.
    kill_prefix(backend, prefix)
    relink_exposed(
        carry.exposed,
        prefix,
        backend,
        report=lambda message: _print("expose", message),
    )

    runtime = webview2.install(
        backend,
        prefix,
        latest=options.webview2 != "pinned",
        catalog=catalog,
        cache_dir=paths.cache,
    )
    _print("webview2", f"{runtime} ({options.webview2})")

    schemes_before = _scheme_snapshot(backend, prefix)
    silent_args, install_result, before, after = _install_app(
        backend, prefix, installer_path, kind, options
    )
    _print(
        "install",
        f"{silent_args or ['(gui)']} exit {install_result.exit_code} "
        f"in {install_result.duration_s:.1f}s; log {install_result.log_path}",
    )

    new_files = detect.diff_new_files(before, after)
    if install_result.exit_code != 0:
        raise WinBridgeError(
            f"installer exited {install_result.exit_code}; "
            f"log {install_result.log_path}; retry with --manual"
        )
    if not detect.new_executables(before, after):
        raise WinBridgeError(
            "install verification failed: the installer exited 0 but wrote "
            f"no new executable under Program Files or per-user AppData; "
            f"log {install_result.log_path}; run 'winbridge doctor'"
        )
    _print("new files", len(new_files))

    exe = _resolve_exe(prefix, before, after, installer_path, options)
    _print("exe", exe)

    schemes_after = _scheme_snapshot(backend, prefix)
    install_root = str(PureWindowsPath(exe).parent)
    detected_schemes = detect.detect_schemes(
        schemes_before,
        schemes_after,
        install_root,
        on_reserved=lambda scheme: _print(
            "scheme rejected", f"{scheme} (reserved browser/file scheme)"
        ),
    )
    schemes = _merge_schemes(detected_schemes, options.schemes)
    if prefix_existed:
        # The prefix was adopted, so its registration is already present before
        # the second install and the registry diff is empty. Carry the old
        # schemes forward instead of silently dropping them (and leaving the
        # scheme handler entry pointing at a profile that no longer knows it).
        schemes = _merge_schemes(carry.schemes, schemes)
    schemes = _drop_reserved(schemes)
    version = detect.detect_app_version(
        backend, prefix, exe, installer_path.name
    )
    name = detect.detect_display_name(
        backend, prefix, installer_path.name, exe
    )
    _print("schemes", ", ".join(schemes) or "none")
    _print("app version", version)
    _print("display name", name)

    profile = Profile(
        schema=1,
        id=app_id,
        name=name,
        backend=options.backend,
        runner=runner,
        installer=_installer_spec(installer_path, kind, silent_args, carry),
        exe=exe,
        schemes=schemes,
        exposed=list(carry.exposed),
        pin=carry.pin,
        keep_z_drive=options.keep_z_drive,
        webview2=WebView2Runtime(
            version=runtime,
            source=options.webview2,
            installed_at=_utc_now(),
            extra=dict(carry.webview2_extra),
        ),
        created_at=carry.created_at,
        extra=dict(carry.extra),
    )
    profile.dump(profile_path)
    _print("profile", profile_path)

    exec_path = launcher.winbridge_exec_path()
    icon = _extract_icon(app_id, exe, prefix, paths, backend)
    _print("icon", icon or "generic")
    launcher_path = launcher.write_desktop_entry(
        paths,
        profile,
        exec_path=exec_path,
        icon=icon,
    )
    _print("launcher", launcher_path)
    _refresh_scheme_handler(profile, paths, exec_path)

    verification = _verify_stage(
        profile, paths, config, backend, desktop, options
    )
    _print("verify", _verify_summary(verification))
    profile.last_verified = _last_verified(carry, verification, version)

    startup_wm_class = _startup_wm_class(verification.window_class, exe)
    launcher_path = launcher.write_desktop_entry(
        paths,
        profile,
        exec_path=exec_path,
        startup_wm_class=startup_wm_class,
        icon=icon,
    )
    _print("launcher", f"{launcher_path} (StartupWMClass={startup_wm_class or 'none'})")
    profile.pin = _pin_app(
        profile, exe, verification, desktop, options, carry
    )
    profile.dump(profile_path)
    _print("profile", f"{profile_path} (final)")
    _print("result", verification.result or "not verified")

    if verification.error is not None:
        raise verification.error
    return profile


# --- install -----------------------------------------------------------------


def _require_wow64(prefix: Path, runner: str) -> None:
    """Fail before WebView2 when the runner has no 32-bit (WoW64) support.

    A 64-bit-only runner leaves ``syswow64`` empty. WebView2's installer then
    has no usable 32-bit runtime and only times out after the full installer
    budget, so detect the missing marker right after prefix creation. The
    32-bit ``ntdll.dll`` is the cheapest proof that WoW64 exists in this prefix.
    """
    marker = prefix / "drive_c" / "windows" / "syswow64" / "ntdll.dll"
    if marker.is_file():
        _print("wow64", "present")
        return
    _print("wow64", "missing")
    raise WinBridgeError(
        f"runner {runner} has no 32-bit (WoW64) support: {marker} is missing; "
        "WebView2 cannot be installed and the app installer was not run; "
        "use a runner that includes 32-bit support"
    )


def _install_app(
    backend: Backend,
    prefix: Path,
    installer_path: Path,
    kind: str,
    options: SetupOptions,
) -> tuple[list[str], InstallResult, detect.FileSnapshot, detect.FileSnapshot]:
    """Run the silent attempt(s), falling back to the GUI when allowed.

    An unrecognized installer gets ``/S`` then ``/silent``. A zero exit is not
    enough to accept a probe: it must also have written a new or changed
    executable, because the wrong silent flag commonly exits cleanly without
    installing anything while still touching config or logs under AppData. Each
    probe gets a fresh before snapshot, so a failed attempt's files can never
    satisfy a later one. A known installer has one vector; its empty install is
    reported by the install-verification stage with the log path.

    Returns the successful (or last) attempt's before/after snapshots so the
    caller verifies and detects against that attempt only.
    """
    candidates = _silent_candidates(kind, options)
    attempts: list[
        tuple[list[str], InstallResult, detect.FileSnapshot, detect.FileSnapshot]
    ] = []
    for args in candidates:
        attempt_before = detect.snapshot_programs(prefix)
        result = installer_engine.install(
            backend,
            prefix,
            installer_path,
            list(args),
            timeout_s=_INSTALL_TIMEOUT_S,
            settle_s=_INSTALL_SETTLE_S,
            poll_s=_INSTALL_POLL_S,
        )
        after = detect.snapshot_programs(prefix)
        attempts.append((list(args), result, attempt_before, after))
        installed = bool(detect.new_executables(attempt_before, after))
        if result.exit_code == 0 and installed:
            return list(args), result, attempt_before, after
        if result.exit_code == 0 and len(candidates) > 1:
            _print(
                "install attempt",
                f"{list(args)} exited 0 but wrote no new executable; "
                "trying the next flag",
            )
        elif result.exit_code != 0:
            _print(
                "install attempt",
                f"{list(args)} exited {result.exit_code}; log {result.log_path}",
            )

    if options.manual:
        _print("install", "silent attempts failed; starting the GUI installer")
        attempt_before = detect.snapshot_programs(prefix)
        result = installer_engine.install(
            backend,
            prefix,
            installer_path,
            [],
            timeout_s=_INSTALL_TIMEOUT_S,
            settle_s=_INSTALL_SETTLE_S,
            poll_s=_INSTALL_POLL_S,
        )
        if result.exit_code != 0:
            raise WinBridgeError(
                f"manual installer exited {result.exit_code}; log {result.log_path}"
            )
        return [], result, attempt_before, detect.snapshot_programs(prefix)

    last_args, last_result, last_before, last_after = attempts[-1]
    if last_result.exit_code == 0:
        # Every probe exited cleanly but the last wrote no executable. Return
        # it so the shared install-verification error carries the log path.
        return last_args, last_result, last_before, last_after
    raise WinBridgeError(
        f"silent install failed (exit {last_result.exit_code}); "
        f"log {last_result.log_path}; "
        "retry with --manual to run the GUI installer inside the prefix"
    )


def _silent_candidates(kind: str, options: SetupOptions) -> list[list[str]]:
    """Explicit flags win; a known type has exactly one vector."""
    if options.silent_args is not None:
        return [list(options.silent_args)]
    known = installer_engine.silent_args(kind)
    if known:
        return [known]
    return [list(args) for args in _UNKNOWN_FALLBACKS]


# --- detection ---------------------------------------------------------------


def _resolve_exe(
    prefix: Path,
    before: detect.FileSnapshot,
    after: detect.FileSnapshot,
    installer_path: Path,
    options: SetupOptions,
) -> str:
    if options.exe is not None:
        explicit = options.exe.strip()
        if not explicit:
            raise UsageError("--exe must not be empty")
        return explicit

    detected = detect.detect_exe(prefix, before, after, installer_path.name)
    if detected is not None:
        return detected

    candidates = _ranked_exe_guests(prefix, before, after, installer_path.name)
    if candidates and sys.stdin.isatty() and not options.yes:
        for index, guest in enumerate(candidates, start=1):
            _print("exe candidate", f"{index}. {guest}")
        answer = input("Choose the main executable by number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]

    if not candidates:
        raise WinBridgeError(
            "no app executable was found; re-run 'winbridge setup' with --exe"
        )

    detail = ", ".join(candidates)
    raise WinBridgeError(
        "cannot determine the main executable automatically "
        f"(candidates: {detail}); re-run setup with --exe"
    )


def _ranked_exe_guests(
    prefix: Path,
    before: detect.FileSnapshot,
    after: detect.FileSnapshot,
    installer_name: str,
) -> list[str]:
    changed = detect.diff_new_files(before, after)
    guests = [guest for guest in changed if guest.casefold().endswith(".exe")]
    host_to_guest: dict[Path, str] = {}
    hosts: list[Path] = []
    for guest in guests:
        host = _host_for_guest(prefix, guest)
        if host is None or host.is_symlink() or not host.is_file():
            continue
        hosts.append(host)
        host_to_guest[host] = guest
    return [
        host_to_guest[path]
        for path in detect.rank_exe_candidates(
            hosts,
            installer_name,
            shortcut_targets=detect.shortcut_exe_targets(prefix, changed, hosts),
        )
    ]


# --- verify ------------------------------------------------------------------


def _verify_stage(
    profile: Profile,
    paths: Paths,
    config: Config,
    backend: Backend,
    desktop: Desktop | None,
    options: SetupOptions,
) -> _Verification:
    """Run verification, retrying once after a cold-start miss.

    The first launch after install can miss its window or paint a transparent
    surface for the whole timeout while an immediate relaunch renders normally.
    Only the default automatic verify retries; explicit ``--verify``,
    ``--no-verify``, unsupported desktops, and tool failures keep their existing
    single-attempt behavior. ``verify_app`` restarts the prefix itself, so the
    retry cannot stack a second instance.
    """
    if options.verify is False:
        return _Verification(result=None)

    verification, retryable = _verify_once(
        profile,
        paths,
        config,
        backend,
        desktop,
        options,
        frames_label="verify frames",
    )
    if not retryable or options.verify is not None:
        return verification

    timeout = float(config.verify_timeout_s)
    _print(
        "verify retry",
        f"first attempt was not visible after {timeout:g}s; relaunching once",
    )
    if verification.evidence is not None:
        _print("verify retry", f"first attempt evidence: {verification.evidence}")
    else:
        _print(
            "verify retry",
            "first attempt left no frames; its evidence directory stays in the log",
        )
    retry, _ = _verify_once(
        profile,
        paths,
        config,
        backend,
        desktop,
        options,
        frames_label="verify retry frames",
    )
    return retry


def _verify_once(
    profile: Profile,
    paths: Paths,
    config: Config,
    backend: Backend,
    desktop: Desktop | None,
    options: SetupOptions,
    *,
    frames_label: str,
) -> tuple[_Verification, bool]:
    """Run one verify attempt; return its mapping and whether it is retryable."""
    try:
        result = verify_app(
            profile,
            paths,
            backend,
            desktop,
            timeout=float(config.verify_timeout_s),
            poll_s=_VERIFY_POLL_S,
            capture_s=_VERIFY_CAPTURE_S,
        )
    except UnsupportedEnvironment as exc:
        verification = _Verification(result=_RESULT_SKIPPED)
        if options.verify is True:
            verification = _Verification(
                result=_RESULT_SKIPPED, error=exc
            )
        _print("verify", f"skipped: {exc}")
        return verification, False
    except UsageError:
        raise
    except WinBridgeError as exc:
        # A backend or desktop tool failed while proving the render. That is a
        # failed verify, not a setup failure: keep the same exit-4 contract and
        # the already-written profile and launcher. Do not retry a tool failure.
        _print("verify", f"failed: {exc}")
        return (
            _Verification(
                result=_RESULT_FAIL,
                error=VerifyFailed(f"verify failed: {exc}"),
            ),
            False,
        )

    evidence = result.evidence_dir or (
        result.frames[0].parent if result.frames else None
    )
    _print(
        frames_label,
        f"{len(result.frames)} kept"
        + (f" in {evidence}" if evidence is not None else ""),
    )
    if result.passed:
        return (
            _Verification(
                result=_RESULT_PASS,
                window_class=result.window_class,
                first_paint_s=result.first_paint_s,
                evidence=evidence,
            ),
            False,
        )

    detail = f"verify failed: {result.reason}"
    if evidence is not None:
        detail = f"{detail}; evidence: {evidence}"
    return (
        _Verification(
            result=_RESULT_FAIL,
            window_class=result.window_class,
            first_paint_s=result.first_paint_s,
            evidence=evidence,
            error=VerifyFailed(detail),
        ),
        result.reason in _VERIFY_RETRY_REASONS,
    )


def _verify_summary(verification: _Verification) -> str:
    if verification.result is None:
        return "skipped (--no-verify)"
    if verification.result == _RESULT_PASS:
        paint = (
            "not observed"
            if verification.first_paint_s is None
            else f"{verification.first_paint_s:.2f}s"
        )
        return (
            f"PASS (first paint {paint}, "
            f"class {verification.window_class or 'not found'}; cursor not checked)"
        )
    if verification.result == _RESULT_FAIL:
        return f"FAIL (class {verification.window_class or 'not found'})"
    return "skipped (desktop cannot verify)"


# --- launcher and pin --------------------------------------------------------


def _pin_app(
    profile: Profile,
    exe: str,
    verification: _Verification,
    desktop: Desktop | None,
    options: SetupOptions,
    carry: _CarryOver,
) -> PinSpec | None:
    if desktop is None or "pin" not in desktop.capabilities:
        _print("pin", "not available on this desktop")
        return carry.pin

    match = _pin_match(verification.window_class, exe)
    try:
        changed = desktop.pin(launcher.desktop_id(profile.id), match)
    except (OSError, WinBridgeError) as exc:
        # The launcher and app already work; a bar-config or filesystem problem
        # must not discard a verified setup. The profile keeps its previous pin.
        _print("pin", f"failed: {exc}")
        return carry.pin

    if not changed:
        # ``pin`` reports False for both "already pinned" and "no taskbar".
        # Without a write there is nothing for the shell to reload, and a
        # first-time setup must not record a pin that does not exist. A
        # re-run keeps whatever the old profile already recorded.
        _print(
            "pin",
            f"{launcher.desktop_id(profile.id)} ({match}) "
            "already present or unavailable; not changed",
        )
        return carry.pin

    _print("pin", f"{launcher.desktop_id(profile.id)} ({match}) updated")
    omarchy.restart_shell(enabled=options.shell_restart)
    return PinSpec(match=match, extra=dict(carry.pin_extra))


def _startup_wm_class(window_class: str | None, exe: str) -> str | None:
    """Return an app-specific window class, never a runner-generic one.

    The default runner reports ``steam_proton`` for every window. That value is
    useless for launcher grouping and pin focus matching, so fall back to the
    executable's stem. A non-generic verified class wins.
    """
    value = window_class.strip() if window_class is not None else ""
    if value and value.casefold() not in _GENERIC_WINDOW_CLASSES:
        return value
    if not value:
        # Verify was skipped: do not invent a window class for the launcher.
        return None
    stem = PureWindowsPath(exe).stem.strip()
    return stem or value


def _pin_match(window_class: str | None, exe: str) -> str:
    value = (
        _startup_wm_class(window_class, exe)
        or PureWindowsPath(exe).stem.strip()
        or PureWindowsPath(exe).name
        or exe
    )
    return f"^{re.escape(value)}$"


# --- profile helpers ---------------------------------------------------------


def _prepare_profile_slot(profile_path: Path, options: SetupOptions) -> _CarryOver:
    """Refuse a collision, or back up and carry a ``--force`` profile forward."""
    previous: Profile | None = None
    if profile_path.is_file():
        if not options.force:
            raise UsageError(
                f"a profile already exists at {profile_path}; "
                "re-run with --force to replace it (the old profile is backed up)"
            )
        _backup_profile(profile_path)
        try:
            previous = Profile.load(profile_path)
        except ProfileError as exc:
            # --force is also the recovery path for a damaged profile: the
            # original bytes are in the backup, and setup cannot carry fields
            # it cannot parse.
            _LOGGER.warning("[setup] previous profile is unreadable: %s", exc)
            _print("profile", f"previous profile unreadable ({exc}); backup kept")

    return _CarryOver(
        extra=dict(previous.extra) if previous is not None else {},
        installer_extra=(
            dict(previous.installer.extra)
            if previous is not None and previous.installer is not None
            else {}
        ),
        pin_extra=(
            dict(previous.pin.extra)
            if previous is not None and previous.pin is not None
            else {}
        ),
        verified_extra=(
            dict(previous.last_verified.extra)
            if previous is not None and previous.last_verified is not None
            else {}
        ),
        exposed=list(previous.exposed) if previous is not None else [],
        schemes=list(previous.schemes) if previous is not None else [],
        pin=previous.pin if previous is not None else None,
        webview2_extra=(
            dict(previous.webview2.extra)
            if previous is not None and previous.webview2 is not None
            else {}
        ),
        created_at=previous.created_at if previous is not None else _utc_now(),
    )


def _backup_profile(profile_path: Path) -> Path:
    backup = profile_path.with_name(profile_path.name + _BACKUP_SUFFIX)
    shutil.copy2(profile_path, backup)
    _print("profile backup", backup)
    return backup


def _installer_spec(
    installer_path: Path,
    kind: str,
    silent_args: list[str],
    carry: _CarryOver,
) -> InstallerSpec:
    resolved = installer_path.resolve()
    return InstallerSpec(
        path=str(resolved),
        dir=str(resolved.parent),
        glob=_installer_glob(resolved),
        type=kind,
        silent_args=list(silent_args),
        extra=dict(carry.installer_extra),
    )


def _installer_glob(installer_path: Path) -> str:
    """Derive the update scan pattern from the installer's product token.

    ``update`` scans ``installer.dir`` for newer versions of the same product.
    The first alphabetic token in the stem survives version bumps and separator
    changes ("Example App 1.2.3.exe" -> ``*Example*.exe``), while a generic
    "setup.exe" still becomes the spec's canonical ``*setup*.exe``.
    """
    suffix = installer_path.suffix
    tokens = re.findall(r"[A-Za-z0-9]+", installer_path.stem)
    token = next(
        (value for value in tokens if any(character.isalpha() for character in value)),
        tokens[0] if tokens else "",
    )
    if token:
        return f"*{token}*{suffix}"
    return f"*{suffix}" if suffix else "*"


def _last_verified(
    carry: _CarryOver,
    verification: _Verification,
    version: str,
) -> LastVerified | None:
    if verification.result is None:
        return None
    return LastVerified(
        app_version=version,
        result=verification.result,
        first_paint_s=verification.first_paint_s,
        at=_utc_now(),
        extra=dict(carry.verified_extra),
    )


# --- small helpers -----------------------------------------------------------


def _scheme_snapshot(
    backend: Backend, prefix: Path
) -> dict[str, dict[str, str]]:
    r"""Merge the machine and per-user class stores for scheme detection.

    Wine's ``reg export HKEY_CLASSES_ROOT`` does not include
    ``HKCU\Software\Classes``; a per-user Tauri NSIS install therefore has no
    scheme in the HKCR export. Exporting both views keeps currentUser and
    perMachine installers working, and duplicate registrations collapse in
    :func:`winbridge.core.detect.detect_schemes`.
    """
    merged: dict[str, dict[str, str]] = {}
    for hive in _SCHEME_HIVES:
        merged.update(export_hive(backend, prefix, hive))
    return merged


def _merge_schemes(
    detected: list[str], explicit: list[str] | None
) -> list[str]:
    merged = list(detected)
    for scheme in explicit or []:
        if scheme not in merged:
            merged.append(scheme)
    return merged


def _drop_reserved(schemes: list[str]) -> list[str]:
    """Remove legacy/carried reserved schemes and report each one.

    New detections are already filtered, but a profile written by an older
    build (or a hand-edited file) can still carry ``https``. Setup must not keep
    it in the new profile.
    """
    kept: list[str] = []
    for scheme in schemes:
        if is_reserved_scheme(scheme):
            _print("scheme rejected", f"{scheme} (reserved browser/file scheme)")
            continue
        kept.append(scheme)
    return kept


def _refresh_scheme_handler(
    profile: Profile, paths: Paths, exec_path: str
) -> None:
    """Refresh an existing login-bridge handler after scheme detection.

    Setup writes no handler (``login-bridge install`` owns it), but a re-run
    that changes the recorded schemes must not leave the old ``MimeType`` in
    place. A missing handler is normal and stays missing.
    """
    handler = paths.applications / f"{launcher.desktop_id(profile.id)}-handler.desktop"
    if not handler.is_file():
        return
    if not profile.schemes:
        launcher.remove_scheme_handler(paths, profile.id)
        _print("scheme handler", f"{handler} removed (no schemes recorded)")
        return
    try:
        updated = launcher.write_scheme_handler(paths, profile, exec_path=exec_path)
    except (OSError, WinBridgeError) as exc:
        _print("scheme handler", f"warning: cannot refresh {handler}: {exc}")
        return
    _print("scheme handler", f"{updated} (MimeType refreshed)")


def _extract_icon(
    app_id: str,
    exe: str,
    prefix: Path,
    paths: Paths,
    backend: Backend,
) -> Path | None:
    host = _host_for_guest(prefix, exe)
    if host is None or host.is_symlink() or not host.is_file():
        return None
    runner_root: Path = getattr(backend, "runner_root", paths.runners)
    destination = paths.applications / f"{launcher.desktop_id(app_id)}.png"
    return launcher.extract_icon(host, destination, runner_root)


def _host_for_guest(prefix: Path, guest: str) -> Path | None:
    match = _GUEST_PATH.match(guest)
    if match is None:
        return None
    parts = [
        part
        for part in match.group("rest").replace("/", "\\").split("\\")
        if part
    ]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return prefix / f"drive_{match.group('drive').lower()}" / Path(*parts)


def _slug_for_installer(installer_path: Path) -> str:
    try:
        return slugify(PureWindowsPath(installer_path.name).stem)
    except ValueError as exc:
        raise UsageError(
            f"cannot derive an app id from {installer_path.name!r}; pass --id"
        ) from exc


def app_id_for(installer: Path | str, app_id: str | None = None) -> str:
    """Return the explicit id or the slug derived from the installer name.

    The CLI uses this before setup starts so app-scoped logging and the
    pipeline agree on the identity; an invalid id stays a usage error either
    way.
    """
    if app_id is not None:
        _check_app_id(app_id)
        return app_id
    return _slug_for_installer(Path(installer))


def _runner_reference(
    config: Config, catalog: Catalog, options: SetupOptions
) -> str:
    reference = (
        options.runner
        or config.default_runner
        or catalog.default_runner().ref
    )
    if not _is_runner_ref(reference):
        raise UsageError(f"invalid runner reference: {reference!r}")
    return reference


def _is_runner_ref(reference: str) -> bool:
    value = reference.strip()
    if not value or value != reference:
        return False
    if value.startswith("path:"):
        candidate = value[len("path:") :]
        return bool(candidate) and Path(candidate).is_absolute()
    namespace, separator, version = value.partition("@")
    return bool(separator and namespace and version) and not any(
        character.isspace() for character in value
    )


def _check_app_id(app_id: str) -> None:
    if not _APP_ID.fullmatch(app_id):
        raise UsageError(f"invalid app id: {app_id!r}")


def _check_scheme(scheme: str) -> None:
    if not _SCHEME.fullmatch(scheme):
        raise UsageError(f"invalid URL scheme: {scheme!r}")
    reject_reserved_scheme(scheme, context="setup")


def _check_disk(paths: Paths, *, report: bool = True) -> None:
    try:
        free = shutil.disk_usage(paths.prefixes).free
    except OSError as exc:
        _LOGGER.warning("[setup] cannot check free disk space: %s", exc)
        return
    if free < _MIN_FREE_BYTES:
        raise WinBridgeError(
            f"not enough free disk space for a prefix: "
            f"{free // (1024 * 1024)} MiB available, "
            f"{_MIN_FREE_BYTES // (1024 * 1024)} MiB required"
        )
    if report:
        _print("disk", f"{free // (1024 * 1024)} MiB free")


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _print(label: str, value: object) -> None:
    print(f"[setup] {label}: {value}", flush=True)
