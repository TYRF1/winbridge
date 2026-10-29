"""The composition root wires a runner-bound backend and detects the desktop."""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from winbridge.backends.wine import WineBackend
from winbridge.core.config import Config
from winbridge.core.context import Context, build_context
from winbridge.core.errors import UnsupportedEnvironment, UsageError
from winbridge.core.runner import Catalog, Runner, RunnerNotInstalled
from winbridge.core.state import Paths
from winbridge.desktops import HyprlandDesktop, UnsupportedDesktop, X11Desktop


def _without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)


def install_default_runner(paths: Paths, catalog: Catalog, fake_runner: Path) -> Path:
    runner = catalog.default_runner()
    root = paths.runners / runner.id / runner.version
    (root / "bin").mkdir(parents=True)
    # Copy rather than link outside the runner: runner_wine_path refuses a
    # wine binary whose resolved target escapes the runner root.
    shutil.copy2(fake_runner / "bin" / "wine", root / "bin" / "wine")
    for name in ("wineboot", "wineserver"):
        (root / "bin" / name).symlink_to("wine")
    return root


def _runner_archive(tmp_path: Path, catalog: Catalog) -> Catalog:
    """Return a catalog whose default runner is an installable local archive."""
    archive = tmp_path / "runner.tar.gz"
    wine = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("bin/wine")
        info.size = len(wine)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(wine))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    default = catalog.default_runner()
    runner = Runner(
        default.id,
        default.version,
        default.kind,
        archive.as_uri(),
        digest,
        "bin/wine",
        default=True,
    )
    return Catalog((runner,), catalog.webview2, catalog.latest_webview2_url)


def test_build_context_wires_runner_bound_backend(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    fake_runner: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_session(monkeypatch)
    root = install_default_runner(paths, catalog, fake_runner)

    ctx = build_context(paths, config, catalog)

    assert isinstance(ctx, Context)
    assert ctx.backend.name == "wine"
    assert isinstance(ctx.backend, WineBackend)
    assert ctx.backend.runner_root == root
    assert isinstance(ctx.desktop, UnsupportedDesktop)
    assert ctx.desktop.capabilities == set()
    assert ctx.paths is paths
    assert ctx.catalog is catalog
    assert ctx.config is config


def test_build_context_detects_hyprland(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    fake_runner: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_default_runner(paths, catalog, fake_runner)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "signature")

    ctx = build_context(paths, config, catalog)

    assert isinstance(ctx.desktop, HyprlandDesktop)


def test_build_context_detects_x11(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    fake_runner: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_default_runner(paths, catalog, fake_runner)
    _without_session(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")

    ctx = build_context(paths, config, catalog)

    assert isinstance(ctx.desktop, X11Desktop)


def test_build_context_uses_the_config_runner_override(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    fake_runner: Path,
) -> None:
    config.default_runner = f"path:{fake_runner}"

    ctx = build_context(paths, config, catalog)

    assert isinstance(ctx.backend, WineBackend)
    assert ctx.backend.runner_root == fake_runner


def test_build_context_requires_an_installed_runner(
    paths: Paths, config: Config, catalog: Catalog
) -> None:
    """Commands other than setup must not download a runner silently."""
    with pytest.raises(RunnerNotInstalled, match="runners install"):
        build_context(paths, config, catalog)


def test_build_context_installs_the_catalog_default_when_setup_asks(
    tmp_path: Path, paths: Paths, config: Config, catalog: Catalog
) -> None:
    installable = _runner_archive(tmp_path, catalog)
    reports: list[str] = []

    ctx = build_context(
        paths,
        config,
        installable,
        install_missing_runner=True,
        report=reports.append,
    )

    runner = installable.default_runner()
    root = paths.runners / runner.id / runner.version
    assert isinstance(ctx.backend, WineBackend)
    assert ctx.backend.runner_root == root
    assert (root / "bin" / "wine").is_file()
    assert any("downloading" in line for line in reports)
    assert any("installed at" in line for line in reports)


def test_build_context_never_installs_an_imported_missing_path_runner(
    tmp_path: Path, paths: Paths, catalog: Catalog
) -> None:
    config = Config(default_runner=f"path:{tmp_path / 'missing-runner'}")

    with pytest.raises(RunnerNotInstalled):
        build_context(
            paths,
            config,
            catalog,
            install_missing_runner=True,
        )


def test_build_context_rejects_bottles_until_v02(
    paths: Paths, config: Config, catalog: Catalog
) -> None:
    with pytest.raises(UnsupportedEnvironment) as exc_info:
        build_context(paths, config, catalog, backend_name="bottles")

    assert exc_info.value.exit_code == 3


def test_build_context_rejects_an_unknown_backend(
    paths: Paths, config: Config, catalog: Catalog
) -> None:
    with pytest.raises(UsageError, match="unknown backend"):
        build_context(paths, config, catalog, backend_name="nope")


def test_build_context_passes_the_desktop_through(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    fake_runner: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_default_runner(paths, catalog, fake_runner)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "signature")
    desktop = object()

    ctx = build_context(paths, config, catalog, desktop=desktop)

    assert ctx.desktop is desktop
