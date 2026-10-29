"""Post-install detection: filesystem snapshot, exe, schemes, and metadata."""

from __future__ import annotations

import codecs
import hashlib
import os
import struct
import subprocess
from pathlib import Path

import pytest

from winbridge.core.detect import (
    FileInfo,
    FileSnapshot,
    _pe_subsystem,
    detect_app_version,
    detect_display_name,
    detect_exe,
    detect_schemes,
    diff_new_files,
    new_executables,
    rank_exe_candidates,
    shortcut_exe_targets,
    snapshot_programs,
)
from winbridge.util.reg import parse_reg

FIXTURES = Path(__file__).parents[1] / "fixtures"
REGISTRY_FIXTURE = FIXTURES / "registry.reg"

UNINSTALL_64 = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_32 = (
    r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft"
    r"\Windows\CurrentVersion\Uninstall"
)
UNINSTALL_HKCU = r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_HKCU_WOW = (
    r"HKEY_CURRENT_USER\Software\WOW6432Node\Microsoft"
    r"\Windows\CurrentVersion\Uninstall"
)


class RegBackend:
    """A backend that answers ``reg export`` with canned UTF-16 .reg text."""

    name = "fake-reg"

    def __init__(self, exports: dict[str, str] | None = None) -> None:
        self.exports = exports or {}
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((prefix, args))
        if args[:2] != ("reg", "export"):
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="not found\n")
        hive, guest = args[2], args[3]
        payload = self.exports.get(hive)
        if payload is None:
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="not found\n")
        host = Path(prefix).joinpath("drive_c", *guest.split("\\")[1:])
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_bytes(codecs.BOM_UTF16_LE + payload.encode("utf-16-le"))
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _reg(*sections: tuple[str, dict[str, str]]) -> str:
    lines = ["Windows Registry Editor Version 5.00", ""]
    for key, values in sections:
        lines.append(f"[{key}]")
        for name, value in values.items():
            if name == "":
                lines.append(f'@="{_escape(value)}"')
            else:
                lines.append(f'"{_escape(name)}"="{_escape(value)}"')
        lines.append("")
    return "\n".join(lines)


def _host(prefix: Path, guest: str) -> Path:
    return prefix / "drive_c" / Path(*guest.split("\\")[1:])


def _write(
    prefix: Path,
    guest: str,
    data: bytes = b"x",
    *,
    mtime_ns: int | None = None,
) -> Path:
    host = _host(prefix, guest)
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(data)
    if mtime_ns is not None:
        os.utime(host, ns=(mtime_ns, mtime_ns))
    return host


def _prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    return prefix


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pe(
    subsystem: int,
    *,
    pe32_plus: bool = False,
    size: int = 512,
    optional_size: int | None = None,
) -> bytes:
    """Build a minimal synthetic PE image with the requested subsystem."""
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    if optional_size is None:
        optional_size = 240 if pe32_plus else 224
    optional = bytearray(optional_size)
    struct.pack_into("<H", optional, 0, 0x20B if pe32_plus else 0x10B)
    struct.pack_into("<H", optional, 68, subsystem)
    coff = bytearray(20)
    struct.pack_into("<H", coff, 16, optional_size)
    body = bytes(dos) + b"PE\x00\x00" + bytes(coff) + bytes(optional)
    return body + b"\x00" * max(0, size - len(body))


def _lnk(
    target: str,
    *,
    encoding: str = "utf-16-le",
    arguments: str | None = None,
) -> bytes:
    """Build a minimal but valid shortcut with one primary target."""
    unicode = encoding == "utf-16-le"
    flags = 0x2
    if arguments is not None:
        flags |= 0x20
    if unicode:
        flags |= 0x80
    header = bytearray(76)
    struct.pack_into("<I", header, 0, 76)
    struct.pack_into("<I", header, 20, flags)
    header_size = 0x24 if unicode else 0x1C
    info = bytearray(header_size)
    if unicode:
        target_bytes = target.encode("utf-16-le") + b"\x00\x00"
        struct.pack_into("<I", info, 28, header_size)
    else:
        target_bytes = target.encode("latin-1") + b"\x00"
        struct.pack_into("<I", info, 16, header_size)
    struct.pack_into("<I", info, 0, header_size + len(target_bytes))
    struct.pack_into("<I", info, 4, header_size)
    struct.pack_into("<I", info, 8, 1)
    body = bytes(header) + bytes(info) + target_bytes
    if arguments is not None:
        argument_bytes = (arguments + "\x00").encode(encoding)
        body += struct.pack("<H", len(arguments) + 1) + argument_bytes
    return body


# ---------------------------------------------------------------------------
# Snapshot and diff
# ---------------------------------------------------------------------------


def test_snapshot_covers_both_program_files_roots(tmp_path: Path):
    prefix = _prefix(tmp_path)
    app = _write(prefix, r"C:\Program Files\Example\Example.exe", b"abc", mtime_ns=111)
    x86 = _write(prefix, r"C:\Program Files (x86)\Other\Other.exe", b"de")
    _write(prefix, r"C:\Windows\System32\helper.exe", b"not-a-program")

    snapshot = snapshot_programs(prefix)

    assert set(snapshot.files) == {
        r"C:\Program Files\Example\Example.exe",
        r"C:\Program Files (x86)\Other\Other.exe",
    }
    assert snapshot.files[r"C:\Program Files\Example\Example.exe"] == FileInfo(
        size=3,
        mtime_ns=app.stat().st_mtime_ns,
        ctime_ns=app.stat().st_ctime_ns,
        sha256=_digest(b"abc"),
    )
    assert snapshot.files[r"C:\Program Files (x86)\Other\Other.exe"] == FileInfo(
        size=2,
        mtime_ns=x86.stat().st_mtime_ns,
        ctime_ns=x86.stat().st_ctime_ns,
        sha256=_digest(b"de"),
    )


def test_snapshot_skips_symlinks_and_non_files(tmp_path: Path):
    prefix = _prefix(tmp_path)
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"secret")
    programs = prefix / "drive_c" / "Program Files" / "Example"
    programs.mkdir(parents=True)
    (programs / "link.exe").symlink_to(outside)
    (programs / "subdir.exe").mkdir()
    _write(prefix, r"C:\Program Files\Example\real.exe")

    snapshot = snapshot_programs(prefix)

    assert set(snapshot.files) == {r"C:\Program Files\Example\real.exe"}


def test_snapshot_covers_per_user_appdata_local_and_roaming(tmp_path: Path):
    prefix = _prefix(tmp_path)
    local_exe = r"C:\users\alice\AppData\Local\Example\Example.exe"
    roaming_exe = r"C:\users\alice\AppData\Roaming\Example\Example.exe"
    local_path = _write(prefix, local_exe, b"local", mtime_ns=333)
    roaming_path = _write(prefix, roaming_exe, b"roaming", mtime_ns=444)
    _write(prefix, r"C:\Windows\System32\helper.exe", b"not-a-program")

    snapshot = snapshot_programs(prefix)

    assert set(snapshot.files) == {local_exe, roaming_exe}
    assert snapshot.files[local_exe] == FileInfo(
        size=5,
        mtime_ns=333,
        ctime_ns=local_path.stat().st_ctime_ns,
        sha256=_digest(b"local"),
    )
    assert snapshot.files[roaming_exe] == FileInfo(
        size=7,
        mtime_ns=444,
        ctime_ns=roaming_path.stat().st_ctime_ns,
        sha256=_digest(b"roaming"),
    )


def test_snapshot_hashes_executables_only(tmp_path: Path):
    prefix = _prefix(tmp_path)
    exe = r"C:\Program Files\Example\Example.exe"
    text = r"C:\Program Files\Example\readme.txt"
    _write(prefix, exe, b"same bytes")
    _write(prefix, text, b"same bytes")

    snapshot = snapshot_programs(prefix)

    assert snapshot.files[exe].sha256 == _digest(b"same bytes")
    assert snapshot.files[text].sha256 is None


def test_snapshot_prunes_appdata_cache_dirs_recursively(tmp_path: Path):
    prefix = _prefix(tmp_path)
    _write(prefix, r"C:\users\alice\AppData\Local\Example\Example.exe")
    _write(prefix, r"C:\users\alice\AppData\Local\Example\data\settings.json")
    cache_names = {
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
    }
    for name in cache_names:
        _write(prefix, rf"C:\users\alice\AppData\Local\{name}\nested\blob.bin")
        _write(prefix, rf"C:\users\alice\AppData\Local\Example\{name}\nested\blob.bin")
    for index in range(40):
        _write(
            prefix,
            rf"C:\users\alice\AppData\Local\EBWebView\large\nested\blob-{index}.bin",
        )

    assert set(snapshot_programs(prefix).files) == {
        r"C:\users\alice\AppData\Local\Example\Example.exe",
        r"C:\users\alice\AppData\Local\Example\data\settings.json",
    }


def test_snapshot_ignores_a_symlinked_appdata_dir(tmp_path: Path):
    prefix = _prefix(tmp_path)
    outside = tmp_path / "outside"
    (outside / "Example").mkdir(parents=True)
    (outside / "Example" / "Example.exe").write_bytes(b"secret")
    user_dir = prefix / "drive_c" / "users" / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / "AppData").symlink_to(outside, target_is_directory=True)

    assert snapshot_programs(prefix).files == {}



def test_snapshot_ignores_a_program_files_symlink(tmp_path: Path):
    prefix = _prefix(tmp_path)
    outside = tmp_path / "outside"
    (outside / "Example").mkdir(parents=True)
    (outside / "Example" / "Outside.exe").write_bytes(b"secret")
    (prefix / "drive_c" / "Program Files Link").symlink_to(outside)

    assert snapshot_programs(prefix).files == {}


def test_snapshot_missing_prefix_is_empty(tmp_path: Path):
    assert snapshot_programs(tmp_path / "missing").files == {}


def test_diff_new_files_returns_new_and_changed_sorted(tmp_path: Path):
    prefix = _prefix(tmp_path)
    unchanged = _write(prefix, r"C:\Program Files\Example\A.exe", b"a")
    changed = _write(prefix, r"C:\Program Files\Example\B.exe", b"b")
    removed = _write(prefix, r"C:\Program Files\Example\C.exe", b"c")
    before = snapshot_programs(prefix)

    changed.write_bytes(b"bbbb")
    os.utime(changed, ns=(999, 999))
    removed.unlink()
    added = _write(prefix, r"C:\Program Files\Example\D.exe", b"d")
    after = snapshot_programs(prefix)

    assert diff_new_files(before, after) == sorted(
        [
            r"C:\Program Files\Example\B.exe",
            r"C:\Program Files\Example\D.exe",
        ]
    )
    assert unchanged.exists()
    assert added.exists()


def test_diff_new_files_is_empty_for_identical_snapshots(tmp_path: Path):
    prefix = _prefix(tmp_path)
    _write(prefix, r"C:\Program Files\Example\A.exe")
    snapshot = snapshot_programs(prefix)
    assert diff_new_files(snapshot, snapshot) == []


def test_diff_new_files_compares_content_not_metadata_for_executables():
    exe = r"C:\Program Files\Example\Example.exe"
    digest = "a" * 64
    before = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=1, ctime_ns=1, sha256=digest)}
    )
    touched = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=2, ctime_ns=2, sha256=digest)}
    )
    rewritten = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=2, ctime_ns=2, sha256="b" * 64)}
    )

    assert diff_new_files(before, touched) == []
    assert diff_new_files(before, rewritten) == [exe]


def test_diff_new_files_falls_back_to_metadata_without_a_digest():
    text = r"C:\Program Files\Example\readme.txt"
    before = FileSnapshot({text: FileInfo(size=4, mtime_ns=1, ctime_ns=1)})
    touched = FileSnapshot({text: FileInfo(size=4, mtime_ns=2, ctime_ns=2)})
    unchanged = FileSnapshot({text: FileInfo(size=4, mtime_ns=1, ctime_ns=1)})

    assert diff_new_files(before, touched) == [text]
    assert diff_new_files(before, unchanged) == []


# ---------------------------------------------------------------------------
# Exe ranking and detection
# ---------------------------------------------------------------------------


def test_rank_excludes_uninstallers_and_prefers_product_name(tmp_path: Path):
    app = tmp_path / "Example" / "Example.exe"
    app.parent.mkdir()
    app.write_bytes(b"x" * 100)
    unins = tmp_path / "Example" / "unins000.exe"
    unins.write_bytes(b"x" * 100)
    ranked = rank_exe_candidates([unins, app], "Example-1.0-setup.exe")
    assert ranked[0].name == "Example.exe"
    assert all("unins" not in p.name for p in ranked)


@pytest.mark.parametrize(
    "name",
    [
        "unins000.exe",
        "Uninstall.exe",
        "updater.exe",
        "Update.exe",
        "crashpad_handler.exe",
        "Helper.exe",
    ],
)
def test_rank_excludes_uninstall_update_crash_and_helper_names(tmp_path: Path, name: str):
    excluded = tmp_path / name
    excluded.parent.mkdir(exist_ok=True)
    excluded.write_bytes(b"x")
    keep = tmp_path / "Example.exe"
    keep.write_bytes(b"x")

    ranked = rank_exe_candidates([excluded, keep], "Example-1.0-setup.exe")

    assert ranked == [keep]


@pytest.mark.parametrize(
    "name",
    [
        "node.exe",
        "NODE.EXE",
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
    ],
)
def test_rank_excludes_bundled_sidecar_basenames(tmp_path: Path, name: str):
    sidecar = tmp_path / "Example" / name
    sidecar.parent.mkdir()
    sidecar.write_bytes(_pe(3, size=100_000))
    app = tmp_path / "Example" / "Example.exe"
    app.write_bytes(_pe(2))

    ranked = rank_exe_candidates([sidecar, app], "Example-1.0-setup.exe")

    assert ranked == [app]


@pytest.mark.parametrize(
    "name",
    [
        "renderer.exe",
        "chrome_proxy.exe",
        "qtwebengineprocess.exe",
        "msedgewebview2.exe",
        "webviewhost.exe",
        "browser.exe",
    ],
)
def test_rank_prefers_the_app_over_a_larger_gui_helper(tmp_path: Path, name: str):
    helper = tmp_path / "Example" / name
    helper.parent.mkdir()
    helper.write_bytes(_pe(2, size=100_000))
    app = tmp_path / "Example" / "Example.exe"
    app.write_bytes(_pe(2))

    ranked = rank_exe_candidates([helper, app], "Example-1.0-setup.exe")

    assert ranked == [app]


@pytest.mark.parametrize(
    "name", ["node.exe", "pythonw.exe", "renderer.exe", "browser.exe"]
)
def test_rank_does_not_select_a_lone_sidecar(tmp_path: Path, name: str):
    sidecar = tmp_path / name
    sidecar.write_bytes(_pe(2))

    assert rank_exe_candidates([sidecar], "Example-1.0-setup.exe") == []


def test_rank_never_selects_node_even_when_larger_and_name_similar(tmp_path: Path):
    node = tmp_path / "Example" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(_pe(3, size=100_000))
    app = tmp_path / "Example" / "Example.exe"
    app.write_bytes(_pe(2))

    ranked = rank_exe_candidates([node, app], "node-1.0-setup.exe")

    assert ranked == [app]


def test_rank_exact_sidecar_list_does_not_match_substrings(tmp_path: Path):
    app = tmp_path / "NodeLauncher.exe"
    app.write_bytes(_pe(2))

    assert rank_exe_candidates([app], "Example-1.0-setup.exe") == [app]


def test_rank_prefers_gui_over_console_with_equal_names_and_sizes(tmp_path: Path):
    gui = tmp_path / "A" / "Example.exe"
    console = tmp_path / "B" / "Example.exe"
    gui.parent.mkdir()
    console.parent.mkdir()
    gui.write_bytes(_pe(2))
    console.write_bytes(_pe(3))

    ranked = rank_exe_candidates([console, gui], "Example-1.0-setup.exe")

    assert ranked[0] == gui
    assert ranked[1] == console


def test_rank_gui_bonus_outweighs_a_larger_console_sidecar(tmp_path: Path):
    gui = tmp_path / "Example" / "Example.exe"
    console = tmp_path / "Example" / "Example Console.exe"
    gui.parent.mkdir()
    gui.write_bytes(_pe(2))
    console.write_bytes(_pe(3, size=10_000))

    ranked = rank_exe_candidates([console, gui], "Example-1.0-setup.exe")

    assert ranked == [gui, console]


def test_rank_prefers_a_shortcut_target_over_another_gui_exe(tmp_path: Path):
    other = tmp_path / "Example" / "Other.exe"
    other.parent.mkdir()
    other.write_bytes(_pe(2))
    named = tmp_path / "Example" / "Example.exe"
    named.write_bytes(_pe(2))

    ranked = rank_exe_candidates(
        [named, other],
        "Example-1.0-setup.exe",
        shortcut_targets={other},
    )

    assert ranked[0] == other


# ---------------------------------------------------------------------------
# PE subsystem and shortcut parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pe32_plus", [False, True])
def test_pe_subsystem_reads_gui_from_pe32_and_pe32_plus(
    tmp_path: Path, pe32_plus: bool
):
    path = tmp_path / "Example.exe"
    path.write_bytes(_pe(2, pe32_plus=pe32_plus))

    assert _pe_subsystem(path) == 2


def test_pe_subsystem_reads_console(tmp_path: Path):
    path = tmp_path / "node.exe"
    path.write_bytes(_pe(3))

    assert _pe_subsystem(path) == 3


@pytest.mark.parametrize(("pe32_plus", "minimum"), [(False, 96), (True, 112)])
def test_pe_subsystem_accepts_the_arch_minimum_optional_header(
    tmp_path: Path, pe32_plus: bool, minimum: int
):
    path = tmp_path / "Example.exe"
    path.write_bytes(_pe(2, pe32_plus=pe32_plus, optional_size=minimum))

    assert _pe_subsystem(path) == 2


@pytest.mark.parametrize(("pe32_plus", "minimum"), [(False, 96), (True, 112)])
def test_pe_subsystem_rejects_header_below_the_arch_minimum(
    tmp_path: Path, pe32_plus: bool, minimum: int
):
    path = tmp_path / "Example.exe"
    path.write_bytes(_pe(2, pe32_plus=pe32_plus, optional_size=minimum - 1))

    assert _pe_subsystem(path) is None


def test_pe_subsystem_rejects_an_undersized_optional_header(tmp_path: Path):
    data = bytearray(_pe(2))
    struct.pack_into("<H", data, 0x40 + 4 + 16, 69)

    path = tmp_path / "short-header.exe"
    path.write_bytes(bytes(data))

    assert _pe_subsystem(path) is None


def test_pe_subsystem_rejects_an_oversized_optional_header(tmp_path: Path):
    data = bytearray(_pe(2))
    struct.pack_into("<H", data, 0x40 + 4 + 16, 0x1001)

    path = tmp_path / "huge-header.exe"
    path.write_bytes(bytes(data))

    assert _pe_subsystem(path) is None


def test_pe_subsystem_rejects_a_truncated_optional_header(tmp_path: Path):
    data = _pe(2)[: 0x40 + 24 + 100]

    path = tmp_path / "truncated-header.exe"
    path.write_bytes(data)

    assert _pe_subsystem(path) is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"MZ",
        b"XX" + b"\x00" * 62,
        b"MZ" + b"\x00" * 62,
        b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40) + b"XX" + b"\x00" * 40,
        b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40) + b"PE\x00\x00" + b"\x00" * 20,
        b"MZ" + b"\x00" * 58 + struct.pack("<I", 0xFFFFFFF0) + b"\x00" * 40,
    ],
)
def test_pe_subsystem_is_neutral_for_short_or_malformed_files(
    tmp_path: Path, data: bytes
):
    path = tmp_path / "broken.exe"
    path.write_bytes(data)

    assert _pe_subsystem(path) is None


def test_pe_subsystem_is_neutral_for_an_unknown_optional_header_magic(tmp_path: Path):
    data = bytearray(_pe(2))
    struct.pack_into("<H", data, 64 + 24, 0x999)

    path = tmp_path / "unknown.exe"
    path.write_bytes(bytes(data))

    assert _pe_subsystem(path) is None


def test_pe_subsystem_is_neutral_for_a_missing_file(tmp_path: Path):
    assert _pe_subsystem(tmp_path / "missing.exe") is None


def test_shortcut_exe_targets_reads_utf16_and_latin1_links(tmp_path: Path):
    prefix = _prefix(tmp_path)
    desktop = r"C:\users\alice\Desktop\Example.lnk"
    machine = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Example.lnk"
    broken = r"C:\users\alice\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\bad.lnk"
    app = _write(prefix, r"C:\Program Files\Example\Example App.exe", _pe(2))
    other = _write(prefix, r"C:\Program Files\Example\Other.exe", _pe(2))
    _write(prefix, desktop, _lnk(r"C:\Program Files\Example\Example App.exe"))
    _write(
        prefix,
        machine,
        _lnk(r"C:\Program Files\Example\Other.exe", encoding="latin-1"),
    )
    _write(prefix, broken, b"\x00not-a-link\xff")

    targets = shortcut_exe_targets(prefix, {desktop, machine, broken}, [app, other])

    assert targets == {app, other}


def test_shortcut_exe_targets_uses_only_changed_links(tmp_path: Path):
    prefix = _prefix(tmp_path)
    stale = r"C:\users\alice\Desktop\Stale.lnk"
    fresh = r"C:\users\alice\Desktop\Fresh.lnk"
    stale_exe = _write(prefix, r"C:\Program Files\Example\Stale.exe", _pe(2))
    fresh_exe = _write(prefix, r"C:\Program Files\Example\Fresh.exe", _pe(2))
    _write(prefix, stale, _lnk(r"C:\Program Files\Example\Stale.exe"))
    _write(prefix, fresh, _lnk(r"C:\Program Files\Example\Fresh.exe"))

    targets = shortcut_exe_targets(prefix, {fresh}, [stale_exe, fresh_exe])

    assert targets == {fresh_exe}


def test_shortcut_exe_targets_uses_all_links_for_weak_fallback(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    app = _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, link, _lnk(r"C:\Program Files\Example\Example.exe"))

    assert shortcut_exe_targets(prefix, None, [app]) == {app}


def test_shortcut_exe_targets_requires_the_exact_target_path(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    candidate = _write(prefix, r"C:\Program Files\Example\Other.exe", _pe(2))
    _write(prefix, r"C:\Elsewhere\Other.exe", _pe(2))
    _write(prefix, link, _lnk(r"C:\Elsewhere\Other.exe"))

    assert shortcut_exe_targets(prefix, {link}, [candidate]) == frozenset()


def test_shortcut_exe_targets_matches_paths_case_insensitively(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    candidate = _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, link, _lnk(r"C:\PROGRAM FILES\EXAMPLE\EXAMPLE.EXE"))

    assert shortcut_exe_targets(prefix, {link}, [candidate]) == {candidate}


def test_shortcut_exe_targets_ignores_helper_targets(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    app = _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    helper = _write(prefix, r"C:\Program Files\Example\browser.exe", _pe(2))
    _write(prefix, link, _lnk(r"C:\Program Files\Example\browser.exe"))

    assert shortcut_exe_targets(prefix, {link}, [app, helper]) == frozenset()


def test_shortcut_exe_targets_ignores_a_missing_target(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    missing = prefix / "drive_c" / "Program Files" / "Example" / "Other.exe"
    _write(prefix, link, _lnk(r"C:\Program Files\Example\Other.exe"))

    assert shortcut_exe_targets(prefix, {link}, [missing]) == frozenset()


def test_shortcut_exe_targets_rejects_traversal_targets(tmp_path: Path):
    prefix = _prefix(tmp_path)
    link = r"C:\users\alice\Desktop\Example.lnk"
    other = _write(prefix, r"C:\Other.exe", _pe(2))
    _write(prefix, link, _lnk(r"C:\..\..\Other.exe"))

    assert shortcut_exe_targets(prefix, {link}, [other]) == frozenset()


def test_rank_prefers_the_name_and_shallow_location(tmp_path: Path):
    root = tmp_path / "Example"
    app = root / "Example.exe"
    nested = root / "resources" / "AppCore.exe"
    other = tmp_path / "Other" / "Other.exe"
    for path, size in ((app, 100), (nested, 100), (other, 100)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    ranked = rank_exe_candidates([nested, other, app], "Example-1.0-setup.exe")

    assert ranked[0] == app
    assert set(ranked) == {app, nested, other}


def test_rank_prefers_the_larger_file_when_names_and_depth_tie(tmp_path: Path):
    small = tmp_path / "A" / "Example.exe"
    large = tmp_path / "B" / "Example.exe"
    small.parent.mkdir()
    large.parent.mkdir()
    small.write_bytes(b"x" * 10)
    large.write_bytes(b"x" * 10_000)

    ranked = rank_exe_candidates([small, large], "Example-1.0-setup.exe")

    assert ranked == [large, small]


def test_rank_prefers_the_shallower_file_when_name_size_and_directory_tie(
    tmp_path: Path,
):
    shallow = tmp_path / "A" / "Example.exe"
    deep = tmp_path / "A" / "sub" / "Example.exe"
    shallow.parent.mkdir()
    deep.parent.mkdir(parents=True)
    shallow.write_bytes(b"x" * 100)
    deep.write_bytes(b"x" * 100)

    ranked = rank_exe_candidates([deep, shallow], "Example-1.0-setup.exe")

    assert ranked == [shallow, deep]


def test_rank_filters_non_exe_files_and_deduplicates(tmp_path: Path):
    app = tmp_path / "Example" / "Example.exe"
    app.parent.mkdir()
    app.write_bytes(b"x")
    readme = tmp_path / "Example" / "readme.txt"
    readme.write_bytes(b"x")

    ranked = rank_exe_candidates([app, app, readme], "Example-1.0-setup.exe")

    assert ranked == [app]


def test_rank_handles_a_windows_installer_path(tmp_path: Path):
    app = tmp_path / "Example" / "Example.exe"
    app.parent.mkdir()
    app.write_bytes(b"x")
    other = tmp_path / "Other" / "Other.exe"
    other.parent.mkdir()
    other.write_bytes(b"x")

    ranked = rank_exe_candidates(
        [other, app], r"C:\Users\me\Downloads\Example-1.0-setup.exe"
    )

    assert ranked[0] == app


def test_new_executables_requires_new_path_or_both_digests():
    exe = r"C:\Program Files\Example\Example.exe"
    digest_a = "a" * 64
    digest_b = "b" * 64
    before = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=1, ctime_ns=1, sha256=digest_a)}
    )
    rewritten = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=2, ctime_ns=2, sha256=digest_b)}
    )
    touched = FileSnapshot(
        {exe: FileInfo(size=4, mtime_ns=2, ctime_ns=2, sha256=digest_a)}
    )
    unreadable = FileSnapshot({exe: FileInfo(size=4, mtime_ns=2, ctime_ns=2)})

    assert new_executables(before, rewritten) == [exe]
    assert new_executables(before, touched) == []
    assert new_executables(unreadable, rewritten) == []
    assert new_executables(unreadable, touched) == []
    assert new_executables(FileSnapshot({}), touched) == [exe]


def test_detect_exe_returns_the_guest_path_of_a_clear_winner(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\Example.exe", b"main")
    _write(prefix, r"C:\Program Files\Example\unins000.exe", b"uninstall")
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Example.exe"
    )


def test_detect_exe_finds_a_per_user_local_install(tmp_path: Path):
    prefix = _prefix(tmp_path)
    _write(prefix, r"C:\users\alice\AppData\Local\Other\Other.exe", b"pre-existing")
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\users\alice\AppData\Local\Example\Example.exe", b"main")
    _write(prefix, r"C:\users\alice\AppData\Local\Example\unins000.exe", b"uninstall")
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == (
        r"C:\users\alice\AppData\Local\Example\Example.exe"
    )


def test_detect_exe_accepts_a_changed_executable(tmp_path: Path):
    prefix = _prefix(tmp_path)
    exe = _write(prefix, r"C:\Program Files\Example\Example.exe", b"old")
    before = snapshot_programs(prefix)
    exe.write_bytes(b"new-and-larger")
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Example.exe"
    )


def test_detect_exe_ignores_unchanged_files(tmp_path: Path):
    prefix = _prefix(tmp_path)
    _write(prefix, r"C:\Program Files\Example\Example.exe", b"same")
    before = snapshot_programs(prefix)
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_returns_none_when_candidates_are_ambiguous(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\A\Example.exe", b"same-size")
    _write(prefix, r"C:\Program Files\B\Example.exe", b"same-size")
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_returns_none_when_only_excluded_names_appear(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\unins000.exe")
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_ignores_non_exe_new_files(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\readme.txt")
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_rejects_path_traversal_in_a_snapshot(tmp_path: Path):
    prefix = _prefix(tmp_path)
    outside = tmp_path / "evil.exe"
    outside.write_bytes(b"evil")
    before = FileSnapshot({})
    after = FileSnapshot(
        {
            r"C:\..\..\evil.exe": FileInfo(size=4, mtime_ns=1, ctime_ns=1),
            r"C:\Program Files\Example\..\..\evil.exe": FileInfo(
                size=4, mtime_ns=1, ctime_ns=1
            ),
        }
    )

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_handles_uppercase_extension(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\EXAMPLE.EXE", b"main")
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\EXAMPLE.EXE"
    )


def test_detect_exe_skips_a_bundled_console_sidecar(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\node.exe", _pe(3, size=100_000))
    _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Example.exe"
    )


def test_detect_exe_rejects_a_lone_sidecar_named_candidate(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\node.exe", _pe(2))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_with_node_and_shortcut_picks_the_app(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    app_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, r"C:\Program Files\Example\node.exe", _pe(3, size=100_000))
    _write(prefix, app_guest, _pe(2))
    _write(prefix, r"C:\users\alice\Desktop\Example.lnk", _lnk(app_guest))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == app_guest


def test_detect_exe_preferred_exe_survives_a_changed_helper(tmp_path: Path):
    prefix = _prefix(tmp_path)
    app_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, app_guest, _pe(2))
    before = snapshot_programs(prefix)
    helper_guest = r"C:\Program Files\Example\runtimehost.exe"
    _write(prefix, helper_guest, _pe(2, size=100_000))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == helper_guest
    assert (
        detect_exe(
            prefix,
            before,
            after,
            "Example-1.0-setup.exe",
            preferred_exe=app_guest,
        )
        == app_guest
    )


def test_detect_exe_preferred_exe_beats_a_new_helper_shortcut(tmp_path: Path):
    prefix = _prefix(tmp_path)
    app_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, app_guest, _pe(2))
    before = snapshot_programs(prefix)
    helper_guest = r"C:\Program Files\Example\runtimehost.exe"
    _write(prefix, helper_guest, _pe(2, size=100_000))
    _write(prefix, r"C:\users\alice\Desktop\Example.lnk", _lnk(helper_guest))
    after = snapshot_programs(prefix)

    assert (
        detect_exe(
            prefix,
            before,
            after,
            "Example-1.0-setup.exe",
            preferred_exe=app_guest,
        )
        == app_guest
    )


def test_detect_exe_preferred_exe_resolves_case_insensitively(tmp_path: Path):
    prefix = _prefix(tmp_path)
    app_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, app_guest, _pe(2))

    assert (
        detect_exe(
            prefix,
            FileSnapshot({}),
            FileSnapshot({}),
            "Example-1.0-setup.exe",
            preferred_exe=r"c:\PROGRAM FILES\EXAMPLE\EXAMPLE.EXE",
        )
        == app_guest
    )


@pytest.mark.parametrize("name", ["readme.txt", "library.dll", "Example"])
def test_detect_exe_preferred_exe_rejects_non_executables(
    tmp_path: Path, name: str
):
    prefix = _prefix(tmp_path)
    guest = rf"C:\Program Files\Example\{name}"
    _write(prefix, guest, b"not an executable")

    assert (
        detect_exe(
            prefix,
            FileSnapshot({}),
            FileSnapshot({}),
            "Example-1.0-setup.exe",
            preferred_exe=guest,
        )
        is None
    )


def test_detect_exe_preferred_exe_rejects_another_drive(tmp_path: Path):
    prefix = _prefix(tmp_path)
    other = prefix / "drive_d" / "Example.exe"
    other.parent.mkdir()
    other.write_bytes(_pe(2))

    assert (
        detect_exe(
            prefix,
            FileSnapshot({}),
            FileSnapshot({}),
            "Example-1.0-setup.exe",
            preferred_exe=r"D:\Example.exe",
        )
        is None
    )


def test_detect_exe_preferred_exe_rejects_a_symlinked_intermediate(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Example.exe").write_bytes(_pe(2))
    link = prefix / "drive_c" / "Program Files" / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    assert (
        detect_exe(
            prefix,
            FileSnapshot({}),
            FileSnapshot({}),
            "Example-1.0-setup.exe",
            preferred_exe=r"C:\Program Files\linked\Example.exe",
        )
        is None
    )


def test_detect_exe_missing_preferred_does_not_fall_back_to_a_helper(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\runtimehost.exe", _pe(2))
    after = snapshot_programs(prefix)

    assert (
        detect_exe(
            prefix,
            before,
            after,
            "Example-1.0-setup.exe",
            preferred_exe=r"C:\Program Files\Example\Missing.exe",
        )
        is None
    )


def test_detect_exe_outside_preferred_does_not_fall_back_to_a_helper(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\runtimehost.exe", _pe(2))
    after = snapshot_programs(prefix)
    outside = tmp_path / "elsewhere" / "Example.exe"
    outside.parent.mkdir()
    outside.write_bytes(_pe(2))

    assert (
        detect_exe(
            prefix,
            before,
            after,
            "Example-1.0-setup.exe",
            preferred_exe=outside,
        )
        is None
    )


def test_detect_exe_never_prefers_a_recorded_sidecar(tmp_path: Path):
    prefix = _prefix(tmp_path)
    node_guest = r"C:\Program Files\Example\node.exe"
    _write(prefix, node_guest, _pe(3, size=100_000))
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\browser.exe", _pe(2))
    after = snapshot_programs(prefix)

    assert (
        detect_exe(
            prefix,
            before,
            after,
            "Example-1.0-setup.exe",
            preferred_exe=node_guest,
        )
        is None
    )


@pytest.mark.parametrize(
    ("shortcut_guest", "encoding"),
    [
        (r"C:\users\alice\Desktop\Example.lnk", "utf-16-le"),
        (
            r"C:\users\alice\AppData\Roaming\Microsoft\Windows\Start Menu"
            r"\Programs\Example\Example.lnk",
            "latin-1",
        ),
        (
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Example"
            r"\Example.lnk",
            "utf-16-le",
        ),
    ],
)
def test_detect_exe_prefers_an_installer_shortcut_target(
    tmp_path: Path, shortcut_guest: str, encoding: str
):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, r"C:\Program Files\Example\Other.exe", _pe(2))
    _write(
        prefix,
        shortcut_guest,
        _lnk(r"C:\Program Files\Example\Other.exe", encoding=encoding),
    )
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Other.exe"
    )


def test_detect_exe_refuses_ambiguous_gui_candidates_without_a_shortcut(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, r"C:\Program Files\Example\runtimehost.exe", _pe(2, size=100_000))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_uses_a_new_shortcut_to_pick_between_gui_candidates(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    app_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, app_guest, _pe(2))
    _write(prefix, r"C:\Program Files\Example\runtimehost.exe", _pe(2, size=100_000))
    _write(prefix, r"C:\users\alice\Desktop\Example.lnk", _lnk(app_guest))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == app_guest


def test_detect_exe_uses_a_pre_existing_app_shortcut_on_update(tmp_path: Path):
    prefix = _prefix(tmp_path)
    app_guest = r"C:\Program Files\Example\Example.exe"
    browser_guest = r"C:\Program Files\Example\runtimehost.exe"
    _write(prefix, app_guest, _pe(2))
    _write(prefix, browser_guest, _pe(2, size=100_000))
    _write(prefix, r"C:\users\alice\Desktop\Example.lnk", _lnk(app_guest))
    before = snapshot_programs(prefix)
    _write(prefix, app_guest, _pe(2, size=1024))
    _write(prefix, browser_guest, _pe(2, size=100_001))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == app_guest


def test_detect_exe_ignores_a_pre_existing_shortcut(tmp_path: Path):
    prefix = _prefix(tmp_path)
    _write(
        prefix,
        r"C:\users\alice\Desktop\Example.lnk",
        _lnk(r"C:\Program Files\Other\Other.exe"),
    )
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, r"C:\Program Files\Other\Other.exe", _pe(2))
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Example.exe"
    )


def test_detect_exe_ignores_a_pre_existing_shortcut_to_another_gui_helper(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    app_guest = r"C:\Program Files\Example\Example.exe"
    helper_guest = r"C:\Program Files\Example\runtimehost.exe"
    _write(prefix, app_guest, _pe(2))
    _write(prefix, helper_guest, _pe(2, size=100_000))
    _write(prefix, r"C:\users\alice\Desktop\Example.lnk", _lnk(helper_guest))
    before = snapshot_programs(prefix)
    _write(prefix, app_guest, _pe(2, size=1024))
    _write(prefix, helper_guest, _pe(2, size=100_001))
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") is None


def test_detect_exe_ignores_shortcut_argument_paths(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    target_guest = r"C:\Program Files\Example\Example App.exe"
    argument_guest = r"C:\Program Files\Example\Example.exe"
    _write(prefix, target_guest, _pe(2))
    _write(prefix, argument_guest, _pe(2, size=10_000))
    _write(
        prefix,
        r"C:\users\alice\Desktop\Example.lnk",
        _lnk(target_guest, arguments=argument_guest),
    )
    after = snapshot_programs(prefix)

    assert detect_exe(prefix, before, after, "Example-1.0-setup.exe") == target_guest


def test_detect_exe_ignores_a_malformed_shortcut(tmp_path: Path):
    prefix = _prefix(tmp_path)
    before = snapshot_programs(prefix)
    _write(prefix, r"C:\Program Files\Example\Example.exe", _pe(2))
    _write(prefix, r"C:\users\alice\Desktop\broken.lnk", b"\x00\xff\x00")
    after = snapshot_programs(prefix)

    assert (
        detect_exe(prefix, before, after, "Example-1.0-setup.exe")
        == r"C:\Program Files\Example\Example.exe"
    )


# ---------------------------------------------------------------------------
# Scheme detection
# ---------------------------------------------------------------------------


def test_detect_schemes_requires_url_protocol_and_install_root():
    parsed = parse_reg((FIXTURES / "registry.reg").read_text(encoding="utf-8"))
    schemes = detect_schemes({}, parsed, Path(r"C:\Program Files\Example"))
    assert schemes == ["example-app"]


def test_detect_schemes_refuses_a_reserved_browser_scheme():
    root = r"HKEY_CLASSES_ROOT\https"
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (root + r"\shell\open\command", {"": r'"C:\Program Files\Example\app.exe" "%1"'}),
        )
    )
    rejected: list[str] = []

    schemes = detect_schemes(
        {},
        after,
        Path(r"C:\Program Files\Example"),
        on_reserved=rejected.append,
    )

    assert schemes == []
    assert rejected == ["https"]


def test_detect_schemes_returns_nothing_when_nothing_changed():
    parsed = parse_reg(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    assert detect_schemes(parsed, parsed, Path(r"C:\Program Files\Example")) == []


def test_detect_schemes_keeps_a_changed_command():
    root = r"HKEY_CLASSES_ROOT\example-app"
    before = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (root + r"\shell\open\command", {"": r'"C:\Program Files\Example\old.exe" "%1"'}),
        )
    )
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (root + r"\shell\open\command", {"": r'"C:\Program Files\Example\new.exe" "%1"'}),
        )
    )

    assert detect_schemes(before, after, Path(r"C:\Program Files\Example")) == [
        "example-app"
    ]


def test_detect_schemes_keeps_a_new_command_on_an_existing_root():
    root = r"HKEY_CLASSES_ROOT\example-app"
    before = parse_reg(_reg((root, {"URL Protocol": ""})))
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (root + r"\shell\open\command", {"": r'"C:\Program Files\Example\app.exe"'}),
        )
    )

    assert detect_schemes(before, after, Path(r"C:\Program Files\Example")) == [
        "example-app"
    ]


def test_detect_schemes_is_case_insensitive_and_accepts_trailing_separators():
    after = {
        r"hkey_classes_root\Example-App": {"url protocol": ""},
        r"hkey_classes_root\Example-App\shell\open\command": {
            "": r'"c:\PROGRAM FILES\example\Example.exe" "%1"'
        },
    }
    assert (
        detect_schemes({}, after, Path(r"C:\Program Files\Example\\"))
        == ["example-app"]
    )


def test_detect_schemes_accepts_an_unquoted_command():
    root = r"HKEY_CLASSES_ROOT\example-app"
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (root + r"\shell\open\command", {"": r"C:\Program Files\Example\app.exe %1"}),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == [
        "example-app"
    ]


def test_detect_schemes_reads_the_32_bit_hkcr_namespace():
    root = r"HKEY_CLASSES_ROOT\Wow6432Node\example-app"
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (
                root + r"\shell\open\command",
                {"": r'"C:\Program Files\Example\app.exe" "%1"'},
            ),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == [
        "example-app"
    ]


def test_detect_schemes_reads_the_32_bit_machine_class_store():
    root = r"HKEY_LOCAL_MACHINE\SOFTWARE\Classes\Wow6432Node\example-app"
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (
                root + r"\shell\open\command",
                {"": r'"C:\Program Files (x86)\Example\app.exe" "%1"'},
            ),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files (x86)\Example")) == [
        "example-app"
    ]


def test_detect_schemes_rejects_a_url_protocol_on_a_subkey():
    command = r"HKEY_CLASSES_ROOT\example-app\shell\open\command"
    after = parse_reg(
        _reg(
            (command, {"URL Protocol": "", "": r'"C:\Program Files\Example\app.exe"'}),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == []


def test_detect_schemes_requires_a_command_key():
    after = parse_reg(_reg((r"HKEY_CLASSES_ROOT\example-app", {"URL Protocol": ""})))
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == []


def test_detect_schemes_rejects_a_command_that_traverses_out_of_the_tree():
    root = r"HKEY_CLASSES_ROOT\example-app"
    after = parse_reg(
        _reg(
            (root, {"URL Protocol": ""}),
            (
                root + r"\shell\open\command",
                {"": r'"C:\Program Files\Example\..\Outside\app.exe" "%1"'},
            ),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == []


def test_detect_schemes_deduplicates_case_variants_across_views():
    first = r"HKEY_CLASSES_ROOT\Example-App"
    second = r"HKEY_CURRENT_USER\Software\Classes\example-app"
    after = parse_reg(
        _reg(
            (first, {"URL Protocol": ""}),
            (
                first + r"\shell\open\command",
                {"": r'"C:\Program Files\Example\app.exe"'},
            ),
            (second, {"URL Protocol": ""}),
            (
                second + r"\shell\open\command",
                {"": r'"C:\Program Files\Example\app.exe"'},
            ),
        )
    )
    assert detect_schemes({}, after, Path(r"C:\Program Files\Example")) == [
        "example-app"
    ]


# ---------------------------------------------------------------------------
# App version and display name
# ---------------------------------------------------------------------------


def _uninstall_entry(key: str, **values: str) -> tuple[str, dict[str, str]]:
    return key, values


def test_detect_app_version_reads_display_version_from_the_64_bit_view(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                    InstallLocation=r"C:\Program Files\Example",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "1.2.3"
    )


def test_detect_app_version_falls_through_to_the_32_bit_view(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_32: _reg(
                _uninstall_entry(
                    UNINSTALL_32 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="2.0.1",
                    InstallLocation=r"C:\Program Files (x86)\Example",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files (x86)\Example\Example.exe",
            Path("Example-setup.exe"),
        )
        == "2.0.1"
    )


def test_detect_app_version_reads_the_per_user_uninstall_hive(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_HKCU: _reg(
                _uninstall_entry(
                    UNINSTALL_HKCU + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="3.0.0",
                    InstallLocation=r"C:\users\me\AppData\Local\Programs\Example",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\users\me\AppData\Local\Programs\Example\Example.exe",
            Path("Example-setup.exe"),
        )
        == "3.0.0"
    )


def test_detect_app_version_reads_the_32_bit_per_user_uninstall_hive(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_HKCU_WOW: _reg(
                _uninstall_entry(
                    UNINSTALL_HKCU_WOW + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="3.1.0",
                    InstallLocation=(
                        r"C:\users\me\AppData\Local\Programs\Example"
                    ),
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\users\me\AppData\Local\Programs\Example\Example.exe",
            Path("Example-setup.exe"),
        )
        == "3.1.0"
    )


def test_detect_app_version_prefers_the_entry_whose_install_location_has_the_exe(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Other",
                    DisplayName="Other",
                    DisplayVersion="9.9.9",
                    InstallLocation=r"C:\Program Files\Other",
                ),
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                    InstallLocation=r"C:\Program Files\Example",
                ),
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "1.2.3"
    )


def test_detect_app_version_does_not_match_an_install_location_prefix(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\ExampleOld",
                    DisplayName="Example Old",
                    DisplayVersion="9.9.9",
                    InstallLocation=r"C:\Program Files\Example-old",
                ),
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                    InstallLocation=r"C:\Program Files\Example",
                ),
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "1.2.3"
    )


def test_detect_app_version_falls_back_to_the_installer_filename(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend()

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "Example-1.0-setup.exe"
    )


def test_detect_app_version_is_unknown_without_registry_or_installer(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend()

    assert detect_app_version(backend, prefix, None, None) == "unknown"


def test_detect_app_version_ignores_an_empty_display_version(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="  ",
                    InstallLocation=r"C:\Program Files\Example",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "Example-1.0-setup.exe"
    )


def test_detect_app_version_rejects_a_lone_unrelated_entry(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\{GUID}",
                    DisplayName="Runtime Component",
                    DisplayVersion="9.9.9",
                    InstallLocation=r"C:\Program Files\Common Files\Runtime",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("Example-1.0-setup.exe"),
        )
        == "Example-1.0-setup.exe"
    )


def test_detect_app_version_uses_exe_evidence_with_a_generic_installer_name(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\{GUID}",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                    InstallLocation=r"C:\Program Files\Example",
                )
            )
        }
    )

    assert (
        detect_app_version(
            backend,
            prefix,
            r"C:\Program Files\Example\Example.exe",
            Path("setup.exe"),
        )
        == "1.2.3"
    )


def test_detect_app_version_uses_name_similarity_when_no_exe_is_given(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                ),
                _uninstall_entry(
                    UNINSTALL_64 + r"\Other",
                    DisplayName="Other",
                    DisplayVersion="9.9.9",
                ),
            )
        }
    )

    assert (
        detect_app_version(backend, prefix, None, Path("Example-1.0-setup.exe"))
        == "1.2.3"
    )


def test_detect_display_name_reads_the_uninstall_key(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                )
            )
        }
    )

    assert (
        detect_display_name(backend, prefix, Path("Example-1.0-setup.exe"))
        == "Example"
    )


def test_detect_display_name_falls_through_to_the_32_bit_view(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_32: _reg(
                _uninstall_entry(
                    UNINSTALL_32 + r"\Example",
                    DisplayName="Example 32",
                    DisplayVersion="1.0.0",
                )
            )
        }
    )

    assert (
        detect_display_name(backend, prefix, Path("Example-1.0-setup.exe"))
        == "Example 32"
    )


def test_detect_display_name_falls_back_to_installer_stem(fake_backend, tmp_path: Path):
    installer = tmp_path / "Example-1.0-setup.exe"
    installer.write_bytes(b"")
    assert detect_display_name(fake_backend, Path("/prefix"), installer) == "Example-1.0-setup"


def test_detect_display_name_ignores_an_empty_display_name(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName=" ",
                    DisplayVersion="1.2.3",
                )
            )
        }
    )

    assert (
        detect_display_name(backend, prefix, Path("Example-1.0-setup.exe"))
        == "Example-1.0-setup"
    )


def test_detect_display_name_prefers_the_entry_matching_the_installer(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\Other",
                    DisplayName="Other",
                    DisplayVersion="9.9.9",
                ),
                _uninstall_entry(
                    UNINSTALL_64 + r"\Example",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                ),
            )
        }
    )

    assert (
        detect_display_name(backend, prefix, Path("Example-1.0-setup.exe")) == "Example"
    )


def test_detect_display_name_rejects_a_lone_unrelated_entry(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\{GUID}",
                    DisplayName="Runtime Component",
                    DisplayVersion="9.9.9",
                    InstallLocation=r"C:\Program Files\Common Files\Runtime",
                )
            )
        }
    )

    assert (
        detect_display_name(
            backend,
            prefix,
            Path("Example-1.0-setup.exe"),
            r"C:\Program Files\Example\Example.exe",
        )
        == "Example-1.0-setup"
    )
    assert (
        detect_display_name(backend, prefix, Path("Example-1.0-setup.exe"))
        == "Example-1.0-setup"
    )


def test_detect_display_name_uses_exe_evidence_with_a_generic_installer_name(
    tmp_path: Path,
):
    prefix = _prefix(tmp_path)
    backend = RegBackend(
        {
            UNINSTALL_64: _reg(
                _uninstall_entry(
                    UNINSTALL_64 + r"\{GUID}",
                    DisplayName="Example",
                    DisplayVersion="1.2.3",
                    InstallLocation=r"C:\Program Files\Example",
                )
            )
        }
    )

    assert (
        detect_display_name(
            backend,
            prefix,
            Path("setup.exe"),
            r"C:\Program Files\Example\Example.exe",
        )
        == "Example"
    )
