"""Spec-safe ``.desktop`` entries, ``Exec`` escaping, and icon extraction.

The desktop-entry ``Exec`` grammar is a small shell dialect: arguments with
whitespace or reserved characters are double quoted, backslashes and ``%`` are
escaped, and field codes are expanded before the line is parsed. A Windows path
pasted into ``Exec`` unchanged therefore loses its separators and the launcher
fails silently, so every token goes through :func:`escape_exec_arg` — which
applies both the Exec quoting rule and the general desktop string rule.

The main entry runs the wrapper (``winbridge run <id>``); scheme callbacks get a
separate ``NoDisplay`` entry with ``%u`` so a URL never reaches the app launcher.
The main entry never carries a scheme ``MimeType`` — registration is owned by
``login-bridge install`` and ``xdg-mime``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from winbridge.core.errors import UsageError
from winbridge.core.profile import Profile
from winbridge.core.schemes import reject_reserved_scheme
from winbridge.core.state import Paths

__all__ = [
    "desktop_id",
    "escape_exec_arg",
    "extract_icon",
    "remove_desktop_entry",
    "remove_scheme_handler",
    "render_desktop_entry",
    "render_scheme_handler",
    "winbridge_exec_path",
    "write_desktop_entry",
    "write_scheme_handler",
]

_LOGGER = logging.getLogger("winbridge")
_GENERIC_ICON = "application-x-executable"
_APP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_RESERVED_EXEC_CHARS = frozenset("\"'\\<>~|&;$*?#()`")
_TOOL_TIMEOUT_S = 30
_DATABASE_TOOL = "update-desktop-database"
_ICON_GROUP_TYPE = "14"  # RT_GROUP_ICON


def desktop_id(app_id: str) -> str:
    """Return the bare entry id (``winbridge-<id>``), never with a suffix.

    The omarchy-shell click path appends ``.desktop`` itself, so a stored id
    carrying the suffix would double-append and fail.
    """
    _check_app_id(app_id)
    return f"winbridge-{app_id}"


def escape_exec_arg(value: str) -> str:
    """Escape one ``Exec`` argument.

    Two escaping layers apply, in the order the desktop specification reads
    them: the Exec quoting rules (backslashes, double quotes, backticks, dollar
    signs, and the wrapping quotes) are written first, then the general desktop
    string rule doubles every backslash again. A literal backslash therefore
    becomes a quadruple backslash, which is what GLib needs to hand one
    backslash to the program. Percent signs are doubled once so the field-code
    expander emits them literally; a field code such as ``%u`` must be appended
    by the caller, never passed through this function. Line breaks are folded to
    spaces so one entry stays one key per line.
    """
    text = "".join(" " if character in "\r\n\0" else character for character in value)
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    escaped = escaped.replace("%", "%%")
    if not escaped:
        quoted = '""'
    elif any(character.isspace() or character in _RESERVED_EXEC_CHARS for character in escaped):
        quoted = '"' + escaped + '"'
    else:
        quoted = escaped
    # General desktop-entry string escaping is applied over the Exec quoting.
    return quoted.replace("\\", "\\\\")


def winbridge_exec_path() -> str:
    """Return the escaped absolute command that launches WinBridge.

    An installed console script is preferred; otherwise the running interpreter
    runs the module. Both branches escape their tokens so a virtualenv or system
    path containing spaces still parses as one argument.
    """
    script = shutil.which("winbridge")
    if script:
        return escape_exec_arg(os.path.abspath(script))
    return f"{escape_exec_arg(os.path.abspath(sys.executable))} -m winbridge"


def render_desktop_entry(
    profile: Profile,
    *,
    exec_path: str,
    startup_wm_class: str | None = None,
    icon: str | os.PathLike[str] | None = None,
) -> str:
    """Render the main application entry.

    ``exec_path`` must already be escaped (pass :func:`winbridge_exec_path` or
    another pre-escaped command). ``icon`` is the result of :func:`extract_icon`;
    when it is ``None`` a generic theme icon name is used. The entry has no
    ``MimeType``; scheme handlers live in :func:`render_scheme_handler`.
    """
    app_id = _check_app_id(profile.id)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_entry_value(profile.name) or _entry_value(app_id)}",
        f"Exec={exec_path} run {app_id}",
        f"Icon={_entry_value(icon) if icon else _GENERIC_ICON}",
        "Terminal=false",
        "Categories=Utility;",
    ]
    wm_class = _entry_value(startup_wm_class) if startup_wm_class else ""
    if wm_class:
        lines.append(f"StartupWMClass={wm_class}")
    return "\n".join(lines) + "\n"


def render_scheme_handler(profile: Profile, *, exec_path: str) -> str:
    """Render the separate ``NoDisplay`` URL-scheme handler entry.

    Only schemes recorded in the profile are eligible. ``%u`` is a real field
    code: the desktop passes the callback URL as a single argument.
    """
    app_id = _check_app_id(profile.id)
    schemes = _profile_schemes(profile)
    mimetypes = "".join(f"x-scheme-handler/{scheme};" for scheme in schemes)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_entry_value(profile.name) or _entry_value(app_id)}",
        f"Exec={exec_path} open-url {app_id} %u",
        "NoDisplay=true",
        f"MimeType={mimetypes}",
        "Terminal=false",
    ]
    return "\n".join(lines) + "\n"


def write_desktop_entry(
    paths: Paths,
    profile: Profile,
    *,
    exec_path: str,
    startup_wm_class: str | None = None,
    icon: str | os.PathLike[str] | None = None,
) -> Path:
    """Write the main entry at mode ``0644`` and return its path.

    Setup writes this before verification (no ``StartupWMClass``) and rewrites it
    afterwards with the verified class. Refreshing the desktop database is
    best-effort: a missing tool is not an error.
    """
    target = _desktop_path(paths, profile.id)
    text = render_desktop_entry(
        profile,
        exec_path=exec_path,
        startup_wm_class=startup_wm_class,
        icon=icon,
    )
    _write_entry(target, text)
    _LOGGER.info(
        "launcher: wrote %s (Exec=%s run %s, icon=%s, StartupWMClass=%s)",
        target,
        exec_path,
        profile.id,
        icon or _GENERIC_ICON,
        startup_wm_class or "none",
    )
    _refresh_desktop_database(paths)
    return target


def write_scheme_handler(paths: Paths, profile: Profile, *, exec_path: str) -> Path:
    """Write the ``NoDisplay`` scheme handler and return its path.

    Only ``login-bridge install`` calls this; ``setup`` records schemes but
    writes no handler.
    """
    target = _handler_path(paths, profile.id)
    _write_entry(target, render_scheme_handler(profile, exec_path=exec_path))
    _LOGGER.info("launcher: wrote scheme handler %s", target)
    _refresh_desktop_database(paths)
    return target


def remove_desktop_entry(paths: Paths, app_id: str) -> None:
    """Delete this app's main entry, and nothing else."""
    target = _desktop_path(paths, app_id)
    if _remove_entry(target):
        _LOGGER.info("launcher: removed %s", target)
    _refresh_desktop_database(paths)


def remove_scheme_handler(paths: Paths, app_id: str) -> None:
    """Delete this app's scheme handler, and nothing else."""
    target = _handler_path(paths, app_id)
    if _remove_entry(target):
        _LOGGER.info("launcher: removed scheme handler %s", target)
    _refresh_desktop_database(paths)


def extract_icon(
    exe: Path | str, dest: Path | str, runner_root: Path | str
) -> Path | None:
    """Extract the largest icon from ``exe`` into ``dest``, or return ``None``.

    ``wrestool`` and ``icotool`` are optional host tools (a runner ``bin``
    directory is also searched). Both are given an explicit output directory:
    ``wrestool`` writes to standard output by default, so relying on the
    working directory would silently discard every icon. Any missing tool,
    non-executable source, tool failure, or I/O error is a clean ``None`` so the
    caller can fall back to a generic icon; this never raises for a missing
    toolchain.
    """
    source = Path(exe)
    target = Path(dest)
    runner = Path(runner_root)
    if not source.is_file():
        _LOGGER.debug("launcher: icon source is not a file: %s", source)
        return None

    wrestool = _find_tool("wrestool", runner)
    icotool = _find_tool("icotool", runner)
    if wrestool is None or icotool is None:
        missing = sorted(
            name
            for name, found in (("wrestool", wrestool), ("icotool", icotool))
            if found is None
        )
        _LOGGER.info(
            "launcher: icon extraction unavailable (missing %s); using a generic icon",
            ", ".join(missing),
        )
        return None

    with tempfile.TemporaryDirectory(prefix="winbridge-icon-") as workdir_name:
        workdir = Path(workdir_name)
        # wrestool writes resources to stdout unless --output names a directory;
        # a trailing separator forces directory semantics on every version.
        output_dir = f"{workdir}{os.sep}"
        if not _run_tool(
            [
                wrestool,
                "--extract",
                f"--type={_ICON_GROUP_TYPE}",
                f"--output={output_dir}",
                str(source.absolute()),
            ],
            workdir,
        ):
            return None
        ico = _largest(workdir.glob("*.ico"))
        if ico is None:
            _LOGGER.debug("launcher: %s contains no icon group", source)
            return None
        if not _run_tool([icotool, "--extract", f"--output={output_dir}", str(ico)], workdir):
            return None
        png = _largest(workdir.glob("*.png"))
        if png is None:
            _LOGGER.debug("launcher: icotool produced no PNG for %s", ico)
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(png, target)
            target.chmod(0o644)
        except OSError as exc:
            _LOGGER.warning("launcher: could not store icon at %s: %s", target, exc)
            return None

    _LOGGER.info("launcher: extracted icon %s from %s", target, source)
    return target


# --- internals --------------------------------------------------------------


def _check_app_id(app_id: str) -> str:
    """Reject ids that would escape ``applications/`` or break an ``Exec`` line."""
    if not _APP_ID_PATTERN.fullmatch(app_id):
        raise UsageError(f"invalid app id: {app_id!r}")
    return app_id


def _profile_schemes(profile: Profile) -> list[str]:
    """Validate and de-duplicate the profile's schemes, preserving order."""
    schemes: list[str] = []
    for scheme in profile.schemes:
        if not _SCHEME_PATTERN.fullmatch(scheme):
            raise UsageError(f"profile {profile.id!r}: invalid URL scheme {scheme!r}")
        reject_reserved_scheme(scheme, context=f"profile {profile.id!r}")
        if scheme not in schemes:
            schemes.append(scheme)
    if not schemes:
        raise UsageError(f"profile {profile.id!r} has no schemes to register")
    return schemes


def _entry_value(value: str | os.PathLike[str] | None) -> str:
    """Fold controls and escape backslashes for a single desktop-entry value."""
    if not value:
        return ""
    folded = "".join(
        " " if character in "\r\n\t" or ord(character) < 0x20 else character
        for character in os.fspath(value)
    )
    return folded.replace("\\", "\\\\").strip()


def _desktop_path(paths: Paths, app_id: str) -> Path:
    return paths.applications / f"{desktop_id(app_id)}.desktop"


def _handler_path(paths: Paths, app_id: str) -> Path:
    return paths.applications / f"{desktop_id(app_id)}-handler.desktop"


def _write_entry(target: Path, text: str) -> None:
    """Atomically write one entry at ``0644`` with no temp file left behind."""
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
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _remove_entry(target: Path) -> bool:
    """Unlink a regular entry file we own; never touch a directory or others."""
    if target.is_symlink() or target.is_file():
        target.unlink()
        return True
    return False


def _refresh_desktop_database(paths: Paths) -> None:
    """Run ``update-desktop-database`` best-effort; silent when unavailable."""
    directory = paths.applications
    if not directory.is_dir():
        return
    tool = shutil.which(_DATABASE_TOOL)
    if tool is None:
        _LOGGER.debug("launcher: %s not found; skipping", _DATABASE_TOOL)
        return
    try:
        subprocess.run(
            [tool, str(directory)],
            check=False,
            capture_output=True,
            timeout=_TOOL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.debug("launcher: %s failed: %s", _DATABASE_TOOL, exc)


def _find_tool(name: str, runner_root: Path) -> str | None:
    """Locate an icon tool on PATH, then under the runner's ``bin``."""
    found = shutil.which(name)
    if found:
        return found
    candidate = runner_root / "bin" / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _run_tool(argv: list[str], cwd: Path) -> bool:
    """Run one icon tool in ``cwd``; failures are logged, never raised."""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            timeout=_TOOL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.debug("launcher: %s failed: %s", argv[0], exc)
        return False
    if completed.returncode != 0:
        _LOGGER.debug("launcher: %s exited %s", argv[0], completed.returncode)
        return False
    return True


def _largest(candidates: Iterable[Path]) -> Path | None:
    """Pick the largest regular file; icon groups order by payload size."""
    files = [candidate for candidate in candidates if candidate.is_file()]
    return max(files, key=lambda path: path.stat().st_size, default=None)
