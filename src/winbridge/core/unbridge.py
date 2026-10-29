"""Clean removal of every artifact WinBridge created for one app.

``unbridge`` is deliberately the inverse of setup: the prefix is stopped and
deleted, the profile and both desktop entries disappear, the scheme handler and
its ``mimeapps.list`` lines are unregistered, and the exact bare desktop id is
removed from the bar. Nothing outside WinBridge's own directories is touched;
an exposed host folder is only ever a symlink inside the prefix, so deleting
the prefix removes the link and leaves the host directory intact.

Every artifact is derived from the app id rather than from the profile. A
profile that is already gone, a missing prefix, a missing entry, or a missing
pin is therefore a warning, and a second run is a no-op instead of an error.
The default is to refuse until the caller has confirmed; the CLI prompts and
passes ``confirm=False`` only after an explicit yes or ``--yes``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from winbridge.backends.base import Backend
from winbridge.core import launcher, omarchy, urlbridge
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.prefix import kill_prefix
from winbridge.core.profile import Profile
from winbridge.core.state import Paths

__all__ = ["NoopBackend", "unbridge"]

_LOGGER = logging.getLogger("winbridge")


class NoopBackend:
    """A backend for cleanup when there is no prefix left to touch.

    ``unbridge`` skips the backend entirely for an absent prefix, so the CLI
    does not need a runner just to remove metadata. Every operation that would
    touch a prefix fails loudly: if a prefix appears between the caller's check
    and cleanup, it must not be deleted without a real backend.
    """

    name = "none"

    def create_prefix(self, app_id: str, *, keep_z: bool = False) -> Path:
        raise WinBridgeError("no backend is available to create a prefix during unbridge")

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout_s
        raise _missing_runner(prefix)

    def install(
        self,
        prefix: Path,
        installer: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        del log_path, timeout_s
        raise WinBridgeError("no backend is available to install during unbridge")

    def run(self, prefix: Path, exe: str, argv: list[str]) -> subprocess.Popen[bytes]:
        raise WinBridgeError("no backend is available to run an app during unbridge")

    def expose(self, prefix: Path, host: Path, guest: str) -> None:
        raise WinBridgeError("no backend is available to expose a folder during unbridge")

    def remove(self, prefix: Path) -> None:
        raise _missing_runner(prefix)


def unbridge(
    app_id: str,
    paths: Paths,
    backend: Backend,
    *,
    desktop: Any = None,
    confirm: bool = True,
    restart_shell: bool = True,
) -> None:
    """Remove every WinBridge-owned artifact for ``app_id``.

    ``desktop`` owns the pin when supplied; otherwise the exact
    ``winbridge-<id>`` entry is removed from omarchy-shell's default config.
    ``confirm=True`` refuses before any side effect; the CLI prompts first and
    then passes ``confirm=False``. ``restart_shell`` is only acted on when a pin
    was actually removed, because that is the only change the bar must see.
    """
    if confirm:
        raise UsageError(
            f"unbridge {app_id!r} requires confirmation; re-run with --yes"
        )

    desktop_id = launcher.desktop_id(app_id)
    profile = _load_profile(app_id, paths)
    if profile is not None:
        _LOGGER.info(
            "[unbridge] app %s (%s), profile %s",
            profile.id,
            profile.name,
            paths.profiles / f"{app_id}.toml",
        )

    prefix = paths.prefix_for(app_id)
    _remove_prefix(app_id, prefix, paths, backend)
    _remove_profile(app_id, paths)
    _remove_desktop_entries(app_id, desktop_id, paths)
    pin_removed = _remove_pin(desktop_id, desktop)

    if pin_removed and restart_shell:
        omarchy.restart_shell(enabled=True)

    _LOGGER.info("[unbridge] %s is unbridged", app_id)


# --- internals --------------------------------------------------------------


def _missing_runner(prefix: Path) -> WinBridgeError:
    """Explain why a metadata-only cleanup cannot touch a newly appeared prefix."""
    return WinBridgeError(
        f"runner missing: no backend is available to stop or remove {prefix}; "
        "install the runner and retry"
    )


def _load_profile(app_id: str, paths: Paths) -> Profile | None:
    """Read the profile for logging, or warn and continue when it is unusable.

    Cleanup never trusts profile fields: the app id is the only input used to
    locate artifacts. A corrupt profile therefore must not block removing the
    prefix, entries, registration, and pin that still belong to the app.
    """
    target = paths.profiles / f"{app_id}.toml"
    if not target.is_file():
        _LOGGER.warning(
            "[unbridge] no profile for %s at %s; removing remaining artifacts",
            app_id,
            target,
        )
        return None
    try:
        return Profile.load(target)
    except (OSError, WinBridgeError) as exc:
        _LOGGER.warning("[unbridge] cannot read profile %s: %s", target, exc)
        return None


def _remove_prefix(
    app_id: str,
    prefix: Path,
    paths: Paths,
    backend: Backend,
) -> bool:
    """Stop and delete the prefix, refusing anything outside the owned store."""
    _check_owned_prefix(app_id, prefix, paths)
    if not prefix.exists():
        _LOGGER.warning("[unbridge] no prefix at %s", prefix)
        return False
    if not prefix.is_dir():
        raise UsageError(f"refusing to remove non-directory prefix {prefix}")

    _LOGGER.info("[unbridge] stopping processes in %s", prefix)
    kill_prefix(backend, prefix)
    _LOGGER.info("[unbridge] removing prefix %s", prefix)
    backend.remove(prefix)
    if prefix.exists():
        raise WinBridgeError(f"backend did not remove prefix {prefix}")
    return True


def _check_owned_prefix(app_id: str, prefix: Path, paths: Paths) -> None:
    """Reject a prefix that is a symlink or resolves outside ``paths.prefixes``.

    The backend repeats this check, but doing it here means no process is
    signalled and no profile is touched when the path is not ours.
    """
    if prefix.is_symlink():
        raise UsageError(
            f"refusing to unbridge {app_id!r}: prefix {prefix} is a symlink"
        )
    store = paths.prefixes.resolve()
    resolved = prefix.resolve()
    if resolved == store or not resolved.is_relative_to(store):
        raise UsageError(
            f"refusing to unbridge {app_id!r}: prefix {prefix} is outside "
            f"{paths.prefixes}"
        )


def _remove_profile(app_id: str, paths: Paths) -> None:
    """Remove the profile and setup's ``--force`` backup, exact names only."""
    _remove_file(paths.profiles / f"{app_id}.toml", "profile")
    _remove_file(paths.profiles / f"{app_id}.toml.bak", "profile backup")


def _remove_desktop_entries(app_id: str, desktop_id: str, paths: Paths) -> None:
    """Delete the launcher, icon, handler, and our mimeapps associations."""
    entry = paths.applications / f"{desktop_id}.desktop"
    icon = paths.applications / f"{desktop_id}.png"
    handler = paths.applications / f"{desktop_id}-handler.desktop"
    if not _present(entry):
        _LOGGER.warning("[unbridge] no launcher entry at %s", entry)
    if not _present(handler):
        _LOGGER.warning("[unbridge] no scheme handler entry at %s", handler)

    _remove_file(icon, "icon")
    launcher.remove_desktop_entry(paths, app_id)
    urlbridge.unregister_app_schemes(app_id, paths)
    _LOGGER.info(
        "[unbridge] cleaned launcher, icon, and scheme registration for %s", app_id
    )


def _remove_file(target: Path, label: str) -> bool:
    """Unlink one exact owned file; a missing file is a no-op.

    A directory at the same name is never recursed into. The path is built by
    the caller from a validated app id and a fixed suffix, so this only ever
    reaches WinBridge-owned names such as ``winbridge-<id>.png`` and
    ``<id>.toml.bak``; unrelated files in the same directories stay untouched.
    """
    if target.is_symlink() or target.is_file():
        try:
            target.unlink()
        except OSError as exc:
            raise WinBridgeError(f"cannot remove {label} {target}: {exc}") from exc
        _LOGGER.info("[unbridge] removed %s %s", label, target)
        return True
    if target.exists():
        _LOGGER.warning(
            "[unbridge] refusing to remove non-file %s %s", label, target
        )
    return False


def _present(path: Path) -> bool:
    """Count a dangling symlink as present so its removal is still reported."""
    return path.is_symlink() or path.exists()


def _remove_pin(desktop_id: str, desktop: Any) -> bool:
    """Remove the exact bare desktop id through the adapter or omarchy.

    A missing pin is normal. A failed write is a warning rather than a hard
    failure: the app's files must still be removed, and the leftover pin can be
    inspected separately. A failing desktop adapter never blocks cleanup.

    Only an adapter that actually advertises ``pin`` may own the rewrite. X11
    and unsupported sessions expose no pin capability, so cleanup must still
    reach omarchy's config explicitly; otherwise a pin left by an earlier
    Hyprland session would survive ``unbridge`` from a TTY or X11 login.
    """
    capabilities: set[str] = getattr(desktop, "capabilities", set())
    if desktop is None or "pin" not in capabilities:
        try:
            removed = bool(omarchy.unpin_app(omarchy.DEFAULT_SHELL_CONFIG, desktop_id))
        except (OSError, WinBridgeError) as exc:
            _LOGGER.warning("[unbridge] could not remove pin %s: %s", desktop_id, exc)
            return False
    else:
        unpin = getattr(desktop, "unpin", None)
        if not callable(unpin):
            _LOGGER.warning(
                "[unbridge] desktop adapter cannot remove pin %s", desktop_id
            )
            return False
        try:
            removed = bool(unpin(desktop_id))
        except (OSError, WinBridgeError) as exc:
            _LOGGER.warning("[unbridge] could not remove pin %s: %s", desktop_id, exc)
            return False

    if removed:
        _LOGGER.info("[unbridge] removed pin %s", desktop_id)
    else:
        _LOGGER.warning("[unbridge] no pin %s to remove", desktop_id)
    return removed
