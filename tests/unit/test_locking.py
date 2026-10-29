from __future__ import annotations

import stat
from pathlib import Path

import pytest

from winbridge.core.locking import file_lock


def test_lock_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "app.lock"

    with file_lock(lock):
        with pytest.raises(BlockingIOError):
            with file_lock(lock, blocking=False):
                pass


def test_lock_creates_private_lock_file_and_parent(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "app.lock"

    with file_lock(lock):
        assert lock.is_file()
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700


def test_lock_tightens_preexisting_file_mode(tmp_path: Path) -> None:
    lock = tmp_path / "app.lock"
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)

    with file_lock(lock):
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    lock = tmp_path / "app.lock"

    with file_lock(lock):
        pass

    with file_lock(lock, blocking=False):
        pass


def test_lock_releases_on_exception(tmp_path: Path) -> None:
    lock = tmp_path / "app.lock"

    with pytest.raises(RuntimeError, match="boom"):
        with file_lock(lock):
            raise RuntimeError("boom")

    # A leaked flock would make this non-blocking acquire raise BlockingIOError.
    with file_lock(lock, blocking=False):
        pass


def test_lock_file_survives_release(tmp_path: Path) -> None:
    lock = tmp_path / "app.lock"

    with file_lock(lock):
        pass

    # Locking is advisory over a persistent file; doctor relies on that to find
    # stale locks without treating the file itself as garbage.
    assert lock.exists()
