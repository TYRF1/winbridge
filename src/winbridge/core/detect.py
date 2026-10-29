r"""Post-install detection: files, executables, URL schemes, and metadata.

The setup pipeline snapshots the program directories before and after the
installer runs. A new or changed executable is rank-ordered by name similarity
to the installer, location depth, size, PE subsystem, and installer shortcut
targets; a clear winner is recorded automatically, while an ambiguous set
returns ``None`` so the caller can prompt or require ``--exe``. URL handlers and
Uninstall metadata are read from registry exports, checking both 32-bit and
64-bit views plus the per-user hive.

Snapshot keys are guest paths (``C:\...``), because those are the values stored
in a profile and passed back to Wine. ``rank_exe_candidates`` operates on host
``Path`` objects; ``detect_exe`` translates between the two.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from winbridge.core.schemes import is_reserved_scheme
from winbridge.util.reg import export_hive

if TYPE_CHECKING:
    from winbridge.backends.base import Backend

__all__ = [
    "FileInfo",
    "FileSnapshot",
    "detect_app_version",
    "detect_display_name",
    "detect_exe",
    "detect_schemes",
    "diff_new_files",
    "guest_exe_path",
    "is_excluded_exe",
    "new_executables",
    "rank_exe_candidates",
    "shortcut_exe_targets",
    "snapshot_programs",
]

_LOGGER = logging.getLogger("winbridge")

_DRIVE_PATH = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$")
_DRIVE_PREFIX = re.compile(r"^(?P<drive>[A-Za-z]:)[\\/]?(?P<rest>.*)$")
_EXE_TARGET = re.compile(r"^(?P<path>.+?\.exe)(?:\s|$)", re.IGNORECASE)
_SCHEME_NAME = re.compile(r"^[a-z][a-z0-9+.-]*$", re.IGNORECASE)
_EXCLUDED_EXE = re.compile(r"unins|uninstall|update|crash|helper", re.IGNORECASE)

#: Common non-app executables. These are hard exclusions: a known sidecar or
#: browser helper must never be auto-selected as the app. A user who really
#: wants one can still pass ``--exe``. Tauri and other bundled runtimes ship
#: node/python/ffmpeg sidecars plus browser-engine helpers that are frequently
#: larger than the GUI binary and would otherwise win on size alone. The
#: separate uninstall/update/crash/helper regex is applied as well.
_SIDECAR_EXE_NAMES = frozenset(
    name.casefold()
    for name in (
        "node.exe",
        "nodejs.exe",
        "python.exe",
        "pythonw.exe",
        "ffmpeg.exe",
        "ffprobe.exe",
        "crashpad_handler.exe",
        "elevate.exe",
        "vc_redist.x64.exe",
        "vc_redist.x86.exe",
        "uninstall.exe",
        "unins000.exe",
        "unins001.exe",
        "renderer.exe",
        "chrome_proxy.exe",
        "qtwebengineprocess.exe",
        "msedgewebview2.exe",
        "webviewhost.exe",
        "browser.exe",
    )
)

#: PE optional-header Subsystem values. The offset is 68 for both PE32 and
#: PE32+; GUI binaries (2) are the only ones that can produce the app window.
#: Console (3) and unreadable files stay neutral.
_IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
_PE32_MAGIC = 0x10B
_PE32_PLUS_MAGIC = 0x20B
_PE_SUBSYSTEM_OFFSET = 68
#: The COFF SizeOfOptionalHeader field must cover the architecture's minimum
#: header and stay bounded. PE32 needs at least 96 bytes; PE32+ at least 112.
_PE32_MIN_OPTIONAL_HEADER = 96
_PE32_PLUS_MIN_OPTIONAL_HEADER = 112
_PE_MAX_OPTIONAL_HEADER = 0x1000

#: A shortcut (``.lnk``) in the start menu or on the desktop is installer
#: intent: it names the executable a user is meant to launch. New/changed links
#: get a strong bonus; pre-existing links are weak fallback evidence only.
_GUI_BONUS = 1.0
_SHORTCUT_STRONG_BONUS = 0.30
_SHORTCUT_WEAK_BONUS = 0.15
#: With several GUI candidates and no shortcut evidence, a name lead this large
#: is required to auto-pick one. Otherwise setup asks for ``--exe``.
_GUI_NAME_MARGIN = 0.25
#: Shortcut files are small; cap reads so a corrupt/huge link cannot stall
#: detection. Targets live near the start of a valid link anyway.
_LNK_READ_LIMIT = 1024 * 1024

# Minimal MS-SHLLINK offsets/flags needed to read the primary target path.
_LNK_HEADER_SIZE = 76
_LNK_HAS_LINK_TARGET_ID_LIST = 0x1
_LNK_HAS_LINK_INFO = 0x2
_LNK_INFO_VOLUME_AND_LOCAL_BASE_PATH = 0x1
_LNK_INFO_HEADER_MIN = 0x1C
_LNK_INFO_HEADER_UNICODE = 0x24

#: Base scores are normalized to 0..1, then GUI, shortcut, and sidecar terms
#: are added. The margin is deliberately conservative: a second candidate close
#: to the leader is a prompt, not a guess.
_AMBIGUITY_MARGIN = 0.08
_NAME_WEIGHT = 0.55
_SIZE_WEIGHT = 0.25
_LOCATION_WEIGHT = 0.20
#: A registry display-name/key must be at least this close to the installer
#: stem before it can win an ambiguous multi-entry prefix without an exe match.
_ENTRY_NAME_THRESHOLD = 1.5

_CLASS_ROOTS: tuple[tuple[str, ...], ...] = (
    ("HKEY_CLASSES_ROOT",),
    # MS-RRP: the 32-bit HKCR namespace and the canonical 32-bit machine
    # class store both live under a Wow6432Node child, not WOW6432Node\Classes.
    ("HKEY_CLASSES_ROOT", "WOW6432NODE"),
    ("HKEY_CURRENT_USER", "SOFTWARE", "CLASSES"),
    ("HKEY_CURRENT_USER", "SOFTWARE", "CLASSES", "WOW6432NODE"),
    ("HKEY_LOCAL_MACHINE", "SOFTWARE", "CLASSES"),
    ("HKEY_LOCAL_MACHINE", "SOFTWARE", "CLASSES", "WOW6432NODE"),
    # Retained as a compatibility form: some exporters/tools spell the
    # redirected store as WOW6432Node\Classes.
    ("HKEY_LOCAL_MACHINE", "SOFTWARE", "WOW6432NODE", "CLASSES"),
)

#: Uninstall roots in precedence order: 64-bit machine view, 32-bit machine
#: view, per-user 64-bit, then per-user 32-bit (MS-RRP redirect).
_UNINSTALL_ROOTS: tuple[str, ...] = (
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft"
    r"\Windows\CurrentVersion\Uninstall",
    r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKEY_CURRENT_USER\Software\WOW6432Node\Microsoft"
    r"\Windows\CurrentVersion\Uninstall",
)


@dataclass(frozen=True, slots=True)
class FileInfo:
    """The file attributes that change across an install.

    ``sha256`` is populated only for ``.exe`` files. When both snapshots have
    a digest, :func:`diff_new_files` compares content instead of metadata, so
    a chmod or timestamp touch on an unchanged executable is not install
    evidence. ``ctime_ns`` remains the fallback for non-executable files and
    for any executable that could not be hashed.
    """

    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Guest-path keyed snapshot of the program directories."""

    files: dict[str, FileInfo] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    score: float
    name_score: float
    stem_score: float
    gui: bool = False
    shortcut: bool = False


# ---------------------------------------------------------------------------
# Guest/host path translation
# ---------------------------------------------------------------------------


def _guest_to_host(prefix: Path, guest: str) -> Path | None:
    """Map ``C:\\...`` to ``<prefix>/drive_c/...``; reject traversal."""
    match = _DRIVE_PATH.match(guest)
    if match is None:
        return None
    parts = [part for part in match.group("rest").replace("/", "\\").split("\\") if part]
    if any(part in (".", "..") for part in parts):
        return None
    base = prefix / f"drive_{match.group('drive').lower()}"
    return base.joinpath(*parts)


def _host_to_guest(prefix: Path, host: Path) -> str | None:
    drive_c = prefix / "drive_c"
    try:
        relative = host.relative_to(drive_c)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return "C:\\" + "\\".join(relative.parts)


# ---------------------------------------------------------------------------
# Snapshot and diff
# ---------------------------------------------------------------------------


#: Streaming read size for executable SHA-256 digests.
_HASH_CHUNK = 1024 * 1024


#: Directory names treated as caches anywhere under AppData. Wine and the apps
#: it runs churn these during any launch, so walking them makes the snapshot
#: unbounded and noisy. They are pruned recursively; a product that stored its
#: executable inside one of these directories would be invisible by design.
_APPDATA_CACHE_DIRS = frozenset(
    name.casefold()
    for name in (
        "EBWebView",
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnCache",
        "ShaderCache",
        "Service Worker",
        "blob_storage",
        "IndexedDB",
        "Local Storage",
        "Session Storage",
        "Crashpad",
        "Temp",
        "CrashDumps",
        "D3DSCache",
        "Packages",
        "VirtualStore",
        "ConnectedDevicesPlatform",
    )
)


def _sha256(path: Path) -> str | None:
    """Return the file's SHA-256 hex digest, or ``None`` when unreadable."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _host_contained(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _program_roots(prefix: Path) -> list[Path]:
    drive_c = prefix / "drive_c"
    if drive_c.is_symlink() or not drive_c.is_dir():
        return []

    roots: list[Path] = []
    try:
        roots.extend(
            path
            for path in sorted(drive_c.glob("Program Files*"))
            if not path.is_symlink() and path.is_dir()
        )
    except OSError:
        pass

    users = drive_c / "users"
    if users.is_symlink() or not users.is_dir():
        return roots
    try:
        user_dirs = sorted(users.iterdir())
    except OSError:
        return roots
    for user_dir in user_dirs:
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        # AppData may be a symlink or junction pointing outside the prefix.
        # Refuse it before constructing Local/Roaming roots from it.
        appdata = user_dir / "AppData"
        if appdata.is_symlink() or not appdata.is_dir():
            continue
        if not _host_contained(appdata, drive_c):
            continue
        # Per-user installers write directly under Local or Roaming. Local
        # already contains Local\Programs, so adding both would walk it twice.
        for name in ("Local", "Roaming"):
            root = appdata / name
            if root.is_symlink() or not root.is_dir():
                continue
            if not _host_contained(root, drive_c):
                continue
            roots.append(root)
    return roots


def _walk_files(root: Path, *, suffix: str | None = None) -> Iterable[Path]:
    """Yield regular files under an AppData root, pruning caches and symlinks.

    Program Files is walked without cache pruning: every installed file there
    is app content. Under AppData, known cache directory names are pruned at
    any depth because they can hold thousands of churning files. ``suffix``
    restricts the walk when a root is captured only for a specific file kind
    (for example installer shortcuts).
    """
    appdata_root = root.parent.name.casefold() == "appdata"
    for raw_dirpath, raw_dirnames, raw_filenames in os.walk(root, followlinks=False):
        directory = Path(raw_dirpath)
        raw_dirnames[:] = sorted(
            name
            for name in raw_dirnames
            if not (directory / name).is_symlink()
            and not (appdata_root and name.casefold() in _APPDATA_CACHE_DIRS)
        )
        for filename in sorted(raw_filenames):
            if suffix is not None and not filename.casefold().endswith(suffix):
                continue
            path = directory / filename
            if path.is_symlink():
                continue
            yield path


def _record_snapshot_file(
    prefix: Path, path: Path, files: dict[str, FileInfo]
) -> None:
    """Record one regular file keyed by its guest path."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode):
        return
    guest = _host_to_guest(prefix, path)
    if guest is None:
        return
    digest = _sha256(path) if path.suffix.casefold() == ".exe" else None
    files[guest] = FileInfo(
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        sha256=digest,
    )


def snapshot_programs(prefix: Path) -> FileSnapshot:
    """Snapshot regular files under program roots, AppData, and shortcuts.

    Machine installs land under ``Program Files*``; per-user installers,
    including NSIS and Tauri apps, may instead write directly under
    ``users\\<user>\\AppData\\Local`` or ``AppData\\Roaming``. Every user's
    ``Local`` and ``Roaming`` tree is captured, so a per-user app is visible to
    the before/after diff. ``Local\\Programs`` is a subset of ``Local`` and is
    not walked separately. Known cache directory names are pruned recursively
    under AppData; the before/after diff already ignores pre-existing files.
    Executable files are streamed through SHA-256 so a metadata-only touch is
    not install evidence; non-executable files keep metadata-only comparison.

    Desktop and machine Start Menu roots are captured for ``.lnk`` files only,
    so installer shortcuts can be matched to their before/after changes. Other
    files there are deliberately ignored: a stray executable on the desktop is
    not install evidence.

    Symlinks are skipped: an exposed host folder must not be mistaken for
    installed content, and a symlink loop must not hang detection. Unreadable
    entries are ignored because a vanished file is not a detection failure.
    """
    prefix = Path(prefix)
    files: dict[str, FileInfo] = {}
    for root in _program_roots(prefix):
        for path in _walk_files(root):
            _record_snapshot_file(prefix, path, files)
    for root in _shortcut_roots(prefix):
        for path in _walk_files(root, suffix=".lnk"):
            _record_snapshot_file(prefix, path, files)
    return FileSnapshot(dict(sorted(files.items())))


def diff_new_files(before: FileSnapshot, after: FileSnapshot) -> list[str]:
    """Return guest paths that are new or whose content/attributes changed.

    A file present in both snapshots is compared by SHA-256 when both sides
    carry a digest; a metadata-only change with identical content is not new.
    New files and entries without a digest fall back to size/mtime/ctime.
    """
    changed: list[str] = []
    for path, info in after.files.items():
        previous = before.files.get(path)
        if previous is None:
            changed.append(path)
        elif info.sha256 is not None and previous.sha256 is not None:
            if info.sha256 != previous.sha256:
                changed.append(path)
        elif (previous.size, previous.mtime_ns, previous.ctime_ns) != (
            info.size,
            info.mtime_ns,
            info.ctime_ns,
        ):
            changed.append(path)
    return sorted(changed)


def new_executables(before: FileSnapshot, after: FileSnapshot) -> list[str]:
    """Return only genuinely new or content-changed executables.

    A path counts when it is absent from ``before`` or when both snapshots
    carry a SHA-256 digest and the digests differ. If either digest is missing,
    the executable is not install evidence: metadata-only changes such as
    ``chmod`` must not make an install succeed. Candidates are still ranked
    separately by :func:`detect_exe`.
    """
    changed: list[str] = []
    for path, info in after.files.items():
        if not path.casefold().endswith(".exe"):
            continue
        previous = before.files.get(path)
        if previous is None:
            changed.append(path)
        elif (
            previous.sha256 is not None
            and info.sha256 is not None
            and previous.sha256 != info.sha256
        ):
            changed.append(path)
    return sorted(changed)


# ---------------------------------------------------------------------------
# Exe ranking
# ---------------------------------------------------------------------------


def _installer_stem(installer_name: str | Path) -> str:
    return PureWindowsPath(str(installer_name)).stem


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _file_size(path: Path) -> int | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_size


def _path_key(path: Path) -> str:
    """Case-insensitive comparison key for an absolute host path."""
    return os.path.normcase(os.path.abspath(path)).casefold()


def is_excluded_exe(name: str | Path) -> bool:
    """Return whether a basename is never auto-selected as the app."""
    path = PureWindowsPath(str(name))
    return (
        path.name.casefold() in _SIDECAR_EXE_NAMES
        or _EXCLUDED_EXE.search(path.stem) is not None
    )


def _casefold_child(
    directory: Path, name: str, *, root: Path | None = None
) -> Path | None:
    """Return a direct child matching ``name`` case-insensitively.

    ``directory`` must not be a symlink. When ``root`` is supplied, the
    directory must also resolve inside it before it is enumerated, so an
    intermediate link can never expose paths outside the prefix.
    """
    if directory.is_symlink():
        return None
    if root is not None and not _host_contained(directory, root):
        return None
    exact = directory / name
    if exact.exists() or exact.is_symlink():
        return exact
    wanted = name.casefold()
    try:
        entries = directory.iterdir()
    except OSError:
        return None
    for entry in entries:
        if entry.name.casefold() == wanted:
            return entry
    return None


def guest_exe_path(prefix: Path, guest: str | Path) -> Path | None:
    """Resolve a guest ``.exe`` path case-insensitively inside ``drive_c``.

    Only a guest path with a drive prefix and ``.exe`` suffix is accepted.
    Absolute host paths, non-executables, traversal, symlinks, and paths
    outside ``drive_c`` return ``None``.
    """
    prefix = Path(prefix)
    drive_c = prefix / "drive_c"
    if drive_c.is_symlink() or not drive_c.is_dir():
        return None
    text = str(guest).strip()
    match = _DRIVE_PATH.match(text)
    if match is None:
        return None
    if PureWindowsPath(text).suffix.casefold() != ".exe":
        return None
    parts = [
        part
        for part in match.group("rest").replace("/", "\\").split("\\")
        if part
    ]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    if match.group("drive").casefold() != "c":
        return None
    current = drive_c
    for part in parts:
        child = _casefold_child(current, part, root=drive_c)
        if child is None:
            return None
        current = child
    if current.is_symlink() or not current.is_file():
        return None
    if not _host_contained(current, drive_c):
        return None
    return current


def _pe_subsystem(path: Path) -> int | None:
    """Return the PE optional-header Subsystem, or ``None`` when unreadable.

    Both PE32 and PE32+ place ``Subsystem`` at offset 68 of the optional
    header. The COFF ``SizeOfOptionalHeader`` is validated against a bounded
    range, and the whole declared optional header must exist. A short,
    truncated, oversized, or malformed file is neutral: callers must not treat
    an unparseable binary as either GUI or console.
    """
    try:
        with path.open("rb") as handle:
            dos = handle.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return None
            e_lfanew = int(struct.unpack_from("<I", dos, 0x3C)[0])
            # Reject offsets that cannot fit the headers we are about to read.
            if e_lfanew < 64 or e_lfanew > 0x10000000:
                return None
            handle.seek(e_lfanew)
            coff = handle.read(24)
            if len(coff) < 24 or coff[:4] != b"PE\0\0":
                return None
            optional_size = int(struct.unpack_from("<H", coff, 20)[0])
            if not 2 <= optional_size <= _PE_MAX_OPTIONAL_HEADER:
                return None
            optional = handle.read(optional_size)
    except (OSError, struct.error, ValueError):
        return None
    if len(optional) < optional_size:
        return None
    magic = int(struct.unpack_from("<H", optional, 0)[0])
    if magic == _PE32_MAGIC:
        minimum_size = _PE32_MIN_OPTIONAL_HEADER
    elif magic == _PE32_PLUS_MAGIC:
        minimum_size = _PE32_PLUS_MIN_OPTIONAL_HEADER
    else:
        return None
    if optional_size < minimum_size:
        return None
    return int(struct.unpack_from("<H", optional, _PE_SUBSYSTEM_OFFSET)[0])


def _read_u16(data: bytes, offset: int) -> int | None:
    """Read a little-endian u16 within bounds, or ``None``."""
    if offset < 0 or offset + 2 > len(data):
        return None
    return int.from_bytes(data[offset : offset + 2], "little")


def _read_u32(data: bytes, offset: int) -> int | None:
    """Read a little-endian u32 within bounds, or ``None``."""
    if offset < 0 or offset + 4 > len(data):
        return None
    return int.from_bytes(data[offset : offset + 4], "little")


def _lnk_c_string(data: bytes, offset: int, *, unicode: bool) -> str | None:
    """Read one NUL-terminated ANSI or UTF-16LE string from shortcut bytes."""
    if offset <= 0 or offset >= len(data):
        return None
    if unicode:
        end = offset
        while end + 1 < len(data):
            if data[end : end + 2] == b"\x00\x00":
                break
            end += 2
        raw = data[offset:end]
        return raw.decode("utf-16-le", errors="ignore")
    end = data.find(b"\x00", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("latin-1", errors="ignore")


def _join_lnk_path(base: str, suffix: str) -> str | None:
    """Join LinkInfo's local base path and common suffix."""
    base = base.rstrip("\x00")
    suffix = suffix.rstrip("\x00")
    if not base:
        return suffix or None
    if not suffix:
        return base
    if base.endswith(("\\", "/")) or suffix.startswith(("\\", "/")):
        return base + suffix
    return base + "\\" + suffix


def _lnk_link_info_target(data: bytes) -> str | None:
    """Read the primary target from a shortcut's LinkInfo structure.

    Only the local-base-path target is returned; ``COMMAND_LINE_ARGUMENTS``
    and other StringData fields are intentionally ignored so an argument that
    mentions an executable cannot act as shortcut evidence. Links without a
    usable LinkInfo are ignored rather than raw-scanned.
    """
    if len(data) < _LNK_HEADER_SIZE:
        return None
    flags = _read_u32(data, 20)
    if flags is None or not flags & _LNK_HAS_LINK_INFO:
        return None
    offset = _LNK_HEADER_SIZE
    if flags & _LNK_HAS_LINK_TARGET_ID_LIST:
        id_size = _read_u16(data, offset)
        if id_size is None:
            return None
        offset += 2 + id_size
    size = _read_u32(data, offset)
    header_size = _read_u32(data, offset + 4)
    if size is None or header_size is None:
        return None
    if size < _LNK_INFO_HEADER_MIN or header_size < _LNK_INFO_HEADER_MIN:
        return None
    if header_size > size or size > len(data) - offset:
        return None
    info = data[offset : offset + size]
    info_flags = _read_u32(info, 8)
    if info_flags is None or not info_flags & _LNK_INFO_VOLUME_AND_LOCAL_BASE_PATH:
        return None

    local_offset = _read_u32(info, 16)
    suffix_offset = _read_u32(info, 24)
    if local_offset is None or suffix_offset is None:
        return None
    unicode_local = _read_u32(info, 28) if header_size >= _LNK_INFO_HEADER_UNICODE else None
    unicode_suffix = (
        _read_u32(info, 32) if header_size >= _LNK_INFO_HEADER_UNICODE else None
    )
    if unicode_local:
        base = _lnk_c_string(info, unicode_local, unicode=True)
        suffix = (
            _lnk_c_string(info, unicode_suffix, unicode=True)
            if unicode_suffix
            else ""
        )
    else:
        base = _lnk_c_string(info, local_offset, unicode=False)
        suffix = (
            _lnk_c_string(info, suffix_offset, unicode=False) if suffix_offset else ""
        )
    if base is None:
        return None
    return _join_lnk_path(base, suffix or "")


def _shortcut_files(root: Path) -> Iterable[Path]:
    """Yield regular ``.lnk`` files under ``root`` without following symlinks."""
    for raw_dirpath, raw_dirnames, raw_filenames in os.walk(root, followlinks=False):
        directory = Path(raw_dirpath)
        raw_dirnames[:] = sorted(
            name for name in raw_dirnames if not (directory / name).is_symlink()
        )
        for filename in sorted(raw_filenames):
            if not filename.casefold().endswith(".lnk"):
                continue
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _shortcut_roots(prefix: Path) -> list[Path]:
    """Return the per-user and machine shortcut roots that exist in ``prefix``."""
    drive_c = prefix / "drive_c"
    if drive_c.is_symlink() or not drive_c.is_dir():
        return []
    roots = [
        drive_c / "ProgramData" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    ]
    users = drive_c / "users"
    if not users.is_symlink() and users.is_dir():
        try:
            user_dirs = sorted(users.iterdir())
        except OSError:
            user_dirs = []
        for user_dir in user_dirs:
            if user_dir.is_symlink() or not user_dir.is_dir():
                continue
            roots.append(user_dir / "Desktop")
            roots.append(
                user_dir
                / "AppData"
                / "Roaming"
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
            )
    found: list[Path] = []
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        if not _host_contained(root, drive_c):
            continue
        found.append(root)
    return found


def shortcut_exe_targets(
    prefix: Path,
    changed: Iterable[str] | None,
    candidates: Iterable[str | Path],
) -> frozenset[Path]:
    """Return candidate executables that are exact primary shortcut targets.

    Only a `.lnk`'s LinkInfo primary target counts. The target must exist, be a
    regular file, resolve inside the prefix, and match a candidate executable
    path exactly. Argument strings, link names, and merely equal basenames are
    not evidence. ``changed`` limits the scan to guest paths from
    :func:`diff_new_files`; ``None`` scans all links for weak fallback evidence.
    """
    prefix = Path(prefix)
    candidate_by_key = {_path_key(Path(raw)): Path(raw) for raw in candidates}
    if not candidate_by_key:
        return frozenset()
    changed_keys = None if changed is None else {guest.casefold() for guest in changed}
    if changed_keys is not None and not changed_keys:
        return frozenset()
    targets: set[Path] = set()
    for root in _shortcut_roots(prefix):
        for path in _shortcut_files(root):
            if changed_keys is not None:
                guest = _host_to_guest(prefix, path)
                if guest is None or guest.casefold() not in changed_keys:
                    continue
            try:
                with path.open("rb") as handle:
                    data = handle.read(_LNK_READ_LIMIT)
            except OSError:
                continue
            target = _lnk_link_info_target(data)
            if target is None:
                continue
            if is_excluded_exe(target):
                continue
            host = _guest_to_host(prefix, target)
            if host is None or not _host_contained(host, prefix):
                continue
            candidate = candidate_by_key.get(_path_key(host))
            if candidate is None or candidate.is_symlink() or not candidate.is_file():
                continue
            targets.add(candidate)
    return frozenset(targets)


def _ranked_candidates(
    paths: Iterable[str | Path],
    installer_name: str | Path,
    *,
    shortcut_targets: Iterable[str | Path] | None = None,
    shortcut_bonus: float = _SHORTCUT_STRONG_BONUS,
) -> list[_Candidate]:
    stem = _installer_stem(installer_name)
    shortcuts = {
        _path_key(Path(target))
        for target in shortcut_targets or ()
        if str(target).strip()
    }
    unique: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw)
        if path.suffix.casefold() != ".exe":
            continue
        if is_excluded_exe(path):
            continue
        key = _path_key(path)
        unique.setdefault(key, path)

    measured: list[tuple[Path, int]] = []
    for path in unique.values():
        size = _file_size(path)
        if size is not None:
            measured.append((path, size))
    if not measured:
        return []

    try:
        common = Path(os.path.commonpath([str(path.parent) for path, _ in measured]))
    except ValueError:
        common = Path()
    max_size = max(size for _, size in measured) or 1
    candidates: list[_Candidate] = []
    for path, size in measured:
        depth = max(0, len(path.parent.parts) - len(common.parts))
        location_score = 1.0 / (1.0 + depth)
        stem_score = _similarity(path.stem, stem)
        name_score = max(stem_score, _similarity(path.parent.name, stem))
        gui = _pe_subsystem(path) == _IMAGE_SUBSYSTEM_WINDOWS_GUI
        key = _path_key(path)
        shortcut = key in shortcuts
        score = (
            _NAME_WEIGHT * name_score
            + _SIZE_WEIGHT * (size / max_size)
            + _LOCATION_WEIGHT * location_score
            + (_GUI_BONUS if gui else 0.0)
            + (shortcut_bonus if shortcut else 0.0)
        )
        candidates.append(
            _Candidate(
                path=path,
                score=score,
                name_score=name_score,
                stem_score=stem_score,
                gui=gui,
                shortcut=shortcut,
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path.name.casefold(), str(item.path)))
    return candidates


def rank_exe_candidates(
    paths: Iterable[str | Path],
    installer_name: str | Path,
    *,
    shortcut_targets: Iterable[str | Path] | None = None,
    shortcut_bonus: float = _SHORTCUT_STRONG_BONUS,
) -> list[Path]:
    """Rank plausible main executables, excluding uninstall/update/helper names.

    Location depth is measured relative to the shallowest common parent, so a
    product-root executable outranks a nested browser/helper process. Size is
    normalized against the largest candidate's size and used only as a
    weighted term. The name term compares both the executable stem and its
    parent directory with the installer stem. GUI PE binaries receive a bonus,
    an exact installer-shortcut target receives ``shortcut_bonus``, and known
    bundled helpers/sidecars are never candidates.
    """
    return [
        candidate.path
        for candidate in _ranked_candidates(
            paths,
            installer_name,
            shortcut_targets=shortcut_targets,
            shortcut_bonus=shortcut_bonus,
        )
    ]


def _clear_winner(
    candidates: list[_Candidate],
    *,
    strong_shortcut: bool,
    weak_shortcut: bool,
) -> _Candidate | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    gui = [candidate for candidate in candidates if candidate.gui]
    if strong_shortcut:
        targeted = [candidate for candidate in candidates if candidate.shortcut]
        if len(targeted) == 1:
            return targeted[0]
    if not strong_shortcut and len(gui) >= 2:
        by_name = sorted(
            gui,
            key=lambda item: (
                -item.name_score,
                -item.score,
                item.path.name.casefold(),
                str(item.path),
            ),
        )
        if by_name[0].name_score - by_name[1].name_score >= _GUI_NAME_MARGIN:
            return by_name[0]
        if weak_shortcut:
            targeted = [candidate for candidate in gui if candidate.shortcut]
            if len(targeted) == 1:
                rival_stem = max(
                    (candidate.stem_score for candidate in gui if candidate is not targeted[0]),
                    default=0.0,
                )
                if targeted[0].stem_score >= rival_stem and targeted[0].stem_score > 0:
                    return targeted[0]
        return None
    if not strong_shortcut and len(gui) == 1:
        return gui[0]

    if candidates[0].score - candidates[1].score >= _AMBIGUITY_MARGIN:
        return candidates[0]
    return None


def detect_exe(
    prefix: Path,
    before: FileSnapshot,
    after: FileSnapshot,
    installer_name: str | Path,
    *,
    preferred_exe: str | Path | None = None,
) -> str | None:
    """Return the guest path of a clear executable winner, or ``None``.

    ``None`` means "no new executable" or "ambiguous"; the caller prompts on a
    TTY or requires ``--exe``. Every candidate and score is logged so an
    ambiguous result is diagnosable from a bug report. Installer shortcuts
    under the desktop and Start Menu are read here because only ``detect_exe``
    has the prefix.

    ``preferred_exe`` is the recorded profile executable on update/rebuild. It
    is authoritative when provided: a valid recorded path is returned
    immediately, while a missing, excluded, or out-of-prefix path returns
    ``None`` rather than falling through to a changed helper. Setup passes
    ``None``.
    """
    prefix = Path(prefix)
    if preferred_exe is not None:
        recorded = str(preferred_exe).strip()
        host = guest_exe_path(prefix, recorded) if recorded else None
        if host is None or is_excluded_exe(host):
            _LOGGER.warning(
                "[detect] recorded executable is missing, excluded, or outside "
                "the prefix: %s",
                recorded or "(empty)",
            )
            return None
        guest = _host_to_guest(prefix, host) or recorded
        _LOGGER.info("[detect] exe: %s (recorded)", guest)
        return guest

    changed = diff_new_files(before, after)
    host_paths: list[Path] = []
    guest_by_host: dict[Path, str] = {}
    for guest in changed:
        if not guest.casefold().endswith(".exe"):
            continue
        host = _guest_to_host(prefix, guest)
        if host is None or host.is_symlink() or not host.is_file():
            continue
        host_paths.append(host)
        guest_by_host[host] = guest

    strong_targets = shortcut_exe_targets(prefix, changed, host_paths)
    weak_targets: frozenset[Path] = frozenset()
    if strong_targets:
        shortcut_targets = strong_targets
        shortcut_bonus = _SHORTCUT_STRONG_BONUS
    else:
        weak_targets = shortcut_exe_targets(prefix, None, host_paths)
        shortcut_targets = weak_targets
        shortcut_bonus = _SHORTCUT_WEAK_BONUS
    ranked = _ranked_candidates(
        host_paths,
        installer_name,
        shortcut_targets=shortcut_targets,
        shortcut_bonus=shortcut_bonus,
    )
    winner = _clear_winner(
        ranked,
        strong_shortcut=bool(strong_targets),
        weak_shortcut=bool(weak_targets),
    )
    if winner is None:
        if ranked:
            details = ", ".join(
                f"{candidate.path} ({candidate.score:.2f}"
                f"{', gui' if candidate.gui else ''}"
                f"{', shortcut' if candidate.shortcut else ''})"
                for candidate in ranked
            )
            _LOGGER.warning("[detect] executable detection is ambiguous: %s", details)
        else:
            _LOGGER.info("[detect] no new executable found")
        return None

    winner_guest = guest_by_host.get(winner.path) or _host_to_guest(prefix, winner.path)
    if winner_guest is None:
        return None
    _LOGGER.info("[detect] exe: %s (score %.2f)", winner_guest, winner.score)
    return winner_guest


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _normalize_key(key: str) -> str:
    return key.replace("/", "\\").strip("\\").casefold()


def _key_index(registry: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {_normalize_key(key): values for key, values in registry.items()}


def _value(values: dict[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in values.items():
        if key.casefold() == target:
            return value
    return None


def _same_values(left: dict[str, str], right: dict[str, str]) -> bool:
    return {key.casefold(): value for key, value in left.items()} == {
        key.casefold(): value for key, value in right.items()
    }


def _normalize_path(value: str) -> str:
    """Canonicalize a Windows path for case-insensitive containment checks.

    ``.`` and ``..`` are resolved lexically so a command cannot claim to be
    inside the install tree while escaping it. The drive letter is preserved
    separately because ``..`` can never move above a drive root.
    """
    text = value.strip().strip('"').replace("/", "\\")
    drive = ""
    match = _DRIVE_PREFIX.match(text)
    if match is not None:
        drive = match.group("drive").upper() + "\\"
        text = match.group("rest")
    parts: list[str] = []
    for part in text.split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return (drive + "\\".join(parts)).rstrip("\\").casefold()


def _is_within(target: str, root: str | Path) -> bool:
    normalized_target = _normalize_path(str(target))
    normalized_root = _normalize_path(str(root))
    if not normalized_target or not normalized_root:
        return False
    return normalized_target == normalized_root or normalized_target.startswith(
        normalized_root + "\\"
    )


def _command_target(command: str) -> str | None:
    stripped = command.strip()
    if not stripped:
        return None
    if stripped.startswith('"'):
        closing = stripped.find('"', 1)
        if closing <= 1:
            return None
        return stripped[1:closing]
    # An unquoted path may contain spaces; stop at the .exe so arguments are
    # not mistaken for part of the executable path.
    executable = _EXE_TARGET.match(stripped)
    if executable is not None:
        return executable.group("path")
    return stripped.split(None, 1)[0]


def _command_points_into(command: str, root: str | Path) -> bool:
    target = _command_target(command)
    return target is not None and _is_within(target, root)


def _class_root_parts(key: str) -> list[str] | None:
    parts = [part for part in key.replace("/", "\\").split("\\") if part]
    folded = [part.casefold() for part in parts]
    for root in _CLASS_ROOTS:
        root_folded = [part.casefold() for part in root]
        if len(folded) == len(root_folded) + 1 and folded[: len(root_folded)] == root_folded:
            scheme = parts[-1]
            if _SCHEME_NAME.fullmatch(scheme):
                return parts
            return None
    return None


def _scheme_changed(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    key: str,
    values: dict[str, str],
) -> bool:
    normalized = _normalize_key(key)
    previous_root = before.get(normalized)
    if previous_root is None or not _same_values(previous_root, values):
        return True

    command_key = _normalize_key(key.rstrip("\\") + r"\shell\open\command")
    current_command = after.get(command_key)
    if current_command is None:
        return False
    previous_command = before.get(command_key)
    return previous_command is None or not _same_values(previous_command, current_command)


def detect_schemes(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    install_root: str | Path,
    *,
    on_reserved: Callable[[str], None] | None = None,
) -> list[str]:
    """Return schemes added or changed by this install and rooted in its tree.

    A key qualifies only when it is a scheme root (not a subkey), carries a
    ``URL Protocol`` value, and its ``shell\\open\\command`` target lies inside
    ``install_root``. Commands that merely mention the install root in their
    arguments do not qualify. Prefix collisions such as ``Example-old`` are
    rejected by separator-aware containment. Results are lowercase because URL
    schemes are case-insensitive.

    Reserved browser/file schemes are never returned: claiming ``https`` or
    ``file`` would hijack links and local files for every app. ``on_reserved``
    receives each rejected scheme so setup can warn instead of failing.
    """
    before_index = _key_index(before)
    after_index = _key_index(after)
    found: set[str] = set()
    for key, values in after.items():
        parts = _class_root_parts(key)
        if parts is None:
            continue
        if _value(values, "URL Protocol") is None:
            continue

        command_key = _normalize_key(key.rstrip("\\") + r"\shell\open\command")
        command_values = after_index.get(command_key)
        if command_values is None:
            continue
        command = _value(command_values, "")
        if command is None or not _command_points_into(command, install_root):
            continue
        if not _scheme_changed(before_index, after_index, key, values):
            continue
        # URL schemes are case-insensitive; downstream registration and
        # profile comparisons expect the canonical lowercase form.
        scheme = parts[-1].casefold()
        if is_reserved_scheme(scheme):
            _LOGGER.warning("[detect] refused reserved scheme: %s", scheme)
            if on_reserved is not None:
                on_reserved(scheme)
            continue
        found.add(scheme)

    schemes = sorted(found)
    for scheme in schemes:
        _LOGGER.info("[detect] scheme: %s", scheme)
    if not schemes:
        _LOGGER.debug("[detect] no new URL schemes for %s", install_root)
    return schemes


# ---------------------------------------------------------------------------
# Uninstall metadata
# ---------------------------------------------------------------------------


def _is_entry_key(root: str, key: str) -> bool:
    normalized_root = _normalize_key(root)
    normalized_key = _normalize_key(key)
    prefix = normalized_root + "\\"
    if not normalized_key.startswith(prefix):
        return False
    remainder = normalized_key[len(prefix) :]
    return bool(remainder) and "\\" not in remainder


def _uninstall_entries(
    backend: Backend, prefix: Path
) -> list[tuple[str, dict[str, str]]]:
    entries: list[tuple[str, dict[str, str]]] = []
    for root in _UNINSTALL_ROOTS:
        registry = export_hive(backend, prefix, root)
        for key in sorted(registry, key=str.casefold):
            if _is_entry_key(root, key):
                entries.append((key, registry[key]))
    return entries


def _entry_score(
    key: str,
    values: dict[str, str],
    installer_stem: str,
    exe: str | Path | None,
) -> float:
    score = 0.0
    install_location = _value(values, "InstallLocation")
    if exe is not None and install_location is not None:
        if _is_within(str(exe), install_location):
            score += 6.0

    if exe is not None:
        exe_name = PureWindowsPath(str(exe)).name.casefold()
        icon = _value(values, "DisplayIcon")
        if icon is not None:
            icon_target = _command_target(icon) or icon
            if _normalize_path(icon_target) == _normalize_path(str(exe)):
                score += 5.0
            elif exe_name and exe_name in _normalize_path(icon):
                score += 2.0
        normalized_exe = _normalize_path(str(exe))
        if normalized_exe and any(
            normalized_exe in _normalize_path(value) for value in values.values()
        ):
            score += 2.0

    display_name = _value(values, "DisplayName") or ""
    entry_name = key.rsplit("\\", 1)[-1]
    score += 3.0 * max(
        _similarity(display_name, installer_stem),
        _similarity(entry_name, installer_stem),
    )
    return score


def _choose_entry_value(
    entries: list[tuple[str, dict[str, str]]],
    field: str,
    installer: str | Path | None,
    exe: str | Path | None,
) -> str | None:
    stem = _installer_stem(installer) if installer is not None else ""
    candidates: list[tuple[float, int, str]] = []
    for index, (key, values) in enumerate(entries):
        raw = _value(values, field)
        if raw is None:
            continue
        value = raw.strip()
        if not value:
            continue
        candidates.append((_entry_score(key, values, stem, exe), index, value))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    top = candidates[0]
    if top[0] < _ENTRY_NAME_THRESHOLD:
        _LOGGER.debug(
            "[detect] no plausible Uninstall entry for %s; best score %.2f is below %.2f",
            field,
            top[0],
            _ENTRY_NAME_THRESHOLD,
        )
        return None
    if len(candidates) == 1:
        return top[2]
    if all(value == top[2] for _, _, value in candidates):
        return top[2]
    second = candidates[1]
    if top[0] - second[0] >= 0.5:
        return top[2]
    _LOGGER.debug(
        "[detect] %s is ambiguous across Uninstall entries: %s",
        field,
        ", ".join(f"{value} ({score:.2f})" for score, _, value in candidates),
    )
    return None


def detect_app_version(
    backend: Backend,
    prefix: Path,
    exe: str | Path | None,
    installer: str | Path | None,
) -> str:
    """Read Uninstall ``DisplayVersion``, then the installer filename, then ``unknown``.

    Both machine views and the per-user hive are exported. When several entries
    exist, the one whose ``InstallLocation``/``DisplayIcon`` contains ``exe``
    wins; otherwise display-name similarity to the installer breaks the tie.
    The filename fallback is the literal basename, not a version parsed out of
    it: evidence paths must not contain a guessed semver.
    """
    entries = _uninstall_entries(backend, Path(prefix))
    version = _choose_entry_value(entries, "DisplayVersion", installer, exe)
    if version is not None:
        _LOGGER.info("[detect] app version: %s (Uninstall DisplayVersion)", version)
        return version

    if installer is not None:
        fallback = PureWindowsPath(str(installer)).name
        if fallback:
            _LOGGER.info("[detect] app version: %s (installer filename)", fallback)
            return fallback
    _LOGGER.info("[detect] app version: unknown")
    return "unknown"


def detect_display_name(
    backend: Backend,
    prefix: Path,
    installer: str | Path | None,
    exe: str | Path | None = None,
) -> str:
    """Read Uninstall ``DisplayName``, then fall back to the installer stem.

    ``exe`` is optional and additive for existing callers. Passing the detected
    guest executable lets the reader reject an unrelated lone Uninstall entry
    (for example a bundled runtime) even when the installer name is generic.
    """
    entries = _uninstall_entries(backend, Path(prefix))
    name = _choose_entry_value(entries, "DisplayName", installer, exe)
    if name is not None:
        _LOGGER.info("[detect] display name: %s (Uninstall DisplayName)", name)
        return name

    if installer is not None:
        fallback = PureWindowsPath(str(installer)).stem
        if fallback:
            _LOGGER.info("[detect] display name: %s (installer stem)", fallback)
            return fallback
    _LOGGER.info("[detect] display name: unknown")
    return "unknown"
