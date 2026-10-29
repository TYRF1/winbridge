"""Logging, evidence directories, and URL redaction."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.logging import RichHandler

from winbridge.core.state import Paths

__all__ = [
    "append_run_log",
    "evidence_dir",
    "redact_url",
    "run_log_path",
    "setup_logging",
]

_LOGGER_NAME = "winbridge"
_GLOBAL_LOG_NAME = "winbridge.log"
_GLOBAL_LOG_MAX_BYTES = 5 * 1024 * 1024
_GLOBAL_LOG_BACKUPS = 2
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_GLOBAL_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_REDACTED = "[redacted]"


def _timestamp() -> str:
    """Return a filesystem-safe UTC timestamp shared by logs and evidence."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def _private_dir(path: Path) -> None:
    """Create ``path`` and every missing parent as ``0700`` for its owner."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _touch_private(path: Path) -> None:
    """Create ``path`` if needed and leave it readable only by its owner."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _component(value: str, name: str) -> None:
    """Reject values that would escape their evidence or log directory."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"invalid {name}: {value!r}")


class _OwnedHandler:
    """Marker so a later invocation can replace handlers it installed."""

    _winbridge_owned = True


class _OwnedFileHandler(_OwnedHandler, logging.FileHandler):
    """A file handler that remembers it belongs to the winbridge logger."""


class _OwnedRichHandler(_OwnedHandler, RichHandler):
    """A console handler that remembers it belongs to the winbridge logger."""


class _OwnedRotatingFileHandler(_OwnedHandler, RotatingFileHandler):
    """Global invocation log; files created by rotation stay owner-only."""

    def doRollover(self) -> None:
        super().doRollover()
        os.chmod(self.baseFilename, 0o600)


def evidence_dir(paths: Paths, app_id: str, version: str = "unknown") -> Path:
    """Create and return ``evidence/<app>/<version>/<timestamp>/`` at ``0700``."""
    _component(app_id, "app id")
    _component(version, "version")

    app_dir = paths.evidence / app_id
    version_dir = app_dir / version
    run_dir = version_dir / _timestamp()
    for directory in (paths.evidence, app_dir, version_dir, run_dir):
        _private_dir(directory)
    return run_dir


def run_log_path(paths: Paths, app_id: str) -> Path:
    """Return the per-app run log shared by the app, helper, and trace recipes.

    ``app_id`` follows the same component rule as evidence so a profile can
    never point the log outside ``paths.logs``.
    """
    _component(app_id, "app id")
    return paths.logs / f"run-{app_id}.log"


def append_run_log(path: Path, message: str) -> None:
    """Append one owner-only line to ``path``, creating it when missing.

    The write is a single ``O_APPEND`` write so the app's own output and the
    cursor helper can share the file without rewriting each other. Callers that
    treat logging as best-effort catch :class:`OSError`.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (message.rstrip("\n") + "\n").encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
    finally:
        os.close(fd)


def setup_logging(
    paths: Paths,
    *,
    command: str,
    app_id: str | None = None,
    version: str = "unknown",
    verbose: bool = False,
) -> logging.Logger:
    """Configure the ``winbridge`` logger for one invocation and return it.

    App-scoped commands write ``winbridge.log`` inside a fresh evidence
    directory; commands without an app write ``logs/<ts>-<command>.log``.
    Every invocation also appends to the rolling global log inside the
    WinBridge state directory (``paths.logs.parent / "winbridge.log"``). A
    Rich console handler is added only when stderr is a TTY. Reconfiguring the
    logger replaces the handlers this function owns instead of stacking them.
    """
    _component(command, "command")

    if app_id is None:
        _private_dir(paths.logs)
        log_path = paths.logs / f"{_timestamp()}-{command}.log"
    else:
        log_path = evidence_dir(paths, app_id, version) / "winbridge.log"

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_winbridge_owned", False):
            logger.removeHandler(handler)
            handler.close()

    _touch_private(log_path)
    file_handler = _OwnedFileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(file_handler)

    global_dir = paths.logs.parent
    _private_dir(global_dir)
    global_path = global_dir / _GLOBAL_LOG_NAME
    _touch_private(global_path)
    global_handler = _OwnedRotatingFileHandler(
        global_path,
        maxBytes=_GLOBAL_LOG_MAX_BYTES,
        backupCount=_GLOBAL_LOG_BACKUPS,
        encoding="utf-8",
    )
    global_handler.setLevel(logging.DEBUG)
    global_handler.setFormatter(logging.Formatter(_GLOBAL_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(global_handler)

    if sys.stderr.isatty():
        # Human progress belongs on stderr: stdout is the machine channel and
        # ``--json`` output must parse line by line even on a real TTY.
        rich_handler = _OwnedRichHandler(
            console=Console(stderr=True),
            show_path=False,
            show_time=False,
        )
        rich_handler.setLevel(logging.DEBUG)
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(rich_handler)

    return logger


def redact_url(url: str) -> str:
    """Keep a URL's scheme, host, and port; replace the rest with ``[redacted]``.

    Values that do not parse as an absolute URL with a host cannot be safely
    shortened, so they are fully redacted rather than echoed.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return _REDACTED

    if not parsed.scheme or not host:
        return _REDACTED

    host_part = f"[{host}]" if ":" in host else host
    if port is not None:
        host_part = f"{host_part}:{port}"
    return f"{parsed.scheme}://{host_part}/{_REDACTED}"
