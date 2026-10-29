"""Advisory per-app and per-runner file locks."""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["file_lock"]

#: Per-thread locks held by this process. A blocking re-acquire in the same
#: thread (setup -> verify -> run) must not deadlock on its own flock; a
#: non-blocking acquire still contends, so a second command is refused.
_HELD = threading.local()


def _held() -> dict[str, int]:
    paths: dict[str, int] | None = getattr(_HELD, "paths", None)
    if paths is None:
        paths = {}
        _HELD.paths = paths
    return paths


@contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` for the context's lifetime.

    The lock file is created when missing and kept after release, so ``doctor``
    can report stale locks. ``blocking=False`` raises :class:`BlockingIOError`
    while another open file description holds the lock. A blocking acquire in
    the same thread is re-entrant; nested CLI work (setup's verify calling
    ``run``) shares the outer lock instead of deadlocking.
    """
    key = os.path.abspath(path)
    held = _held()
    if blocking and held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    parent = path.parent
    try:
        # The state layout creates ``locks/`` up front, but a lock is still
        # usable when a caller points at a fresh directory.
        parent.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    else:
        parent.chmod(0o700)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # ``open`` only applies ``mode`` when it creates the file; tighten a
        # lock file left behind by an older or laxer caller.
        os.fchmod(fd, 0o600)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
