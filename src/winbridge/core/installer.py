r"""Installer detection, silent flags, and post-install quiescence.

Silent installers are only half the job: NSIS and Inno launchers frequently
return as soon as they hand work to a child process, so the prefix can still be
changing after ``wine setup.exe`` exits. The engine observes a settle window and
then waits for the prefix to empty before a caller snapshots, verifies, or
removes anything.

MSI packages are not executables, so the flag vector includes the ``msiexec``
launcher and this module inserts the package path after ``/i``. Every installer
(EXE, MSI, Burn) is first copied into ``C:\windows\temp`` inside the prefix,
because ``dosdevices/z:`` is stripped and Wine cannot reliably open a host path
from inside the guest. The copy is invoked by its guest path and removed once
quiescence completes (kept on timeout for diagnosis).

An unrecognized installer has no safe silent flags; section 6.7's ordered ``/S``
then ``/silent`` probing and its GUI fallback belong to the setup pipeline.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from winbridge.backends.base import Backend, BackendTimeout
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.staging import discard_staged, stage_installer
from winbridge.core.state import Paths
from winbridge.util.log import evidence_dir
from winbridge.util.proc import processes_for_prefix

__all__ = [
    "InstallResult",
    "InstallerTimeout",
    "detect_type",
    "install",
    "silent_args",
]

_LOGGER = logging.getLogger("winbridge")
_HEADER_BYTES = 256 * 1024
_LOG_NAME = "installer.log"
_MSI_SUFFIX = ".msi"
_MSIEXEC = "msiexec"
_MSI_INSTALL_FLAG = "/i"

#: Exact flag vectors from the design spec, keyed by detected kind. ``unknown``
#: deliberately has none: guessing a silent flag hides a real failure.
_SILENT_ARGS: dict[str, tuple[str, ...]] = {
    "nsis": ("/S",),
    "inno": ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"),
    "msi": (_MSIEXEC, "/i", "/qn", "/norestart"),
    "burn": ("/quiet", "/norestart"),
}


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of one silent install plus its quiescence wait."""

    exit_code: int
    duration_s: float
    log_path: Path


class InstallerTimeout(WinBridgeError):
    """The installer or its children exceeded the hard timeout."""


def detect_type(path: Path) -> str:
    """Classify an installer as ``nsis``, ``inno``, ``msi``, ``burn``, or ``unknown``.

    The extension decides MSI packages; everything else is sniffed from the
    first 256 KiB, which covers the PE stub and every header marker this project
    supports. A missing or unreadable installer is a usage error, not an unknown
    format: preflight is supposed to catch it earlier.
    """
    target = Path(path)
    if not target.is_file():
        raise UsageError(f"installer not found: {target}")
    if target.suffix.lower() == _MSI_SUFFIX:
        return "msi"
    try:
        with target.open("rb") as handle:
            header = handle.read(_HEADER_BYTES)
    except OSError as exc:
        raise UsageError(f"cannot read installer {target}: {exc}") from exc

    if b"Nullsoft" in header:
        return "nsis"
    if b"Inno Setup" in header:
        return "inno"
    if b"Burn" in header:
        return "burn"
    return "unknown"


def silent_args(kind: str) -> list[str]:
    """Return the exact silent flags for ``kind``; an unknown kind has none."""
    return list(_SILENT_ARGS.get(kind.strip().lower(), ()))


def _backend_paths(backend: Backend) -> Paths:
    """Return the backend's state layout, or the real XDG layout as a fallback."""
    paths = getattr(backend, "paths", None)
    return paths if isinstance(paths, Paths) else Paths.default()


def _resolve_log_path(
    backend: Backend, prefix: Path, log_path: Path | None
) -> Path:
    if log_path is not None:
        return Path(log_path)
    app_id = prefix.name
    if not app_id:
        raise UsageError(f"cannot derive an evidence directory from prefix {prefix}")
    return evidence_dir(_backend_paths(backend), app_id) / _LOG_NAME


def _prepare_log(path: Path) -> None:
    """Create the log before the installer runs, owner-only and appendable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _uses_msiexec(installer: Path, args: list[str]) -> bool:
    """Whether this invocation must run through ``msiexec``."""
    return installer.suffix.lower() == _MSI_SUFFIX or (
        bool(args) and args[0].strip().lower() == _MSIEXEC
    )


def _msiexec_args(args: list[str], package: str) -> list[str]:
    """Insert the package path after ``/i``; the leading launcher is optional."""
    flags = list(args)
    if flags and flags[0].strip().lower() == _MSIEXEC:
        flags = flags[1:]
    if _MSI_INSTALL_FLAG not in flags:
        flags.insert(0, _MSI_INSTALL_FLAG)
    index = flags.index(_MSI_INSTALL_FLAG) + 1
    return [*flags[:index], package, *flags[index:]]


def _wait_for_quiescence(
    prefix: Path,
    *,
    exit_code: int,
    timeout_s: float,
    settle_s: float,
    poll_s: float,
    log_path: Path,
) -> None:
    """Let late installer children surface, then wait until the prefix is empty.

    A launcher can exit before the work it started is visible, so the settle
    window is observed *before* the first probe. After that the prefix must be
    empty; ``timeout_s`` bounds how long a surviving child may take to exit.
    """
    if settle_s <= 0:
        return

    time.sleep(settle_s)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        remaining = processes_for_prefix(prefix)
        if not remaining:
            _LOGGER.info("[install] prefix %s is quiet", prefix)
            return
        now = time.monotonic()
        if now >= deadline:
            pids = ", ".join(str(pid) for pid in sorted(remaining))
            raise InstallerTimeout(
                f"installer in {prefix} did not settle within {timeout_s:g}s "
                f"(exit {exit_code}, still running: {pids}); see {log_path}"
            )
        if poll_s > 0:
            time.sleep(min(poll_s, deadline - now))


def install(
    backend: Backend,
    prefix: Path,
    installer: Path,
    args: list[str],
    *,
    timeout_s: float = 900.0,
    settle_s: float = 5.0,
    poll_s: float = 0.5,
    log_path: Path | None = None,
) -> InstallResult:
    """Run ``installer`` silently and wait for the prefix to stop changing.

    The installer's stdout and stderr are redirected to ``log_path`` (or the
    app's evidence directory) so an installer that exits early still leaves
    usable evidence. Quiescence is measured from the installer's exit; a prefix
    that never stops changing raises :class:`InstallerTimeout` naming the log.

    Every installer is copied into ``C:\\windows\\temp`` inside the prefix and
    invoked by its guest path, then removed once quiescence completes. On a
    timeout the copy is kept: a live installer may still be reading it, and it
    documents the guest path. The original host installer and the log remain
    the primary evidence.
    """
    prefix_path = Path(prefix)
    installer_path = Path(installer)
    log = _resolve_log_path(backend, prefix_path, log_path)
    _prepare_log(log)

    staged, guest = stage_installer(prefix_path, installer_path, name_prefix="winbridge")
    if _uses_msiexec(installer_path, args):
        program, argv = Path(_MSIEXEC), _msiexec_args(args, guest)
    else:
        program, argv = Path(guest), list(args)

    started = time.monotonic()
    keep_staged = False
    try:
        _LOGGER.info(
            "[install] %s %s (prefix=%s, log=%s, timeout=%gs)",
            program,
            argv,
            prefix_path,
            log,
            timeout_s,
        )
        try:
            exit_code = backend.install(
                prefix_path,
                program,
                argv,
                log_path=log,
                timeout_s=timeout_s,
            )
        except BackendTimeout as exc:
            raise InstallerTimeout(
                f"installer in {prefix_path} exceeded {timeout_s:g}s "
                f"and was stopped; see {log}"
            ) from exc
        _LOGGER.info("[install] exit %s; waiting for quiescence", exit_code)

        _wait_for_quiescence(
            prefix_path,
            exit_code=exit_code,
            timeout_s=timeout_s,
            settle_s=settle_s,
            poll_s=poll_s,
            log_path=log,
        )
    except InstallerTimeout:
        keep_staged = staged is not None
        if staged is not None:
            _LOGGER.warning(
                "[install] kept staged package %s for evidence after a timeout", staged
            )
        raise
    finally:
        if not keep_staged:
            discard_staged(staged)

    duration_s = time.monotonic() - started
    _LOGGER.info("[install] finished in %.1fs (exit %s)", duration_s, exit_code)
    return InstallResult(exit_code=exit_code, duration_s=duration_s, log_path=log)
