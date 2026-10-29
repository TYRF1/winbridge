from __future__ import annotations

import logging
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from winbridge import __version__
from winbridge.cli import app, main
from winbridge.core.errors import (
    UnsupportedEnvironment,
    UsageError,
    VerifyFailed,
    WinBridgeError,
)
from winbridge.core.state import Paths

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_winbridge_logger() -> Iterator[None]:
    logger = logging.getLogger("winbridge")

    def clear() -> None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)

    clear()
    yield
    clear()


def test_version_flag() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_help_lists_global_options() -> None:
    result = CliRunner().invoke(app, ["--help"], color=False)
    assert result.exit_code == 0
    # Rich may wrap or colorize option names depending on the runner's terminal.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    compact = re.sub(r"\s+", "", plain)
    assert "--verbose" in compact
    assert "--version" in compact


def test_verbose_flag_is_accepted() -> None:
    result = CliRunner().invoke(app, ["--verbose", "--version"])
    assert result.exit_code == 0


def test_unknown_option_exits_two() -> None:
    result = CliRunner().invoke(app, ["--not-an-option"])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    ("error_type", "exit_code"),
    [
        (WinBridgeError, 1),
        (UsageError, 2),
        (UnsupportedEnvironment, 3),
        (VerifyFailed, 4),
    ],
)
def test_error_hierarchy_and_exit_codes(
    error_type: type[WinBridgeError], exit_code: int
) -> None:
    error = error_type("boom")
    assert isinstance(error, WinBridgeError)
    assert error.exit_code == exit_code
    assert str(error) == "boom"


def _app_raising(exc: BaseException) -> typer.Typer:
    failing = typer.Typer()

    @failing.command()
    def boom() -> None:
        raise exc

    return failing


@pytest.mark.parametrize(
    "error_type",
    [WinBridgeError, UsageError, UnsupportedEnvironment, VerifyFailed],
)
def test_main_maps_error_to_exit_code(
    error_type: type[WinBridgeError],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = error_type("something went wrong")
    monkeypatch.setattr("winbridge.cli.app", _app_raising(error))
    monkeypatch.setattr(sys, "argv", ["winbridge"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == error_type.exit_code
    assert "something went wrong" in capsys.readouterr().err


def test_main_exits_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = typer.Typer()

    @quiet.command()
    def noop() -> None:
        return None

    monkeypatch.setattr("winbridge.cli.app", quiet)
    monkeypatch.setattr(sys, "argv", ["winbridge"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0


def test_cli_writes_a_command_log(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    result = CliRunner().invoke(app, ["list"], obj={"paths": paths})

    assert result.exit_code == 0, result.output
    logs = list(paths.logs.glob("*-list.log"))
    assert len(logs) == 1
    assert logs[0].stat().st_size > 0


def test_cli_verbose_sets_the_logger_to_debug(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    result = CliRunner().invoke(app, ["--verbose", "list"], obj={"paths": paths})

    assert result.exit_code == 0, result.output
    assert logging.getLogger("winbridge").level == logging.DEBUG


def test_python_m_winbridge_entry_point_works() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "winbridge", "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert __version__ in completed.stdout


@pytest.mark.parametrize("command", [["list"], ["doctor"]])
def test_read_only_log_destination_does_not_block_the_command(
    tmp_path: Path, command: list[str]
) -> None:
    paths = Paths.from_root(tmp_path)
    paths.logs.write_text("not a directory", encoding="utf-8")

    result = CliRunner().invoke(app, command, obj={"paths": paths})

    assert result.exit_code == 0, result.output
    combined = result.output + result.stderr
    assert "warning: logging unavailable" in combined
