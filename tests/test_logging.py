import logging
import sys
from pathlib import Path

import pytest
import structlog

from tbot.monitoring.logging import (
    RetryingRotatingFileHandler,
    configure_logging,
    configure_quiet,
)


def test_the_console_can_show_less_than_the_log_file(tmp_path: object) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging(None, console_level=logging.WARNING)
        [console] = root.handlers
        assert console.level == logging.WARNING
        assert root.level == logging.INFO  # the file, when there is one, still gets INFO
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        structlog.reset_defaults()


def test_commands_print_warnings_only_and_readably(capsys: pytest.CaptureFixture[str]) -> None:
    configure_quiet()
    log = structlog.get_logger("test")
    log.info("hidden", a=1)
    log.warning("clock_synced", offset_seconds=1.5)
    err = capsys.readouterr().err
    assert "hidden" not in err
    assert " warning clock_synced  offset_seconds=1.5" in err


def test_a_session_console_reads_like_tbot_log(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging(None)
        structlog.get_logger("test").warning("stale_stream", symbol="BTCUSDT")
        err = capsys.readouterr().err
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        configure_quiet()
    assert " warning stale_stream  symbol=BTCUSDT" in err


def test_a_log_another_process_holds_open_keeps_its_records(tmp_path: Path) -> None:
    path = tmp_path / "bot.jsonl"
    # Backups enough for every record where a rename of an open file works (Linux).
    handler = RetryingRotatingFileHandler(path, maxBytes=200, backupCount=20, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    def write(text: str) -> None:
        handler.emit(logging.makeLogRecord({"msg": text}))

    try:
        for index in range(5):
            write(f"first {index} " + "x" * 60)
        with path.open("rb"):  # on Windows nobody can rename it now
            for index in range(5):
                write(f"held {index} " + "x" * 60)
        handler.retry_at = 0.0  # a minute later
        write("after " + "x" * 60)
    finally:
        handler.close()
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("bot.jsonl*"))
    for index in range(5):
        assert f"held {index} " in text
    assert "after " in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.rotating"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses renaming an open file")
def test_a_backup_held_open_during_a_rotation_loses_no_records(tmp_path: Path) -> None:
    path = tmp_path / "bot.jsonl"
    handler = RetryingRotatingFileHandler(path, maxBytes=300, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    errors: list[str] = []
    handler.handleError = lambda record: errors.append(record.getMessage())  # type: ignore[method-assign]

    def write(text: str) -> None:
        handler.emit(logging.makeLogRecord({"msg": text + " " + "x" * 60}))

    try:
        for index in range(5):
            write(f"first-{index}")  # the fifth rotates: bot.jsonl.1
        for index in range(1, 4):
            write(f"second-{index}")
        with (tmp_path / "bot.jsonl.1").open("rb"):  # a reader holds the backup
            write("third-0")  # base moved aside, then .1 -> .2 refused
        handler.retry_at = 0.0  # a minute later
        for index in range(6):
            write(f"fourth-{index}")
    finally:
        handler.close()
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("bot.jsonl*"))
    assert errors == []
    assert all(f"second-{index} " in text for index in range(1, 4))
    assert not list(tmp_path.glob("*.rotating"))
