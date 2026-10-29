from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge.cli import app, main
from winbridge.core.errors import WinBridgeError
from winbridge.core.profile import LastVerified, Profile, WebView2Runtime
from winbridge.core.state import Paths


@pytest.fixture(autouse=True)
def _cli_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: Paths.from_root(tmp_path))


def invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_list_and_inspect_json(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="example-app"))

    result = invoke("list", "--json")
    assert result.exit_code == 0
    assert json.loads(result.output)["apps"][0]["id"] == "example-app"

    result = invoke("inspect", "example-app", "--json")
    assert result.exit_code == 0
    assert json.loads(result.output)["id"] == "example-app"


def test_list_json_shape(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="example-app"))

    result = invoke("list", "--json")

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "apps": [
            {
                "id": "example-app",
                "name": "Example App",
                "runner": "soda@11.0-10",
                "verified": True,
            }
        ]
    }


@pytest.mark.parametrize(
    ("last_verified", "expected"),
    [
        (None, False),
        (LastVerified("1.0.0", "pass", 10.0, "2026-10-27T10:00:00Z"), True),
        (LastVerified("1.0.0", "fail", 10.0, "2026-10-27T10:00:00Z"), False),
        (LastVerified("1.0.0", "skipped", None, "2026-10-27T10:00:00Z"), False),
    ],
)
def test_list_json_verified_flag(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
    last_verified: LastVerified | None,
    expected: bool,
) -> None:
    write_profile(tmp_path, make_profile(last_verified=last_verified))

    result = invoke("list", "--json")

    assert json.loads(result.output)["apps"][0]["verified"] is expected


def test_list_json_is_sorted_and_empty_by_default() -> None:
    result = invoke("list", "--json")
    assert result.exit_code == 0
    assert json.loads(result.output) == {"apps": []}


def test_list_human_output(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="example-app"))

    result = invoke("list")

    assert result.exit_code == 0
    assert "example-app" in result.output
    assert "Example App" in result.output
    assert "soda@11.0-10" in result.output
    assert "verified" in result.output


def test_list_human_output_when_empty() -> None:
    result = invoke("list")

    assert result.exit_code == 0
    assert "No apps installed" in result.output


def test_list_sorts_by_id(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="second-app", name="Second App"))
    write_profile(tmp_path, make_profile(id="first-app", name="First App"))

    result = invoke("list", "--json")

    assert [row["id"] for row in json.loads(result.output)["apps"]] == [
        "first-app",
        "second-app",
    ]


def test_list_skips_and_reports_a_corrupt_profile(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="good-app"))
    corrupt = Paths.from_root(tmp_path).profiles / "broken-app.toml"
    corrupt.write_text("schema = [", encoding="utf-8")

    result = invoke("list")

    assert result.exit_code == 0
    combined = result.output + result.stderr
    assert "good-app" in combined
    assert str(corrupt) in combined
    assert "warning" in combined


def test_list_skips_an_invalid_utf8_profile(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="good-app"))
    broken = Paths.from_root(tmp_path).profiles / "broken-app.toml"
    broken.write_bytes(b"\xff\xfe\x00not utf-8")

    result = invoke("list")

    assert result.exit_code == 0
    combined = result.output + result.stderr
    assert "good-app" in combined
    assert str(broken) in combined
    assert "warning" in combined


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_list_skips_an_unreadable_profile(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="good-app"))
    locked = Paths.from_root(tmp_path).profiles / "locked-app.toml"
    locked.write_text("schema = 1\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        result = invoke("list")
    finally:
        locked.chmod(0o600)

    assert result.exit_code == 0
    combined = result.output + result.stderr
    assert "good-app" in combined
    assert str(locked) in combined
    assert "warning" in combined


def test_list_json_reports_corrupt_profile_paths(
    tmp_path: Path,
) -> None:
    corrupt = Paths.from_root(tmp_path).profiles / "broken-app.toml"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("not toml", encoding="utf-8")

    result = invoke("list", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["apps"] == []
    assert payload["corrupt"][0]["path"] == str(corrupt)


def test_inspect_json_returns_the_webview2_metadata(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(
        tmp_path,
        make_profile(
            id="example-app",
            webview2=WebView2Runtime(version="156.0.4314.42", source="pinned"),
        ),
    )

    result = invoke("inspect", "example-app", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["webview2"]["version"] == "156.0.4314.42"
    assert payload["webview2"]["source"] == "pinned"


def test_inspect_json_returns_profile_with_extra_keys(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    profile = make_profile(id="example-app")
    profile.extra["future"] = {"x": 1}
    assert profile.installer is not None
    profile.installer.extra["arch"] = "x86_64"
    assert profile.pin is not None
    profile.pin.extra["priority"] = 3
    assert profile.last_verified is not None
    profile.last_verified.extra["window_class"] = "Example"
    profile.exposed[0].extra["label"] = "repos"
    write_profile(tmp_path, profile)

    result = invoke("inspect", "example-app", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == profile.to_dict()
    assert payload["future"] == {"x": 1}
    assert payload["installer"]["type"] == "nsis"
    assert payload["installer"]["arch"] == "x86_64"
    assert payload["pin"]["priority"] == 3
    assert payload["last_verified"]["window_class"] == "Example"
    assert payload["exposed"][0]["label"] == "repos"


def test_inspect_human_output(
    tmp_path: Path,
    write_profile: Callable[[Path, Profile], Path],
    make_profile: Callable[..., Profile],
) -> None:
    write_profile(tmp_path, make_profile(id="example-app"))

    result = invoke("inspect", "example-app")

    assert result.exit_code == 0
    assert 'id = "example-app"' in result.output
    assert 'name = "Example App"' in result.output


def test_inspect_missing_app_fails() -> None:
    result = invoke("inspect", "missing-app", "--json")

    assert result.exit_code != 0
    assert isinstance(result.exception, WinBridgeError)
    assert "missing-app" in str(result.exception)


@pytest.mark.parametrize("app_id", ["../escape", "a/b", "a\\b", ".", ".."])
def test_inspect_refuses_unsafe_app_ids(
    app_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["winbridge", "inspect", app_id, "--json"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "invalid app id" in capsys.readouterr().err
