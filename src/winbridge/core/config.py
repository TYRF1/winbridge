"""Global configuration in ``$XDG_CONFIG_HOME/winbridge/config.toml``."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from winbridge.core.errors import WinBridgeError
from winbridge.core.state import atomic_write_toml

__all__ = ["Config", "ConfigError"]

_BACKENDS = frozenset({"wine", "bottles"})


class ConfigError(WinBridgeError, ValueError):
    """The global config is unreadable or invalid."""


@dataclass
class Config:
    """Global defaults. CLI flags override them; profiles override them too."""

    backend: str = "wine"
    default_runner: str | None = None
    verify_timeout_s: int = 300

    @classmethod
    def load(cls, path: Path | str) -> Config:
        """Read config, returning defaults when the file does not exist."""
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            with target.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{target}: invalid TOML: {exc}") from exc

        backend = data.get("backend", "wine")
        if not isinstance(backend, str):
            raise ConfigError(f"{target}: backend must be a string")

        default_runner = data.get("default_runner")
        if default_runner is not None and not isinstance(default_runner, str):
            raise ConfigError(f"{target}: default_runner must be a string")

        verify_timeout_s = data.get("verify_timeout_s", 300)
        if isinstance(verify_timeout_s, bool) or not isinstance(verify_timeout_s, int):
            raise ConfigError(f"{target}: verify_timeout_s must be an integer")

        config = cls(
            backend=backend,
            default_runner=default_runner,
            verify_timeout_s=verify_timeout_s,
        )
        config.validate()
        return config

    def dump(self, path: Path | str) -> None:
        """Write the config atomically below ``path``."""
        self.validate()
        data: dict[str, Any] = {"backend": self.backend}
        if self.default_runner is not None:
            data["default_runner"] = self.default_runner
        data["verify_timeout_s"] = self.verify_timeout_s
        atomic_write_toml(path, data)

    def validate(self) -> None:
        """Raise :class:`ConfigError` when a value is outside its contract."""
        if self.backend not in _BACKENDS:
            raise ConfigError(
                f"backend must be one of {', '.join(sorted(_BACKENDS))}; "
                f"got {self.backend!r}"
            )
        if self.default_runner is not None and not self.default_runner.strip():
            raise ConfigError("default_runner must be null or a non-empty reference")
        if (
            isinstance(self.verify_timeout_s, bool)
            or not isinstance(self.verify_timeout_s, int)
            or self.verify_timeout_s <= 0
        ):
            raise ConfigError(
                f"verify_timeout_s must be a positive integer; got {self.verify_timeout_s!r}"
            )
