"""Shared fixtures.

Every test runs with XDG and HOME redirected into its own ``tmp_path`` so no
test can read or write real user state.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from winbridge.core.config import Config
from winbridge.core.profile import (
    ExposedPath,
    InstallerSpec,
    LastVerified,
    PinSpec,
    Profile,
)
from winbridge.core.runner import Catalog
from winbridge.core.state import Paths
from winbridge.core.webview2 import WEBVIEW2_CLIENT_GUID
from winbridge.desktops.base import Window

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.from_root(tmp_path)


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.load(FIXTURES / "catalog.toml")


@pytest.fixture
def make_profile() -> Callable[..., Profile]:
    def _make(**overrides: Any) -> Profile:
        defaults: dict[str, Any] = {
            "schema": 1,
            "id": "example-app",
            "name": "Example App",
            "backend": "wine",
            "runner": "soda@11.0-10",
            "installer": InstallerSpec(
                path="/home/me/downloads/example-setup.exe",
                dir="/home/me/downloads",
                glob="*setup*.exe",
                type="nsis",
                silent_args=["/S"],
            ),
            "exe": "C:\\users\\me\\AppData\\Local\\Programs\\Example\\Example.exe",
            "schemes": ["example-app"],
            "exposed": [
                ExposedPath(
                    host="/home/me/repos",
                    guest="C:\\users\\me\\repos",
                )
            ],
            "pin": PinSpec(match="^Example$"),
            "created_at": "2026-10-27T10:00:00Z",
            "last_verified": LastVerified(
                app_version="1.2.3",
                result="pass",
                first_paint_s=95.0,
                at="2026-10-27T10:04:00Z",
            ),
        }
        defaults.update(overrides)
        return Profile(**defaults)

    return _make


@pytest.fixture
def profile(make_profile: Callable[..., Profile]) -> Profile:
    return make_profile()


@pytest.fixture
def write_profile() -> Callable[[Path, Profile], Path]:
    def _write(root: Path, profile: Profile) -> Path:
        target = Paths.from_root(root).profiles / f"{profile.id}.toml"
        profile.dump(target)
        return target

    return _write


@pytest.fixture
def fake_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runner root whose ``bin`` tools are the recording fake Wine script."""
    runner = tmp_path / "runner"
    (runner / "bin").mkdir(parents=True)
    for name in ("wine", "wineboot", "wineserver"):
        (runner / "bin" / name).symlink_to(FIXTURES / "fake-wine.sh")
    monkeypatch.setenv("FAKE_WINE_LOG", str(tmp_path / "calls.log"))
    return runner


class FakePopen:
    """The smallest Popen stand-in: a live pid plus terminal methods."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _guest_file(prefix: Path, guest: str) -> Path:
    """Map one guest ``C:\\...`` path to its host location inside ``prefix``."""
    drive, separator, rest = guest.strip().partition(":")
    if not separator or drive.upper() != "C":
        raise ValueError(f"guest path must be on C:; got {guest!r}")
    parts = [part for part in rest.replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(f"invalid guest path: {guest!r}")
    return prefix / "drive_c" / Path(*parts)


class FakeBackend:
    """Record every backend call; emulate the registry subset later tasks use."""

    name = "fake"

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.calls: list[str] = []
        self.wine_calls: list[str] = []
        self.wine_timeouts: list[float | None] = []
        self.installed: list[tuple[Path, Path, list[str]]] = []
        #: Bytes of the staged installer as seen at install time (guest path).
        self.install_payloads: list[bytes] = []
        self.install_logs: list[Path | None] = []
        self.install_timeouts: list[float | None] = []
        self.install_saw_log: list[bool] = []
        #: Set to raise ``BackendTimeout`` from ``install``.
        self.install_timeout_error: Exception | None = None
        self.install_exit_code = 0
        #: Per-call exit codes; consumed in order before ``install_exit_code``.
        self.install_exit_codes: list[int] = []
        #: Optional side effect for a successful install, used by setup tests.
        self.on_install: Callable[[Path, Path, list[str]], None] | None = None
        self.runs: list[tuple[Path, str, list[str]]] = []
        self.exposed: list[tuple[Path, Path, str]] = []
        self.removed: list[Path] = []
        self.killed: list[str] = []
        self.reg_values: dict[str, dict[str, str]] = {}
        self.reg_writes: list[tuple[str, str, str]] = []
        #: Registry text returned by ``reg export`` for every hive.
        self.reg_export_text: str | None = None
        #: Per-hive overrides for ``reg export``.
        self.reg_exports: dict[str, str] = {}
        self.popen = FakePopen()
        #: A 64-bit-only runner leaves syswow64 empty, like a real prefix.
        self.wow64 = True

    def create_prefix(self, app_id: str, *, keep_z: bool = False) -> Path:
        self.calls.append(f"create_prefix {app_id} keep_z={keep_z}")
        prefix = self.paths.prefix_for(app_id)
        prefix.mkdir(parents=True, exist_ok=True)
        # Wine's wineboot creates the host user profile under drive_c/users;
        # exposure re-linking needs a realistic prefix in tests.
        (prefix / "drive_c" / "users" / "me").mkdir(parents=True, exist_ok=True)
        if self.wow64:
            syswow64 = prefix / "drive_c" / "windows" / "syswow64"
            syswow64.mkdir(parents=True, exist_ok=True)
            (syswow64 / "ntdll.dll").write_bytes(b"MZ")
        # Mirror Wine's default host-root link so the setup z-drive test starts
        # from the same state a real prefix would have.
        dosdevices = prefix / "dosdevices"
        dosdevices.mkdir(exist_ok=True)
        link = dosdevices / "z:"
        if not link.is_symlink():
            link.symlink_to("/")
        return prefix

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        record = " ".join(args)
        self.wine_calls.append(record)
        self.wine_timeouts.append(timeout_s)
        self.calls.append(record)
        if args and args[0] == "reg":
            return self._reg(prefix, args)
        if args and args[0] == "taskkill":
            image = args[args.index("/IM") + 1] if "/IM" in args else record
            self.killed.append(image)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def _reg_export(
        self, prefix: Path, args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        hive = args[2] if len(args) > 2 else ""
        guest = args[3] if len(args) > 3 else ""
        content = self.reg_exports.get(hive, self.reg_export_text)
        if content is None:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="not found\n")
        try:
            target = _guest_file(prefix, guest)
        except ValueError as exc:
            return subprocess.CompletedProcess([], 2, stdout="", stderr=f"{exc}\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def _reg(
        self, prefix: Path, args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        action = args[1] if len(args) > 1 else ""
        key = args[2] if len(args) > 2 else ""
        name = args[args.index("/v") + 1] if "/v" in args else ""

        if action == "export":
            return self._reg_export(prefix, args)
        if action == "query":
            value = self.reg_values.get(key, {}).get(name)
            if value is None:
                return subprocess.CompletedProcess([], 1, stdout="", stderr="not found\n")
            return subprocess.CompletedProcess(
                [], 0, stdout=f"    {name}    REG_SZ    {value}\n", stderr=""
            )
        if action == "add":
            value = args[args.index("/d") + 1] if "/d" in args else ""
            self.reg_values.setdefault(key, {})[name] = value
            self.reg_writes.append((name, key, value))
        elif action == "delete":
            self.reg_values.get(key, {}).pop(name, None)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def install(
        self,
        prefix: Path,
        installer: Path,
        args: list[str],
        *,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> int:
        self.calls.append(f"install {installer} {' '.join(args)}")
        self.installed.append((prefix, installer, list(args)))
        try:
            staged_payload = _guest_file(prefix, str(installer)).read_bytes()
        except (OSError, ValueError):
            staged_payload = b""
        self.install_payloads.append(staged_payload)
        self.install_logs.append(log_path)
        self.install_timeouts.append(timeout_s)
        self.install_saw_log.append(log_path is not None and log_path.exists())
        if self.install_timeout_error is not None:
            raise self.install_timeout_error
        code = self.install_exit_codes.pop(0) if self.install_exit_codes else self.install_exit_code
        if log_path is not None:
            log_path.write_text(
                f"fake installer output (exit {code})\n",
                encoding="utf-8",
            )
        if code == 0 and self.on_install is not None:
            self.on_install(prefix, installer, list(args))
        return code

    def run(self, prefix: Path, exe: str, argv: list[str]) -> FakePopen:
        self.calls.append(f"run {exe} {' '.join(argv)}")
        self.runs.append((prefix, exe, list(argv)))
        return self.popen

    def expose(self, prefix: Path, host: Path, guest: str) -> None:
        self.calls.append(f"expose {host} {guest}")
        self.exposed.append((prefix, host, guest))

    def remove(self, prefix: Path) -> None:
        self.calls.append(f"remove {prefix}")
        self.removed.append(prefix)
        if prefix.is_dir() and not prefix.is_symlink():
            shutil.rmtree(prefix)


@pytest.fixture
def fake_backend(paths: Paths) -> FakeBackend:
    return FakeBackend(paths)


def _fake_install_registry(
    *,
    display_name: str,
    version: str,
    scheme: str,
    exe_guest: str,
    install_root: str,
) -> str:
    """Build the registry diff a fake install produces."""
    exe_reg = exe_guest.replace("\\", "\\\\")
    root_reg = install_root.replace("\\", "\\\\")
    return (
        "Windows Registry Editor Version 5.00\n\n"
        f"[HKEY_CLASSES_ROOT\\{scheme}]\n"
        '"URL Protocol"=""\n\n'
        f"[HKEY_CLASSES_ROOT\\{scheme}\\shell\\open\\command]\n"
        f'@="\\"{exe_reg}\\""\n\n'
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"
        "\\Uninstall\\Example]\n"
        f'"DisplayName"="{display_name}"\n'
        f'"DisplayVersion"="{version}"\n'
        f'"InstallLocation"="{root_reg}"\n'
    )


@pytest.fixture
def make_fake_installer(
    tmp_path: Path, fake_backend: FakeBackend
) -> Callable[..., Path]:
    """Create a fake installer that writes an app tree and registry on success."""

    def _make(
        *,
        filename: str = "Example App.exe",
        display_name: str = "Example",
        version: str = "1.2.3",
        scheme: str = "example-app",
        exe_name: str = "Example.exe",
        install_dir: str = "Example",
        install_root: str | None = None,
        kind: str = "nsis",
        effect: bool = True,
    ) -> Path:
        installer = tmp_path / filename
        marker = {
            "nsis": b"Nullsoft Install System v3.10",
            "inno": b"Inno Setup 6",
            "burn": b"Burn bootstrapper",
            "unknown": b"",
        }[kind]
        installer.write_bytes(b"MZ" + b"\x00" * 64 + marker)

        root_guest = install_root or rf"C:\Program Files\{install_dir}"
        exe_guest = f"{root_guest}\\{exe_name}"
        registry = _fake_install_registry(
            display_name=display_name,
            version=version,
            scheme=scheme,
            exe_guest=exe_guest,
            install_root=root_guest,
        )
        counter = {"installs": 0}

        def _install(prefix: Path, _installer: Path, _args: list[str]) -> None:
            counter["installs"] += 1
            program_dir = prefix / "drive_c" / Path(*root_guest.split("\\")[1:])
            program_dir.mkdir(parents=True, exist_ok=True)
            # Vary the size so a re-install is always a filesystem diff even on
            # a coarse-mtime filesystem.
            (program_dir / exe_name).write_bytes(
                b"MZ" + b"\x00" * (4096 + counter["installs"])
            )
            fake_backend.reg_export_text = registry

        fake_backend.on_install = _install if effect else None
        fake_backend.reg_export_text = None
        # Adopt an existing runtime: the setup pipeline should detect it and
        # only apply the per-app override, never download in a unit test.
        client_key = (
            "HKLM\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
            f"{WEBVIEW2_CLIENT_GUID}"
        )
        fake_backend.reg_values[client_key] = {"pv": "156.0.4314.42"}
        return installer

    return _make


def _write_background_png(path: Path) -> None:
    """Write a deterministic wallpaper-like background for verify diffs."""
    size = (1920, 1080)
    image = Image.new("RGB", size, (18, 24, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] - 1, 140), fill=(96, 42, 160))
    draw.rectangle(
        (size[0] // 4, size[1] // 3, size[0] * 3 // 4, size[1] * 2 // 3),
        fill=(28, 78, 132),
    )
    for x in range(-size[1], size[0], 96):
        draw.line((x, 0, x + size[1], size[1]), fill=(44, 132, 176), width=6)
    for x in range(0, size[0], 160):
        draw.ellipse((x, 760, x + 90, 850), outline=(220, 180, 60), width=5)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


def _write_textured_png(path: Path, width: int, height: int, *, blank: bool) -> None:
    """Write a frame that the verifier must judge visible unless ``blank`` is set."""
    size = (max(1, width), max(1, height))
    image = Image.new("RGB", size, (242, 242, 242))
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (0, 0, size[0] - 1, max(1, size[1] // 8)),
            fill=(32, 48, 64),
        )
        for x in range(-size[1], size[0], 24):
            draw.line((x, 0, x + size[1], size[1]), fill=(60, 120, 180), width=3)
        draw.rectangle(
            (size[0] // 8, size[1] // 3, size[0] // 2, size[1] // 2),
            outline=(200, 60, 40),
            width=4,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


class FakeDesktop:
    """Record every desktop call; pictures are real, visible PNG files.

    The default window matches :class:`FakeBackend`'s pid (4242), so ``setup``'s
    automatic verify passes without extra wiring. ``screenshot_screen`` writes a
    deterministic textured background, and ``screenshot`` writes content that
    differs from it. Tests that need a timeout set ``wait_result = None`` (or
    ``windows_result = []``); tests that need an invisible frame set
    ``blank_screenshots = True``.
    """

    name = "fake"

    def __init__(self) -> None:
        self.window = Window("0x1", 4242, "Example - Main", "Example", 10, 20, 800, 600)
        self.windows_result: list[Window] = [self.window]
        self.wait_result: Window | None = self.window
        self.windows_calls = 0
        self.waits: list[tuple[set[int], float]] = []
        self.screenshots: list[Path] = []
        self.screen_screenshots: list[Path] = []
        self.captures: list[tuple[Window, Path]] = []
        self.pins: list[tuple[str, str]] = []
        self.pinned: str | None = None
        self.unpinned: list[str] = []
        self.pin_result = True
        self.unpin_result = True
        self.blank_screenshots = False

    @property
    def capabilities(self) -> set[str]:
        return {"windows", "screenshot", "pin"}

    def windows(self) -> list[Window]:
        self.windows_calls += 1
        return list(self.windows_result)

    def wait_for_window(self, pid_tree: set[int], timeout: float) -> Window | None:
        self.waits.append((set(pid_tree), timeout))
        return self.wait_result

    def screenshot(self, window: Window, dest: Path) -> None:
        target = Path(dest)
        self.captures.append((window, target))
        _write_textured_png(
            target,
            window.width,
            window.height,
            blank=self.blank_screenshots,
        )
        self.screenshots.append(target)

    def screenshot_screen(self, dest: Path) -> None:
        target = Path(dest)
        _write_background_png(target)
        self.screen_screenshots.append(target)

    def pin(self, desktop_id: str, match: str) -> bool:
        self.pins.append((desktop_id, match))
        if self.pin_result:
            self.pinned = desktop_id
        return self.pin_result

    def unpin(self, desktop_id: str) -> bool:
        self.unpinned.append(desktop_id)
        if self.pinned == desktop_id:
            self.pinned = None
        return self.unpin_result


@pytest.fixture
def fake_desktop() -> FakeDesktop:
    return FakeDesktop()
