"""Prefix lifecycle: remove Wine's host-root link and stop a prefix cleanly."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from winbridge.backends.base import Backend
from winbridge.core.errors import WinBridgeError
from winbridge.util.proc import processes_for_prefix

__all__ = ["PrefixStillRunning", "kill_prefix", "strip_z_drive"]

_LOGGER = logging.getLogger("winbridge")
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_POLL_S = 0.1


class PrefixStillRunning(WinBridgeError):
    """``wineserver -k`` did not stop every process in the prefix in time."""


def strip_z_drive(prefix: Path) -> bool:
    """Remove Wine's ``dosdevices/z:`` host-root link; return whether it existed.

    Only ``--keep-z-drive`` skips this. ``expose`` is the single supported way
    to add a host link afterwards.
    """
    link = prefix / "dosdevices" / "z:"
    if not (link.is_symlink() or link.exists()):
        return False
    if link.is_dir() and not link.is_symlink():
        raise WinBridgeError(f"refusing to remove a directory at {link}")
    try:
        link.unlink()
    except FileNotFoundError:
        return False
    return True


def kill_prefix(
    backend: Backend,
    prefix: Path,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    poll_s: float = _DEFAULT_POLL_S,
    proc_probe: Callable[[Path], set[int]] | None = None,
) -> None:
    """Stop every process in ``prefix`` and wait until none remain.

    ``wineserver -k`` requests shutdown; the wait is the actual guarantee. The
    timeout exists so a stuck Windows process fails loudly instead of hanging a
    setup or update forever.
    """
    backend.wine(prefix, "wineserver", "-k")
    probe = proc_probe if proc_probe is not None else processes_for_prefix
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        remaining = probe(prefix)
        if not remaining:
            _LOGGER.info("[prefix] no processes left in %s", prefix)
            return
        now = time.monotonic()
        if now >= deadline:
            pids = ", ".join(str(pid) for pid in sorted(remaining))
            raise PrefixStillRunning(
                f"prefix {prefix} still has {len(remaining)} process(es) "
                f"after wineserver -k: {pids}"
            )
        if poll_s > 0:
            time.sleep(min(poll_s, deadline - now))
