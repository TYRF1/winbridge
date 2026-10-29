from __future__ import annotations

import stat
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import tomli_w

from winbridge.core.profile import (
    ExposedPath,
    InstallerSpec,
    LastVerified,
    PinSpec,
    Profile,
    ProfileError,
    WebView2Runtime,
)


def test_profile_roundtrip_preserves_unknown_keys(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile()
    profile.extra["future"] = {"x": 1}
    dest = tmp_path / "profile.toml"

    profile.dump(dest)
    loaded = Profile.load(dest)

    assert loaded == profile
    assert loaded.extra["future"] == {"x": 1}


def test_profile_roundtrip_preserves_webview2_and_keep_z_drive(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile(
        keep_z_drive=True,
        webview2=WebView2Runtime(
            version="156.0.4314.42",
            source="pinned",
            installed_at="2026-10-28T00:00:00Z",
            extra={"channel": "stable"},
        ),
    )
    dest = tmp_path / "profile.toml"

    profile.dump(dest)
    loaded = Profile.load(dest)

    assert loaded.keep_z_drive is True
    assert loaded.webview2 is not None
    assert loaded.webview2.version == "156.0.4314.42"
    assert loaded.webview2.extra == {"channel": "stable"}
    parsed = tomllib.loads(dest.read_text(encoding="utf-8"))
    assert parsed["webview2"]["source"] == "pinned"
    assert parsed["webview2"]["channel"] == "stable"


def test_profile_roundtrip_preserves_nested_fields(
    tmp_path: Path, profile: Profile
) -> None:
    dest = tmp_path / "profile.toml"

    profile.dump(dest)
    loaded = Profile.load(dest)

    assert loaded == profile
    assert loaded.installer == InstallerSpec(
        path="/home/me/downloads/example-setup.exe",
        dir="/home/me/downloads",
        glob="*setup*.exe",
        type="nsis",
        silent_args=["/S"],
    )
    assert loaded.exposed == [
        ExposedPath(host="/home/me/repos", guest="C:\\users\\me\\repos")
    ]
    assert loaded.pin == PinSpec(match="^Example$")
    assert loaded.last_verified == LastVerified(
        app_version="1.2.3",
        result="pass",
        first_paint_s=95.0,
        at="2026-10-27T10:04:00Z",
    )


def test_profile_roundtrip_preserves_nested_unknown_keys(
    tmp_path: Path, profile: Profile
) -> None:
    original = tmp_path / "profile.toml"
    profile.dump(original)
    data = profile.to_dict()
    data["installer"]["arch"] = "x86_64"
    data["exposed"][0]["label"] = "repos"
    data["pin"]["priority"] = 3
    data["last_verified"]["window_class"] = "Example"
    original.write_text(tomli_w.dumps(data), encoding="utf-8")

    loaded = Profile.load(original)

    assert loaded.installer is not None
    assert loaded.installer.extra == {"arch": "x86_64"}
    assert loaded.exposed[0].extra == {"label": "repos"}
    assert loaded.pin is not None
    assert loaded.pin.extra == {"priority": 3}
    assert loaded.last_verified is not None
    assert loaded.last_verified.extra == {"window_class": "Example"}

    rewritten = tmp_path / "rewritten.toml"
    loaded.dump(rewritten)
    parsed = tomllib.loads(rewritten.read_text(encoding="utf-8"))

    assert parsed["installer"]["arch"] == "x86_64"
    assert parsed["exposed"][0]["label"] == "repos"
    assert parsed["pin"]["priority"] == 3
    assert parsed["last_verified"]["window_class"] == "Example"
    assert Profile.load(rewritten) == loaded


def test_profile_dump_refuses_nested_extra_that_shadows_known_fields(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile()
    assert profile.installer is not None
    profile.installer.extra["path"] = "C:\\elsewhere\\setup.exe"
    dest = tmp_path / "profile.toml"

    with pytest.raises(ProfileError, match="installer.extra"):
        profile.dump(dest)

    assert not dest.exists()


def test_profile_dump_writes_private_file_without_temp_files(
    tmp_path: Path, profile: Profile
) -> None:
    dest = tmp_path / "store" / "example-app.toml"

    profile.dump(dest)
    profile.dump(dest)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert [path.name for path in dest.parent.iterdir()] == ["example-app.toml"]
    parsed = tomllib.loads(dest.read_text(encoding="utf-8"))
    assert parsed["schema"] == 1
    assert parsed["id"] == "example-app"
    assert parsed["installer"]["silent_args"] == ["/S"]
    assert parsed["last_verified"]["result"] == "pass"


def test_profile_dump_omits_unset_optional_tables(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile(
        installer=None,
        pin=None,
        last_verified=None,
        schemes=[],
        exposed=[],
    )
    dest = tmp_path / "profile.toml"

    profile.dump(dest)
    loaded = Profile.load(dest)

    assert loaded == profile
    parsed = tomllib.loads(dest.read_text(encoding="utf-8"))
    assert "installer" not in parsed
    assert "pin" not in parsed
    assert "last_verified" not in parsed
    assert parsed["schemes"] == []
    assert parsed["exposed"] == []


def test_profile_load_reads_unknown_top_level_keys(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    dest = tmp_path / "profile.toml"
    make_profile().dump(dest)
    body = dest.read_text(encoding="utf-8")
    dest.write_text(
        f'future = "value"\n{body}\n[future_table]\nanswer = 42\n',
        encoding="utf-8",
    )

    loaded = Profile.load(dest)

    assert loaded.extra == {
        "future": "value",
        "future_table": {"answer": 42},
    }


def test_profile_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Profile.load(tmp_path / "missing.toml")


def test_profile_load_wraps_corrupt_toml(tmp_path: Path) -> None:
    dest = tmp_path / "profile.toml"
    dest.write_text("schema = [\n", encoding="utf-8")

    with pytest.raises(ProfileError, match="invalid TOML"):
        Profile.load(dest)


def test_profile_load_validates_schema(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    dest = tmp_path / "profile.toml"
    make_profile().dump(dest)
    dest.write_text(
        dest.read_text(encoding="utf-8").replace("schema = 1", "schema = 2"),
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="schema"):
        Profile.load(dest)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 2},
        {"id": ""},
        {"id": "   "},
        {"name": ""},
        {"backend": "proton"},
        {"runner": ""},
        {"runner": "soda"},
        {"runner": "path:relative/runner"},
    ],
)
def test_profile_validate_rejects_invalid_values(
    make_profile: Callable[..., Profile], overrides: dict[str, object]
) -> None:
    profile = make_profile(**overrides)

    with pytest.raises(ProfileError):
        profile.validate()


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend": "bottles"},
        {"runner": "path:/opt/runners/soda"},
        {"runner": "soda@11.0-10"},
    ],
)
def test_profile_validate_accepts_supported_values(
    make_profile: Callable[..., Profile], overrides: dict[str, object]
) -> None:
    assert make_profile(**overrides).validate() is None


def test_profile_dump_refuses_invalid_profile(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile(backend="proton")
    dest = tmp_path / "profile.toml"

    with pytest.raises(ProfileError, match="backend"):
        profile.dump(dest)

    assert not dest.exists()


def test_profile_dump_refuses_extra_keys_that_shadow_known_fields(
    tmp_path: Path, make_profile: Callable[..., Profile]
) -> None:
    profile = make_profile()
    profile.extra["id"] = "shadow-app"
    dest = tmp_path / "profile.toml"

    with pytest.raises(ProfileError, match="extra"):
        profile.dump(dest)

    assert not dest.exists()


def test_profile_to_dict_includes_unknown_keys_and_nested_tables(
    make_profile: Callable[..., Profile],
) -> None:
    profile = make_profile()
    profile.extra["future"] = {"x": 1}

    data = profile.to_dict()

    assert data["id"] == "example-app"
    assert data["installer"] == {
        "path": "/home/me/downloads/example-setup.exe",
        "dir": "/home/me/downloads",
        "glob": "*setup*.exe",
        "type": "nsis",
        "silent_args": ["/S"],
    }
    assert data["last_verified"] == {
        "app_version": "1.2.3",
        "result": "pass",
        "first_paint_s": 95.0,
        "at": "2026-10-27T10:04:00Z",
    }
    assert data["future"] == {"x": 1}
