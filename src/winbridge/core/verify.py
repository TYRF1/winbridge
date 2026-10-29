"""Visible-render smoke harness.

Verify owns the prefix for the duration of the check: it kills anything running
there, captures the bare desktop, launches through the same path as ``run``,
finds the window by process membership rather than by title, and captures frames
on a schedule. The pre-launch background is mandatory for a supported adapter:
a transparent window can show wallpaper that looks like content, so a missing,
corrupt, or geometrically incompatible background fails with
``reason="background"`` instead of passing on blank/alpha alone. Wine reparents
an app to PID 1, so the launch's pid tree alone is not enough: every poll
combines it with the processes whose ``WINEPREFIX`` is this prefix. Only a
window owned by that candidate set can be verified; a title/class match without
a prefix process is never accepted. Among matching windows, only main-sized ones
are eligible and the largest wins, so a tiny helper/splash window cannot become
the evidence. A frame counts as rendered only when two consecutive captures are
visible **and come from the same window id**, so a half-painted frame or a
window switch cannot pass. Visible means non-blank and not identical to the
pre-launch desktop background: a transparent window lets wallpaper show through
and must not be mistaken for app content. Evidence is left behind for a human to
look at; no automated check is trusted over the frames themselves.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from winbridge.backends.base import Backend
from winbridge.core.detect import detect_app_version
from winbridge.core.errors import UnsupportedEnvironment
from winbridge.core.locking import file_lock
from winbridge.core.prefix import kill_prefix
from winbridge.core.profile import Profile
from winbridge.core.run import Launch, StageCallback, run_app
from winbridge.core.state import Paths
from winbridge.desktops.base import Desktop, DesktopError, Window
from winbridge.util.log import evidence_dir
from winbridge.util.proc import processes_for_prefix

__all__ = [
    "BACKGROUND_REASON",
    "TIMEOUT_REASON",
    "TRANSPARENT_REASON",
    "BackgroundUnavailable",
    "VerifyResult",
    "capture_background",
    "is_blank",
    "require_desktop",
    "verify_app",
    "watch_launch",
]

_LOGGER = logging.getLogger("winbridge")

_REQUIRED_CAPABILITIES = frozenset({"windows", "screenshot"})
_DEFAULT_STDDEV_THRESHOLD = 3.0
_DEFAULT_DOMINANT_SHARE = 0.98
_HISTOGRAM_BINS = 16
_PASS_REASON = "passed"
#: Public so setup can distinguish a timeout from a tool/unsupported failure.
TIMEOUT_REASON = "timeout"
_TIMEOUT_REASON = TIMEOUT_REASON
#: A mapped window can be invisible for the whole run. That is not a timeout:
#: Wine/WebView2 simply painted nothing into a fully transparent surface.
TRANSPARENT_REASON = "transparent"
#: The background comparison is mandatory for a supported desktop. A failed,
#: missing, corrupt, or geometrically incompatible background fails closed.
BACKGROUND_REASON = "background"
#: Alpha at or below this level carries no visible content. PNG captures from
#: X11/RGB keep no alpha channel and are judged by the blank heuristic alone.
_ALPHA_TRANSPARENT_MAX = 8
#: Pre-launch desktop capture used to tell wallpaper from app content.
_BACKGROUND_FILENAME = "background.png"
#: A frame is desktop-identical only when both signals agree. The strict AND
#: keeps dark content with small contrast from being mistaken for wallpaper.
_BACKGROUND_MEAN_DIFF = 0.5
_BACKGROUND_IDENTICAL_SHARE = 0.999
#: Ignore a thin compositor border/shadow ring when comparing the interior.
_BACKGROUND_INSET_PX = 2
_FRAME_PREFIX = "frame"
#: A zero capture interval would spin without making progress; keep a floor.
_MIN_INTERVAL_S = 0.001
#: Helper/splash windows are not the app's main surface. The onboarding UI is
#: full-size; a 160x20 logo strip must never become the verify evidence.
_MIN_MAIN_WINDOW_WIDTH = 200
_MIN_MAIN_WINDOW_HEIGHT = 150
_MIN_MAIN_WINDOW_AREA = 30_000


class BackgroundUnavailable(Exception):
    """The mandatory background signal cannot be used for this run."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of one smoke run.

    ``first_paint_s`` is the timestamp of the first visible frame, not the
    confirming frame that made the run pass. Visible means both non-blank and
    not identical to the pre-launch desktop background; a transparent window
    showing wallpaper is not content. ``frames`` lists only the evidence
    retained on disk (first, passing, and last, or every capture with
    ``keep_all``); the mandatory background capture is kept beside them.
    ``cursor_checked`` is always ``False``: the default runner can draw without
    a cursor, and verify does not inspect it. ``transparent_frames`` counts
    captures with no visible content: blank, alpha-transparent, opacity 0, or
    background-identical. It is diagnostic only and never turns a failed run
    into a pass. ``evidence_dir`` is the run directory even when no frame was
    captured, so a background-only failure still has a place to look.
    """

    passed: bool
    first_paint_s: float | None
    window_class: str | None
    frames: list[Path]
    reason: str
    cursor_checked: bool = False
    #: Captures with no visible content: blank, fully transparent (alpha max
    #: near zero), compositor opacity 0, or identical to the pre-launch desktop.
    #: Reported for diagnosis; never a pass.
    transparent_frames: int = 0
    #: The verify run/evidence directory, independent of retained frames.
    evidence_dir: Path | None = None


def is_blank(
    image: Path,
    *,
    stddev_threshold: float = _DEFAULT_STDDEV_THRESHOLD,
    dominant_share: float = _DEFAULT_DOMINANT_SHARE,
) -> bool:
    """Decide whether a frame has no visible content.

    Grayscale standard deviation catches a flat window; the dominant 16-bin
    histogram share catches a window that is almost one color with a few stray
    pixels (for example a wedged WebView2 leaving a hairline of chrome). Either
    signal alone is a blank frame.
    """
    with Image.open(image) as opened:
        gray = opened.convert("L")

    stddev = float(ImageStat.Stat(gray).stddev[0])
    histogram = gray.histogram()
    total = sum(histogram)
    if total <= 0:
        return True
    bins = [0] * _HISTOGRAM_BINS
    for value, count in enumerate(histogram):
        bins[value * _HISTOGRAM_BINS // len(histogram)] += count
    share = max(bins) / total
    return stddev < stddev_threshold or share > dominant_share


def _alpha_range(image: Path) -> tuple[int, int] | None:
    """Return the alpha-channel range, or ``None`` when the frame has no alpha.

    RGB PNGs from ``grim``/``import`` are opaque by construction and keep their
    existing blank check; only captures that actually carry alpha are judged on
    visibility too. An alpha channel is 8-bit, so its histogram ends are the
    min and max; summing it avoids Pillow's union-typed ``getextrema``.
    """
    with Image.open(image) as opened:
        if "A" not in opened.getbands():
            return None
        histogram = opened.getchannel("A").histogram()
    used = [value for value, count in enumerate(histogram) if count]
    if not used:
        return None
    return used[0], used[-1]


def capture_background(desktop: Desktop, run_dir: Path) -> Path:
    """Capture and validate the pre-launch desktop; failure is fatal.

    The background is the only signal that distinguishes wallpaper from app
    content on a transparent window, so a missing or unreadable capture cannot
    fall back to a blank/alpha pass. The caller turns the exception into a
    failed result with ``reason="background"``.
    """
    capture = getattr(desktop, "screenshot_screen", None)
    if not callable(capture):
        raise BackgroundUnavailable("desktop adapter has no screen capture")

    path = run_dir / _BACKGROUND_FILENAME
    try:
        capture(path)
    except Exception as exc:
        raise BackgroundUnavailable(f"screen capture failed: {exc}") from exc

    if not path.is_file():
        raise BackgroundUnavailable(f"screen capture wrote no file: {path}")

    try:
        with Image.open(path) as opened:
            if opened.width <= 0 or opened.height <= 0:
                raise BackgroundUnavailable("screen capture has empty dimensions")
            opened.verify()
    except BackgroundUnavailable:
        raise
    except Exception as exc:
        raise BackgroundUnavailable(f"screen capture is not a usable image: {exc}") from exc

    _LOGGER.info("[verify] background: %s", path)
    return path


def _background_difference(
    frame: Path, background: Path, window: Window
) -> tuple[float, float]:
    """Return ``(mean_abs_diff, identical_share)`` for the window region.

    The pre-launch full-screen capture is cropped to the window geometry. A
    frame that matches it is wallpaper showing through a transparent window, not
    app content. Geometry or crop incompatibility is fatal: falling back to
    blank/alpha would let a transparent window pass, so the caller reports
    ``background`` instead.
    """
    try:
        with Image.open(frame) as opened:
            frame_image = opened.convert("RGB")
    except OSError:
        # A corrupt frame is handled by the existing frame-failure path.
        raise

    try:
        with Image.open(background) as opened:
            screen = opened.convert("RGB")
    except Exception as exc:
        raise BackgroundUnavailable(f"cannot read background: {exc}") from exc

    left, top = window.x, window.y
    right, bottom = left + window.width, top + window.height
    if left < 0 or top < 0 or right > screen.width or bottom > screen.height:
        raise BackgroundUnavailable(
            f"window {window.id} rect {left},{top} {window.width}x{window.height} "
            f"is outside the {screen.width}x{screen.height} background"
        )

    inset = _BACKGROUND_INSET_PX
    frame_crop = frame_image.crop(
        (inset, inset, frame_image.width - inset, frame_image.height - inset)
    )
    background_crop = screen.crop(
        (left + inset, top + inset, right - inset, bottom - inset)
    )
    if frame_crop.size != background_crop.size:
        raise BackgroundUnavailable(
            f"frame {frame.name} crop is {frame_crop.width}x{frame_crop.height} "
            f"but background crop is {background_crop.width}x{background_crop.height}"
        )
    if frame_crop.width <= 0 or frame_crop.height <= 0:
        raise BackgroundUnavailable("window is too small for the background inset")

    difference = ImageChops.difference(frame_crop, background_crop)
    means = ImageStat.Stat(difference).mean
    mean_abs = sum(means) / len(means) if means else 0.0
    histogram = difference.convert("L").histogram()
    total = sum(histogram)
    identical = histogram[0] / total if total else 0.0
    return mean_abs, identical


def _diff_text(diff: tuple[float, float] | None) -> str:
    """Format the per-frame background comparison for the verify log."""
    if diff is None:
        return ", diff=unavailable"
    return f", diff={diff[0]:.3f}, identical={diff[1]:.3f}"


def verify_app(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    desktop: Desktop | None,
    *,
    timeout: float = 300,
    poll_s: float = 2.0,
    capture_s: float = 10.0,
    keep_all: bool = False,
    proc_probe: Callable[[Path], set[int]] = processes_for_prefix,
) -> VerifyResult:
    """Run verify under the per-app lock, waiting for any live command.

    Setup and update already hold this lock; ``file_lock`` is re-entrant in the
    same thread, so their nested verify call does not deadlock. A standalone
    verify queues instead of racing setup/update/run/unbridge.
    """
    lock = paths.locks / f"app-{profile.id}.lock"
    with file_lock(lock):
        return _verify_app_locked(
            profile,
            paths,
            backend,
            desktop,
            timeout=timeout,
            poll_s=poll_s,
            capture_s=capture_s,
            keep_all=keep_all,
            proc_probe=proc_probe,
        )


def _verify_app_locked(
    profile: Profile,
    paths: Paths,
    backend: Backend,
    desktop: Desktop | None,
    *,
    timeout: float = 300,
    poll_s: float = 2.0,
    capture_s: float = 10.0,
    keep_all: bool = False,
    proc_probe: Callable[[Path], set[int]] = processes_for_prefix,
) -> VerifyResult:
    """Claim the prefix, capture the bare desktop, launch, then watch.

    Verify owns the prefix: the previous instance is killed before the
    background capture, otherwise its window would become part of the
    "desktop" and a transparent replacement could match stale app pixels. The
    launch uses ``restart=False`` so that kill is not repeated and the
    background cannot contain a previous instance. Observation itself is
    shared with ``run --wait`` through :func:`watch_launch`.
    """
    adapter = require_desktop(desktop)
    version = _app_version(profile, paths, backend)
    run_dir = evidence_dir(paths, profile.id, version)
    prefix = paths.prefix_for(profile.id)
    _LOGGER.info("[verify] evidence: %s", run_dir)
    _LOGGER.info("[verify] app version: %s", version)

    # Verify owns the prefix: kill the previous instance before the background
    # capture, otherwise its window would become part of the "desktop" and a
    # transparent replacement could match stale app pixels.
    kill_prefix(backend, prefix)
    try:
        background = capture_background(adapter, run_dir)
    except BackgroundUnavailable as exc:
        _LOGGER.error("[verify] background unavailable: %s", exc)
        return _background_failure(run_dir)

    started = time.monotonic()
    # The prefix was killed above, so this launch must not kill again.
    launch = run_app(profile, paths, backend, restart=False)
    return watch_launch(
        launch,
        prefix,
        adapter,
        run_dir,
        background,
        started=started,
        timeout=timeout,
        poll_s=poll_s,
        capture_s=capture_s,
        keep_all=keep_all,
        proc_probe=proc_probe,
    )


def watch_launch(
    launch: Launch,
    prefix: Path,
    desktop: Desktop,
    run_dir: Path,
    background: Path,
    *,
    started: float | None = None,
    timeout: float = 300,
    poll_s: float = 2.0,
    capture_s: float = 10.0,
    keep_all: bool = False,
    on_stage: StageCallback | None = None,
    proc_probe: Callable[[Path], set[int]] = processes_for_prefix,
) -> VerifyResult:
    """Observe an already-started launch and prove it renders.

    A frame is captured immediately (at ``t=0`` relative to ``started``) when
    a window is already mapped, then every ``capture_s``. While no window is
    mapped the watcher polls every ``poll_s``. Passing needs two consecutive
    visible frames from the same window. Each window frame is cropped to the
    window geometry and compared against the pre-launch ``background``. A
    capture that is blank, has a fully transparent alpha channel, reports
    opacity 0, or matches the background never counts as content. A failed,
    missing, corrupt, or geometrically incompatible background fails closed
    with ``reason="background"``; it never falls back to a blank/alpha pass. On
    timeout the result is a failure with ``reason="timeout"``; when every
    observed capture was invisible the reason is ``"transparent"``.

    The candidate pid set is the launch's tree plus the prefix's live processes
    (``proc_probe``, re-run each poll), because Wine reparents the app to PID 1.
    A window is only eligible when its pid is in that set; title/class alone is
    never enough. Windows smaller than main-window thresholds are ignored; the
    largest eligible match wins.

    ``on_stage`` reports ``("window", "found")`` once, ``("paint", "visible")``
    on the first content frame, and ``("paint", "stable")`` on the confirming
    frame. The watcher never kills or launches; the caller owns both.
    """
    if started is None:
        started = time.monotonic()

    def _stage(stage: str, status: str) -> None:
        if on_stage is not None:
            on_stage(stage, status)

    captures: list[Path] = []
    first_frame: Path | None = None
    last_frame: Path | None = None
    passing_frame: Path | None = None
    first_paint: float | None = None
    window_class: str | None = None
    streak = 0
    streak_window: str | None = None
    transparent_frames = 0
    next_capture = started
    poll_interval = max(min(poll_s, capture_s), _MIN_INTERVAL_S)
    capture_interval = max(capture_s, _MIN_INTERVAL_S)
    timeout = max(timeout, 0.0)

    window: Window | None = None
    window_found = False
    failure: str | None = None
    try:
        # Capture immediately if a matching main window is already mapped. After
        # that, every capture re-resolves the window through the adapter. The
        # first lookup runs inside the guard too: a compositor that is already
        # failing is a failed wait (exit 4), not an uncaught launch error.
        window = _locate_window(
            desktop,
            _candidate_pids(launch.pid_tree, prefix, proc_probe),
        )
        if window is not None:
            window_class = window.class_name
            window_found = True
            _stage("window", "found")

        while True:
            now = time.monotonic()
            if now >= next_capture:
                if window is None:
                    window = _locate_window(
                        desktop,
                        _candidate_pids(launch.pid_tree, prefix, proc_probe),
                    )
                    if window is not None:
                        window_class = window.class_name
                        if not window_found:
                            window_found = True
                            _stage("window", "found")
                if window is None:
                    if now - started >= timeout:
                        break
                    next_capture = now + poll_interval
                    continue

                timestamp = time.monotonic() - started
                window_id = window.id
                opacity = window.opacity
                frame = _capture(desktop, window, run_dir, len(captures) + 1)
                captures.append(frame)
                if first_frame is None:
                    first_frame = frame
                last_frame = frame
                blank = is_blank(frame)
                alpha = _alpha_range(frame)
                try:
                    diff = _background_difference(frame, background, window)
                except BackgroundUnavailable as exc:
                    failure = BACKGROUND_REASON
                    _LOGGER.error("[verify] background diff failed: %s", exc)
                    break
                background_match = (
                    diff[0] <= _BACKGROUND_MEAN_DIFF
                    and diff[1] >= _BACKGROUND_IDENTICAL_SHARE
                )
                transparent = (
                    (alpha is not None and alpha[1] <= _ALPHA_TRANSPARENT_MAX)
                    or opacity == 0.0
                    or background_match
                )
                invisible = blank or transparent
                if invisible:
                    transparent_frames += 1
                _LOGGER.info(
                    "[verify] window %s at %.2fs: %dx%d+%d+%d opacity=%s",
                    window_id,
                    timestamp,
                    window.width,
                    window.height,
                    window.x,
                    window.y,
                    "unknown" if opacity is None else f"{opacity:.2f}",
                )
                _LOGGER.info(
                    "[verify] frame %d at %.2fs: %s (window %s%s%s)",
                    len(captures),
                    timestamp,
                    "transparent" if transparent else ("blank" if blank else "content"),
                    window_id,
                    "" if alpha is None else f", alpha={alpha[0]}..{alpha[1]}",
                    _diff_text(diff),
                )
                window = None
                if invisible:
                    streak = 0
                    streak_window = None
                else:
                    if first_paint is None:
                        first_paint = timestamp
                        _stage("paint", "visible")
                    if window_id == streak_window:
                        streak += 1
                    else:
                        streak = 1
                        streak_window = window_id
                    if streak >= 2:
                        passing_frame = frame
                        _stage("paint", "stable")
                        break
                next_capture = time.monotonic() + capture_interval
                continue

            remaining = timeout - (now - started)
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, next_capture - now, remaining))
    except DesktopError as exc:
        # A compositor or capture tool failed. That is a failed verify, not a
        # crash: keep whatever evidence exists and report exit 4.
        failure = f"desktop tool failure: {exc}"
        _LOGGER.warning("[verify] %s", failure)
    except OSError as exc:
        # Pillow's UnidentifiedImageError is an OSError; a truncated or corrupt
        # frame is evidence of a broken capture, not a passing render.
        failure = f"cannot read captured frame: {exc}"
        _LOGGER.warning("[verify] %s", failure)

    passed = failure is None and passing_frame is not None
    if failure is not None:
        reason = failure
    elif passed:
        reason = _PASS_REASON
    elif captures and transparent_frames == len(captures):
        reason = TRANSPARENT_REASON
    else:
        reason = _TIMEOUT_REASON
    frames = _retain(captures, first_frame, last_frame, passing_frame, keep_all=keep_all)
    _LOGGER.info(
        "[verify] %s: first paint %s, window class %s, %d frame(s) kept, "
        "%d transparent, reason %s",
        "PASS" if passed else "FAIL",
        "not observed" if first_paint is None else f"{first_paint:.2f}s",
        window_class or "not found",
        len(frames),
        transparent_frames,
        reason,
    )
    return VerifyResult(
        passed=passed,
        first_paint_s=first_paint,
        window_class=window_class,
        frames=frames,
        reason=reason,
        cursor_checked=False,
        transparent_frames=transparent_frames,
        evidence_dir=run_dir,
    )


def _background_failure(run_dir: Path) -> VerifyResult:
    """Return a failed result when the mandatory background is unusable."""
    return VerifyResult(
        passed=False,
        first_paint_s=None,
        window_class=None,
        frames=[],
        reason=BACKGROUND_REASON,
        evidence_dir=run_dir,
    )


def require_desktop(desktop: Desktop | None) -> Desktop:
    """Refuse to launch when the session cannot observe, capture, or background.

    The check happens before the prefix is killed so an unsupported desktop
    never kills a running app just to discover it cannot verify. A supported
    adapter must also implement ``screenshot_screen``; without the mandatory
    background signal a transparent window could pass as content.
    """
    if desktop is None:
        raise UnsupportedEnvironment("desktop not supported: no adapter detected")
    missing = _REQUIRED_CAPABILITIES - set(desktop.capabilities)
    if missing:
        raise UnsupportedEnvironment(
            f"desktop not supported: {desktop.name} lacks {', '.join(sorted(missing))}"
        )
    if not callable(getattr(desktop, "screenshot_screen", None)):
        raise UnsupportedEnvironment(
            f"desktop not supported: {desktop.name} lacks screen capture"
        )
    return desktop


def _app_version(profile: Profile, paths: Paths, backend: Backend) -> str:
    """Resolve the evidence version from the prefix, installer name, or unknown."""
    installer = profile.installer.path if profile.installer is not None else None
    return detect_app_version(
        backend,
        paths.prefix_for(profile.id),
        profile.exe or None,
        installer,
    )


def _window_area(window: Window) -> int:
    return max(0, window.width) * max(0, window.height)


def _is_main_window(window: Window) -> bool:
    """Reject helper/splash strips; verify wants the app's real surface."""
    return (
        window.width >= _MIN_MAIN_WINDOW_WIDTH
        and window.height >= _MIN_MAIN_WINDOW_HEIGHT
        and _window_area(window) >= _MIN_MAIN_WINDOW_AREA
    )


def _match_window(windows: list[Window], pid_tree: set[int]) -> Window | None:
    """Return the largest main-sized window owned by a candidate pid."""
    eligible = [
        window
        for window in windows
        if window.pid in pid_tree and _is_main_window(window)
    ]
    return max(eligible, key=_window_area, default=None)


def _candidate_pids(
    launch_pids: set[int], prefix: Path, proc_probe: Callable[[Path], set[int]]
) -> set[int]:
    """Combine the launch tree with this prefix's live processes.

    Wine's loader exits after the app is up and the app is reparented to PID 1,
    so the launch tree can be gone by the time the window maps. Re-probing the
    prefix on every poll catches the reparented process.
    """
    return set(launch_pids) | set(proc_probe(prefix))


def _locate_window(desktop: Desktop, pid_tree: set[int]) -> Window | None:
    """Resolve the app's main window from the adapter's current windows.

    ``wait_for_window`` is retained as an adapter hint, but its single result
    is not trusted blindly: the full window list is filtered for size and the
    largest eligible pid/prefix match wins. A window without a candidate pid is
    ignored, whatever its title or class says.
    """
    windows = desktop.windows()
    waited = desktop.wait_for_window(pid_tree, 0.0)
    if waited is not None and all(window.id != waited.id for window in windows):
        windows = [*windows, waited]
    return _match_window(windows, pid_tree)


def _capture(desktop: Desktop, window: Window, run_dir: Path, index: int) -> Path:
    """Capture one frame into the evidence directory."""
    frame = run_dir / f"{_FRAME_PREFIX}-{index:04d}.png"
    desktop.screenshot(window, frame)
    return frame


def _retain(
    captures: list[Path],
    first_frame: Path | None,
    last_frame: Path | None,
    passing_frame: Path | None,
    *,
    keep_all: bool,
) -> list[Path]:
    """Keep the evidence a human needs and delete the rest.

    Default retention is the first frame, the passing frame, and the last
    frame. The first frame shows the window at launch, the passing frame is what
    confirmed the render, and the last frame preserves a failure. ``keep_all``
    skips cleanup entirely.
    """
    if keep_all:
        return list(captures)

    keep = {path for path in (first_frame, last_frame, passing_frame) if path is not None}
    for capture in captures:
        if capture in keep:
            continue
        try:
            capture.unlink()
        except OSError as exc:
            _LOGGER.warning("[verify] cannot remove intermediate frame %s: %s", capture, exc)
    return [capture for capture in captures if capture in keep]
