"""Expose host folders through the prefix's guest user profile.

Wine's ``Z:`` host-root link is stripped at prefix creation, so this module is
the only supported way to hand a host folder to an app. The link lives under
``drive_c/users/<host-user>`` because an app that validates paths inside its
Windows profile still accepts a directory there.

The profile's ``exposed`` table is the ownership record. ``unexpose`` never
trusts a path alone: it removes a link only when the entry is recorded and the
symlink still resolves to that recorded host. An existing file, directory, or
foreign symlink is never replaced.
"""

from __future__ import annotations

import getpass
import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path

from winbridge.backends.base import Backend
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.profile import ExposedPath, Profile
from winbridge.core.state import Paths

__all__ = ["expose", "guest_user_dir", "relink_exposed", "unexpose"]

_LOGGER = logging.getLogger("winbridge")
_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z")
_USERS = ("drive_c", "users")
#: Wine's built-in accounts are not the host user and must never be selected.
_BUILTIN_USERS = frozenset({"public", "all users", "default", "default user"})


def guest_user_dir(prefix: Path | str) -> Path:
    """Return the host-user directory under ``drive_c/users``.

    Wine prefixes also contain built-in accounts (``Public``, ``All Users``,
    ``Default``, ``Default User``) and Proton adds ``steamuser``. Built-ins are
    ignored. When more than one real candidate remains, the current host user
    wins; anything else ambiguous is an error rather than a guess: linking into
    the wrong profile would silently hide the folder from the app.
    """
    root = Path(prefix)
    users = root.joinpath(*_USERS)
    if users.is_symlink():
        raise WinBridgeError(f"refusing a symlinked guest users directory: {users}")
    if not users.is_dir():
        raise WinBridgeError(
            f"prefix {root} has no drive_c/users directory; run 'winbridge setup' first"
        )
    if not _is_within(users, root):
        raise WinBridgeError(f"guest users directory escapes the prefix: {users}")

    all_entries = sorted(entry for entry in users.iterdir() if entry.is_dir())
    entries = [
        entry for entry in all_entries if entry.name.casefold() not in _BUILTIN_USERS
    ]
    if not entries:
        builtins = ", ".join(entry.name for entry in all_entries)
        detail = f" (only built-in accounts: {builtins})" if builtins else ""
        raise WinBridgeError(
            f"prefix {root} has no guest user under drive_c/users{detail}"
        )
    if len(entries) > 1:
        current = _current_user()
        preferred = [
            entry for entry in entries if entry.name.casefold() == current.casefold()
        ]
        if len(preferred) != 1:
            names = ", ".join(entry.name for entry in entries)
            raise WinBridgeError(
                f"prefix {root} has multiple guest users under drive_c/users "
                f"({names}); expected exactly one"
            )
        entries = preferred

    user = entries[0]
    if user.is_symlink():
        raise WinBridgeError(f"refusing a symlinked guest user directory: {user}")
    if not _is_within(user, root):
        raise WinBridgeError(f"guest user directory escapes the prefix: {user}")
    return user


def expose(
    profile: Profile,
    paths: Paths,
    host: Path | str,
    name: str | None = None,
) -> ExposedPath:
    """Symlink ``host`` into the guest user profile and record the ownership.

    ``name`` defaults to the host folder's basename. Repeating the same exposure
    is idempotent and reuses the recorded guest name's case. The same host may
    be exposed under several names; a different host at the same (Windows
    case-insensitive) name is refused.
    """
    host_path = _expand(host)
    if not host_path.is_absolute():
        raise UsageError(f"host folder must be absolute; got {str(host)!r}")
    if not host_path.is_dir():
        raise UsageError(f"host folder not found: {host_path}")

    folder = _check_name(name if name is not None else host_path.name)
    prefix = _prefix(profile, paths)
    user = guest_user_dir(prefix)
    guest = f"C:\\users\\{user.name}\\{folder}"

    recorded = _find_by_guest(profile, guest)
    if recorded is not None:
        recorded_host = _absolute_host(recorded)
        if recorded_host is None or not _same_target(host_path, recorded_host):
            raise UsageError(
                f"{guest} is already exposed from {recorded.host}; "
                "run 'winbridge unexpose' first"
            )
        _, recorded_name = _guest_parts(recorded.guest)
        _ensure_link(user, recorded_name, host_path, owned=True)
        return recorded

    _ensure_link(user, folder, host_path, owned=False)
    item = ExposedPath(host=str(host_path), guest=guest)
    profile.exposed.append(item)
    _LOGGER.info("[expose] %s -> %s", guest, host_path)
    return item


def unexpose(profile: Profile, paths: Paths, ref: str) -> ExposedPath:
    """Remove one recorded exposure, addressed by host path or guest name.

    Only a symlink that still resolves to the recorded host is unlinked. A
    missing link is treated as stale and only the record is dropped.
    """
    item = _match_record(profile, ref)
    user = guest_user_dir(_prefix(profile, paths))
    recorded_user, name = _guest_parts(item.guest)
    if recorded_user.casefold() != user.name.casefold():
        raise WinBridgeError(
            f"recorded guest user {recorded_user!r} does not match prefix user "
            f"{user.name!r} for {item.guest}; re-run 'winbridge expose'"
        )
    link = user / name
    recorded = _absolute_host(item)
    if recorded is None:
        raise WinBridgeError(
            f"invalid recorded host for {item.guest}: {item.host!r}"
        )

    if link.is_symlink():
        if not _same_target(link, recorded):
            raise UsageError(
                f"refusing to remove {link}: it does not resolve to {recorded}"
            )
        try:
            link.unlink()
        except OSError as exc:
            raise WinBridgeError(f"cannot remove {link}: {exc}") from exc
        _LOGGER.info("[expose] removed %s -> %s", link, recorded)
    elif link.exists():
        raise UsageError(
            f"refusing to remove {link}: it is not a symlink owned by WinBridge"
        )

    profile.exposed = [entry for entry in profile.exposed if entry is not item]
    return item


def relink_exposed(
    exposed: Iterable[ExposedPath],
    prefix: Path,
    backend: Backend,
    *,
    report: Callable[[str], object] | None = None,
) -> None:
    """Recreate recorded host-folder links in a prefix that may be new.

    Used after setup adopts or recreates a prefix and after a runner rebuild.
    An intact link is left alone; a missing host folder, a changed guest user,
    or an unexpected collision is reported and skipped, never fatal: the app is
    already usable and the ownership record remains for a later ``expose``.
    """
    items = list(exposed)
    if not items:
        return

    def say(message: str) -> None:
        if report is not None:
            report(message)
        else:
            _LOGGER.info("[expose] %s", message)

    try:
        user = guest_user_dir(prefix)
    except (OSError, WinBridgeError) as exc:
        message = f"cannot recreate exposed folders in {prefix}: {exc}"
        if report is not None:
            report(f"warning: {message}")
        else:
            _LOGGER.warning("[expose] %s", message)
        return

    for item in items:
        try:
            recorded_user, name = _guest_parts(item.guest)
        except WinBridgeError as exc:
            say(f"warning: cannot recreate {item.guest!r}: {exc}")
            continue
        host = _absolute_host(item)
        if host is None:
            say(f"warning: invalid recorded host for {item.guest}: {item.host!r}")
            continue
        if recorded_user.casefold() != user.name.casefold():
            say(
                f"warning: recorded guest user {recorded_user!r} does not match "
                f"prefix user {user.name!r} for {item.guest}; re-run 'winbridge expose'"
            )
            continue
        link = user / name
        if link.is_symlink() and _same_target(link, host):
            continue
        if link.is_symlink() or link.exists():
            say(
                f"warning: cannot recreate {item.guest} -> {host}: "
                f"{link} already exists and is not the recorded link"
            )
            continue
        try:
            backend.expose(prefix, host, item.guest)
        except (OSError, WinBridgeError) as exc:
            say(f"warning: cannot recreate {item.guest} -> {host}: {exc}")
        else:
            say(f"{item.guest} -> {host}")


def _prefix(profile: Profile, paths: Paths) -> Path:
    app_id = profile.id
    if (
        not app_id
        or app_id != app_id.strip()
        or app_id.startswith(".")
        or any(char in app_id for char in ("/", "\\", "\x00"))
    ):
        raise UsageError(f"invalid app id: {app_id!r}")
    return paths.prefix_for(app_id)


def _check_name(value: str) -> str:
    if value in {".", ".."} or not _NAME_PATTERN.fullmatch(value):
        raise UsageError(
            f"invalid exposure name: {value!r}; "
            "use letters, digits, dot, underscore, or hyphen"
        )
    return value


def _ensure_link(user: Path, folder: str, host: Path, *, owned: bool) -> None:
    link = user / folder
    if link.is_symlink():
        if owned and _same_target(link, host):
            return
        if owned:
            raise UsageError(
                f"refusing to replace {link}: it is a symlink to {_readlink(link)}, "
                f"not {host}"
            )
        raise UsageError(
            f"refusing to replace {link}: an existing symlink is not recorded "
            "by WinBridge"
        )
    if link.exists():
        raise UsageError(
            f"refusing to replace {link}: path already exists and is not owned "
            "by WinBridge"
        )
    collision = _case_collision(user, folder)
    if collision is not None:
        raise UsageError(
            f"refusing to create {link}: {collision.name!r} already uses that "
            "name (Windows paths are case-insensitive)"
        )
    try:
        link.symlink_to(host, target_is_directory=True)
    except OSError as exc:
        raise WinBridgeError(f"cannot expose {host} at {link}: {exc}") from exc


def _find_by_guest(profile: Profile, guest: str) -> ExposedPath | None:
    wanted = guest.casefold()
    for item in profile.exposed:
        if item.guest.casefold() == wanted:
            return item
    return None


def _case_collision(user: Path, folder: str) -> Path | None:
    wanted = folder.casefold()
    for entry in user.iterdir():
        if entry.name != folder and entry.name.casefold() == wanted:
            return entry
    return None


def _match_record(profile: Profile, ref: str) -> ExposedPath:
    text = ref
    matches: list[ExposedPath] = []
    if _NAME_PATTERN.fullmatch(text):
        name = _check_name(text)
        for item in profile.exposed:
            try:
                recorded_name = _guest_parts(item.guest)[1]
            except WinBridgeError:
                continue
            if recorded_name.casefold() == name.casefold():
                matches.append(item)
    else:
        host = _expand(text)
        if not host.is_absolute():
            raise UsageError(f"host folder must be absolute; got {ref!r}")
        wanted = _resolved(host)
        for item in profile.exposed:
            recorded = _absolute_host(item)
            if recorded is not None and _resolved(recorded) == wanted:
                matches.append(item)

    if not matches:
        raise WinBridgeError(f"not exposed: {ref}")
    if len(matches) > 1:
        raise UsageError(
            f"{ref!r} matches multiple exposed folders; pass the guest name "
            "to choose one"
        )
    return matches[0]


def _absolute_host(item: ExposedPath) -> Path | None:
    try:
        recorded = Path(item.host).expanduser()
    except (OSError, RuntimeError):
        return None
    return recorded if recorded.is_absolute() else None


def _expand(value: str | Path) -> Path:
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError) as exc:
        raise UsageError(f"cannot resolve host folder {str(value)!r}: {exc}") from exc


def _guest_parts(guest: str) -> tuple[str, str]:
    drive, separator, rest = guest.partition(":")
    if not separator or drive.upper() != "C" or "\x00" in guest:
        raise WinBridgeError(f"invalid recorded guest path: {guest!r}")
    parts = [
        part for part in rest.replace("\\", "/").split("/") if part not in {"", "."}
    ]
    if len(parts) != 3 or parts[0].casefold() != "users" or parts[1] in {".", ".."}:
        raise WinBridgeError(f"invalid recorded guest path: {guest!r}")
    name = parts[2]
    if name in {".", ".."} or not _NAME_PATTERN.fullmatch(name):
        raise WinBridgeError(f"invalid recorded guest path: {guest!r}")
    return parts[1], name


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


def _same_target(first: Path, second: Path) -> bool:
    return _resolved(first) == _resolved(second)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _is_within(path: Path, root: Path) -> bool:
    return _resolved(path).is_relative_to(_resolved(root))


def _readlink(link: Path) -> str:
    try:
        return str(link.readlink())
    except OSError:
        return "an unknown target"
