"""Guarded staging inside a prefix: containment and unique names."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from winbridge.core.errors import WinBridgeError
from winbridge.core.staging import discard_staged, stage_installer


def _prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    return prefix


def _source(tmp_path: Path, name: str = "setup.exe") -> Path:
    source = tmp_path / name
    source.write_bytes(b"payload")
    return source


def test_stages_a_unique_copy_inside_the_prefix(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    source = _source(tmp_path)

    first_host, first_guest = stage_installer(
        prefix, source, name_prefix="winbridge"
    )
    second_host, second_guest = stage_installer(
        prefix, source, name_prefix="winbridge"
    )

    try:
        assert first_host != second_host
        assert first_host.parent == prefix / "drive_c" / "windows" / "temp"
        assert first_guest == f"C:\\windows\\temp\\{first_host.name}"
        assert second_guest == f"C:\\windows\\temp\\{second_host.name}"
        assert first_host.read_bytes() == b"payload"
        assert stat.S_IMODE(first_host.stat().st_mode) == 0o600
    finally:
        discard_staged(first_host)
        discard_staged(second_host)

    assert not first_host.exists()
    assert not second_host.exists()


def test_rejects_a_symlinked_temp_directory_before_copying(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (prefix / "drive_c" / "windows" / "temp").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(WinBridgeError, match="symlink"):
        stage_installer(prefix, _source(tmp_path), name_prefix="winbridge")

    assert list(outside.iterdir()) == []


def test_rejects_a_symlinked_intermediate_directory(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    outside = tmp_path / "outside"
    (outside / "windows" / "temp").mkdir(parents=True)
    (prefix / "drive_c").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WinBridgeError, match="symlink"):
        stage_installer(prefix, _source(tmp_path), name_prefix="winbridge")

    assert list((outside / "windows" / "temp").iterdir()) == []


def test_wraps_a_copy_failure_in_a_winbridge_error(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    source = tmp_path / "not-a-file"
    source.mkdir()

    with pytest.raises(WinBridgeError, match="cannot stage"):
        stage_installer(prefix, source, name_prefix="winbridge")

    assert list((prefix / "drive_c" / "windows" / "temp").iterdir()) == []


def test_discard_tolerates_a_missing_payload(tmp_path: Path) -> None:
    discard_staged(tmp_path / "missing.exe")
    discard_staged(None)
