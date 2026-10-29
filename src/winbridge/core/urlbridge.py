"""Login-callback bridge: scheme handler registration and URL forwarding.

Registration has two owners. The desktop entry lives in the applications
directory and is written by :mod:`winbridge.core.launcher`; the default handler
association is owned by ``xdg-mime``. WinBridge therefore never hand-edits
``mimeapps.list`` to register a scheme. It only removes its own lines there when
unregistering, because ``xdg-mime`` has no "unset default" operation, and it
preserves every comment, blank line, section, and unrelated entry while doing
so.

Forwarding reuses the safe run path: a callback never kills a running app, a
cold prefix is started and receives the URL after the settle delay, and the URL
always stays one argument (no shell). Callback URLs can carry one-time tokens,
so logs and errors only ever contain :func:`~winbridge.util.log.redact_url`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from winbridge.backends.base import Backend
from winbridge.core import launcher
from winbridge.core.errors import UnsupportedEnvironment, UsageError, WinBridgeError
from winbridge.core.profile import Profile
from winbridge.core.run import run_app
from winbridge.core.schemes import is_reserved_scheme, reject_reserved_scheme
from winbridge.core.state import Paths
from winbridge.util.log import redact_url

__all__ = [
    "login_bridge_status",
    "open_url",
    "register_schemes",
    "unregister_app_schemes",
    "unregister_schemes",
]

_LOGGER = logging.getLogger("winbridge")
_SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_TOOL = "xdg-mime"
_TOOL_TIMEOUT_S = 30
_MIMEAPPS = "mimeapps.list"
_HANDLER_SUFFIX = "-handler"


def run(args: list[str]) -> str:
    """Run one ``xdg-mime`` invocation and return its stripped stdout.

    This is the module's only external process call, and it is module-level so
    tests can replace it. An argument list keeps the call shell-free. A missing
    tool is a clean environment failure; a non-zero exit is a real failure with
    the tool's own diagnostic attached.
    """
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise UnsupportedEnvironment(
            f"{args[0]} not found; install xdg-utils"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise WinBridgeError(f"{args[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise WinBridgeError(
            f"{' '.join(args)} exited {completed.returncode}: {detail}"
        )
    return completed.stdout.strip()


def register_schemes(profile: Profile, paths: Paths) -> list[str]:
    """Register every scheme recorded in ``profile`` and return them in order.

    Only the profile's schemes are eligible. The handler entry is written first
    (so ``xdg-mime`` can see it), then one ``xdg-mime default`` call per unique
    scheme claims the ``x-scheme-handler/<scheme>`` association. ``mimeapps.list``
    is not edited here; ``xdg-mime`` owns that file.
    """
    schemes = _schemes(profile)
    if not schemes:
        raise UsageError(f"profile {profile.id!r} has no schemes to register")

    handler = launcher.write_scheme_handler(
        paths, profile, exec_path=launcher.winbridge_exec_path()
    )
    for scheme in schemes:
        run([_TOOL, "default", handler.name, f"x-scheme-handler/{scheme}"])
        _LOGGER.info("urlbridge: %s is the default handler for %s", handler.name, scheme)
    return schemes


def unregister_schemes(profile: Profile, paths: Paths) -> list[str]:
    """Remove our handler entry and every association that names it.

    ``mimeapps.list`` is rewritten atomically and only when something changed.
    The handler id itself is the only criterion, so entries left behind by a
    scheme that a later ``setup --force`` removed are cleaned too. Comments,
    blank lines, section order, unrelated keys, other applications' handlers,
    and the file mode all survive. A missing file is not created; a symlinked
    file is written through so the user's link is preserved.
    """
    schemes = _schemes(profile)
    unregister_app_schemes(profile.id, paths)
    return schemes


def unregister_app_schemes(app_id: str, paths: Paths) -> None:
    """Remove the handler entry and associations derived from ``app_id``.

    This is the profile-free cleanup path used by ``unbridge``: the handler
    filename and ``mimeapps.list`` entries depend only on the app id, so they
    can be removed even after the profile is gone. It has exactly the same
    preservation rules as :func:`unregister_schemes`.
    """
    launcher.remove_scheme_handler(paths, app_id)
    _remove_associations(paths.config_home / _MIMEAPPS, app_id)


def open_url(
    profile: Profile,
    backend: Backend,
    url: str,
    *,
    paths: Paths | None = None,
) -> int:
    """Forward one callback URL to ``profile`` without ever killing it.

    The URL's scheme must be recorded in the profile. A warm prefix receives the
    URL immediately through ``wine start``; a cold one is started first and gets
    the URL after the run helper's two-second settle delay. ``paths`` defaults to
    the real XDG layout; tests and callers with an explicit root pass it in.
    Returns ``0`` on success. Failures raise with a redacted URL in the message.
    """
    scheme = _validate_url(profile, url)
    _LOGGER.info("urlbridge: forwarding %s callback %s", scheme, redact_url(url))
    run_app(profile, paths or Paths.default(), backend, url=url)
    return 0


def login_bridge_status(profile: Profile, paths: Paths) -> dict[str, str]:
    """Return each scheme's default handler, or ``""`` when none is set.

    The answer comes from ``xdg-mime query default``; ``paths`` locates our
    handler entry so its presence is recorded in the diagnostic log.
    """
    schemes = _schemes(profile)
    handler = _handler_path(paths, profile)
    _LOGGER.debug(
        "urlbridge: handler entry %s is %s",
        handler,
        "present" if handler.is_file() else "missing",
    )
    return {
        scheme: run([_TOOL, "query", "default", f"x-scheme-handler/{scheme}"])
        for scheme in schemes
    }


# --- internals --------------------------------------------------------------


def _schemes(profile: Profile) -> list[str]:
    """Validate and de-duplicate the profile's schemes, preserving order."""
    schemes: list[str] = []
    for scheme in profile.schemes:
        if not _SCHEME_PATTERN.fullmatch(scheme):
            raise UsageError(f"profile {profile.id!r}: invalid URL scheme {scheme!r}")
        reject_reserved_scheme(scheme, context=f"profile {profile.id!r}")
        if scheme not in schemes:
            schemes.append(scheme)
    return schemes


def _validate_url(profile: Profile, url: str) -> str:
    """Return the URL's lowercased scheme, or reject it before any forwarding.

    Leading/trailing whitespace, control characters, and unparseable URLs are
    refused outright: a callback arrives as one desktop field code and should be
    exactly one clean URL. The rejection never echoes the URL, because it can
    carry a one-time token.
    """
    if not url or url != url.strip():
        raise UsageError(f"profile {profile.id!r}: invalid callback URL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise UsageError(f"profile {profile.id!r}: invalid callback URL")
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as exc:
        raise UsageError(f"profile {profile.id!r}: invalid callback URL") from exc

    recorded = _schemes(profile)
    if scheme and is_reserved_scheme(scheme):
        raise UsageError(
            f"profile {profile.id!r}: URL scheme {scheme!r} is reserved; "
            "callback forwarding refuses it"
        )
    if not scheme or scheme not in {item.lower() for item in recorded}:
        known = ", ".join(recorded) or "none"
        label = scheme or "<none>"
        raise UsageError(
            f"profile {profile.id!r}: unregistered URL scheme {label!r}; "
            f"recorded schemes: {known}"
        )
    return scheme


def _handler_path(paths: Paths, profile: Profile) -> Path:
    return paths.applications / f"{launcher.desktop_id(profile.id)}{_HANDLER_SUFFIX}.desktop"


def _handler_ids(app_id: str) -> set[str]:
    """Accept both spellings a default list may use for our handler."""
    bare = f"{launcher.desktop_id(app_id)}{_HANDLER_SUFFIX}"
    return {bare, f"{bare}.desktop"}


def _remove_associations(target: Path, app_id: str) -> bool:
    """Strip every entry that names our handler; return whether it changed.

    ``xdg-mime`` follows symlinks and writes the real file, so a symlinked
    ``mimeapps.list`` is resolved first. Replacing the link with a regular file
    would leave our association behind in the target and silently destroy the
    user's link.
    """
    if target.is_symlink():
        target = target.resolve()
    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError) as exc:
        raise WinBridgeError(f"cannot read {target}: {exc}") from exc

    handlers = _handler_ids(app_id)
    lines: list[str] = []
    for line in original.splitlines():
        kept = _without_our_entries(line, handlers)
        if kept is not None:
            lines.append(kept)
    text = "\n".join(lines)
    if lines and original.endswith("\n"):
        text += "\n"
    if text == original:
        return False

    mode = _file_mode(target)
    _write_text(target, text, mode=mode)
    _LOGGER.info("urlbridge: removed our associations from %s", target)
    return True


def _without_our_entries(line: str, handlers: set[str]) -> str | None:
    """Drop our components from one line; ``None`` removes the whole line.

    The key is deliberately ignored: a value naming our handler is ours whether
    it sits under a current scheme, a scheme dropped by ``setup --force``, or an
    unrelated mimetype. Other handlers in the same list stay untouched.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return line
    key, separator, value = stripped.partition("=")
    if not separator:
        return line

    entries = [entry.strip() for entry in value.split(";") if entry.strip()]
    kept = [entry for entry in entries if entry not in handlers]
    if len(kept) == len(entries):
        return line
    if not kept:
        return None
    return f"{key.strip()}={';'.join(kept)};"


def _file_mode(target: Path) -> int:
    """Keep the existing mode; a fresh user file is readable, never executable."""
    with contextlib.suppress(OSError):
        return target.stat().st_mode & 0o777
    return 0o644


def _write_text(target: Path, text: str, *, mode: int) -> None:
    """Atomically replace ``target`` with ``text`` and no leftover temp file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise
