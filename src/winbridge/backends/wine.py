"""Runner-bound Wine backend.

The backend never uses a distro Wine: every command resolves to the selected
runner's own ``bin/wine`` (and its sibling ``wineserver``). That is what makes a
prefix reproducible and keeps ``run`` from picking a different Wine than the one
that installed the app.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

from winbridge.backends.base import BackendError, BackendTimeout
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.prefix import strip_z_drive
from winbridge.core.state import Paths
from winbridge.util.log import redact_url, run_log_path

__all__ = ["WineBackend", "wine_env"]

_LOGGER = logging.getLogger("winbridge")
_WINE_RELPATHS = ("bin/wine", "files/bin/wine")
_SYSTEM_REG = "system.reg"
_VALID_ARCHES = frozenset({"win32", "win64"})
#: Every Wine tool call is bounded; a wedged wineserver or registry query must
#: never hang a command forever. Installers pass their own, longer bound.
_COMMAND_TIMEOUT_S = 300.0


def _check_arch(arch: str | None) -> None:
    if arch is not None and arch not in _VALID_ARCHES:
        raise UsageError(f"WINEARCH must be win32 or win64; got {arch!r}")


def wine_env(
    prefix: Path,
    runner_root: Path,
    arch: str | None = None,
) -> dict[str, str]:
    """Build the environment for one Wine invocation.

    ``WAYLAND_DISPLAY`` is removed so Wine uses X11 (its Wayland driver is not
    part of the supported matrix), and an inherited ``WINEARCH`` is removed
    unless the caller supplied one explicitly. The runner's ``bin`` is prepended
    to ``PATH`` so Wine's helper tools resolve to the same build as the loader.
    An inherited ``WINEDEBUG`` is always ignored; ``WINBRIDGE_WINEDEBUG`` is the
    explicit, verbatim trace escape and defaults to ``-all`` when unset or empty.
    """
    _check_arch(arch)
    env = dict(os.environ)
    env["WINEPREFIX"] = str(prefix)
    trace = os.environ.get("WINBRIDGE_WINEDEBUG")
    env["WINEDEBUG"] = trace if trace else "-all"
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("WAYLAND_SOCKET", None)
    if arch is None:
        env.pop("WINEARCH", None)
    else:
        env["WINEARCH"] = arch

    runner_bin = Path(runner_root) / "bin"
    inherited_path = env.get("PATH")
    env["PATH"] = (
        f"{runner_bin}{os.pathsep}{inherited_path}" if inherited_path else str(runner_bin)
    )
    return env


def _private_dir(directory: Path) -> None:
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        directory.chmod(0o700)


@contextmanager
def _private_append_log(path: Path) -> Iterator[BinaryIO]:
    """Open an owner-only append stream for installer or app output."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("ab")
    try:
        os.fchmod(handle.fileno(), 0o600)
        yield handle
    finally:
        handle.close()


def _read_capture(handle: BinaryIO) -> str:
    """Read a command's captured output after it exits."""
    handle.flush()
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace")


def _display_argv(argv: list[str]) -> list[str]:
    """Keep callback payloads out of verbose logs; schemes and hosts stay."""
    return [redact_url(arg) if "://" in arg else arg for arg in argv]


def _kill_group(process: subprocess.Popen[Any]) -> None:
    """Stop a timed-out command and every child it started.

    The backend starts each command in its own session, so the group is exactly
    the Wine process tree for that invocation. Killing only the direct child
    would leave an installer or ``wineboot`` grandchild holding the prefix.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        with suppress(OSError):
            process.kill()


def _check_app_id(app_id: str) -> None:
    if (
        not app_id
        or app_id != app_id.strip()
        or app_id.startswith(".")
        or any(char in app_id for char in ("/", "\\", "\x00"))
    ):
        raise UsageError(f"invalid app id: {app_id!r}")


def _guest_path(prefix: Path, guest: str) -> Path:
    value = guest.strip()
    drive, separator, rest = value.partition(":")
    if not value or "\x00" in value or not separator or drive.upper() != "C":
        raise UsageError(f"guest path must be on the C: drive; got {guest!r}")
    parts = [part for part in rest.replace("\\", "/").split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        raise UsageError(f"invalid guest path: {guest!r}")

    target = prefix / "drive_c"
    for part in parts:
        target = target / part
    if not target.resolve().is_relative_to(prefix.resolve()):
        raise UsageError(f"guest path escapes the prefix: {guest!r}")
    return target


class WineBackend:
    """Execute a Wine-kind runner directly via ``WINEPREFIX``."""

    name = "wine"

    def __init__(
        self,
        runner_root: Path,
        paths: Paths,
        *,
        arch: str | None = None,
    ) -> None:
        _check_arch(arch)
        root = Path(runner_root)
        if not root.is_absolute():
            raise UsageError(f"runner root must be absolute; got {str(runner_root)!r}")
        self.runner_root = root
        self.paths = paths
        self.arch = arch
        self.wine_path = self._find_wine_path()

    def _find_wine_path(self) -> Path:
        for relative in _WINE_RELPATHS:
            candidate = self.runner_root / relative
            if candidate.is_file():
                return candidate
        raise BackendError(
            f"runner {self.runner_root} has no wine binary "
            f"(looked for {', '.join(_WINE_RELPATHS)})"
        )

    def _wineserver_path(self) -> Path | None:
        candidate = self.wine_path.parent / "wineserver"
        return candidate if candidate.is_file() else None

    def _env(self, prefix: Path) -> dict[str, str]:
        return wine_env(prefix, self.runner_root, arch=self.arch)

    def _run(
        self,
        prefix: Path,
        argv: list[str],
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one Wine command with a hard timeout and file-backed output.

        Captured stdout/stderr go to temporary files, not pipes. Wine children
        (and ``wineserver``) inherit the descriptors; ``communicate()`` would
        otherwise wait for pipe EOF and report a finished installer as a
        timeout because one grandchild still holds the pipe open. Completion is
        the direct child's exit; the timeout still kills its whole process
        group.
        """
        _LOGGER.debug("run %s (WINEPREFIX=%s)", _display_argv(argv), prefix)
        timeout = _COMMAND_TIMEOUT_S if timeout_s is None else max(timeout_s, 0.0)
        with ExitStack() as stack:
            stdout_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
            stderr_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
            try:
                process = subprocess.Popen(
                    argv,
                    env=self._env(prefix),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BackendError(f"cannot run {argv[0]}: {exc}") from exc
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_group(process)
                with suppress(subprocess.SubprocessError):
                    process.wait(timeout=5)
                raise BackendTimeout(
                    f"{Path(argv[0]).name} exceeded its {timeout:g}s timeout"
                ) from exc
            stdout = _read_capture(stdout_file)
            stderr = _read_capture(stderr_file)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def create_prefix(self, app_id: str, *, keep_z: bool = False) -> Path:
        """Create or adopt the prefix, then strip Z: unless explicitly kept."""
        _check_app_id(app_id)
        prefix = self.paths.prefix_for(app_id)
        _private_dir(self.paths.prefixes)
        _private_dir(prefix)
        marker = prefix / _SYSTEM_REG

        if not marker.is_file():
            _LOGGER.info("[prefix] initializing %s", prefix)
            booted = self.wine(prefix, "wineboot", "-u")
            if booted.returncode != 0:
                detail = (booted.stderr or booted.stdout or "").strip()
                suffix = f": {detail}" if detail else ""
                raise WinBridgeError(
                    f"wineboot failed for {prefix} (exit {booted.returncode}){suffix}"
                )

        settled = self.wine(prefix, "wineserver", "-w")
        if settled.returncode != 0:
            _LOGGER.warning(
                "[prefix] wineserver -w exited %s for %s", settled.returncode, prefix
            )

        if not marker.is_file():
            raise WinBridgeError(
                f"wineboot did not initialize {prefix}; missing {_SYSTEM_REG}"
            )
        if not keep_z and strip_z_drive(prefix):
            _LOGGER.info("[prefix] removed Z: host-root link from %s", prefix)
        return prefix

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a Wine program, or the runner's ``wineserver`` when asked for it.

        ``wineserver`` is a Unix daemon, not a PE program, so it must be
        executed directly instead of through the Wine loader. ``timeout_s``
        bounds the whole process group.
        """
        if not args:
            raise UsageError("wine requires at least one argument")
        if args[0] == "wineserver":
            daemon = self._wineserver_path()
            argv = [str(daemon), *args[1:]] if daemon else [str(self.wine_path), *args]
        else:
            argv = [str(self.wine_path), *args]
        return self._run(prefix, argv, timeout_s=timeout_s)

    def install(
        self,
        prefix: Path,
        installer: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        """Run the installer, inheriting stdio unless a log path is given.

        Inherited stdio keeps a GUI installer usable under ``--manual``; the log
        path is the silent path, where there is no terminal to read and the
        output must survive as evidence. ``timeout_s`` bounds the installer's
        process group: a hung installer is killed and reported as
        :class:`BackendTimeout`, with the evidence log already on disk.
        """
        argv = [str(self.wine_path), str(installer), *args]
        timeout = _COMMAND_TIMEOUT_S if timeout_s is None else max(timeout_s, 0.0)
        _LOGGER.info("[install] %s (timeout %gs)", _display_argv(argv), timeout)
        with ExitStack() as stack:
            try:
                if log_path is None:
                    process = subprocess.Popen(
                        argv,
                        env=self._env(prefix),
                        start_new_session=True,
                    )
                else:
                    handle = stack.enter_context(_private_append_log(Path(log_path)))
                    process = subprocess.Popen(
                        argv,
                        env=self._env(prefix),
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as exc:
                raise BackendError(f"cannot run installer {installer}: {exc}") from exc

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_group(process)
                with suppress(subprocess.SubprocessError):
                    process.wait(timeout=5)
                raise BackendTimeout(
                    f"installer {installer.name} exceeded its {timeout:g}s timeout"
                ) from exc
        return process.returncode

    def run(self, prefix: Path, exe: str, argv: list[str]) -> subprocess.Popen[bytes]:
        """Start the app in its own session, with private, non-inherited stdio.

        The launch gets ``/dev/null`` for stdin, and stdout/stderr go to
        ``logs/run-<prefix>.log`` instead of the caller's pipes: a bridge or
        WebView2 child that outlives the launcher would otherwise keep a
        caller's pipe open and make ``run`` look hung after it returned. The
        run log is best-effort; if it cannot be opened, the output is discarded
        and the app still launches.
        """
        command = [str(self.wine_path), exe, *argv]
        _LOGGER.info("[run] %s", _display_argv(command))
        log_path = run_log_path(self.paths, prefix.name)
        with ExitStack() as stack:
            try:
                stdout: int | BinaryIO = stack.enter_context(
                    _private_append_log(log_path)
                )
            except OSError as exc:
                _LOGGER.warning(
                    "[run] cannot open %s (%s); app output is discarded",
                    log_path,
                    exc,
                )
                stdout = subprocess.DEVNULL
            else:
                _LOGGER.info("[run] app output -> %s", log_path)
            try:
                return subprocess.Popen(
                    command,
                    env=self._env(prefix),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BackendError(f"cannot run {exe}: {exc}") from exc

    def expose(self, prefix: Path, host: Path, guest: str) -> None:
        """Symlink ``host`` at a C: path inside the prefix; never replace."""
        host_path = Path(host)
        if not host_path.is_absolute():
            raise UsageError(f"host folder must be absolute; got {str(host)!r}")
        if not host_path.is_dir():
            raise UsageError(f"host folder not found: {host_path}")

        target = _guest_path(prefix, guest)
        if target.is_symlink() or target.exists():
            raise UsageError(f"guest path already exists: {guest}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(host_path, target_is_directory=True)
        except OSError as exc:
            raise WinBridgeError(f"cannot expose {host_path} at {guest}: {exc}") from exc
        _LOGGER.info("[expose] %s -> %s", guest, host_path)

    def remove(self, prefix: Path) -> None:
        """Remove a prefix below ``paths.prefixes``; refuse anything else."""
        target = Path(prefix)
        store = self.paths.prefixes.resolve()
        if target.is_symlink():
            raise UsageError(f"refusing to remove a symlinked prefix: {target}")
        resolved = target.resolve()
        if resolved == store or not resolved.is_relative_to(store):
            raise UsageError(
                f"refusing to remove {target}; it is outside {self.paths.prefixes}"
            )
        if not target.exists():
            return
        if not target.is_dir():
            raise UsageError(f"prefix is not a directory: {target}")
        _LOGGER.info("[prefix] removing %s", target)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise WinBridgeError(f"cannot remove prefix {target}: {exc}") from exc
