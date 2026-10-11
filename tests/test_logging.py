import logging

import pytest
import structlog

from tbot.monitoring.logging import configure_logging, configure_quiet


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
