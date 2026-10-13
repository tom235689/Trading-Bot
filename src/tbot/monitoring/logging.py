"""Structured logging: readable console output plus JSON lines on disk."""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from tbot.monitoring.logview import format_record

MAX_BYTES = 20 * 1024 * 1024
BACKUPS = 5
ROTATE_RETRY_SECONDS = 60.0  # after Windows refused to rename the log


class RetryingRotatingFileHandler(RotatingFileHandler):
    """Rotates like its parent, but while Windows refuses to rename the log (another
    process has it open) it writes on and tries again later: the parent would lose
    every record from then on and shift the backups away at each one."""

    retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> int:
        return time.monotonic() >= self.retry_at and super().shouldRollover(record)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        pending = self.baseFilename + ".rotating"
        try:
            if not os.path.exists(pending):  # else a rotation stopped halfway: finish it first
                os.replace(self.baseFilename, pending)
            for index in range(self.backupCount - 1, 0, -1):
                older = f"{self.baseFilename}.{index}"
                if os.path.exists(older):
                    os.replace(older, f"{self.baseFilename}.{index + 1}")
            os.replace(pending, f"{self.baseFilename}.1")
        except FileNotFoundError:
            pass  # nothing written yet
        except PermissionError:  # Windows: another process has one of the files open
            self.retry_at = time.monotonic() + ROTATE_RETRY_SECONDS
        self.stream = self._open()


def readable(logger: WrappedLogger, method: str, event: EventDict) -> str:
    """`time level message key=value`, as `tbot log` prints the JSON log."""
    return format_record(event)


def configure_quiet() -> None:
    """Commands other than sessions: warnings and errors only, readable, on stderr."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            readable,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        # Looked up at every call: a test or a caller may have replaced sys.stderr.
        logger_factory=lambda *args: structlog.PrintLogger(sys.stderr),
        cache_logger_on_first_use=False,
    )


def configure_logging(
    log_file: Path | None = None, level: int = logging.INFO, console_level: int = logging.NOTSET
) -> None:
    """Console lines plus JSON lines in log_file; console_level can show less on the console,
    such as a supervisor's console file that would otherwise grow for as long as the bot runs."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, readable],
            foreign_pre_chain=shared,  # websockets, httpx: same timestamp and level
        )
    )
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file = RetryingRotatingFileHandler(
            log_file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
        file.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=shared,
            )
        )
        root.addHandler(file)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
