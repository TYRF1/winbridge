from __future__ import annotations

import io
import logging
import re
import stat
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from rich.logging import RichHandler

from winbridge.core.state import Paths
from winbridge.util.log import evidence_dir, redact_url, setup_logging


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _reset_winbridge_logger() -> Iterator[None]:
    logger = logging.getLogger("winbridge")

    def _clear() -> None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    _clear()
    yield
    _clear()


def _only_log(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    assert len(matches) == 1, matches
    return matches[0]


def _global_log(paths: Paths) -> Path:
    return paths.logs.parent / "winbridge.log"


def _rotating_handler(logger: logging.Logger) -> RotatingFileHandler:
    handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
    assert len(handlers) == 1, handlers
    return handlers[0]


def test_redact_url_keeps_scheme_and_host_only() -> None:
    assert (
        redact_url("https://example.com/cb?code=SECRET&state=x")
        == "https://example.com/[redacted]"
    )


def test_redact_url_replaces_an_empty_path() -> None:
    assert redact_url("https://example.com") == "https://example.com/[redacted]"


def test_redact_url_keeps_the_port() -> None:
    assert (
        redact_url("https://example.com:8443/cb?code=SECRET")
        == "https://example.com:8443/[redacted]"
    )


def test_redact_url_drops_credentials() -> None:
    assert redact_url("https://user:pass@example.com/cb") == "https://example.com/[redacted]"


def test_redact_url_keeps_ipv6_host_bracketed() -> None:
    assert redact_url("http://[::1]:8080/cb?code=SECRET") == "http://[::1]:8080/[redacted]"


def test_redact_url_without_host_is_fully_redacted() -> None:
    assert redact_url("mailto:someone@example.com") == "[redacted]"
    assert redact_url("file:///home/me/private.txt") == "[redacted]"
    assert redact_url("https://") == "[redacted]"


def test_redact_url_rejects_non_urls() -> None:
    assert redact_url("SECRET-TOKEN") == "[redacted]"
    assert redact_url("") == "[redacted]"


def test_redact_url_rejects_an_unparsable_port() -> None:
    assert redact_url("https://example.com:not-a-port/cb") == "[redacted]"


def test_evidence_dir_is_private(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    path = evidence_dir(paths, "app", "1.0")

    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_evidence_dir_builds_the_documented_layout(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    path = evidence_dir(paths, "app", "1.0")

    assert path.is_dir()
    assert path.parent.parent.parent == paths.evidence
    assert path.parent.parent.name == "app"
    assert path.parent.name == "1.0"
    for directory in (paths.evidence, path.parent.parent, path.parent, path):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_evidence_dir_defaults_to_unknown_version(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    assert evidence_dir(paths, "app").parent.name == "unknown"


@pytest.mark.parametrize(
    "app_id",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("../escape", id="parent"),
        pytest.param("a/b", id="slash"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_evidence_dir_rejects_unsafe_app_ids(tmp_path: Path, app_id: str) -> None:
    with pytest.raises(ValueError):
        evidence_dir(Paths.from_root(tmp_path), app_id)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("../escape", id="parent"),
        pytest.param("a/b", id="slash"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_evidence_dir_rejects_unsafe_versions(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError):
        evidence_dir(Paths.from_root(tmp_path), "app", version)


def test_setup_logging_app_writes_evidence_log(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    logger = setup_logging(paths, command="setup", app_id="app", version="1.0")
    logger.info("[setup] detected webview2=%s", "none")

    log = _only_log(paths.evidence / "app" / "1.0", "*/winbridge.log")
    assert "[setup] detected webview2=none" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert not paths.logs.exists()

    global_log = _global_log(paths)
    assert "[setup] detected webview2=none" in global_log.read_text(encoding="utf-8")
    assert stat.S_IMODE(global_log.stat().st_mode) == 0o600


def test_setup_logging_without_app_writes_command_log(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    logger = setup_logging(paths, command="doctor")
    logger.info("[doctor] checked prefix")

    log = _only_log(paths.logs, "*-doctor.log")
    assert log.name.endswith("-doctor.log")
    assert "[doctor] checked prefix" in log.read_text(encoding="utf-8")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.logs.stat().st_mode) == 0o700
    assert not paths.evidence.exists()

    global_log = _global_log(paths)
    assert "[doctor] checked prefix" in global_log.read_text(encoding="utf-8")
    assert stat.S_IMODE(global_log.stat().st_mode) == 0o600


def test_setup_logging_writes_a_record_once_to_each_destination(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    logger = setup_logging(paths, command="doctor")
    logger.info("[doctor] teed record")

    command_log = _only_log(paths.logs, "*-doctor.log")
    global_log = _global_log(paths)
    for destination in (command_log, global_log):
        assert destination.read_text(encoding="utf-8").count("[doctor] teed record") == 1


def test_global_log_records_timestamp_level_and_logger_name(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    setup_logging(paths, command="doctor")
    logging.getLogger("winbridge.core.doctor").info("[doctor] named record")

    text = _global_log(paths).read_text(encoding="utf-8")
    assert re.search(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} INFO\s+"
        r"winbridge\.core\.doctor\s+\[doctor\] named record$",
        text,
        re.MULTILINE,
    ), text


def test_global_log_lives_inside_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    paths = Paths.default()
    logger = setup_logging(paths, command="doctor")
    logger.info("[doctor] production path")

    global_log = data_home / "winbridge" / "winbridge.log"
    assert global_log.is_file()
    assert "[doctor] production path" in global_log.read_text(encoding="utf-8")
    assert stat.S_IMODE(global_log.stat().st_mode) == 0o600
    assert stat.S_IMODE(global_log.parent.stat().st_mode) == 0o700
    assert not (data_home / "winbridge.log").exists()


def test_global_log_uses_a_rotating_handler(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    logger = setup_logging(paths, command="doctor")

    handler = _rotating_handler(logger)
    assert Path(handler.baseFilename) == _global_log(paths)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 2


def test_global_log_rotation_keeps_every_file_private(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    logger = setup_logging(paths, command="doctor")
    logger.info("[doctor] before rotation")
    _rotating_handler(logger).doRollover()
    logger.info("[doctor] after rotation")

    base = _global_log(paths)
    backup = base.with_name(base.name + ".1")
    assert "before rotation" in backup.read_text(encoding="utf-8")
    assert "after rotation" in base.read_text(encoding="utf-8")
    assert stat.S_IMODE(base.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_setup_logging_is_verbose_only_when_requested(tmp_path: Path) -> None:
    quiet_paths = Paths.from_root(tmp_path / "quiet")
    loud_paths = Paths.from_root(tmp_path / "loud")

    quiet = setup_logging(quiet_paths, command="run")
    assert quiet.level == logging.INFO
    quiet.debug("[run] hidden detail")

    loud = setup_logging(loud_paths, command="run", verbose=True)
    assert loud.level == logging.DEBUG
    loud.debug("[run] subprocess detail")

    quiet_log = _only_log(quiet_paths.logs, "*-run.log")
    loud_log = _only_log(loud_paths.logs, "*-run.log")
    assert "hidden detail" not in quiet_log.read_text(encoding="utf-8")
    assert "subprocess detail" in loud_log.read_text(encoding="utf-8")


def test_setup_logging_replaces_its_own_handlers(tmp_path: Path) -> None:
    paths = Paths.from_root(tmp_path)

    first = setup_logging(paths, command="doctor")
    first_handler = first.handlers[0]

    second = setup_logging(paths, command="doctor")

    assert second is first
    assert len(second.handlers) == 2
    assert first_handler not in second.handlers
    global_path = str(_global_log(paths))
    global_handlers = [
        handler
        for handler in second.handlers
        if getattr(handler, "baseFilename", None) == global_path
    ]
    assert len(global_handlers) == 1


def test_setup_logging_adds_rich_handler_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stderr", _TtyBuffer())

    logger = setup_logging(Paths.from_root(tmp_path), command="doctor")

    handler = next(handler for handler in logger.handlers if isinstance(handler, RichHandler))
    # RichHandler defaults to a stdout console; that corrupts NDJSON on a TTY.
    assert handler.console.stderr is True


def test_rich_handler_keeps_stdout_pure_for_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On a real TTY, log lines go to stderr and never interleave with NDJSON."""
    stderr = _TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    logger = setup_logging(Paths.from_root(tmp_path), command="run")

    logger.info('[cli] {"event": "log"}')

    plain = re.sub(r"\x1b\[[0-9;]*m", "", stderr.getvalue())
    assert '[cli] {"event": "log"}' in plain
    assert capsys.readouterr().out == ""


def test_setup_logging_adds_no_rich_handler_without_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    logger = setup_logging(Paths.from_root(tmp_path), command="doctor")

    assert not any(isinstance(handler, RichHandler) for handler in logger.handlers)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("../escape", id="parent"),
        pytest.param("a/b", id="slash"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_setup_logging_rejects_unsafe_commands(tmp_path: Path, command: str) -> None:
    with pytest.raises(ValueError):
        setup_logging(Paths.from_root(tmp_path), command=command)
