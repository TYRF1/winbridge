"""Safe launch path: start an app without destroying a running instance.

Three entry points converge here: a launcher click, ``winbridge run``, and a
scheme callback. The default path therefore only observes: if the prefix already
has processes it prints that and returns a handle with ``popen=None`` instead of
spawning a second instance. ``--restart`` is the sole kill path, so a repeated
click or a second launch cannot discard unsaved work.

A URL is forwarded with Wine's ``start`` verb as a single argument. It never goes
through a shell, so a callback payload cannot be re-parsed as commands.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from winbridge.backends.base import Backend
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.locking import file_lock
from winbridge.core.prefix import kill_prefix
from winbridge.core.profile import Profile
from winbridge.core.state import Paths
from winbridge.util.log import redact_url
from winbridge.util.proc import pid_tree, processes_for_prefix

__all__ = ["Launch", "StageCallback", "run_app"]

#: A lifecycle observer: ``(stage, status)`` such as ``("launch", "started")``.
StageCallback = Callable[[str, str], None]

#: Wine needs a moment to bring the app up before ``start`` can reach it.
_URL_FORWARD_DELAY_S = 2.0
_ALREADY_RUNNING = "already running"


@dataclass(frozen=True, slots=True)
class Launch:
    """One launch attempt.

    ``popen`` is ``None`` when the app was already running and nothing was
    spawned. ``pid_tree`` is the pid set verify uses to match a window: the
    spawned process and its descendants, or the processes already observed in
    the prefix.
    """

    popen: subprocess.Popen[bytes] | None
    pid_tree: set[int]


def run_app(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    *,
    argv: list[str] | None = None,
    url: str | None = None,
    restart: bool = False,
    proc_probe: Callable[[Path], set[int]] = processes_for_prefix,
    before_start: Callable[[], None] | None = None,
    on_stage: StageCallback | None = None,
) -> Launch:
    """Launch ``profile``, or forward ``url`` to it, without killing by default.

    ``before_start`` runs inside the app lock once the prefix is known to be
    empty (or was just restarted) and before anything is spawned; an exception
    prevents the spawn. ``on_stage`` receives lifecycle events and owns the
    "already running" output when provided.
    """
    if url is not None and restart:
        # Forwarding must never restart: a callback that arrives for a running
        # app has to reach that instance, not replace it.
        raise UsageError("--url cannot be combined with --restart")
    # Validate before the restart branch: a malformed profile must never kill a
    # running app and then discover there is nothing to start.
    exe = _require_exe(profile)
    prefix = paths.prefix_for(profile.id)
    lock = paths.locks / f"app-{profile.id}.lock"

    def _stage(stage: str, status: str) -> None:
        if on_stage is not None:
            on_stage(stage, status)

    # The probe and the spawn are one critical section: two cold launches that
    # both probed before either spawned would start two instances. The lock is
    # released as soon as the Popen handle exists (or the live instance was
    # observed), so a launcher click never waits for the app itself.
    with file_lock(lock):
        if restart:
            # The only kill path. Every other run must preserve unsaved work.
            _stage("stop", "stopping")
            kill_prefix(backend, prefix)
            _stage("stop", "stopped")
            running: set[int] = set()
        else:
            running = set(proc_probe(prefix))

        if running:
            if url is None:
                # A callback owns output; printing here would duplicate it.
                if on_stage is None:
                    pids = ", ".join(str(pid) for pid in sorted(running))
                    print(f"{_ALREADY_RUNNING} (pids {pids}); use --restart")
                else:
                    _stage("launch", "already-running")
                return Launch(popen=None, pid_tree=running)
            # Forward into the live instance; it owns its own window and state.
            _forward(backend, prefix, url)
            return Launch(popen=None, pid_tree=running)

        if before_start is not None:
            before_start()
        _stage("launch", "starting")
        launch = _start(exe, prefix, backend, argv or [])
        _stage("launch", "started")
        if url is not None:
            time.sleep(_URL_FORWARD_DELAY_S)
            _forward(backend, prefix, url)
        return launch


def _require_exe(profile: Profile) -> str:
    """Return the recorded exe, or fail before anything is killed or spawned."""
    exe = profile.exe.strip()
    if not exe:
        raise WinBridgeError(
            f"profile {profile.id!r} has no exe; re-run 'winbridge setup'"
        )
    return exe


def _forward(backend: Backend, prefix: Path, url: str) -> None:
    """Hand ``url`` to the app through Wine's ``start`` verb, without a shell."""
    result = backend.wine(prefix, "start", url)
    if result.returncode != 0:
        raise WinBridgeError(
            f"cannot forward {redact_url(url)} to {prefix}: "
            f"wine start exited {result.returncode}"
        )


def _start(
    exe: str,
    prefix: Path,
    backend: Backend,
    argv: list[str],
) -> Launch:
    """Start the validated exe and capture the process tree it owns."""
    popen = backend.run(prefix, exe, list(argv))
    return Launch(popen=popen, pid_tree={popen.pid} | pid_tree(popen.pid))
