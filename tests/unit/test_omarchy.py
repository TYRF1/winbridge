"""omarchy-shell taskbar pins: preservation, dedupe, removal, and restart."""

from __future__ import annotations

import json
import logging
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from winbridge.core import omarchy
from winbridge.core.errors import WinBridgeError
from winbridge.core.omarchy import (
    DEFAULT_SHELL_CONFIG,
    TASKBAR_ID,
    pin_app,
    restart_shell,
    unpin_app,
)


def _write(path: Path, data: dict[str, Any]) -> str:
    text = json.dumps(data, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _taskbar(apps: list[Any] | None = None) -> dict[str, Any]:
    widget: dict[str, Any] = {"id": TASKBAR_ID}
    if apps is not None:
        widget["apps"] = list(apps)
    return widget


def _shell(*, left: list[Any], right: list[Any] | None = None) -> dict[str, Any]:
    bar: dict[str, Any] = {"left": left}
    if right is not None:
        bar["right"] = right
    return {"theme": {"name": "omarchy"}, "bar": bar}


def _layout_shell(
    *, left: list[Any], right: list[Any] | None = None
) -> dict[str, Any]:
    """The current Omarchy 4 shape: sections live under ``bar.layout``."""
    layout: dict[str, Any] = {"left": left}
    if right is not None:
        layout["right"] = right
    return {"theme": {"name": "omarchy"}, "bar": {"layout": layout}}


# --- constants --------------------------------------------------------------


def test_taskbar_widget_id_is_the_live_plugin_id() -> None:
    assert TASKBAR_ID == "io.github.joeyvigil.taskbar"


def test_default_shell_config_is_omarchy_shell_json() -> None:
    assert DEFAULT_SHELL_CONFIG.name == "shell.json"
    assert DEFAULT_SHELL_CONFIG.parent.name == "omarchy"


# --- pinning ----------------------------------------------------------------


def test_pin_preserves_unrelated_keys_and_dedupes(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        {
            "theme": {"name": "omarchy"},
            "bar": {"left": [{"id": TASKBAR_ID, "apps": ["steam"]}], "right": [{"id": "clock"}]},
        },
    )
    assert pin_app(cfg, "winbridge-app", "^App$") is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == {"name": "omarchy"}
    assert data["bar"]["right"] == [{"id": "clock"}]
    assert data["bar"]["left"][0]["apps"] == [
        "steam",
        {"desktopId": "winbridge-app", "match": "^App$"},
    ]
    assert pin_app(cfg, "winbridge-app", "^App$") is False  # already pinned


def test_pin_keeps_unknown_bar_widgets_and_top_level_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        {
            "theme": {"name": "omarchy", "accent": "#ff0000"},
            "bar": {
                "left": [
                    _taskbar(),
                    {"id": "workspaces", "options": {"count": 5}},
                ],
                "center": [{"id": "media"}],
            },
            "plugins": {"custom": [1, 2, 3]},
        },
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == {"name": "omarchy", "accent": "#ff0000"}
    assert data["bar"]["left"][1] == {"id": "workspaces", "options": {"count": 5}}
    assert data["bar"]["center"] == [{"id": "media"}]
    assert data["plugins"] == {"custom": [1, 2, 3]}
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]


def test_pin_finds_the_widget_outside_the_left_section(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(
            left=[{"id": "clock"}],
            right=[{"id": "workspaces"}, _taskbar()],
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"] == [{"id": "clock"}]
    assert data["bar"]["right"][1]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]


def test_pin_updates_the_layout_left_section_and_preserves_neighbours(
    tmp_path: Path,
) -> None:
    """Omarchy 4 nests widgets under ``bar.layout``; that is the live schema."""
    cfg = tmp_path / "shell.json"
    original = _write(
        cfg,
        _layout_shell(
            left=[
                {"id": "workspaces", "options": {"count": 5}},
                _taskbar(["steam"]),
                {"id": "clock"},
            ],
            right=[{"id": "tray"}],
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == {"name": "omarchy"}
    assert data["bar"]["layout"]["left"] == [
        {"id": "workspaces", "options": {"count": 5}},
        {
            "id": TASKBAR_ID,
            "apps": ["steam", {"desktopId": "winbridge-app", "match": "^App$"}],
        },
        {"id": "clock"},
    ]
    assert data["bar"]["layout"]["right"] == [{"id": "tray"}]
    assert (tmp_path / "shell.json.bak").read_text(encoding="utf-8") == original
    assert pin_app(cfg, "winbridge-app", "^App$") is False


def test_pin_finds_the_layout_widget_outside_the_left_section(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _layout_shell(
            left=[{"id": "clock"}],
            right=[{"id": "workspaces"}, _taskbar()],
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["layout"]["left"] == [{"id": "clock"}]
    assert data["bar"]["layout"]["right"][1]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]


def test_pin_prefers_layout_when_both_shapes_exist(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        {
            "bar": {
                "layout": {"left": [_taskbar()]},
                "left": [_taskbar([{"desktopId": "legacy", "match": "^Legacy$"}])],
            }
        },
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["layout"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "legacy", "match": "^Legacy$"}
    ]


def test_pin_prefers_the_left_widget_when_the_id_appears_twice(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(
            left=[_taskbar()],
            right=[_taskbar([{"desktopId": "other", "match": "^Other$"}])],
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]
    assert data["bar"]["right"][0]["apps"] == [
        {"desktopId": "other", "match": "^Other$"}
    ]


def test_pin_upgrades_a_bare_string_pin_in_place(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(
        cfg,
        _shell(
            left=[
                _taskbar(
                    [
                        "steam",
                        "winbridge-app",
                        {"desktopId": "winbridge-other", "match": "^Other$"},
                    ]
                )
            ]
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        "steam",
        {"desktopId": "winbridge-app", "match": "^App$"},
        {"desktopId": "winbridge-other", "match": "^Other$"},
    ]
    assert (tmp_path / "shell.json.bak").read_text(encoding="utf-8") == original


def test_pin_updates_a_stale_match_in_place(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(
        cfg,
        {
            "theme": {"name": "omarchy"},
            "bar": {
                "left": [
                    {"id": "workspaces"},
                    _taskbar(
                        [
                            "steam",
                            {
                                "desktopId": "winbridge-app",
                                "match": "^Old$",
                                "label": "keep",
                            },
                        ]
                    ),
                ],
                "right": [{"id": "clock"}],
            },
        },
    )

    assert pin_app(cfg, "winbridge-app", "^New$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == {"name": "omarchy"}
    assert data["bar"]["right"] == [{"id": "clock"}]
    assert data["bar"]["left"][0] == {"id": "workspaces"}
    assert data["bar"]["left"][1]["apps"] == [
        "steam",
        {"desktopId": "winbridge-app", "match": "^New$", "label": "keep"},
    ]
    assert (tmp_path / "shell.json.bak").read_text(encoding="utf-8") == original


def test_pin_with_the_same_match_is_a_no_op(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(
        cfg,
        _shell(
            left=[
                _taskbar(
                    [{"desktopId": "winbridge-app", "match": "^App$", "label": "keep"}]
                )
            ]
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is False

    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "shell.json.bak").exists()


def test_pin_adds_a_missing_match_to_an_existing_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(left=[_taskbar([{"desktopId": "winbridge-app", "label": "keep"}])]),
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "label": "keep", "match": "^App$"}
    ]


def test_pin_updates_every_entry_for_the_same_desktop_id(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(
            left=[
                _taskbar(
                    [
                        {"desktopId": "winbridge-app", "match": "^Old$"},
                        "winbridge-app",
                        {"desktopId": "winbridge-other", "match": "^Other$"},
                    ]
                )
            ]
        ),
    )

    assert pin_app(cfg, "winbridge-app", "^New$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^New$"},
        {"desktopId": "winbridge-app", "match": "^New$"},
        {"desktopId": "winbridge-other", "match": "^Other$"},
    ]


def test_pin_creates_the_apps_array_when_the_widget_has_none(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(cfg, _shell(left=[_taskbar()]))

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        {"desktopId": "winbridge-app", "match": "^App$"}
    ]


def test_pin_keeps_entries_without_a_desktop_id(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(cfg, _shell(left=[_taskbar([{"label": "legacy"}, 7])]))

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        {"label": "legacy"},
        7,
        {"desktopId": "winbridge-app", "match": "^App$"},
    ]


def test_pin_writes_a_backup_beside_the_config(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[_taskbar()]))

    pin_app(cfg, "winbridge-app", "^App$")

    backup = tmp_path / "shell.json.bak"
    assert backup.read_text(encoding="utf-8") == original


def test_pin_backup_tracks_the_previous_generation(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(cfg, _shell(left=[_taskbar()]))

    assert pin_app(cfg, "winbridge-one", "^One$") is True
    after_first = cfg.read_text(encoding="utf-8")
    assert pin_app(cfg, "winbridge-two", "^Two$") is True

    assert (tmp_path / "shell.json.bak").read_text(encoding="utf-8") == after_first


def test_pin_writes_formatted_json_and_preserves_the_file_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(cfg, _shell(left=[_taskbar()]))
    cfg.chmod(0o640)

    assert pin_app(cfg, "winbridge-app", "^App$") is True

    text = cfg.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "shell.json.bak").stat().st_mode) == 0o640


def test_pin_leaves_only_the_config_and_one_backup(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(cfg, _shell(left=[_taskbar()]))

    pin_app(cfg, "winbridge-app", "^App$")

    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "shell.json",
        "shell.json.bak",
    ]


def test_pin_returns_false_without_creating_a_missing_config(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"

    assert pin_app(cfg, "winbridge-app", "^App$") is False

    assert not cfg.exists()
    assert not (tmp_path / "shell.json.bak").exists()


def test_pin_returns_false_when_the_bar_or_widget_is_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(
        cfg,
        {"theme": {"name": "omarchy"}, "bar": {"left": [{"id": "clock"}]}},
    )

    assert pin_app(cfg, "winbridge-app", "^App$") is False
    assert cfg.read_text(encoding="utf-8") == original

    no_bar = tmp_path / "no-bar.json"
    no_bar_original = _write(no_bar, {"theme": {"name": "omarchy"}})
    assert pin_app(no_bar, "winbridge-app", "^App$") is False
    assert no_bar.read_text(encoding="utf-8") == no_bar_original


def test_pin_refuses_invalid_json_without_writing(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = "{not json"
    cfg.write_text(original, encoding="utf-8")

    with pytest.raises(WinBridgeError):
        pin_app(cfg, "winbridge-app", "^App$")

    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "shell.json.bak").exists()


def test_pin_refuses_a_non_object_document_without_writing(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = "[]\n"
    cfg.write_text(original, encoding="utf-8")

    with pytest.raises(WinBridgeError):
        pin_app(cfg, "winbridge-app", "^App$")

    assert cfg.read_text(encoding="utf-8") == original


def test_pin_refuses_a_non_list_apps_value_without_writing(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[{"id": TASKBAR_ID, "apps": "steam"}]))

    with pytest.raises(WinBridgeError):
        pin_app(cfg, "winbridge-app", "^App$")

    assert cfg.read_text(encoding="utf-8") == original


def test_failed_replace_leaves_the_original_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[_taskbar()]))

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(omarchy.os, "replace", fail_replace)

    with pytest.raises(OSError):
        pin_app(cfg, "winbridge-app", "^App$")

    assert cfg.read_text(encoding="utf-8") == original
    assert not [entry for entry in tmp_path.iterdir() if entry.suffix == ".tmp"]


# --- unpinning --------------------------------------------------------------


def test_unpin_removes_only_the_matching_entries(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(
            left=[
                {
                    "id": TASKBAR_ID,
                    "apps": [
                        "steam",
                        "winbridge-app",
                        {"desktopId": "winbridge-other", "match": "^Other$"},
                        {"desktopId": "winbridge-app", "match": "^App$"},
                        {"desktopId": "winbridge-app-2", "match": "^App2$"},
                    ],
                }
            ]
        ),
    )

    assert unpin_app(cfg, "winbridge-app") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["left"][0]["apps"] == [
        "steam",
        {"desktopId": "winbridge-other", "match": "^Other$"},
        {"desktopId": "winbridge-app-2", "match": "^App2$"},
    ]
    assert (tmp_path / "shell.json.bak").exists()


def test_unpin_finds_the_widget_outside_the_left_section(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _shell(
            left=[{"id": "clock"}],
            right=[_taskbar(["winbridge-app", "steam"])],
        ),
    )

    assert unpin_app(cfg, "winbridge-app") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["right"][0]["apps"] == ["steam"]


def test_unpin_updates_the_layout_left_section_and_preserves_neighbours(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "shell.json"
    _write(
        cfg,
        _layout_shell(
            left=[
                {"id": "workspaces"},
                _taskbar(
                    [
                        "steam",
                        "winbridge-app",
                        {"desktopId": "winbridge-other", "match": "^Other$"},
                    ]
                ),
                {"id": "clock"},
            ],
            right=[{"id": "tray"}],
        ),
    )

    assert unpin_app(cfg, "winbridge-app") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["bar"]["layout"]["left"] == [
        {"id": "workspaces"},
        {
            "id": TASKBAR_ID,
            "apps": [
                "steam",
                {"desktopId": "winbridge-other", "match": "^Other$"},
            ],
        },
        {"id": "clock"},
    ]
    assert data["bar"]["layout"]["right"] == [{"id": "tray"}]


def test_unpin_returns_false_when_the_pin_is_absent(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[_taskbar(["steam"])]))

    assert unpin_app(cfg, "winbridge-app") is False

    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "shell.json.bak").exists()


def test_unpin_returns_false_without_a_config_or_widget(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert unpin_app(missing, "winbridge-app") is False

    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[{"id": "clock"}]))
    assert unpin_app(cfg, "winbridge-app") is False
    assert cfg.read_text(encoding="utf-8") == original


def test_unpin_returns_false_when_the_widget_has_no_apps(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = _write(cfg, _shell(left=[_taskbar()]))

    assert unpin_app(cfg, "winbridge-app") is False

    assert cfg.read_text(encoding="utf-8") == original


def test_unpin_refuses_invalid_json_without_writing(tmp_path: Path) -> None:
    cfg = tmp_path / "shell.json"
    original = "{not json"
    cfg.write_text(original, encoding="utf-8")

    with pytest.raises(WinBridgeError):
        unpin_app(cfg, "winbridge-app")

    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "shell.json.bak").exists()


# --- shell restart ----------------------------------------------------------


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="")


def _explode(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("must not run")


def test_restart_shell_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", _explode)
    assert restart_shell(enabled=False, run=_explode) is False


def test_restart_shell_is_a_no_op_without_the_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", lambda name: None)
    assert restart_shell(run=_explode) is False


def test_restart_shell_runs_omarchy_restart_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", lambda name: "/usr/bin/omarchy")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs)))
        return _completed()

    assert restart_shell(run=fake_run) is True

    assert [argv for argv, _ in calls] == [["/usr/bin/omarchy", "restart", "shell"]]
    assert calls[0][1]["check"] is False
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 30.0


def test_restart_shell_warns_on_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", lambda name: "/usr/bin/omarchy")

    with caplog.at_level(logging.WARNING, logger="winbridge"):
        assert restart_shell(run=lambda argv, **kwargs: _completed(7)) is False

    assert "omarchy" in caplog.text
    assert "7" in caplog.text


def test_restart_shell_warns_instead_of_raising_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", lambda name: "/usr/bin/omarchy")

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no bus")

    with caplog.at_level(logging.WARNING, logger="winbridge"):
        assert restart_shell(run=failing) is False

    assert "no bus" in caplog.text


def test_restart_shell_warns_instead_of_raising_on_a_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(omarchy.shutil, "which", lambda name: "/usr/bin/omarchy")

    def timing_out(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 5)

    with caplog.at_level(logging.WARNING, logger="winbridge"):
        assert restart_shell(run=timing_out) is False

    assert "omarchy" in caplog.text
