"""Desktop entries: Exec escaping, rendering, writing, removal, and icons."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from winbridge.core import launcher
from winbridge.core.errors import UsageError
from winbridge.core.launcher import (
    desktop_id,
    escape_exec_arg,
    extract_icon,
    remove_desktop_entry,
    remove_scheme_handler,
    render_desktop_entry,
    render_scheme_handler,
    winbridge_exec_path,
    write_desktop_entry,
    write_scheme_handler,
)
from winbridge.core.profile import Profile
from winbridge.core.state import Paths

MakeProfile = Callable[..., Profile]


def _no_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tool discovery deterministic regardless of the host image."""
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)


def _write_tool(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    tool.chmod(0o755)
    return tool


_OUTPUT_ARG_PARSER = """\
out=''
prev=''
for arg in "$@"; do
  case "$arg" in
    --output=*) out="${arg#--output=}" ;;
  esac
  if [ "$prev" = "-o" ]; then
    out="$arg"
  fi
  prev="$arg"
done
if [ -z "$out" ]; then
  echo "fake icoutils tool: --output is required" >&2
  exit 2
fi
"""


def _write_output_tool(directory: Path, name: str, body: str) -> Path:
    """A fake icoutils binary that writes only where ``--output`` points."""
    return _write_tool(directory, name, _OUTPUT_ARG_PARSER + body)


# --- escaping ---------------------------------------------------------------


def test_exec_escaping_doubles_backslashes_and_quotes_spaces() -> None:
    assert escape_exec_arg(r"C:\Program Files\App\app.exe") == (
        r'"C:\\\\Program Files\\\\App\\\\app.exe"'
    )


def test_exec_escaping_doubles_percent() -> None:
    assert escape_exec_arg("100%") == "100%%"


def test_exec_escaping_quotes_and_escapes_inner_quotes() -> None:
    assert escape_exec_arg('say "hi" now') == r'"say \\"hi\\" now"'


def test_exec_escaping_leaves_plain_tokens_bare() -> None:
    assert escape_exec_arg("/usr/bin/winbridge") == "/usr/bin/winbridge"
    assert escape_exec_arg("--flag=value") == "--flag=value"


def test_exec_escaping_keeps_an_empty_argument_visible() -> None:
    assert escape_exec_arg("") == '""'


def test_exec_escaping_folds_newlines_into_spaces() -> None:
    assert escape_exec_arg("a\nb") == '"a b"'
    assert "\n" not in escape_exec_arg("a\nb")


# --- desktop ids ------------------------------------------------------------


def test_desktop_id_is_bare_without_the_suffix() -> None:
    assert desktop_id("example-app") == "winbridge-example-app"
    assert desktop_id("example-app").endswith(".desktop") is False


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", "a b", ".hidden", r"a\b", "a\x00b"])
def test_desktop_id_rejects_paths_whitespace_and_nul(bad: str) -> None:
    with pytest.raises(UsageError):
        desktop_id(bad)


# --- wrapper path -----------------------------------------------------------


def test_exec_path_prefers_the_absolute_console_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/opt/my tools/bin/winbridge")
    assert winbridge_exec_path() == '"/opt/my tools/bin/winbridge"'


def test_exec_path_falls_back_to_python_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher.sys, "executable", "/opt/my python/bin/python3")
    assert winbridge_exec_path() == '"/opt/my python/bin/python3" -m winbridge'


# --- main entry -------------------------------------------------------------


def test_entry_uses_absolute_wrapper_and_no_scheme_mimetype(make_profile: MakeProfile) -> None:
    profile = make_profile(schemes=["example-app"])
    text = render_desktop_entry(profile, exec_path="/usr/bin/winbridge")
    assert text.startswith("[Desktop Entry]\n")
    assert "Type=Application" in text
    assert "Name=Example App" in text
    assert "Exec=/usr/bin/winbridge run example-app" in text
    assert "Icon=application-x-executable" in text
    assert "Terminal=false" in text
    assert "Categories=Utility;" in text
    assert "MimeType" not in text
    assert "NoDisplay" not in text
    assert text.endswith("\n")


def test_entry_includes_startup_wm_class_when_known(make_profile: MakeProfile) -> None:
    profile = make_profile()
    text = render_desktop_entry(
        profile,
        exec_path="/usr/bin/winbridge",
        startup_wm_class="Example Class",
    )
    assert "StartupWMClass=Example Class" in text
    assert "StartupWMClass" not in render_desktop_entry(
        profile, exec_path="/usr/bin/winbridge"
    )
    assert "StartupWMClass" not in render_desktop_entry(
        profile, exec_path="/usr/bin/winbridge", startup_wm_class=""
    )


def test_entry_uses_the_extracted_icon_when_given(make_profile: MakeProfile) -> None:
    text = render_desktop_entry(
        make_profile(),
        exec_path="/usr/bin/winbridge",
        icon="/home/me/icons/example-app.png",
    )
    assert "Icon=/home/me/icons/example-app.png" in text


def test_entry_accepts_an_extracted_icon_path(
    make_profile: MakeProfile, tmp_path: Path
) -> None:
    icon = tmp_path / "icons" / "example-app.png"
    text = render_desktop_entry(
        make_profile(),
        exec_path="/usr/bin/winbridge",
        icon=icon,
    )
    assert f"Icon={icon}" in text


def test_entry_escapes_value_backslashes(make_profile: MakeProfile) -> None:
    text = render_desktop_entry(
        make_profile(name=r"Example\App"),
        exec_path="/usr/bin/winbridge",
        startup_wm_class=r"Cls\One",
    )
    assert r"Name=Example\\App" in text
    assert r"StartupWMClass=Cls\\One" in text


def test_entry_name_cannot_inject_extra_keys(make_profile: MakeProfile) -> None:
    text = render_desktop_entry(
        make_profile(name="App\nExec=/bin/sh"),
        exec_path="/usr/bin/winbridge",
    )
    assert "\nExec=/bin/sh" not in text
    assert "Name=App Exec=/bin/sh" in text


# --- scheme handler ---------------------------------------------------------


def test_handler_entry_is_separate_and_takes_a_url(make_profile: MakeProfile) -> None:
    text = render_scheme_handler(
        make_profile(schemes=["example-app"]),
        exec_path="/usr/bin/winbridge",
    )
    assert text.startswith("[Desktop Entry]\n")
    assert "NoDisplay=true" in text
    assert "Exec=/usr/bin/winbridge open-url example-app %u" in text
    assert "MimeType=x-scheme-handler/example-app;" in text
    assert "StartupWMClass" not in text


def test_handler_lists_every_scheme_once(make_profile: MakeProfile) -> None:
    text = render_scheme_handler(
        make_profile(schemes=["first-app", "second-app", "first-app"]),
        exec_path="/usr/bin/winbridge",
    )
    assert "MimeType=x-scheme-handler/first-app;x-scheme-handler/second-app;" in text


@pytest.mark.parametrize(
    "schemes",
    [[], [""], ["1bad"], ["bad scheme"], ["bad/scheme"], ["bad\nscheme"], ["a", r"b\c"]],
)
def test_handler_rejects_empty_or_malformed_schemes(
    make_profile: MakeProfile, schemes: list[str]
) -> None:
    profile = make_profile(schemes=schemes)
    with pytest.raises(UsageError):
        render_scheme_handler(profile, exec_path="/usr/bin/winbridge")


# --- writing ----------------------------------------------------------------


def test_write_desktop_entry_sets_mode_and_returns_its_path(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    written = write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")
    assert written == paths.applications / "winbridge-example-app.desktop"
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert "Exec=/usr/bin/winbridge run example-app" in written.read_text(encoding="utf-8")
    assert [entry.name for entry in paths.applications.iterdir()] == [
        "winbridge-example-app.desktop"
    ]


def test_rewrite_refreshes_the_entry_with_one_wm_class(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    first = write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")
    second = write_desktop_entry(
        paths,
        make_profile(),
        exec_path="/usr/bin/winbridge",
        startup_wm_class="Example",
    )
    text = second.read_text(encoding="utf-8")
    assert first == second
    assert text.count("StartupWMClass=") == 1
    assert "StartupWMClass=Example" in text
    assert stat.S_IMODE(second.stat().st_mode) == 0o644


def test_write_desktop_entry_passes_an_icon_through(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    written = write_desktop_entry(
        paths,
        make_profile(),
        exec_path="/usr/bin/winbridge",
        icon="/home/me/icons/example-app.png",
    )
    assert "Icon=/home/me/icons/example-app.png" in written.read_text(encoding="utf-8")


def test_write_desktop_entry_accepts_an_extracted_icon_path(
    paths: Paths,
    make_profile: MakeProfile,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _no_external_tools(monkeypatch)
    icon = tmp_path / "icons" / "example-app.png"
    written = write_desktop_entry(
        paths,
        make_profile(),
        exec_path="/usr/bin/winbridge",
        icon=icon,
    )
    assert f"Icon={icon}" in written.read_text(encoding="utf-8")


def test_write_scheme_handler_uses_the_handler_suffix(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    written = write_scheme_handler(paths, make_profile(), exec_path="/usr/bin/winbridge")
    assert written == paths.applications / "winbridge-example-app-handler.desktop"
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert "NoDisplay=true" in written.read_text(encoding="utf-8")


def test_write_refreshes_the_desktop_database_best_effort(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: "/usr/bin/update-desktop-database"
        if name == "update-desktop-database"
        else None,
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(list(argv)),
    )
    write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")
    assert calls == [["/usr/bin/update-desktop-database", str(paths.applications)]]


def test_missing_desktop_database_tool_is_not_an_error(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not run an undiscovered tool")

    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher.subprocess, "run", explode)
    write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")


# --- removal ----------------------------------------------------------------


def test_remove_desktop_entry_deletes_only_its_own_file(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    ours = write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")
    handler = write_scheme_handler(paths, make_profile(), exec_path="/usr/bin/winbridge")
    other = paths.applications / "winbridge-other.desktop"
    other.write_text("[Desktop Entry]\n", encoding="utf-8")

    remove_desktop_entry(paths, "example-app")

    assert not ours.exists()
    assert handler.exists()
    assert other.exists()


def test_remove_scheme_handler_deletes_only_its_own_file(
    paths: Paths, make_profile: MakeProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    launcher_entry = write_desktop_entry(paths, make_profile(), exec_path="/usr/bin/winbridge")
    handler = write_scheme_handler(paths, make_profile(), exec_path="/usr/bin/winbridge")

    remove_scheme_handler(paths, "example-app")

    assert not handler.exists()
    assert launcher_entry.exists()


def test_remove_is_idempotent_and_does_not_create_state(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    remove_desktop_entry(paths, "example-app")
    remove_scheme_handler(paths, "example-app")
    assert not paths.applications.exists()


def test_remove_does_not_delete_a_directory(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_external_tools(monkeypatch)
    squatter = paths.applications / "winbridge-example-app.desktop"
    squatter.mkdir(parents=True)

    remove_desktop_entry(paths, "example-app")

    assert squatter.is_dir()


# --- icons ------------------------------------------------------------------


def test_icon_falls_back_when_wrestool_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert extract_icon(tmp_path / "x.exe", tmp_path / "icon.png", tmp_path / "runner") is None


def test_icon_requires_the_executable_to_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _write_output_tool(bindir, "wrestool", 'printf ico > "${out}icon.ico"\n')
    _write_output_tool(bindir, "icotool", 'printf png > "${out}icon.png"\n')
    monkeypatch.setattr(shutil, "which", lambda name: str(bindir / name))

    assert (
        extract_icon(tmp_path / "missing.exe", tmp_path / "icon.png", tmp_path / "runner")
        is None
    )


def test_icon_extracts_the_largest_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _write_output_tool(bindir, "wrestool", 'printf ico > "${out}icon.ico"\n')
    _write_output_tool(
        bindir,
        "icotool",
        "printf 'small' > \"${out}icon_16.png\"\n"
        "printf 'a much larger payload' > \"${out}icon_256.png\"\n",
    )
    monkeypatch.setattr(shutil, "which", lambda name: str(bindir / name))
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"exe")
    dest = tmp_path / "icons" / "example-app.png"

    assert extract_icon(exe, dest, tmp_path / "runner") == dest
    assert dest.read_text(encoding="utf-8") == "a much larger payload"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def test_icon_passes_an_output_directory_to_each_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _write_output_tool(bindir, "wrestool", 'printf ico > "${out}icon.ico"\n')
    _write_output_tool(bindir, "icotool", 'printf png > "${out}icon.png"\n')
    monkeypatch.setattr(shutil, "which", lambda name: str(bindir / name))
    calls: list[list[str]] = []
    real_run = subprocess.run

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        for argument in argv:
            if argument.startswith("--output="):
                output = Path(argument.split("=", 1)[1])
                assert output.is_dir()
                assert argument.endswith(os.sep)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "run", record)
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"exe")
    dest = tmp_path / "icon.png"

    assert extract_icon(exe, dest, tmp_path / "runner") == dest

    assert len(calls) == 2
    wrestool, icotool = calls
    assert Path(wrestool[0]).name == "wrestool"
    assert "--extract" in wrestool
    assert "--type=14" in wrestool
    assert any(argument.startswith("--output=") for argument in wrestool)
    assert Path(icotool[0]).name == "icotool"
    assert "--extract" in icotool
    assert any(argument.startswith("--output=") for argument in icotool)


def test_icon_uses_tools_from_the_runner_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = tmp_path / "runner"
    _write_output_tool(runner / "bin", "wrestool", 'printf ico > "${out}icon.ico"\n')
    _write_output_tool(runner / "bin", "icotool", 'printf png > "${out}icon.png"\n')
    monkeypatch.setattr(shutil, "which", lambda name: None)
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"exe")
    dest = tmp_path / "icon.png"

    assert extract_icon(exe, dest, runner) == dest
    assert dest.read_text(encoding="utf-8") == "png"


def test_icon_returns_none_when_wrestool_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _write_output_tool(bindir, "wrestool", "exit 1\n")
    _write_output_tool(bindir, "icotool", 'printf png > "${out}icon.png"\n')
    monkeypatch.setattr(shutil, "which", lambda name: str(bindir / name))
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"exe")
    dest = tmp_path / "icon.png"

    assert extract_icon(exe, dest, tmp_path / "runner") is None
    assert not dest.exists()


def test_icon_returns_none_when_no_png_is_produced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    _write_output_tool(bindir, "wrestool", 'printf ico > "${out}icon.ico"\n')
    _write_output_tool(bindir, "icotool", "exit 0\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(bindir / name))
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"exe")
    dest = tmp_path / "icon.png"

    assert extract_icon(exe, dest, tmp_path / "runner") is None
    assert not dest.exists()
