from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from winbridge.core.config import Config, ConfigError
from winbridge.core.state import Paths


def test_paths_from_root_are_fully_scoped(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)
    assert paths.profiles == tmp_path / "profiles"
    assert paths.applications == tmp_path / "applications"
    assert paths.config_home == tmp_path
    assert paths.data_home == tmp_path


def test_paths_from_root_puts_every_path_under_root(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path / "state")
    for name in (
        "config",
        "profiles",
        "prefixes",
        "runners",
        "cache",
        "evidence",
        "logs",
        "locks",
        "applications",
        "bundles",
    ):
        assert getattr(paths, name).is_relative_to(paths.root)


def test_prefix_for_is_scoped_to_prefixes(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)
    assert paths.prefix_for("example-app") == tmp_path / "prefixes" / "example-app"


def test_ensure_creates_private_directories(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path / "state")

    paths.ensure()
    paths.ensure()  # idempotent

    for name in (
        "profiles",
        "prefixes",
        "runners",
        "cache",
        "evidence",
        "logs",
        "locks",
        "applications",
        "bundles",
    ):
        directory = getattr(paths, name)
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    assert paths.config.parent.is_dir()


def test_default_uses_xdg_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    paths = Paths.default()

    assert paths.config == config_home / "winbridge" / "config.toml"
    assert paths.profiles == data_home / "winbridge" / "profiles"
    assert paths.prefixes == data_home / "winbridge" / "prefixes"
    assert paths.cache == data_home / "winbridge" / "cache" / "downloads"
    assert paths.applications == data_home / "applications"
    assert paths.root == data_home


def test_default_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    paths = Paths.default()

    assert paths.config == tmp_path / ".config" / "winbridge" / "config.toml"
    assert paths.profiles == tmp_path / ".local" / "share" / "winbridge" / "profiles"


def test_config_defaults() -> None:
    config = Config()
    assert config.backend == "wine"
    assert config.default_runner is None
    assert config.verify_timeout_s == 300


def test_config_roundtrip(tmp_path: Path) -> None:
    dest = tmp_path / "store" / "config.toml"
    config = Config(
        backend="wine",
        default_runner="soda@11.0-10",
        verify_timeout_s=120,
    )

    config.dump(dest)

    assert Config.load(dest) == config
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert sorted(path.name for path in dest.parent.iterdir()) == ["config.toml"]
    parsed = tomllib.loads(dest.read_text(encoding="utf-8"))
    assert parsed == {
        "backend": "wine",
        "default_runner": "soda@11.0-10",
        "verify_timeout_s": 120,
    }


def test_config_load_missing_path_returns_defaults(tmp_path: Path) -> None:
    assert Config.load(tmp_path / "missing.toml") == Config()


def test_config_load_rejects_invalid_timeout(tmp_path: Path) -> None:
    dest = tmp_path / "config.toml"
    dest.write_text("verify_timeout_s = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="verify_timeout_s"):
        Config.load(dest)


def test_config_load_rejects_unknown_backend(tmp_path: Path) -> None:
    dest = tmp_path / "config.toml"
    dest.write_text('backend = "proton"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="backend"):
        Config.load(dest)


def test_config_load_rejects_corrupt_toml(tmp_path: Path) -> None:
    dest = tmp_path / "config.toml"
    dest.write_text("backend = [\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TOML"):
        Config.load(dest)
