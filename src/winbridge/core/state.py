"""XDG state layout and atomic TOML persistence."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

__all__ = ["Paths", "atomic_write_toml"]


def _xdg_dir(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else fallback


def _mkdir_private(directory: Path) -> None:
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        # mkdir's mode is filtered by umask; be explicit for fresh state.
        directory.chmod(0o700)


@dataclass(frozen=True, slots=True)
class Paths:
    """Every state location the CLI uses, rooted in XDG directories."""

    root: Path
    config_home: Path
    data_home: Path
    config: Path
    profiles: Path
    prefixes: Path
    runners: Path
    cache: Path
    evidence: Path
    logs: Path
    locks: Path
    applications: Path
    bundles: Path

    @classmethod
    def default(cls) -> Paths:
        """Resolve the real XDG layout for the current user."""
        home = Path.home()
        config_home = _xdg_dir("XDG_CONFIG_HOME", home / ".config")
        data_home = _xdg_dir("XDG_DATA_HOME", home / ".local" / "share")
        state = data_home / "winbridge"
        return cls(
            root=data_home,
            config_home=config_home,
            data_home=data_home,
            config=config_home / "winbridge" / "config.toml",
            profiles=state / "profiles",
            prefixes=state / "prefixes",
            runners=state / "runners",
            cache=state / "cache" / "downloads",
            evidence=state / "evidence",
            logs=state / "logs",
            locks=state / "locks",
            applications=data_home / "applications",
            bundles=state / "bundles",
        )

    @classmethod
    def from_root(cls, root: Path | str) -> Paths:
        """Put every path under ``root``; used by tests and disposable runs."""
        base = Path(root)
        return cls(
            root=base,
            config_home=base,
            data_home=base,
            config=base / "config.toml",
            profiles=base / "profiles",
            prefixes=base / "prefixes",
            runners=base / "runners",
            cache=base / "cache" / "downloads",
            evidence=base / "evidence",
            logs=base / "logs",
            locks=base / "locks",
            applications=base / "applications",
            bundles=base / "bundles",
        )

    def ensure(self) -> None:
        """Create the directories WinBridge owns, privately and idempotently."""
        _mkdir_private(self.config.parent)
        for directory in (
            self.profiles,
            self.prefixes,
            self.runners,
            self.cache,
            self.evidence,
            self.logs,
            self.locks,
            self.applications,
            self.bundles,
        ):
            _mkdir_private(directory)

    def prefix_for(self, app_id: str) -> Path:
        """Return the Wine prefix path for ``app_id``."""
        return self.prefixes / app_id


def atomic_write_toml(path: Path | str, data: Mapping[str, Any]) -> None:
    """Write ``data`` as TOML to ``path`` via a same-directory temp file."""
    target = Path(path)
    _mkdir_private(target.parent)
    payload = tomli_w.dumps(dict(data))
    fd, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise
