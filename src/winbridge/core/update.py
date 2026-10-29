r"""One-command update and runner re-pinning.

``update`` scans the directory recorded at setup time for the newest build of
the same installer, stops the prefix, reinstalls in place, and re-detects the
exe and version. It always installs the newest match, including when that match
is the already-recorded file (a stable filename can still point at a new build).
The prefix itself is never recreated: the Wine build that created it is part of
its identity, so update leaves ``profile.runner`` alone.

``pin_runner`` records a new runner reference. Recording alone cannot move an
initialized prefix, so the default warns and leaves every byte of it untouched.
``rebuild=True`` is the only automatic path that discards a prefix: it stops and
removes it, recreates it under the new runner, reinstalls WebView2 and the
recorded app, and re-detects the exe/version. The profile, launcher, pin,
schemes, and exposed folders all survive.

Every detected or decided value is printed. Both paths take the per-app lock so
they cannot race setup, verify, or unbridge, and neither ever mutates the
caller's :class:`~winbridge.core.profile.Profile` instance.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from winbridge.backends.base import Backend
from winbridge.core import detect, webview2
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
from winbridge.core.profile import InstallerSpec, LastVerified, Profile, WebView2Runtime
from winbridge.core.runner import Catalog
from winbridge.core.setup import _installer_glob, _is_runner_ref
from winbridge.core.state import Paths
from winbridge.core.verify import verify_app
from winbridge.desktops.base import Desktop

__all__ = ["find_newest_installer", "pin_runner", "update_app"]

_LOGGER = logging.getLogger("winbridge")

#: Installer quiescence: five seconds of no new children, fifteen minutes total.
_INSTALL_TIMEOUT_S = 900.0
_INSTALL_SETTLE_S = 5.0
_INSTALL_POLL_S = 0.5

#: Verify cadence; update has no config flag, so it uses the documented default.
_VERIFY_TIMEOUT_S = 300.0
_VERIFY_POLL_S = 2.0
_VERIFY_CAPTURE_S = 10.0

#: An unrecognized installer has no safe silent vector; probe the two setup uses.
_UNKNOWN_FALLBACKS: tuple[tuple[str, ...], ...] = (("/S",), ("/silent",))

#: The product version in a filename: dotted digits, not embedded in a longer
#: number. The first match wins because it is the version nearest the product
#: name ("App-1.2.3-x64.exe"), not a build number in parentheses.
_VERSION = re.compile(r"(?<![\d.])(?P<version>\d+(?:\.\d+)+)(?![\d.])")

_RESULT_PASS = "pass"
_RESULT_FAIL = "fail"
_RESULT_SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class _Verification:
    """The verify stage's result plus any error to raise after persistence."""

    result: str | None
    window_class: str | None = None
    first_paint_s: float | None = None
    evidence: Path | None = None
    error: WinBridgeError | None = None


# --- newest installer scan ---------------------------------------------------


def find_newest_installer(directory: Path | str, pattern: str) -> Path | None:
    """Return the newest regular file in ``directory`` matching ``pattern``.

    Ordering is the product version parsed from the filename first, then
    modification time, then the name so the result is deterministic. Files with
    no dotted version sort below every versioned file and fall back to mtime.
    Directories and symlinks are ignored: only a regular file is an installer
    candidate.
    """
    base = Path(directory)
    if not base.is_dir():
        return None
    try:
        candidates = [
            path
            for path in base.glob(pattern)
            if path.is_file() and not path.is_symlink()
        ]
    except (OSError, ValueError, NotImplementedError):
        return None
    if not candidates:
        return None
    return max(candidates, key=_installer_sort_key)


def _installer_sort_key(path: Path) -> tuple[tuple[int, ...], int, str]:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (_filename_version(path.name), mtime, path.name)


def _filename_version(name: str) -> tuple[int, ...]:
    """Parse the first dotted numeric version out of an installer filename."""
    match = _VERSION.search(PureWindowsPath(name).stem)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group("version").split("."))


# --- update ------------------------------------------------------------------


def update_app(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    desktop: Desktop | None = None,
    *,
    installer: Path | str | None = None,
    verify: bool | None = None,
    force: bool = False,
) -> Profile:
    """Reinstall one app in place from the newest matching installer.

    The scan uses ``installer.dir`` and ``installer.glob`` from the profile. An
    explicit ``installer`` overrides the scan and replaces the recorded path,
    directory, glob, type, and silent flags. The newest match is always
    installed: a stable filename can point at a new build, so there is no
    "already recorded" shortcut. By default an install only counts when it
    writes a genuinely new or changed executable; a zero exit without one is a
    failure with the log path.

    ``force`` is for same-build reinstalls. When set, a zero-exit install is
    accepted without executable evidence only if no silent candidate failed and
    the recorded executable still exists in the prefix; a warning records that
    no change was detected. The profile and any detected version are still
    persisted.

    ``verify`` is tri-state. ``None`` runs the harness automatically and records
    ``skipped`` when the session cannot capture a window; ``True`` treats that
    as an unsupported environment; ``False`` does not launch at all. A failed
    check keeps the profile, new version, and evidence, then raises
    :class:`~winbridge.core.errors.VerifyFailed`.
    """
    app_id = _check_profile_id(profile.id)
    paths.ensure()
    with _app_lock(paths, app_id):
        return _update_locked(
            profile,
            paths,
            backend,
            desktop,
            installer=installer,
            verify=verify,
            force=force,
        )


def _update_locked(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    desktop: Desktop | None,
    *,
    installer: Path | str | None,
    verify: bool | None,
    force: bool,
) -> Profile:
    selected = _select_installer(profile, installer)
    installer_path, keep_glob = selected
    kind = installer_engine.detect_type(installer_path)
    prefix = _require_prefix(paths, profile)

    _print("update", "app", f"{profile.name} ({profile.id})")
    _print("update", "installer", f"{installer_path} ({kind})")
    if profile.installer is not None and profile.installer.path != str(installer_path):
        _print("update", "previous installer", profile.installer.path)
    _print("update", "prefix", prefix)
    _LOGGER.info(
        "[update] %s: installing %s into %s (runner %s)",
        profile.id,
        installer_path,
        prefix,
        profile.runner,
    )

    # Update owns the prefix while it works. It never deletes it: the Wine build
    # that created it stays the recorded runner.
    kill_prefix(backend, prefix)
    args, result, before, after = _install(
        "update",
        backend,
        prefix,
        installer_path,
        kind,
        profile.installer,
        force=force,
        recorded_exe=profile.exe or None,
    )
    _print(
        "update",
        "install",
        f"{args or ['(gui)']} exit {result.exit_code} "
        f"in {result.duration_s:.1f}s; log {result.log_path}",
    )

    exe = _detect_exe(
        "update", prefix, before, after, installer_path, profile.exe or None
    )
    version = detect.detect_app_version(backend, prefix, exe, installer_path.name)
    _print("update", "exe", exe)
    _print("update", "app version", version)
    runtime: str | None = None
    query_failed = False
    try:
        runtime = webview2.installed_version(backend, prefix)
    except (OSError, WinBridgeError) as exc:
        query_failed = True
        _print(
            "update",
            "webview2",
            f"metadata query failed: {exc}; keeping the recorded metadata",
        )
    if runtime is None:
        if not query_failed:
            _print(
                "update",
                "webview2",
                "runtime not found in the prefix; keeping the recorded metadata",
            )
    else:
        _print("update", "webview2", runtime)

    updated = _copy_profile(profile)
    if runtime is not None:
        updated.webview2 = WebView2Runtime(
            version=runtime,
            source=profile.webview2.source if profile.webview2 is not None else "detected",
            installed_at=_utc_now(),
            extra=(
                dict(profile.webview2.extra) if profile.webview2 is not None else {}
            ),
        )
    updated.installer = _installer_spec(
        installer_path, kind, args, profile.installer, keep_glob=keep_glob
    )
    updated.exe = exe

    profile_path = _profile_path(paths, profile.id)
    # Persist the new build before verifying: a verify that cannot even launch
    # must not leave the profile pointing at the old installer while the prefix
    # already contains the new one.
    updated.last_verified = None
    updated.dump(profile_path)

    verification = _verify_stage(updated, paths, backend, desktop, verify)
    _print("update", "verify", _verify_summary(verification))
    updated.last_verified = _last_verified(
        profile.last_verified, version, verification
    )
    if updated.last_verified is not None:
        updated.dump(profile_path)
    _print("update", "profile", profile_path)

    if verification.error is not None:
        raise verification.error
    return updated


def _select_installer(
    profile: Profile, installer: Path | str | None
) -> tuple[Path, bool]:
    """Return the installer to run and whether to keep the recorded glob.

    The scan's newest match wins by version, then mtime. The recorded path is
    not compared: a stable filename may have been replaced by a newer build in
    place, and re-installing it is the only way to pick that up.
    """
    if installer is not None:
        explicit = Path(installer)
        if not explicit.is_file():
            raise UsageError(f"installer not found: {explicit}")
        return explicit.resolve(), False

    spec = profile.installer
    if spec is None or not spec.dir or not spec.glob:
        raise UsageError(
            f"profile {profile.id!r} records no installer to update from; "
            "pass --installer"
        )

    candidate = find_newest_installer(spec.dir, spec.glob)
    if candidate is None:
        raise WinBridgeError(
            f"no installer matching {spec.glob!r} in {spec.dir}; pass --installer"
        )
    return candidate.resolve(), True


def _require_prefix(paths: Paths, profile: Profile) -> Path:
    prefix = paths.prefix_for(profile.id)
    if prefix.is_symlink():
        raise WinBridgeError(
            f"refusing to touch a symlinked prefix: {prefix}; "
            "remove it and re-run setup"
        )
    if not prefix.is_dir():
        raise WinBridgeError(
            f"prefix for {profile.id!r} is missing at {prefix}; "
            "run 'winbridge setup' to install it"
        )
    return prefix


# --- install -----------------------------------------------------------------


def _silent_plan(
    previous: InstallerSpec | None, kind: str
) -> list[list[str]]:
    """Choose the silent vectors for one install, most specific first.

    A recorded flag vector is only reused for the same installer type: an MSI
    cannot be installed with NSIS flags. An unknown type has no safe flags, so
    the two setup probes are tried and a failure is reported with the log.
    """
    if previous is not None and previous.type == kind and previous.silent_args:
        return [list(previous.silent_args)]
    known = installer_engine.silent_args(kind)
    if known:
        return [known]
    return [list(args) for args in _UNKNOWN_FALLBACKS]


def _install(
    tag: str,
    backend: Backend,
    prefix: Path,
    installer_path: Path,
    kind: str,
    previous: InstallerSpec | None,
    *,
    force: bool = False,
    recorded_exe: str | None = None,
) -> tuple[list[str], InstallResult, detect.FileSnapshot, detect.FileSnapshot]:
    """Run the installer silently and require executable evidence.

    A zero exit is not success: the wrong silent flag commonly exits cleanly
    without installing anything while still writing config or logs under
    AppData. A probe with no genuinely new or changed executable moves on to
    the next vector; the last one fails with the log path. Each probe gets a
    fresh before snapshot, so a failed attempt's files can never satisfy a
    later one. With ``force``, a zero-exit probe is accepted when the recorded
    executable still exists and no candidate failed, and a warning says that
    no executable change was detected.
    """
    candidates = _silent_plan(previous, kind)
    last_args: list[str] | None = None
    last_result: InstallResult | None = None
    last_before: detect.FileSnapshot | None = None
    last_after: detect.FileSnapshot | None = None
    any_failed = False
    for index, args in enumerate(candidates, start=1):
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
        last_args = list(args)
        last_result = result
        last_before = attempt_before
        last_after = after
        if result.exit_code != 0:
            any_failed = True
        if result.exit_code == 0 and detect.new_executables(attempt_before, after):
            return list(args), result, attempt_before, after
        if result.exit_code == 0 and index < len(candidates):
            _print(
                tag,
                "install attempt",
                f"{list(args)} exited 0 but wrote no new executable; "
                "trying the next flag",
            )
            continue
        if result.exit_code == 0:
            _print(
                tag,
                "install attempt",
                f"{list(args)} exited 0 but wrote no new executable; "
                f"log {result.log_path}",
            )
            continue
        _print(
            tag,
            "install attempt",
            f"{list(args)} exited {result.exit_code}; "
            f"log {result.log_path}",
        )

    if last_result is None:
        raise WinBridgeError(
            f"no silent install strategy for {installer_path}; "
            "re-run 'winbridge setup' with --silent-args"
        )
    if last_result.exit_code == 0:
        if (
            force
            and not any_failed
            and last_args is not None
            and _recorded_exe_exists(prefix, recorded_exe)
        ):
            _print(
                tag,
                "install warning",
                "no executable change detected; --force accepted the recorded "
                f"executable {recorded_exe}; log {last_result.log_path}",
            )
            assert last_before is not None and last_after is not None
            return last_args, last_result, last_before, last_after
        raise WinBridgeError(
            "install verification failed: the installer exited 0 but wrote "
            "no new executable under Program Files or per-user AppData; "
            f"log {last_result.log_path}; run 'winbridge doctor'"
        )
    raise WinBridgeError(
        f"silent install failed (exit {last_result.exit_code}); "
        f"log {last_result.log_path}"
    )


def _recorded_exe_exists(prefix: Path, guest: str | None) -> bool:
    """Return whether a recorded guest executable is a real file in ``prefix``.

    The resolved file must stay inside the prefix and no component from the
    guest path down to the prefix may be a symlink, so an exposed host folder
    cannot satisfy the forced same-build path.
    """
    if not guest:
        return False
    path = PureWindowsPath(guest)
    drive = path.drive.rstrip(":").casefold()
    if len(drive) != 1 or not drive.isalpha() or not path.root:
        return False
    parts = path.parts[1:]
    if not parts or any(part in {".", ".."} for part in parts):
        return False
    host = prefix / f"drive_{drive}" / Path(*parts)
    if host.is_symlink() or not host.is_file():
        return False
    try:
        root = prefix.resolve(strict=True)
        resolved = host.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if resolved == root or not resolved.is_relative_to(root):
        return False
    current = host
    while current != prefix:
        if current.is_symlink():
            return False
        if current.parent == current:
            return False
        current = current.parent
    return True


def _detect_exe(
    tag: str,
    prefix: Path,
    before: detect.FileSnapshot,
    after: detect.FileSnapshot,
    installer_path: Path,
    fallback: str | None,
) -> str:
    """Re-detect the main executable, or keep the recorded one."""
    detected = detect.detect_exe(
        prefix,
        before,
        after,
        installer_path.name,
        preferred_exe=fallback,
    )
    if detected is not None:
        return detected
    if fallback:
        raise WinBridgeError(
            f"the recorded executable {fallback} is missing, excluded, or "
            "outside the prefix (cannot determine the main executable); "
            "re-run 'winbridge setup' with --exe"
        )
    raise WinBridgeError(
        "no app executable was found after the install "
        "(cannot determine the main executable); "
        "re-run 'winbridge setup' with --exe"
    )


def _installer_spec(
    installer_path: Path,
    kind: str,
    silent_args: list[str],
    previous: InstallerSpec | None,
    *,
    keep_glob: bool,
) -> InstallerSpec:
    """Record the installer that was actually used, preserving unknown keys."""
    resolved = installer_path.resolve()
    keep_recorded = (
        keep_glob
        and previous is not None
        and bool(previous.glob)
        and fnmatch.fnmatch(installer_path.name, previous.glob)
    )
    glob = previous.glob if keep_recorded and previous is not None else _installer_glob(resolved)
    return InstallerSpec(
        path=str(resolved),
        dir=str(resolved.parent),
        glob=glob,
        type=kind,
        silent_args=list(silent_args),
        extra=dict(previous.extra) if previous is not None else {},
    )


# --- verify ------------------------------------------------------------------


def _verify_stage(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    desktop: Desktop | None,
    verify: bool | None,
) -> _Verification:
    """Run, skip, or refuse verification without deciding when to persist."""
    if verify is False:
        return _Verification(result=None)

    try:
        result = verify_app(
            profile,
            paths,
            backend,
            desktop,
            timeout=_VERIFY_TIMEOUT_S,
            poll_s=_VERIFY_POLL_S,
            capture_s=_VERIFY_CAPTURE_S,
        )
    except UnsupportedEnvironment as exc:
        _print("update", "verify", f"skipped: {exc}")
        return _Verification(
            result=_RESULT_SKIPPED, error=exc if verify is True else None
        )
    except UsageError:
        raise
    except WinBridgeError as exc:
        # A backend or desktop tool failed while proving the render. That is a
        # failed verify, not a failed update: keep the new version and profile.
        _print("update", "verify", f"failed: {exc}")
        return _Verification(
            result=_RESULT_FAIL,
            error=VerifyFailed(f"verify failed: {exc}"),
        )

    evidence = result.evidence_dir or (
        result.frames[0].parent if result.frames else None
    )
    _print(
        "update",
        "verify frames",
        f"{len(result.frames)} kept"
        + (f" in {evidence}" if evidence is not None else ""),
    )
    if result.passed:
        return _Verification(
            result=_RESULT_PASS,
            window_class=result.window_class,
            first_paint_s=result.first_paint_s,
            evidence=evidence,
        )

    detail = f"verify failed: {result.reason}"
    if evidence is not None:
        detail = f"{detail}; evidence: {evidence}"
    return _Verification(
        result=_RESULT_FAIL,
        window_class=result.window_class,
        first_paint_s=result.first_paint_s,
        evidence=evidence,
        error=VerifyFailed(detail),
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


def _last_verified(
    previous: LastVerified | None,
    version: str,
    verification: _Verification,
) -> LastVerified | None:
    if verification.result is None:
        # The previous record described the old build in the old prefix; it is
        # not evidence about the new one.
        return None
    return LastVerified(
        app_version=version,
        result=verification.result,
        first_paint_s=verification.first_paint_s,
        at=_utc_now(),
        extra=dict(previous.extra) if previous is not None else {},
    )


# --- runner pinning ----------------------------------------------------------


def pin_runner(
    profile: Profile,
    ref: str,
    *,
    rebuild: bool = False,
    backend: Backend | None = None,
    paths: Paths | None = None,
    catalog: Catalog | None = None,
    config: Config | None = None,
    desktop: Desktop | None = None,
    confirm: bool = True,
) -> Profile:
    """Record ``ref`` for one app, or recreate its prefix under that runner.

    Record-only is the default and cannot move an initialized prefix, so it
    warns when one exists. ``rebuild=True`` requires ``confirm=False`` and is
    the only automatic path that discards a prefix; the launcher, pin, schemes,
    and exposed folders are preserved. ``config`` is accepted so the CLI can
    pass its resolved context; the new runner is the explicit ``ref``.
    """
    del config, desktop  # Part of the composition contract, not the decision.
    if not _is_runner_ref(ref):
        raise UsageError(f"invalid runner reference: {ref!r}")

    state = paths if paths is not None else Paths.default()
    app_id = _check_profile_id(profile.id)
    state.ensure()
    with _app_lock(state, app_id):
        if not rebuild:
            return _record_runner(profile, ref, state)
        if confirm:
            raise UsageError(
                f"recreating the prefix for {profile.id!r} under {ref} "
                "requires confirmation; re-run with --yes"
            )
        if backend is None:
            raise UsageError("runner rebuild requires a backend")
        return _rebuild_prefix(
            profile,
            ref,
            backend=backend,
            paths=state,
            catalog=catalog,
        )


def _record_runner(profile: Profile, ref: str, paths: Paths) -> Profile:
    """Update only the profile's runner reference; touch no prefix bytes."""
    if ref == profile.runner:
        _print("pin", "runner", f"{ref} (already recorded; nothing to do)")
        return profile

    updated = _copy_profile(profile)
    updated.runner = ref
    profile_path = _profile_path(paths, profile.id)
    updated.dump(profile_path)
    _LOGGER.info("[pin] recorded runner %s for %s; prefix untouched", ref, profile.id)
    _print("pin", "runner", f"{profile.runner} -> {ref} (record-only)")
    _print("pin", "profile", profile_path)

    prefix = paths.prefix_for(profile.id)
    if prefix.exists() or prefix.is_symlink():
        _print(
            "pin",
            "warning",
            f"prefix {prefix} was initialized by {profile.runner} and stays "
            f"tied to it in record-only mode; re-run with --rebuild to "
            f"recreate it under {ref}",
        )
    else:
        _print(
            "pin",
            "prefix",
            "no prefix yet; the next setup or run uses the recorded runner",
        )
    return updated


def _rebuild_prefix(
    profile: Profile,
    ref: str,
    *,
    backend: Backend,
    paths: Paths,
    catalog: Catalog | None,
) -> Profile:
    """Stop and recreate only the prefix, then reinstall WebView2 and the app."""
    spec = profile.installer
    if spec is None or not spec.path:
        raise WinBridgeError(
            f"cannot rebuild {profile.id!r}: the profile records no installer; "
            "re-run 'winbridge setup'"
        )
    installer_path = Path(spec.path)
    if not installer_path.is_file():
        raise WinBridgeError(
            f"cannot rebuild {profile.id!r}: the recorded installer is missing "
            f"at {installer_path}; re-run 'winbridge setup' with a new installer"
        )
    kind = installer_engine.detect_type(installer_path)

    _print("pin", "app", f"{profile.name} ({profile.id})")
    _print("pin", "runner", f"{profile.runner} -> {ref} (rebuild)")
    _print("pin", "installer", f"{installer_path} ({kind})")

    prefix = paths.prefix_for(profile.id)
    if prefix.is_symlink():
        raise WinBridgeError(
            f"refusing to remove a symlinked prefix: {prefix}; "
            "remove it and re-run setup"
        )
    if prefix.is_dir():
        kill_prefix(backend, prefix)
        _LOGGER.info(
            "[pin] recreating prefix %s under runner %s", prefix, ref
        )
        backend.remove(prefix)
        _print("pin", "prefix", f"{prefix} (removed)")
    else:
        _print("pin", "prefix", f"{prefix} (missing; creating)")

    prefix = backend.create_prefix(profile.id, keep_z=profile.keep_z_drive)
    _print("pin", "prefix", prefix)
    if profile.keep_z_drive:
        _print("pin", "z drive", "kept (recorded --keep-z-drive)")
    else:
        removed = strip_z_drive(prefix)
        _print("pin", "z drive", "stripped" if removed else "already absent")
    runtime = webview2.install(
        backend,
        prefix,
        latest=False,
        catalog=catalog,
        cache_dir=paths.cache,
    )
    _print("pin", "webview2", runtime)

    args, result, before, after = _install(
        "pin", backend, prefix, installer_path, kind, spec
    )
    _print(
        "pin",
        "install",
        f"{args or ['(gui)']} exit {result.exit_code} "
        f"in {result.duration_s:.1f}s; log {result.log_path}",
    )

    exe = _detect_exe(
        "pin", prefix, before, after, installer_path, profile.exe or None
    )
    version = detect.detect_app_version(backend, prefix, exe, installer_path.name)
    _print("pin", "exe", exe)
    _print("pin", "app version", version)

    updated = _copy_profile(profile)
    updated.runner = ref
    updated.installer = _installer_spec(
        installer_path, kind, args, spec, keep_glob=True
    )
    updated.exe = exe
    updated.webview2 = WebView2Runtime(
        version=runtime,
        source="pinned",
        installed_at=_utc_now(),
        extra=dict(profile.webview2.extra) if profile.webview2 is not None else {},
    )
    updated.last_verified = None
    _print("pin", "last verified", "cleared (the prefix was recreated)")

    profile_path = _profile_path(paths, profile.id)
    updated.dump(profile_path)
    _print("pin", "profile", profile_path)
    relink_exposed(
        updated.exposed,
        prefix,
        backend,
        report=lambda message: _print("pin", "expose", message),
    )
    return updated


# --- shared helpers ----------------------------------------------------------


@contextmanager
def _app_lock(paths: Paths, app_id: str) -> Iterator[None]:
    """Serialize prefix-owning operations for one app."""
    lock = paths.locks / f"app-{app_id}.lock"
    try:
        with file_lock(lock, blocking=False):
            yield
    except BlockingIOError as exc:
        raise WinBridgeError(
            f"another operation is already running for {app_id!r}; "
            "wait for it to finish and retry"
        ) from exc


def _check_profile_id(app_id: str) -> str:
    if (
        app_id in {"", ".", ".."}
        or app_id != app_id.strip()
        or any(char in app_id for char in ("/", "\\", "\x00"))
    ):
        raise UsageError(f"invalid app id: {app_id!r}")
    return app_id


def _profile_path(paths: Paths, app_id: str) -> Path:
    return paths.profiles / f"{_check_profile_id(app_id)}.toml"


def _copy_profile(profile: Profile) -> Profile:
    """Return an independent copy so the caller's profile is never mutated."""
    return copy.deepcopy(profile)


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _print(tag: str, label: str, value: object) -> None:
    print(f"[{tag}] {label}: {value}", flush=True)
