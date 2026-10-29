"""Process discovery for a Wine prefix.

Wine processes inherit ``WINEPREFIX`` in their environment, so matching that
variable is the only reliable way to find a prefix's processes without trusting
window titles or guessing from executable names. Both helpers tolerate
unreadable or vanished ``/proc`` entries: an exited process is not an error.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

__all__ = ["pid_tree", "processes_for_prefix"]

_PREFIX_VARIABLE = b"WINEPREFIX"
_WINDOWS_IMAGE_SUFFIX = ".exe"


def processes_for_prefix(prefix: Path | str, proc_root: Path = Path("/proc")) -> set[int]:
    """Return every Wine PID whose environment sets ``WINEPREFIX`` to ``prefix``.

    The variable alone is not proof: a shell that exported ``WINEPREFIX`` hands
    it to every child, including this CLI. A candidate therefore has to look
    like a Wine process (``wine``/``wineserver`` in its comm, executable, or
    command line, or a ``.exe`` image) and must not be this process or one of
    its ancestors. Unreadable entries (other users, zombies, kernel threads)
    are skipped.
    """
    target = os.fsencode(str(prefix))
    found: set[int] = set()
    excluded = _self_and_ancestors(proc_root)
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded:
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if not _mentions_prefix(environ, target):
            continue
        if _looks_like_wine(entry):
            found.add(pid)
    return found


def _mentions_prefix(environ: bytes, target: bytes) -> bool:
    for item in environ.split(b"\0"):
        name, separator, value = item.partition(b"=")
        if separator and name == _PREFIX_VARIABLE and value == target:
            return True
    return False


def _looks_like_wine(entry: Path) -> bool:
    """Whether a process's names identify it as Wine or a Windows image."""
    names: list[str] = []
    with suppress(OSError):
        names.append((entry / "comm").read_text(encoding="utf-8", errors="replace"))
    with suppress(OSError):
        names.append(os.readlink(entry / "exe"))
    with suppress(OSError):
        for token in (entry / "cmdline").read_bytes().split(b"\0"):
            if token:
                names.append(token.decode("utf-8", errors="replace"))
    for name in names:
        normalized = name.strip().replace("\\", "/")
        if not normalized:
            continue
        base = normalized.rsplit("/", 1)[-1].casefold()
        if base.startswith("wine") or base.endswith(_WINDOWS_IMAGE_SUFFIX):
            return True
    return False


def _self_and_ancestors(proc_root: Path) -> set[int]:
    """Return this process and every parent, so a shared env cannot match us."""
    excluded: set[int] = set()
    pid = os.getpid()
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        parent = _read_ppid(proc_root / str(pid))
        if parent is None:
            break
        pid = parent
    return excluded


def _task_children(pid: int, proc_root: Path) -> set[int] | None:
    """Read ``task/*/children``; ``None`` means this kernel layout is unusable."""
    task_root = proc_root / str(pid) / "task"
    try:
        tasks = list(task_root.iterdir())
    except OSError:
        return None

    children: set[int] = set()
    readable = False
    for task in tasks:
        try:
            text = (task / "children").read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            continue
        readable = True
        for token in text.split():
            if token.isdigit():
                children.add(int(token))
    return children if readable else None


def _read_ppid(entry: Path) -> int | None:
    """Read a process's parent id from ``status``, falling back to ``stat``."""
    try:
        lines = (entry / "status").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None

    try:
        stat = (entry / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split() if closing >= 0 else []
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _parent_map(proc_root: Path) -> dict[int, list[int]]:
    """Build ``ppid -> [pid, ...]`` from ``/proc/<pid>/status`` or ``stat``."""
    children: dict[int, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return children

    for entry in entries:
        if not entry.name.isdigit():
            continue
        parent = _read_ppid(entry)
        if parent is not None:
            children.setdefault(parent, []).append(int(entry.name))
    return children


def _descendants_from_parent_map(root_pid: int, proc_root: Path) -> set[int]:
    children = _parent_map(proc_root)
    found = {root_pid}
    pending = [root_pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def pid_tree(root_pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    """Return ``root_pid`` and all of its descendants.

    The primary walk reads ``task/*/children``. If that kernel interface is not
    available, a parent map scan over ``/proc/<pid>/status`` is used instead.
    """
    tree = {root_pid}
    primary = _task_children(root_pid, proc_root)
    if primary is None:
        return _descendants_from_parent_map(root_pid, proc_root)

    pending = [child for child in primary if child not in tree]
    tree.update(primary)
    while pending:
        pid = pending.pop()
        children = _task_children(pid, proc_root)
        if children is None:
            return _descendants_from_parent_map(root_pid, proc_root)
        for child in children:
            if child not in tree:
                tree.add(child)
                pending.append(child)
    return tree
