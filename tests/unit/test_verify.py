"""Render-vs-blank smoke harness.

The verifier owns the prefix: it kills anything running there, captures the bare
desktop, launches through the same path as ``run``, matches the window by
process-tree membership, and passes only after two consecutive visible
captures. These tests pin the blank heuristic, the background diff, the t=0
capture, first-paint semantics, evidence retention, timeout behaviour, the CLI
contract, and profile persistence.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from winbridge import cli as cli_module
from winbridge.cli import app, main
from winbridge.core import verify as verify_module
from winbridge.core.config import Config
from winbridge.core.context import Context
from winbridge.core.errors import UnsupportedEnvironment
from winbridge.core.locking import file_lock
from winbridge.core.profile import Profile
from winbridge.core.run import Launch
from winbridge.core.runner import Catalog
from winbridge.core.state import Paths
from winbridge.core.verify import (
    VerifyResult,
    capture_background,
    is_blank,
    verify_app,
    watch_launch,
)
from winbridge.desktops.base import DesktopError, Window

# --- blank heuristic ---------------------------------------------------------


def test_blank_white_image_is_blank(tmp_path: Path) -> None:
    path = tmp_path / "white.png"
    Image.new("RGB", (800, 600), "white").save(path)
    assert is_blank(path) is True


def test_textured_image_is_not_blank(tmp_path: Path) -> None:
    path = tmp_path / "content.png"
    img = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(img)
    for x in range(0, 800, 20):
        draw.line((x, 0, 800 - x, 600), fill="black", width=3)
    img.save(path)
    assert is_blank(path) is False


def test_nearly_uniform_image_is_blank(tmp_path: Path) -> None:
    path = tmp_path / "near-white.png"
    image = Image.new("RGB", (800, 600), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for x in range(0, 800, 40):
        draw.line((x, 0, x, 599), fill=(252, 252, 252))
    image.save(path)

    assert is_blank(path) is True


def test_blank_thresholds_are_overridable(tmp_path: Path) -> None:
    path = tmp_path / "content.png"
    img = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(img).rectangle((0, 0, 799, 99), fill="black")
    img.save(path)

    assert is_blank(path) is False
    assert is_blank(path, stddev_threshold=200.0) is True


# --- core harness ------------------------------------------------------------


def _freeze_verify_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make frame timestamps deterministic; sleeping only advances the clock."""
    clock = {"now": 0.0}
    monkeypatch.setattr("winbridge.core.verify.time.monotonic", lambda: clock["now"])

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr("winbridge.core.verify.time.sleep", sleep)


def test_standalone_verify_acquires_the_app_lock(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """The wrapper queued standalone verify behind the per-app lock."""
    seen: list[tuple[Path, bool]] = []

    @contextmanager
    def recording(lock: Path, blocking: bool = True) -> Any:
        seen.append((Path(lock), blocking))
        yield

    monkeypatch.setattr(verify_module, "file_lock", recording)

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is True
    assert seen == [(paths.locks / f"app-{profile.id}.lock", True)]


def test_verify_reenters_the_app_lock_under_setup(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """Setup/update already hold the lock; nested verify must not deadlock."""
    lock = paths.locks / f"app-{profile.id}.lock"

    with file_lock(lock, blocking=False):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=1,
            poll_s=0.001,
            capture_s=0.001,
        )

    assert result.passed is True


def test_timeout_returns_failure_and_keeps_frames(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    fake_desktop.wait_result = None
    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=0.01,
        poll_s=0.001,
        capture_s=0.001,
    )
    assert result.passed is False
    assert result.reason == "timeout"
    assert result.frames  # the t=0 capture always exists
    assert result.cursor_checked is False


def test_keep_all_retains_every_capture(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    result = verify_app(profile, paths, fake_backend, fake_desktop, timeout=1, keep_all=True)
    assert len(result.frames) == len(fake_desktop.screenshots)


def test_passes_after_two_consecutive_non_blank_frames(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is True
    assert result.reason == "passed"
    assert result.window_class == "Example"
    assert result.first_paint_s is not None
    assert result.first_paint_s < 0.5
    assert result.cursor_checked is False
    assert len(fake_desktop.screenshots) == 2
    assert result.frames == fake_desktop.screenshots
    assert fake_desktop.waits[0][0] == {4242}
    # The harness keeps the first and the confirming frame, not more.
    assert len(result.frames) == 2


def _apply_alpha(pattern: list[str], fake_desktop: Any) -> None:
    """Replace captures with transparent/content RGBA frames from ``pattern``."""
    frames: list[Path] = []

    def screenshot(window: Window, dest: Path) -> None:
        index = min(len(frames), len(pattern) - 1)
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if pattern[index] == "transparent":
            Image.new("RGBA", (window.width, window.height), (0, 0, 0, 0)).save(target)
        else:
            image = Image.new("RGBA", (window.width, window.height), (242, 242, 242, 255))
            draw = ImageDraw.Draw(image)
            for x in range(0, window.width, 20):
                draw.line((x, 0, window.width - x, window.height), fill=(32, 48, 64, 255), width=3)
            image.save(target)
        frames.append(target)

    fake_desktop.screenshot = screenshot
    fake_desktop.frames = frames


def _background_identical_screenshot(fake_desktop: Any) -> None:
    """Make every window frame exactly the pre-launch background crop."""

    def screenshot(window: Window, dest: Path) -> None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(fake_desktop.screen_screenshots[-1]) as image:
            crop = image.convert("RGB").crop(
                (window.x, window.y, window.x + window.width, window.y + window.height)
            )
        crop.save(target)
        fake_desktop.frames.append(target)

    fake_desktop.frames = []
    fake_desktop.screenshot = screenshot


def test_background_identical_frames_report_transparent_not_pass(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """Wallpaper showing through must not pass even when it is textured."""
    _freeze_verify_clock(monkeypatch)
    _background_identical_screenshot(fake_desktop)

    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=100,
            poll_s=2,
            capture_s=10,
        )

    assert result.passed is False
    assert result.reason == "transparent"
    assert result.transparent_frames == len(fake_desktop.frames)
    assert len(fake_desktop.frames) >= 2
    assert "diff=0.000" in caplog.text
    assert "identical=1.000" in caplog.text

    background = result.frames[0].parent / "background.png"
    assert background.is_file()
    window = fake_desktop.window
    with Image.open(background) as image:
        crop = image.convert("RGB").crop(
            (window.x, window.y, window.x + window.width, window.y + window.height)
        )
    crop_path = tmp_path / "wallpaper-crop.png"
    crop.save(crop_path)
    assert is_blank(crop_path) is False


def test_content_differing_from_the_background_passes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)

    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=100,
            poll_s=2,
            capture_s=10,
        )

    assert result.passed is True
    assert result.reason == "passed"
    assert result.transparent_frames == 0
    assert "diff=" in caplog.text
    assert "diff=unavailable" not in caplog.text
    assert (result.frames[0].parent / "background.png").is_file()


def test_background_capture_failure_fails_closed(
    caplog: pytest.LogCaptureFixture,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def unavailable(dest: Path) -> None:
        raise DesktopError("grim exited 1")

    fake_desktop.screenshot_screen = unavailable

    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=1,
            poll_s=0.001,
            capture_s=0.001,
        )

    assert result.passed is False
    assert result.reason == "background"
    assert result.frames == []
    assert result.evidence_dir is not None
    assert result.evidence_dir.is_dir()
    assert fake_backend.runs == []
    assert "background unavailable" in caplog.text
    assert "grim exited 1" in caplog.text


def test_missing_background_file_fails_closed(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def no_file(dest: Path) -> None:
        return None

    fake_desktop.screenshot_screen = no_file

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.reason == "background"
    assert fake_backend.runs == []
    assert result.evidence_dir is not None
    assert not (result.evidence_dir / "background.png").exists()


def test_corrupt_background_file_fails_closed(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def corrupt(dest: Path) -> None:
        Path(dest).write_bytes(b"not a png")

    fake_desktop.screenshot_screen = corrupt

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.reason == "background"
    assert fake_backend.runs == []
    assert result.evidence_dir is not None
    assert (result.evidence_dir / "background.png").is_file()


def test_kill_happens_before_background_capture_and_launch(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    original = fake_desktop.screenshot_screen

    def screen(dest: Path) -> None:
        fake_backend.calls.append("background")
        original(dest)

    fake_desktop.screenshot_screen = screen

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is True
    kill = fake_backend.calls.index("wineserver -k")
    background = fake_backend.calls.index("background")
    launch = next(
        index
        for index, call in enumerate(fake_backend.calls)
        if call.startswith("run ")
    )
    assert kill < background < launch
    assert fake_backend.calls.count("wineserver -k") == 1


def test_out_of_bounds_window_geometry_fails_closed(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    window = Window("0x1", 4242, "Example - Main", "Example", -10, 20, 800, 600)
    fake_desktop.window = window
    fake_desktop.windows_result = [window]
    fake_desktop.wait_result = window

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.reason == "background"
    assert result.frames
    assert result.evidence_dir is not None
    assert result.evidence_dir.is_dir()


def test_background_crop_size_mismatch_fails_closed(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def wrong_size(window: Window, dest: Path) -> None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (window.width + 1, window.height), (32, 48, 64)).save(target)

    fake_desktop.screenshot = wrong_size

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.reason == "background"
    assert result.frames


def test_two_pixel_border_is_ignored_by_the_background_diff(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """Only the 2 px border differs from the desktop; the interior is wallpaper."""
    _freeze_verify_clock(monkeypatch)
    frames: list[Path] = []

    def screenshot(window: Window, dest: Path) -> None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(fake_desktop.screen_screenshots[-1]) as image:
            frame = image.convert("RGB").crop(
                (window.x, window.y, window.x + window.width, window.y + window.height)
            )
        ImageDraw.Draw(frame).rectangle(
            (0, 0, frame.width - 1, frame.height - 1),
            outline=(220, 30, 30),
            width=2,
        )
        frame.save(target)
        frames.append(target)

    fake_desktop.screenshot = screenshot

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is False
    assert result.reason == "transparent"
    assert result.transparent_frames == len(frames)


def test_dark_content_with_small_contrast_passes(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """500 changed pixels keep identical_share below 0.999 and mean diff low."""
    _freeze_verify_clock(monkeypatch)
    frames: list[Path] = []

    def screenshot(window: Window, dest: Path) -> None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(fake_desktop.screen_screenshots[-1]) as image:
            frame = image.convert("RGB").crop(
                (window.x, window.y, window.x + window.width, window.y + window.height)
            )
        ImageDraw.Draw(frame).rectangle((50, 50, 74, 69), fill=(90, 90, 90))
        frame.save(target)
        frames.append(target)

    fake_desktop.screenshot = screenshot

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is True
    assert result.reason == "passed"
    assert result.transparent_frames == 0
    assert len(frames) >= 2


class _NoBackgroundDesktop:
    name = "no-background"
    capabilities = {"windows", "screenshot"}


def test_desktop_without_screen_capture_is_unsupported(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    with pytest.raises(UnsupportedEnvironment, match="screen capture"):
        verify_app(
            profile,
            paths,
            fake_backend,
            _NoBackgroundDesktop(),
            timeout=1,
        )

    assert fake_backend.wine_calls == []
    assert fake_backend.runs == []


def test_fully_transparent_frames_report_transparent_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    _apply_alpha(["transparent"], fake_desktop)

    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=100,
            poll_s=2,
            capture_s=10,
        )

    assert result.passed is False
    assert result.reason == "transparent"
    assert result.first_paint_s is None
    assert result.transparent_frames == len(fake_desktop.frames)
    assert "opacity=unknown" in caplog.text
    assert "alpha=0..0" in caplog.text


def test_transparent_frames_followed_by_content_still_pass(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    _apply_alpha(["transparent", "transparent", "content", "content"], fake_desktop)

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is True
    assert result.reason == "passed"
    assert result.first_paint_s == 20.0
    assert result.transparent_frames == 2


def test_a_zero_opacity_window_is_never_a_pass(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    window = Window("0x1", 4242, "Example - Main", "Example", 10, 20, 800, 600, 0.0)
    fake_desktop.window = window
    fake_desktop.windows_result = [window]
    fake_desktop.wait_result = window

    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=100,
            poll_s=2,
            capture_s=10,
        )

    assert result.passed is False
    assert result.reason == "transparent"
    assert result.transparent_frames == len(fake_desktop.screenshots)
    assert "opacity=0.00" in caplog.text


def test_rgb_screenshots_keep_working_without_alpha_stats(
    caplog: pytest.LogCaptureFixture,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    with caplog.at_level(logging.INFO, logger="winbridge"):
        result = verify_app(
            profile,
            paths,
            fake_backend,
            fake_desktop,
            timeout=1,
            poll_s=0.001,
            capture_s=0.001,
        )

    assert result.passed is True
    assert result.transparent_frames == 0
    assert "alpha=" not in caplog.text


def test_picks_the_largest_matching_main_window(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """A 160x20 logo strip must not win over the onboarding window."""
    tiny = Window("0x1", 4242, "Example logo", "steam_proton", 0, 0, 160, 20)
    main = Window("0x2", 4242, "Example App", "steam_proton", 0, 0, 1280, 800)
    fake_desktop.windows_result = [tiny, main]
    # The adapter's own pid hint can be the first (tiny) window.
    fake_desktop.wait_result = tiny

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is True
    assert [window.id for window, _ in fake_desktop.captures] == ["0x2", "0x2"]
    for _, frame in fake_desktop.captures:
        with Image.open(frame) as image:
            assert image.size == (1280, 800)


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (160, 20),
        (0, 0),
        (199, 600),
        (800, 149),
        (200, 149),
    ],
)
def test_sub_threshold_window_is_not_verified(
    width: int,
    height: int,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    tiny = Window("0x1", 4242, "Example logo", "steam_proton", 0, 0, width, height)
    fake_desktop.windows_result = [tiny]
    fake_desktop.wait_result = tiny

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=0.001,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.frames == []
    assert fake_desktop.screenshots == []


def test_two_consecutive_frames_must_come_from_the_same_window(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    first = Window("0xA", 4242, "Example App", "Example", 0, 0, 800, 600)
    second = Window("0xB", 4242, "Example App", "Example", 0, 0, 800, 600)
    sequence = iter([[], [first], [second], [second]])

    def windows() -> list[Window]:
        return next(sequence, [second])

    fake_desktop.windows = windows
    fake_desktop.wait_result = None

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is True
    assert [window.id for window, _ in fake_desktop.captures] == ["0xA", "0xB", "0xB"]
    assert result.first_paint_s == 0.0
    assert [path.name for path in result.frames] == ["frame-0001.png", "frame-0003.png"]


def test_first_paint_is_the_first_non_blank_frame_not_the_confirming_one(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    pattern = [True, True, False, False]
    original = fake_desktop.screenshot

    def screenshot(window: Window, dest: Path) -> None:
        index = min(len(fake_desktop.screenshots), len(pattern) - 1)
        fake_desktop.blank_screenshots = pattern[index]
        original(window, dest)

    fake_desktop.screenshot = screenshot

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is True
    # Captures: t=0 blank, t=10 blank, t=20 first content, t=30 confirm.
    assert result.first_paint_s == 20.0
    assert len(fake_desktop.screenshots) == 4
    assert [path.name for path in result.frames] == ["frame-0001.png", "frame-0004.png"]
    assert not (result.frames[0].parent / "frame-0002.png").exists()
    assert not (result.frames[0].parent / "frame-0003.png").exists()


def test_blank_frames_report_transparent_and_keep_only_first_and_last(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    fake_desktop.blank_screenshots = True

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
    )

    assert result.passed is False
    assert result.reason == "transparent"
    assert result.first_paint_s is None
    assert result.transparent_frames == len(fake_desktop.screenshots)
    assert len(result.frames) == 2
    assert len(fake_desktop.screenshots) > 3
    run_dir = result.frames[0].parent
    assert result.frames[0].name == "frame-0001.png"
    assert (run_dir / "background.png").is_file()
    assert sorted(path.name for path in run_dir.glob("*.png")) == [
        "background.png",
        "frame-0001.png",
        result.frames[-1].name,
    ]


def test_keep_all_keeps_every_blank_capture(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    _freeze_verify_clock(monkeypatch)
    fake_desktop.blank_screenshots = True

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=100,
        poll_s=2,
        capture_s=10,
        keep_all=True,
    )

    assert result.passed is False
    assert len(fake_desktop.screenshots) > 3
    assert len(result.frames) == len(fake_desktop.screenshots)
    assert len(list(result.frames[0].parent.glob("frame-*.png"))) == len(result.frames)
    assert (result.frames[0].parent / "background.png").is_file()


def test_verify_kills_first_and_leaves_the_app_running(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    kill_index = fake_backend.calls.index("wineserver -k")
    run_index = next(
        index for index, call in enumerate(fake_backend.calls) if call.startswith("run ")
    )
    assert kill_index < run_index
    assert fake_backend.wine_calls.count("wineserver -k") == 1
    assert len(fake_backend.runs) == 1


def test_watch_launch_observes_without_killing_or_launching(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    run_dir = tmp_path / "watch"
    run_dir.mkdir()
    background = capture_background(fake_desktop, run_dir)
    events: list[tuple[str, str]] = []
    launch = Launch(popen=fake_backend.popen, pid_tree={4242})

    result = watch_launch(
        launch,
        paths.prefix_for(profile.id),
        fake_desktop,
        run_dir,
        background,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
        on_stage=lambda stage, status: events.append((stage, status)),
    )

    assert result.passed is True
    assert result.reason == "passed"
    assert result.evidence_dir == run_dir
    assert fake_backend.runs == []
    assert fake_backend.wine_calls == []
    assert events == [
        ("window", "found"),
        ("paint", "visible"),
        ("paint", "stable"),
    ]


def test_watch_launch_pre_loop_desktop_failure_is_a_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """A compositor failure before the first capture is a failed result."""
    run_dir = tmp_path / "watch"
    run_dir.mkdir()
    background = capture_background(fake_desktop, run_dir)

    def broken_windows() -> list[Window]:
        raise DesktopError("no compositor windows")

    monkeypatch.setattr(fake_desktop, "windows", broken_windows)

    result = watch_launch(
        Launch(popen=fake_backend.popen, pid_tree={4242}),
        paths.prefix_for(profile.id),
        fake_desktop,
        run_dir,
        background,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert "desktop tool failure" in result.reason
    assert result.frames == []
    assert result.evidence_dir == run_dir


def test_window_must_belong_to_the_launch_pid_tree(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    fake_desktop.windows_result = [Window("0x9", 999, "Other", "Other", 0, 0, 10, 10)]
    fake_desktop.wait_result = None

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=0.001,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert result.frames == []
    assert fake_desktop.screenshots == []
    assert fake_desktop.waits[0][0] == {4242}


def test_reparented_window_is_matched_by_prefix_processes(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """Wine reparents the app to PID 1; the launch tree no longer owns it."""
    reparented = Window(
        "0x2", 698, "Example App Onboarding", "steam_proton", 0, 0, 800, 600
    )
    fake_desktop.window = reparented
    fake_desktop.windows_result = [reparented]
    fake_desktop.wait_result = reparented
    prefix = paths.prefix_for(profile.id)
    probes: list[Path] = []

    def probe(current: Path) -> set[int]:
        probes.append(current)
        return {698}

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
        proc_probe=probe,
    )

    assert result.passed is True
    assert result.window_class == "steam_proton"
    assert set(probes) == {prefix}
    assert fake_desktop.waits[0][0] == {4242, 698}


def test_prefix_processes_are_reprobed_each_poll(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """The reparented pid may only appear after the launch's first poll."""
    late = Window("0x2", 698, "Unknown", "steam_proton", 0, 0, 800, 600)
    fake_desktop.windows_result = []
    fake_desktop.wait_result = None
    probes: list[Path] = []

    def probe(current: Path) -> set[int]:
        probes.append(current)
        return set() if len(probes) == 1 else {698}

    def wait_for_window(pids: set[int], timeout: float) -> Window | None:
        fake_desktop.waits.append((set(pids), timeout))
        return late if 698 in pids else None

    fake_desktop.wait_for_window = wait_for_window

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
        proc_probe=probe,
    )

    assert result.passed is True
    assert len(probes) >= 2
    assert any(pids == {4242, 698} for pids, _ in fake_desktop.waits)


def test_unrelated_window_is_not_matched(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    unrelated = Window("0x9", 999, "System Settings", "gnome-settings", 0, 0, 10, 10)
    fake_desktop.windows_result = [unrelated]
    fake_desktop.wait_result = None

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=0.001,
        poll_s=0.001,
        capture_s=0.001,
        proc_probe=lambda _: {698},
    )

    assert result.passed is False
    assert result.reason == "timeout"
    assert result.frames == []
    assert fake_desktop.screenshots == []


def test_identity_matching_window_without_a_prefix_pid_is_not_accepted(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    """Title/class is not evidence: the window must belong to the prefix."""
    unrelated = Window(
        "0x2", 999, "Example App - Welcome", "Example", 0, 0, 1280, 800
    )
    fake_desktop.windows_result = [unrelated]
    fake_desktop.wait_result = unrelated

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=0.001,
        poll_s=0.001,
        capture_s=0.001,
        proc_probe=lambda _: set(),
    )

    assert result.passed is False
    assert result.frames == []
    assert fake_desktop.screenshots == []


class _UnsupportedDesktop:
    name = "unsupported"
    capabilities: set[str] = set()


class _NoScreenshotDesktop:
    name = "no-screenshot"
    capabilities = {"windows"}


@pytest.mark.parametrize(
    ("desktop", "detail"),
    [
        (None, "no adapter"),
        (_UnsupportedDesktop(), "unsupported"),
        (_NoScreenshotDesktop(), "screenshot"),
    ],
)
def test_verify_refuses_desktops_that_cannot_capture(
    desktop: Any,
    detail: str,
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
) -> None:
    with pytest.raises(UnsupportedEnvironment, match=detail):
        verify_app(profile, paths, fake_backend, desktop, timeout=1)

    assert fake_backend.wine_calls == []
    assert fake_backend.runs == []


def test_evidence_uses_the_installer_filename_when_no_registry_value(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    evidence = paths.evidence / "example-app" / "example-setup.exe"
    assert result.frames
    assert result.frames[0].parent.parent == evidence


def test_evidence_uses_unknown_when_there_is_no_installer(
    make_profile: Any,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    profile = make_profile(installer=None)

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.frames[0].parent.parent == paths.evidence / "example-app" / "unknown"


def test_desktop_tool_failure_returns_a_failed_result(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def broken_capture(window: Window, dest: Path) -> None:
        raise DesktopError("grim exited 1")

    fake_desktop.screenshot = broken_capture

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert "desktop tool failure" in result.reason
    assert "grim exited 1" in result.reason


def test_corrupt_frame_is_a_failed_result_and_stays_as_evidence(
    profile: Profile,
    paths: Paths,
    fake_backend: Any,
    fake_desktop: Any,
) -> None:
    def corrupt_capture(window: Window, dest: Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"not a png")

    fake_desktop.screenshot = corrupt_capture

    result = verify_app(
        profile,
        paths,
        fake_backend,
        fake_desktop,
        timeout=1,
        poll_s=0.001,
        capture_s=0.001,
    )

    assert result.passed is False
    assert "cannot read captured frame" in result.reason
    assert result.frames
    assert result.frames[0].is_file()


# --- CLI ---------------------------------------------------------------------


def make_context(
    paths: Paths, catalog: Catalog, backend: Any, desktop: Any
) -> Context:
    return Context(
        paths=paths,
        config=Config(),
        catalog=catalog,
        backend=backend,
        desktop=desktop,
    )


def invoke(*args: str, paths: Paths, context: Context | None = None) -> Any:
    payload: dict[str, Any] = {"paths": paths}
    if context is not None:
        payload["context"] = context
    return CliRunner().invoke(app, list(args), obj=payload)


def test_cli_verify_updates_the_profile_and_prints_cursor_limitation(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    frame = paths.evidence / "example-app" / "v1" / "run" / "frame-0001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"png")
    seen: dict[str, Any] = {}

    def fake_verify(
        called_profile: Profile,
        called_paths: Paths,
        called_backend: Any,
        called_desktop: Any,
        **kwargs: Any,
    ) -> VerifyResult:
        seen["profile"] = called_profile.id
        seen.update(kwargs)
        return VerifyResult(True, 3.5, "Example", [frame], "passed")

    monkeypatch.setattr(cli_module, "verify_app", fake_verify)

    result = invoke(
        "verify",
        "example-app",
        "--timeout",
        "30",
        "--keep-all",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert seen == {"profile": "example-app", "timeout": 30, "keep_all": True}
    assert "PASS" in result.stdout
    assert "soda@11.0-10" in result.stdout
    assert "cursor not checked" in result.stdout
    assert str(frame) in result.stdout
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.result == "pass"
    assert loaded.last_verified.app_version == "example-setup.exe"
    assert loaded.last_verified.first_paint_s == 3.5
    assert loaded.last_verified.at


def test_cli_verify_preserves_unknown_last_verified_keys(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    assert profile.last_verified is not None
    profile.last_verified.extra["window_class"] = "Legacy"
    write_profile(paths.root, profile)
    monkeypatch.setattr(
        cli_module,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(True, 1.0, "Example", [], "passed"),
    )

    result = invoke(
        "verify",
        "example-app",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.extra == {"window_class": "Legacy"}


def test_cli_verify_json_shape(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    frame = paths.evidence / "example-app" / "v1" / "run" / "frame-0001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"png")
    monkeypatch.setattr(
        cli_module,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(True, 3.5, "Example", [frame], "passed"),
    )

    result = invoke(
        "verify",
        "example-app",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "app": "example-app",
        "app_version": "example-setup.exe",
        "runner": "soda@11.0-10",
        "desktop": "fake",
        "passed": True,
        "result": "pass",
        "reason": "passed",
        "first_paint_s": 3.5,
        "window_class": "Example",
        "frames": [str(frame)],
        "evidence": str(frame.parent),
        "cursor_checked": False,
        "transparent_frames": 0,
    }


def test_cli_verify_json_includes_transparent_frames(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend, fake_desktop)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(
        cli_module,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(
            False, None, None, [], "transparent", transparent_frames=3
        ),
    )
    monkeypatch.setattr(sys, "argv", ["winbridge", "verify", "example-app", "--json"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "transparent"
    assert payload["transparent_frames"] == 3


def test_cli_verify_json_reports_background_evidence_without_frames(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend, fake_desktop)
    run_dir = paths.evidence / "example-app" / "v1" / "background-failure"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(
        cli_module,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(
            False,
            None,
            None,
            [],
            "background",
            evidence_dir=run_dir,
        ),
    )
    monkeypatch.setattr(sys, "argv", ["winbridge", "verify", "example-app", "--json"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "background"
    assert payload["frames"] == []
    assert payload["evidence"] == str(run_dir)


def test_cli_verify_failure_exits_4_and_persists_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend, fake_desktop)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(
        cli_module,
        "verify_app",
        lambda *args, **kwargs: VerifyResult(False, 1.0, "Example", [], "timeout"),
    )
    monkeypatch.setattr(sys, "argv", ["winbridge", "verify", "example-app"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 4
    assert "verify failed: timeout" in capsys.readouterr().err
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.result == "fail"
    assert loaded.last_verified.first_paint_s == 1.0


def test_cli_verify_tool_failure_exits_4_and_persists_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend, fake_desktop)

    def broken_capture(window: Window, dest: Path) -> None:
        raise DesktopError("grim exited 1")

    fake_desktop.screenshot = broken_capture
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(sys, "argv", ["winbridge", "verify", "example-app", "--timeout", "1"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 4
    assert "verify failed" in capsys.readouterr().err
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.result == "fail"


def test_cli_verify_json_unsupported_desktop_still_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)

    def unsupported(*args: Any, **kwargs: Any) -> VerifyResult:
        raise UnsupportedEnvironment("desktop not supported: no screenshot tool")

    monkeypatch.setattr(cli_module, "verify_app", unsupported)

    result = invoke(
        "verify",
        "example-app",
        "--json",
        paths=paths,
        context=make_context(paths, catalog, fake_backend, fake_desktop),
    )

    payload = json.loads(result.output)
    assert payload["result"] == "skipped"
    assert payload["passed"] is False
    assert "no screenshot tool" in payload["reason"]


def test_cli_verify_unsupported_desktop_exits_3_and_records_skipped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    catalog: Catalog,
    fake_backend: Any,
    fake_desktop: Any,
    write_profile: Any,
    profile: Profile,
) -> None:
    write_profile(paths.root, profile)
    context = make_context(paths, catalog, fake_backend, fake_desktop)

    def unsupported(*args: Any, **kwargs: Any) -> VerifyResult:
        raise UnsupportedEnvironment("desktop not supported: no screenshot tool")

    monkeypatch.setattr(cli_module, "_paths", lambda ctx: paths)
    monkeypatch.setattr(cli_module, "_profile_context", lambda ctx, item: context)
    monkeypatch.setattr(cli_module, "verify_app", unsupported)
    monkeypatch.setattr(sys, "argv", ["winbridge", "verify", "example-app"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 3
    assert "desktop not supported" in capsys.readouterr().err
    loaded = Profile.load(paths.profiles / "example-app.toml")
    assert loaded.last_verified is not None
    assert loaded.last_verified.result == "skipped"
    assert loaded.last_verified.first_paint_s is None
    assert loaded.last_verified.app_version == "example-setup.exe"
