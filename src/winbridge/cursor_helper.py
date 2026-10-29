"""``python -m winbridge.cursor_helper``: the detached cursor-force poll loop.

The CLI never waits for this process and never shares stdio with it. It holds a
per-app advisory lock for its whole lifetime, so repeated ``run
--cursor=force`` calls cannot accumulate helpers. It exits when the prefix has
no Wine processes left. It imports neither the CLI nor
:class:`~winbridge.core.state.Paths`.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path

from winbridge.core.cursor import X11Cursor, run_force
from winbridge.core.errors import WinBridgeError
from winbridge.core.locking import file_lock
from winbridge.util.log import append_run_log

__all__ = ["main"]

_LOGGER = logging.getLogger("winbridge")
_PREFIX = "[cursor]"
#: Non-blocking acquisition is retried briefly so a helper that is just
#: releasing the lock does not make a rapid second launch lose the race.
_LOCK_ATTEMPTS = 5
_LOCK_RETRY_S = 0.05


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m winbridge.cursor_helper",
        description="Apply the X11 arrow cursor to one Wine prefix's windows.",
    )
    parser.add_argument("--prefix", required=True, help="Wine prefix to watch.")
    parser.add_argument("--log", required=True, help="Per-app run log to append to.")
    parser.add_argument("--app-id", required=True, help="App id for diagnostics.")
    parser.add_argument("--lock", required=True, help="Per-app helper lock file.")
    return parser


def _record(log_path: Path, message: str) -> None:
    _LOGGER.info(message)
    try:
        append_run_log(log_path, message)
    except OSError as exc:
        _LOGGER.warning("%s cannot write run log %s: %s", _PREFIX, log_path, exc)


def _acquire_lock(lock_path: Path) -> ExitStack | None:
    """Acquire the per-app lock, or return ``None`` when another helper owns it."""
    for attempt in range(_LOCK_ATTEMPTS):
        stack = ExitStack()
        try:
            stack.enter_context(file_lock(lock_path, blocking=False))
        except BlockingIOError:
            stack.close()
            if attempt + 1 >= _LOCK_ATTEMPTS:
                return None
            time.sleep(_LOCK_RETRY_S)
            continue
        return stack
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    x11_factory: Callable[[], X11Cursor] | None = None,
) -> int:
    """Run the helper; the per-app lock and the display close in ``finally``."""
    args = _parser().parse_args(argv)
    log_path = Path(args.log)
    _LOGGER.debug("%s helper app id %s", _PREFIX, args.app_id)

    try:
        lock = _acquire_lock(Path(args.lock))
    except OSError as exc:
        _record(log_path, f"{_PREFIX} force failed: {exc}")
        return 1
    if lock is None:
        _record(log_path, f"{_PREFIX} already active")
        return 0

    with lock:
        cursor: X11Cursor | None = None
        try:
            cursor = (x11_factory or X11Cursor)()
            return run_force(Path(args.prefix), log_path, x11=cursor)
        except (WinBridgeError, OSError) as exc:
            _record(log_path, f"{_PREFIX} force failed: {exc}")
            return 1
        finally:
            if cursor is not None:
                cursor.close()


if __name__ == "__main__":
    raise SystemExit(main())
