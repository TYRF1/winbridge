"""Versioned per-app profiles.

Schema 1 is additive: unknown top-level keys are kept in :attr:`Profile.extra`
and written back on every dump. Never drop a key silently.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from winbridge.core.errors import WinBridgeError
from winbridge.core.state import atomic_write_toml

__all__ = [
    "ExposedPath",
    "InstallerSpec",
    "LastVerified",
    "PinSpec",
    "Profile",
    "ProfileError",
    "WebView2Runtime",
]

SCHEMA_VERSION = 1
_BACKENDS = frozenset({"wine", "bottles"})
_KNOWN_FIELDS = frozenset(
    {
        "schema",
        "id",
        "name",
        "backend",
        "runner",
        "installer",
        "exe",
        "schemes",
        "exposed",
        "pin",
        "keep_z_drive",
        "webview2",
        "created_at",
        "last_verified",
    }
)
_KNOWN_INSTALLER_FIELDS = frozenset({"path", "dir", "glob", "type", "silent_args"})
_KNOWN_EXPOSED_FIELDS = frozenset({"host", "guest"})
_KNOWN_PIN_FIELDS = frozenset({"match"})
_KNOWN_WEBVIEW2_FIELDS = frozenset({"version", "source", "installed_at"})
_KNOWN_VERIFIED_FIELDS = frozenset(
    {"app_version", "result", "first_paint_s", "at"}
)


class ProfileError(WinBridgeError, ValueError):
    """A profile is unreadable or violates schema 1."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class InstallerSpec:
    path: str
    dir: str
    glob: str
    type: str
    silent_args: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExposedPath:
    host: str
    guest: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PinSpec:
    match: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LastVerified:
    app_version: str
    result: str
    first_paint_s: float | None
    at: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebView2Runtime:
    """The WebView2 runtime observed in the prefix when setup last ran."""

    version: str
    source: str = ""
    installed_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Profile:
    schema: int
    id: str
    name: str
    backend: str = "wine"
    runner: str = ""
    installer: InstallerSpec | None = None
    exe: str = ""
    schemes: list[str] = field(default_factory=list)
    exposed: list[ExposedPath] = field(default_factory=list)
    pin: PinSpec | None = None
    keep_z_drive: bool = False
    webview2: WebView2Runtime | None = None
    created_at: str = field(default_factory=_utc_now)
    last_verified: LastVerified | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> Profile:
        """Read and validate a profile from TOML."""
        source = Path(path)
        try:
            with source.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"{source}: invalid TOML: {exc}") from exc
        except FileNotFoundError:
            # Callers rely on the POSIX missing-file signal; a profile listing
            # treats it as unreadable in the per-file catch.
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileError(f"{source}: cannot read profile: {exc}") from exc

        profile = cls._from_mapping(data, source)
        profile.validate()
        return profile

    @classmethod
    def _from_mapping(cls, data: Mapping[str, Any], source: Path) -> Profile:
        schema = data.get("schema")
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise ProfileError(f"{source}: schema must be the integer 1")

        installer_table = _optional_table(data, "installer", source)
        pin_table = _optional_table(data, "pin", source)
        verified_table = _optional_table(data, "last_verified", source)
        webview2_table = _optional_table(data, "webview2", source)

        return cls(
            schema=schema,
            id=_string(data, "id", source),
            name=_string(data, "name", source),
            backend=_string(data, "backend", source, default="wine"),
            runner=_string(data, "runner", source),
            installer=(
                None if installer_table is None else _parse_installer(installer_table, source)
            ),
            exe=_string(data, "exe", source),
            schemes=_string_list(data, "schemes", source),
            exposed=_parse_exposed(data, source),
            pin=None if pin_table is None else _parse_pin(pin_table, source),
            keep_z_drive=_boolean(data, "keep_z_drive", source),
            webview2=(
                None
                if webview2_table is None
                else _parse_webview2(webview2_table, source)
            ),
            created_at=_string(data, "created_at", source),
            last_verified=(
                None if verified_table is None else _parse_verified(verified_table, source)
            ),
            extra={key: value for key, value in data.items() if key not in _KNOWN_FIELDS},
        )

    def validate(self) -> None:
        """Raise :class:`ProfileError` unless the profile matches schema 1."""
        if self.schema != SCHEMA_VERSION:
            raise ProfileError(
                f"profile {self.id!r}: unsupported schema {self.schema}; "
                f"expected {SCHEMA_VERSION}"
            )
        if not self.id or not self.id.strip():
            raise ProfileError("profile id must be non-empty")
        if not self.name or not self.name.strip():
            raise ProfileError(f"profile {self.id!r}: name must be non-empty")
        if self.backend not in _BACKENDS:
            raise ProfileError(
                f"profile {self.id!r}: backend must be one of "
                f"{', '.join(sorted(_BACKENDS))}; got {self.backend!r}"
            )
        if not _is_runner_ref(self.runner):
            raise ProfileError(
                f"profile {self.id!r}: runner must be 'id@version' or "
                f"'path:/absolute/runner'; got {self.runner!r}"
            )
        _reject_reserved(self.extra, _KNOWN_FIELDS, label="extra", app_id=self.id)
        if self.installer is not None:
            _reject_reserved(
                self.installer.extra,
                _KNOWN_INSTALLER_FIELDS,
                label="installer.extra",
                app_id=self.id,
            )
        if self.pin is not None:
            _reject_reserved(
                self.pin.extra,
                _KNOWN_PIN_FIELDS,
                label="pin.extra",
                app_id=self.id,
            )
        if self.webview2 is not None:
            if not self.webview2.version.strip():
                raise ProfileError(
                    f"profile {self.id!r}: webview2.version must be non-empty"
                )
            _reject_reserved(
                self.webview2.extra,
                _KNOWN_WEBVIEW2_FIELDS,
                label="webview2.extra",
                app_id=self.id,
            )
        if self.last_verified is not None:
            _reject_reserved(
                self.last_verified.extra,
                _KNOWN_VERIFIED_FIELDS,
                label="last_verified.extra",
                app_id=self.id,
            )
        for index, exposed in enumerate(self.exposed):
            _reject_reserved(
                exposed.extra,
                _KNOWN_EXPOSED_FIELDS,
                label=f"exposed[{index}].extra",
                app_id=self.id,
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the TOML-shaped mapping, including unknown keys."""
        data: dict[str, Any] = {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "runner": self.runner,
        }
        if self.installer is not None:
            data["installer"] = _merge_unknown(
                {
                    "path": self.installer.path,
                    "dir": self.installer.dir,
                    "glob": self.installer.glob,
                    "type": self.installer.type,
                    "silent_args": list(self.installer.silent_args),
                },
                self.installer.extra,
            )
        data["exe"] = self.exe
        data["schemes"] = list(self.schemes)
        data["exposed"] = [
            _merge_unknown(
                {"host": exposed.host, "guest": exposed.guest},
                exposed.extra,
            )
            for exposed in self.exposed
        ]
        if self.pin is not None:
            data["pin"] = _merge_unknown({"match": self.pin.match}, self.pin.extra)
        data["keep_z_drive"] = self.keep_z_drive
        if self.webview2 is not None:
            data["webview2"] = _merge_unknown(
                {
                    "version": self.webview2.version,
                    "source": self.webview2.source,
                    "installed_at": self.webview2.installed_at,
                },
                self.webview2.extra,
            )
        data["created_at"] = self.created_at
        if self.last_verified is not None:
            verified: dict[str, Any] = {
                "app_version": self.last_verified.app_version,
                "result": self.last_verified.result,
            }
            if self.last_verified.first_paint_s is not None:
                verified["first_paint_s"] = self.last_verified.first_paint_s
            verified["at"] = self.last_verified.at
            data["last_verified"] = _merge_unknown(verified, self.last_verified.extra)
        for key, value in self.extra.items():
            if key not in _KNOWN_FIELDS:
                data[key] = value
        return data

    def dump(self, path: Path | str) -> None:
        """Validate and atomically write the profile."""
        self.validate()
        atomic_write_toml(path, self.to_dict())


def _unknown(data: Mapping[str, Any], reserved: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in reserved}


def _merge_unknown(known: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(known)
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
    return merged


def _reject_reserved(
    extra: Mapping[str, Any],
    reserved: frozenset[str],
    *,
    label: str,
    app_id: str,
) -> None:
    collisions = sorted(reserved & extra.keys())
    if collisions:
        raise ProfileError(
            f"profile {app_id!r}: {label} contains reserved key(s): "
            f"{', '.join(collisions)}"
        )


def _is_runner_ref(reference: str) -> bool:
    if not reference:
        return False
    if reference.startswith("path:"):
        candidate = reference[len("path:") :]
        return bool(candidate) and Path(candidate).is_absolute()
    namespace, separator, version = reference.partition("@")
    return bool(separator and namespace and version) and not any(
        character.isspace() for character in reference
    )


def _optional_table(
    data: Mapping[str, Any], key: str, source: Path
) -> Mapping[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProfileError(f"{source}: {key} must be a table")
    return value


def _string(
    data: Mapping[str, Any],
    key: str,
    source: Path,
    *,
    default: str = "",
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ProfileError(f"{source}: {key} must be a string")
    return value


def _string_list(data: Mapping[str, Any], key: str, source: Path) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProfileError(f"{source}: {key} must be a list of strings")
    return list(value)


def _parse_installer(data: Mapping[str, Any], source: Path) -> InstallerSpec:
    return InstallerSpec(
        path=_string(data, "path", source),
        dir=_string(data, "dir", source),
        glob=_string(data, "glob", source),
        type=_string(data, "type", source),
        silent_args=_string_list(data, "silent_args", source),
        extra=_unknown(data, _KNOWN_INSTALLER_FIELDS),
    )


def _parse_pin(data: Mapping[str, Any], source: Path) -> PinSpec:
    return PinSpec(
        match=_string(data, "match", source),
        extra=_unknown(data, _KNOWN_PIN_FIELDS),
    )


def _parse_exposed(data: Mapping[str, Any], source: Path) -> list[ExposedPath]:
    value = data.get("exposed", [])
    if not isinstance(value, list):
        raise ProfileError(f"{source}: exposed must be a list of tables")
    exposed: list[ExposedPath] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ProfileError(f"{source}: exposed entries must be tables")
        exposed.append(
            ExposedPath(
                host=_string(entry, "host", source),
                guest=_string(entry, "guest", source),
                extra=_unknown(entry, _KNOWN_EXPOSED_FIELDS),
            )
        )
    return exposed


def _boolean(data: Mapping[str, Any], key: str, source: Path) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ProfileError(f"{source}: {key} must be a boolean")
    return value


def _parse_webview2(data: Mapping[str, Any], source: Path) -> WebView2Runtime:
    return WebView2Runtime(
        version=_string(data, "version", source),
        source=_string(data, "source", source),
        installed_at=_string(data, "installed_at", source),
        extra=_unknown(data, _KNOWN_WEBVIEW2_FIELDS),
    )


def _parse_verified(data: Mapping[str, Any], source: Path) -> LastVerified:
    paint = data.get("first_paint_s")
    if paint is not None and (isinstance(paint, bool) or not isinstance(paint, (int, float))):
        raise ProfileError(f"{source}: last_verified.first_paint_s must be a number")
    return LastVerified(
        app_version=_string(data, "app_version", source),
        result=_string(data, "result", source),
        first_paint_s=None if paint is None else float(paint),
        at=_string(data, "at", source),
        extra=_unknown(data, _KNOWN_VERIFIED_FIELDS),
    )
