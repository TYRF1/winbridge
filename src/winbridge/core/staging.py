"""Guard staging for Windows payloads inside a managed prefix.

``dosdevices/z:`` is stripped from every prefix, so installers and the WebView2
runtime are copied into ``C:\\windows\\temp`` and invoked by their guest path.
This module owns the containment checks for that copy: a symlinked temp
directory (or a symlinked intermediate ``drive_c``/``windows``) must never let a
write or cleanup escape the prefix.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path

from winbridge.core.errors import WinBridgeError

__all__ = ["discard_staged", "stage_installer"]

_LOGGER = logging.getLogger("winbridge")

_WINDOWS_TEMP = "C:\\windows\\temp"
_TEMP_PARTS = ("drive_c", "windows", "temp")
_SUFFIX = re.compile(r"\.[a-z0-9]+\Z")


def _contained(path: Path, root: Path) -> bool:
    """Whether ``path`` resolves inside ``root`` (which must be resolved)."""
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _reject_symlinked_temp(prefix: Path) -> Path:
    """Return ``.../windows/temp`` and refuse any symlink on the way there."""
    current = prefix
    for part in _TEMP_PARTS:
        current = current / part
        if current.is_symlink():
            raise WinBridgeError(
                f"refusing a symlinked Windows temp path in {prefix}: {current}"
            )
    return current


def stage_installer(
    prefix: Path, source: Path, *, name_prefix: str
) -> tuple[Path, str]:
    """Copy ``source`` into ``C:\\windows\\temp`` and return host/guest paths.

    The destination directory is validated before it is created and again after
    ``mkdir``; every existing component must be a real directory inside the
    prefix. Names are unique, so two attempts cannot collide or reuse a
    leftover/symlinked file. Failures are wrapped in :class:`WinBridgeError`.
    """
    prefix_path = Path(prefix)
    source_path = Path(source)
    try:
        prefix_root = prefix_path.resolve()
    except OSError as exc:
        raise WinBridgeError(f"cannot resolve prefix {prefix_path}: {exc}") from exc

    temp_dir = _reject_symlinked_temp(prefix_path)
    if not _contained(temp_dir, prefix_root):
        raise WinBridgeError(
            f"refusing to stage outside prefix {prefix_path}: {temp_dir}"
        )
    try:
        temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise WinBridgeError(
            f"cannot create the staging directory in {prefix_path}: {exc}"
        ) from exc

    # Re-check after mkdir: a concurrent replacement must not redirect the copy.
    temp_dir = _reject_symlinked_temp(prefix_path)
    if not _contained(temp_dir, prefix_root):
        raise WinBridgeError(
            f"refusing to stage outside prefix {prefix_path}: {temp_dir}"
        )

    suffix = source_path.suffix.lower()
    if not _SUFFIX.fullmatch(suffix):
        suffix = ".exe"
    staged = temp_dir / f"{name_prefix}-{uuid.uuid4().hex}{suffix}"
    if staged.is_symlink() or not _contained(staged, prefix_root):
        raise WinBridgeError(f"refusing a staged path outside {prefix_path}: {staged}")

    try:
        shutil.copyfile(source_path, staged)
        staged.chmod(0o600)
    except OSError as exc:
        discard_staged(staged)
        raise WinBridgeError(
            f"cannot stage {source_path.name} in {temp_dir}: {exc}"
        ) from exc
    _LOGGER.info("[stage] copied %s to %s", source_path, staged)
    return staged, f"{_WINDOWS_TEMP}\\{staged.name}"


def discard_staged(path: Path | None) -> None:
    """Remove a staged payload; a missing file is already clean."""
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _LOGGER.warning("[stage] cannot remove staged payload %s: %s", path, exc)
