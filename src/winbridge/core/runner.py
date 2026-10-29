"""Embedded runner catalog, checksum-pinned store, and reuse detection.

Runner references are ``id@version`` for catalog entries and ``path:<root>``
for runners discovered in Bottles or Steam installations. Imported runners are
recorded by reference and never copied or modified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.locking import file_lock
from winbridge.core.state import Paths
from winbridge.util.download import download_verified

__all__ = [
    "DEFAULT_CATALOG_PATH",
    "INSTALL_MANIFEST_NAME",
    "Catalog",
    "CatalogError",
    "Runner",
    "RunnerError",
    "RunnerNotInstalled",
    "UnknownRunner",
    "UnsafeArchive",
    "WebView2Payload",
    "discover_runners",
    "import_runner",
    "install_runner",
    "resolve_runner",
    "runner_wine_path",
]

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.toml"

_LOGGER = logging.getLogger("winbridge")

#: Digest record written inside an installed runner root. Doctor compares the
#: current wine binary against this pinned value instead of trusting presence.
INSTALL_MANIFEST_NAME = ".winbridge-install.json"

_RUNNER_KINDS = frozenset({"wine", "soda", "path"})
_CATALOG_KINDS = frozenset({"wine", "soda"})
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_REUSE_WINE_RELPATHS = ("bin/wine", "files/bin/wine")


class RunnerError(WinBridgeError):
    """A runner reference, archive, or store entry is invalid."""


class RunnerNotInstalled(RunnerError):
    """The requested runner is not present in the store or on disk."""


class UnsafeArchive(WinBridgeError):
    """The archive contains a member that could escape the store."""


class CatalogError(RunnerError, ValueError):
    """The embedded catalog is unreadable or invalid."""


class UnknownRunner(CatalogError):
    """The catalog has no entry for the requested reference."""

    exit_code = UsageError.exit_code


def _is_component(value: str) -> bool:
    """Return whether ``value`` is safe to use as one path or reference part."""
    return (
        bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and not value.startswith(".")
        and not any(char in value for char in ("/", "\\", "\x00", "@"))
    )


def _check_component(value: str, name: str) -> None:
    if not _is_component(value):
        raise RunnerError(f"invalid runner {name}: {value!r}")


def _check_relpath(value: str, name: str) -> PurePosixPath:
    if not value or "\x00" in value or "\\" in value:
        raise RunnerError(f"invalid runner {name}: {value!r}")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or _DRIVE_PREFIX.match(value)
    ):
        raise RunnerError(f"invalid runner {name}: {value!r}")
    return path


def _check_download_url(url: str) -> None:
    if not url.startswith("https://") and not url.startswith("file://"):
        raise RunnerError(f"runner URL must use HTTPS; got {url!r}")


def _check_digest(digest: str) -> None:
    if not _SHA256_HEX.fullmatch(digest):
        raise RunnerError(f"runner SHA-256 must be 64 hexadecimal characters; got {digest!r}")


@dataclass(frozen=True, slots=True)
class Runner:
    """One catalog entry or one imported runner root."""

    id: str
    version: str
    kind: str
    url: str
    sha256: str
    wine_relpath: str
    archive_root: str = ""
    default: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _RUNNER_KINDS:
            raise RunnerError(
                f"runner kind must be one of {', '.join(sorted(_RUNNER_KINDS))}; "
                f"got {self.kind!r}"
            )
        _check_component(self.id, "id")
        _check_relpath(self.wine_relpath, "wine_relpath")
        if self.archive_root:
            _check_relpath(self.archive_root, "archive_root")

        if self.kind == "path":
            if self.sha256:
                raise RunnerError("imported path runners are not downloaded; sha256 must be empty")
            if self.default:
                raise RunnerError("an imported path runner cannot be the catalog default")
            if not self.url or not Path(self.url).is_absolute():
                raise RunnerError(f"runner path must be absolute; got {self.url!r}")
            return

        _check_component(self.version, "version")
        _check_download_url(self.url)
        _check_digest(self.sha256)

    @property
    def ref(self) -> str:
        """Return ``id@version`` or ``path:<root>`` for imported runners."""
        if self.kind == "path":
            return f"path:{self.url}"
        return f"{self.id}@{self.version}"


@dataclass(frozen=True, slots=True)
class WebView2Payload:
    """A checksum-pinned WebView2 runtime installer."""

    version: str
    url: str
    sha256: str
    released: str = ""

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise RunnerError("WebView2 payload version must not be empty")
        if not self.url.startswith("https://"):
            raise RunnerError(f"WebView2 payload URL must use HTTPS; got {self.url!r}")
        _check_digest(self.sha256)
        if self.released:
            try:
                date.fromisoformat(self.released)
            except ValueError as exc:
                raise RunnerError(
                    f"WebView2 payload release date must be ISO 8601; "
                    f"got {self.released!r}"
                ) from exc


@dataclass(frozen=True, slots=True)
class Catalog:
    """The packaged runner catalog: runners plus the WebView2 payloads."""

    runners: tuple[Runner, ...]
    webview2: WebView2Payload
    latest_webview2_url: str

    def get(self, ref: str) -> Runner:
        """Return the catalog entry for ``id@version``."""
        value = ref.strip()
        if value.startswith("path:"):
            raise UnknownRunner(
                "path runners are not catalog entries; use the path reference directly"
            )
        if "@" not in value:
            raise UnknownRunner(f"runner reference must be 'id@version'; got {ref!r}")
        runner_id, version = value.rsplit("@", 1)
        if not runner_id or not version:
            raise UnknownRunner(f"runner reference must be 'id@version'; got {ref!r}")
        for runner in self.runners:
            if runner.id == runner_id and runner.version == version:
                return runner
        raise UnknownRunner(f"no runner {value!r} in the catalog; run 'winbridge runners list'")

    def default_runner(self) -> Runner:
        """Return the catalog's marked default runner."""
        for runner in self.runners:
            if runner.default:
                return runner
        raise CatalogError("catalog has no default runner")

    @classmethod
    def load(cls, path: Path | str) -> Catalog:
        """Read and validate a catalog TOML file."""
        target = Path(path)
        if not target.is_file():
            raise CatalogError(f"catalog not found: {target}")
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CatalogError(f"{target}: invalid catalog TOML: {exc}") from exc

        schema = data.get("schema")
        if schema != 1:
            raise CatalogError(f"{target}: unsupported catalog schema {schema!r}; expected 1")

        raw_runners = data.get("runners")
        if not isinstance(raw_runners, list) or not raw_runners:
            raise CatalogError(f"{target}: catalog must contain at least one runner")
        runners = tuple(_parse_runner(entry, target) for entry in raw_runners)

        refs: set[str] = set()
        for runner in runners:
            if runner.ref in refs:
                raise CatalogError(f"{target}: duplicate runner reference {runner.ref!r}")
            refs.add(runner.ref)

        defaults = [runner for runner in runners if runner.default]
        if len(defaults) != 1:
            raise CatalogError(
                f"{target}: catalog must mark exactly one default runner; found {len(defaults)}"
            )

        payload = _parse_webview2(data.get("webview2"), target)
        latest = _require_string(data, "latest_webview2_url", target)
        if not latest.startswith("https://"):
            raise CatalogError(f"{target}: latest_webview2_url must use HTTPS")

        return cls(runners=runners, webview2=payload, latest_webview2_url=latest)


def _require_string(data: dict[str, Any], key: str, context: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{context}: {key} must be a non-empty string")
    return value


def _parse_runner(data: object, context: Path) -> Runner:
    if not isinstance(data, dict):
        raise CatalogError(f"{context}: every [[runners]] entry must be a table")
    entry: dict[str, Any] = data
    runner_id = _require_string(entry, "id", context)
    version = _require_string(entry, "version", context)
    kind = _require_string(entry, "kind", context)
    url = _require_string(entry, "url", context)
    sha256 = _require_string(entry, "sha256", context)
    wine_relpath = _require_string(entry, "wine_relpath", context)
    archive_root = entry.get("archive_root", "")
    notes = entry.get("notes", "")
    default = entry.get("default", False)
    label = f"{context}: runner {runner_id}@{version}"

    if kind not in _CATALOG_KINDS:
        raise CatalogError(
            f"{label}: kind must be one of {', '.join(sorted(_CATALOG_KINDS))}; got {kind!r}"
        )
    if not url.startswith("https://"):
        raise CatalogError(f"{label}: URL must use HTTPS")
    if not isinstance(archive_root, str) or not isinstance(notes, str):
        raise CatalogError(f"{label}: archive_root and notes must be strings")
    if not isinstance(default, bool):
        raise CatalogError(f"{label}: default must be a boolean")

    try:
        return Runner(
            runner_id,
            version,
            kind,
            url,
            sha256,
            wine_relpath,
            archive_root=archive_root,
            default=default,
            notes=notes,
        )
    except RunnerError as exc:
        raise CatalogError(f"{label}: {exc}") from exc


def _parse_webview2(data: object, context: Path) -> WebView2Payload:
    if not isinstance(data, dict):
        raise CatalogError(f"{context}: missing [webview2] table")
    entry: dict[str, Any] = data
    version = _require_string(entry, "version", context)
    url = _require_string(entry, "url", context)
    sha256 = _require_string(entry, "sha256", context)
    released = entry.get("released", "")
    if not isinstance(released, str):
        raise CatalogError(f"{context}: webview2.released must be a string")
    try:
        return WebView2Payload(version, url, sha256, released)
    except RunnerError as exc:
        raise CatalogError(f"{context}: webview2: {exc}") from exc


def _private_dir(directory: Path) -> None:
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        directory.chmod(0o700)


def runner_wine_path(root: Path | str, runner: Runner) -> Path:
    """Return the runner's ``wine`` binary below ``root``, refusing escapes."""
    base = Path(root)
    relative = _check_relpath(runner.wine_relpath, "wine_relpath")
    candidate = base.joinpath(*relative.parts)
    if not candidate.resolve().is_relative_to(base.resolve()):
        raise RunnerError(f"runner wine path escapes its root: {runner.wine_relpath!r}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_install_manifest(root: Path, runner: Runner, wine: Path) -> None:
    """Pin the archive and installed-wine digests inside an unpublished tree.

    The manifest is written before the atomic publish, so a reader can never see
    an installed runner with a half-written record. Doctor treats a missing
    manifest as "not verifiable" rather than trusting file presence.
    """
    try:
        payload = {
            "schema": 1,
            "ref": runner.ref,
            "archive_sha256": runner.sha256,
            "wine_relpath": runner.wine_relpath,
            "wine_sha256": _sha256_file(wine),
            "installed_at": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        target = root / INSTALL_MANIFEST_NAME
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o600)
    except OSError as exc:
        raise RunnerError(f"cannot write the install manifest for {runner.ref}: {exc}") from exc


def _member_target(root: Path, name: str) -> Path:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or _DRIVE_PREFIX.match(name)
    ):
        raise UnsafeArchive(f"archive member has an unsafe name: {name!r}")
    relative = PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise UnsafeArchive(f"archive member escapes the extraction root: {name!r}")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise UnsafeArchive(f"archive member escapes the extraction root: {name!r}")
    return target


def _member_mode(mode: int) -> int:
    """Keep owner permissions, drop group/other writes and all special bits."""
    permissions = mode & 0o777
    if not permissions:
        return 0o644
    return permissions & 0o755


def _symlink_linkname(member: tarfile.TarInfo) -> None:
    """Allow only relative, in-tree symlink targets (Wine multicall aliases)."""
    linkname = member.linkname
    if (
        not linkname
        or linkname == "."
        or "\x00" in linkname
        or "\\" in linkname
        or linkname.startswith("/")
        or _DRIVE_PREFIX.match(linkname)
    ):
        raise UnsafeArchive(f"archive symlink {member.name!r} has an unsafe target: {linkname!r}")
    relative = PurePosixPath(linkname)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise UnsafeArchive(f"archive symlink {member.name!r} escapes the extraction root")


def _extract_tarball(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            planned: list[tuple[tarfile.TarInfo, Path]] = []
            for member in members:
                if not (member.isreg() or member.isdir() or member.issym()):
                    raise UnsafeArchive(
                        f"archive member {member.name!r} is not a regular file, "
                        "directory, or in-tree symlink"
                    )
                if member.issym():
                    _symlink_linkname(member)
                planned.append((member, _member_target(destination, member.name)))
            for member, target in planned:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    os.symlink(member.linkname, target)
                    continue
                source = tar.extractfile(member)
                if source is None:
                    raise UnsafeArchive(f"archive member {member.name!r} has no data")
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                target.chmod(_member_mode(member.mode))
    except tarfile.TarError as exc:
        raise RunnerError(f"cannot read runner archive {archive.name}: {exc}") from exc
    except OSError as exc:
        raise RunnerError(f"cannot extract runner archive {archive.name}: {exc}") from exc


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _publish(content: Path, target: Path, parent: Path, token: str) -> None:
    """Move ``content`` into ``target``, quarantining an incomplete entry first."""
    stale: Path | None = None
    if target.exists() or target.is_symlink():
        stale = parent / f".stale-{target.name}-{token}"
        os.replace(target, stale)
    try:
        os.replace(content, target)
    except BaseException:
        if stale is not None and not target.exists():
            os.replace(stale, target)
        raise
    if stale is not None:
        _remove(stale)


def install_runner(runner: Runner, paths: Paths) -> Path:
    """Download, verify, and atomically install ``runner`` into the store.

    The per-runner lock serializes concurrent installs. An existing complete
    install is returned without touching the network.
    """
    if runner.kind == "path":
        raise RunnerError(
            f"{runner.ref} is imported by reference; there is nothing to install"
        )

    parent = paths.runners / runner.id
    target = parent / runner.version
    _private_dir(parent)
    _private_dir(paths.cache)

    with file_lock(paths.locks / f"runner-{runner.id}.lock"):
        installed = runner_wine_path(target, runner)
        if installed.is_file():
            _LOGGER.info("[runner] %s is already installed at %s", runner.ref, target)
            return target

        token = secrets.token_hex(4)
        archive = paths.cache / f"{runner.id}-{runner.version}.archive"
        temporary = parent / f".tmp-{runner.version}-{token}"
        try:
            _LOGGER.info(
                "[runner] downloading %s from %s (sha256 %s)",
                runner.ref,
                runner.url,
                runner.sha256,
            )
            download_verified(runner.url, runner.sha256, archive)
            _LOGGER.info("[runner] downloaded and verified %s", archive.name)
            temporary.mkdir(mode=0o700)
            _extract_tarball(archive, temporary)
            _LOGGER.info("[runner] extracted %s into %s", archive.name, temporary)

            content = temporary
            if runner.archive_root:
                content = temporary / PurePosixPath(runner.archive_root)
                if content.is_symlink() or not content.is_dir():
                    raise UnsafeArchive(
                        f"archive for {runner.ref} has no {runner.archive_root!r} root"
                    )
            wine = runner_wine_path(content, runner)
            if not wine.is_file():
                raise RunnerError(
                    f"archive for {runner.ref} does not contain {runner.wine_relpath}"
                )
            _write_install_manifest(content, runner, wine)

            _publish(content, target, parent, token)
            installed_wine = runner_wine_path(target, runner)
            installed_wine.chmod(installed_wine.stat().st_mode | 0o755)
            _LOGGER.info("[runner] installed %s at %s", runner.ref, target)
            return target
        finally:
            _remove(temporary)
            archive.unlink(missing_ok=True)


def resolve_runner(ref: str, paths: Paths, catalog: Catalog) -> Path:
    """Resolve ``id@version``, ``path:<root>``, or ``""`` (the default) to a root."""
    value = ref.strip()
    if not value:
        value = catalog.default_runner().ref

    if value.startswith("path:"):
        raw = value[len("path:") :]
        root = Path(raw)
        if not raw or not root.is_absolute():
            raise UnknownRunner(f"path runner reference must be absolute; got {value!r}")
        if not root.is_dir():
            raise RunnerNotInstalled(f"runner path not found: {root}")
        return root

    runner = catalog.get(value)
    root = paths.runners / runner.id / runner.version
    if not runner_wine_path(root, runner).is_file():
        raise RunnerNotInstalled(
            f"runner {value} is not installed; run 'winbridge runners install {value}'"
        )
    return root


def _reuse_roots(home: Path) -> list[Path]:
    """Directories that may already hold usable runners."""
    return [
        home / ".var/app/com.usebottles.bottles/data/bottles/runners",
        home / ".local/share/Steam/compatibilitytools.d",
        home / ".steam/steam/compatibilitytools.d",
        home / ".steam/root/compatibilitytools.d",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d",
        home / ".var/app/com.valvesoftware.Steam/data/Steam/compatibilitytools.d",
    ]


def _wine_relpath(root: Path) -> str | None:
    for relative in _REUSE_WINE_RELPATHS:
        if (root / relative).is_file():
            return relative
    return None


def discover_runners(paths: Paths) -> list[Path]:
    """List runner roots in Bottles and Steam directories, read-only."""
    store = paths.runners.resolve()
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate_root in _reuse_roots(Path.home()):
        if not candidate_root.is_dir():
            continue
        try:
            candidates = sorted(candidate_root.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            resolved = candidate.resolve()
            if resolved == store or resolved.is_relative_to(store):
                continue
            if _wine_relpath(candidate) is None or resolved in seen:
                continue
            seen.add(resolved)
            found.append(candidate)
    found.sort(key=str)
    return found


def import_runner(path: Path | str) -> Runner:
    """Record an existing runner directory by reference; never copy or mutate."""
    root = Path(path)
    if not root.is_absolute():
        raise RunnerError(f"runner path must be absolute; got {str(path)!r}")
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise RunnerNotInstalled(f"runner path not found: {root}")

    runner_id = root.name if _is_component(root.name) else "imported"
    return Runner(
        runner_id,
        "",
        "path",
        str(root),
        "",
        _wine_relpath(root) or "bin/wine",
        notes="imported by reference; never copied or modified",
    )
