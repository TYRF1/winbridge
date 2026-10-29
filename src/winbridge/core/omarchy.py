"""omarchy-shell taskbar pins.

The bar keeps its widgets in ``$XDG_CONFIG_HOME/omarchy/shell.json``. Current
Omarchy 4 configs nest sections under ``bar.layout`` (for example
``bar.layout.left``); older configs put them directly on ``bar``. Both shapes
are read, and a pin lives in the ``apps`` array of the
``io.github.joeyvigil.taskbar`` widget in any section (``left`` is preferred).
The desktop id is bare: omarchy-shell appends ``.desktop`` itself before handing
it to ``gtk-launch``.

Every rewrite preserves the whole document, including keys this module does not
understand, and replaces the config atomically after keeping one ``.bak``.
Invalid JSON is never rewritten: it raises :class:`WinBridgeError` before any
backup or temporary file is created.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from winbridge.core.errors import WinBridgeError

__all__ = [
    "DEFAULT_SHELL_CONFIG",
    "TASKBAR_ID",
    "pin_app",
    "restart_shell",
    "taskbar_present",
    "unpin_app",
]

#: The live omarchy-shell taskbar widget id.
TASKBAR_ID = "io.github.joeyvigil.taskbar"

#: omarchy-shell's configuration for the current user.
DEFAULT_SHELL_CONFIG = Path.home() / ".config" / "omarchy" / "shell.json"

_LOGGER = logging.getLogger("winbridge")

_BAR_KEY = "bar"
_LAYOUT_KEY = "layout"
_LEFT_SECTION = "left"
_APPS_KEY = "apps"
_DESKTOP_ID_KEY = "desktopId"
_BACKUP_SUFFIX = ".bak"
_OMARCHY = "omarchy"
_RESTART_ARGS = ("restart", "shell")
_COMMAND_TIMEOUT_S = 30.0

_RunCallable = Callable[..., subprocess.CompletedProcess[Any]]


def _backup_path(path: Path) -> Path:
    """Return the backup location beside ``path``."""
    return path.with_name(path.name + _BACKUP_SUFFIX)


def _load_document(path: Path) -> dict[str, Any] | None:
    """Load strict JSON, or ``None`` when the config does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise WinBridgeError(f"cannot read omarchy-shell config {path}: {exc}") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WinBridgeError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(document, dict):
        raise WinBridgeError(f"{path}: expected a JSON object at the top level")
    return document


def _section_lists(container: dict[str, Any]) -> list[list[Any]]:
    """Return list-valued bar sections, preferring ``left``.

    Omarchy 4 nests the current schema under ``bar.layout``; older configs put
    sections directly on ``bar``. ``left`` wins in both shapes; every other
    list-valued key keeps document order.
    """
    names = [name for name in container if name == _LEFT_SECTION]
    names.extend(name for name in container if name != _LEFT_SECTION)
    lists: list[list[Any]] = []
    for name in names:
        section = container[name]
        if isinstance(section, list):
            lists.append(section)
    return lists


def _bar_widget_lists(bar: dict[str, Any]) -> list[list[Any]]:
    """Return every bar widget list, current ``layout`` first, then legacy."""
    lists: list[list[Any]] = []
    layout = bar.get(_LAYOUT_KEY)
    if isinstance(layout, dict):
        lists.extend(_section_lists(layout))
    lists.extend(_section_lists(bar))
    return lists


def _find_widget(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the taskbar widget, preferring the bar's ``left`` section.

    Supports both the current ``bar.layout.<section>`` shape and the legacy
    ``bar.<section>`` shape, so an Omarchy upgrade keeps existing pins working.
    """
    bar = document.get(_BAR_KEY)
    if not isinstance(bar, dict):
        return None

    for section in _bar_widget_lists(bar):
        for widget in section:
            if isinstance(widget, dict) and widget.get("id") == TASKBAR_ID:
                return widget
    return None


def taskbar_present(document: dict[str, Any]) -> bool:
    """Return whether a parsed shell document contains the taskbar widget."""
    return _find_widget(document) is not None


def _apps(widget: dict[str, Any], path: Path) -> list[Any]:
    """Return the widget's apps array, creating it when it is missing."""
    apps = widget.get(_APPS_KEY)
    if apps is None:
        apps = []
        widget[_APPS_KEY] = apps
    if not isinstance(apps, list):
        raise WinBridgeError(f"{path}: {TASKBAR_ID} widget apps must be a list")
    return apps


def _entry_desktop_id(entry: Any) -> str | None:
    """Return the desktop id a taskbar entry refers to, when it has one."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get(_DESKTOP_ID_KEY)
        if isinstance(value, str):
            return value
    return None


def _upsert_pin(apps: list[Any], desktop_id: str, match: str) -> bool:
    """Bring existing pins up to date, or append one; report a change.

    An object entry keeps its position and every unrelated key, but its
    ``match`` is refreshed. A bare string pin is upgraded in place to the
    object form. Duplicate entries for the same id are all normalized; no
    entry is ever removed here.
    """
    found = False
    changed = False
    new_entry = {_DESKTOP_ID_KEY: desktop_id, "match": match}
    for index, entry in enumerate(apps):
        if _entry_desktop_id(entry) != desktop_id:
            continue
        found = True
        if isinstance(entry, str):
            apps[index] = dict(new_entry)
            changed = True
        elif isinstance(entry, dict) and entry.get("match") != match:
            entry["match"] = match
            changed = True

    if not found:
        apps.append(new_entry)
        changed = True
    return changed


def _write_document(path: Path, document: dict[str, Any]) -> None:
    """Back up and atomically replace the config with formatted JSON."""
    payload = json.dumps(document, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temp_path)
        shutil.copy2(path, _backup_path(path))
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def pin_app(shell_json: Path | str, desktop_id: str, match: str) -> bool:
    """Add or refresh a taskbar pin; return ``True`` only when the config changed.

    A missing config or a config without the taskbar widget returns ``False``:
    there is no bar to pin into. An existing object entry keeps its position
    and extra keys but gets the current ``match``; a bare string entry is
    upgraded to ``{"desktopId": ..., "match": ...}`` in place. A pin that
    already carries this match is a no-op returning ``False``.
    """
    path = Path(shell_json)
    document = _load_document(path)
    if document is None:
        return False
    widget = _find_widget(document)
    if widget is None:
        return False

    apps = _apps(widget, path)
    if not _upsert_pin(apps, desktop_id, match):
        return False
    _write_document(path, document)
    return True


def unpin_app(shell_json: Path | str, desktop_id: str) -> bool:
    """Remove exactly the entries that refer to ``desktop_id``.

    Returns ``True`` only when at least one entry was removed. Other apps and
    entries without a matching desktop id are preserved byte-for-byte in the
    rewritten document.
    """
    path = Path(shell_json)
    document = _load_document(path)
    if document is None:
        return False
    widget = _find_widget(document)
    if widget is None:
        return False

    apps = _apps(widget, path)
    kept = [entry for entry in apps if _entry_desktop_id(entry) != desktop_id]
    if len(kept) == len(apps):
        return False
    apps[:] = kept
    _write_document(path, document)
    return True


def restart_shell(
    *,
    enabled: bool = True,
    run: _RunCallable = subprocess.run,
) -> bool:
    """Restart the omarchy-shell bar best-effort; failures are warnings.

    Desktop entries are not always indexed immediately, so a successful pin is
    normally followed by one restart. A missing ``omarchy`` binary is not an
    error; a failing command is logged and reported as ``False``.
    """
    if not enabled:
        return False

    tool = shutil.which(_OMARCHY)
    if tool is None:
        _LOGGER.debug("omarchy: %s not found; skipping shell restart", _OMARCHY)
        return False

    try:
        completed = run(
            [tool, *_RESTART_ARGS],
            check=False,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _LOGGER.warning(
            "omarchy: shell restart exceeded %gs; the bar may need a manual restart",
            _COMMAND_TIMEOUT_S,
        )
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.warning("omarchy: shell restart failed: %s", exc)
        return False

    if completed.returncode != 0:
        _LOGGER.warning(
            "omarchy: shell restart exited %s; the bar may need a manual restart",
            completed.returncode,
        )
        return False
    return True
