import logging

import structlog

from tbot.monitoring.logging import configure_logging


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
