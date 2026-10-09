"""Readable lines from the JSON log a session writes (`tbot log`)."""

import json
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

LEVELS = ("debug", "info", "warning", "error", "critical")
SHOWN = ("timestamp", "level", "event", "text", "logger", "exception")  # not as key=value
INDENT = " " * 28  # under the message, past the time and level


def level_rank(line: str) -> int:
    """Position of the line's level in LEVELS; a line that is not JSON counts as info."""
    try:
        record = json.loads(line)
    except ValueError:
        return 1
    level = record.get("level") if isinstance(record, dict) else None
    return LEVELS.index(level) if level in LEVELS else 1


def format_line(line: str) -> str:
    """`time level message key=value ...`, with an alert's text as the message."""
    line = line.rstrip("\r\n")
    try:
        record = json.loads(line)
    except ValueError:
        return line
    if not isinstance(record, dict):
        return line
    stamp = str(record.get("timestamp", ""))[:19].replace("T", " ")
    level = str(record.get("level", ""))
    message = str(record.get("text") or record.get("event", ""))
    fields = " ".join(f"{key}={_value(value)}" for key, value in record.items() if key not in SHOWN)
    text = f"{stamp} {level:<7} {message}" + (f"  {fields}" if fields else "")
    exception = record.get("exception")
    if exception:
        text += "\n" + str(exception).rstrip()
    return text.replace("\n", "\n" + INDENT)


def _value(value: object) -> str:
    if isinstance(value, str):
        return value if value and " " not in value else json.dumps(value)
    return json.dumps(value)


def tail(path: Path, count: int, minimum: int = 0) -> tuple[list[str], int]:
    """The last `count` lines at or above level `minimum`, and the size read."""
    with path.open("rb") as handle:
        data = handle.read()
    data = data[: data.rfind(b"\n") + 1]  # a line still being written waits for follow()
    kept: list[str] = []
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        if len(kept) >= count:
            break
        if line.strip() and level_rank(line) >= minimum:
            kept.append(line)
    return [format_line(line) for line in reversed(kept)], len(data)


def follow(
    path: Path,
    emit: Callable[[str], None],
    *,
    start: int = 0,
    minimum: int = 0,
    interval: float = 1.0,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Print lines as they are written, from byte `start`; a rotated log starts over."""
    position = start
    pending = b""

    def read(source: Path, end: int) -> None:
        nonlocal position, pending
        with source.open("rb") as handle:
            handle.seek(position)
            data = handle.read(end - position)
        position += len(data)
        *complete, pending = (pending + data).split(b"\n")
        for raw in complete:
            line = raw.decode("utf-8", errors="replace")
            if line.strip() and level_rank(line) >= minimum:
                emit(format_line(line))

    while not stop():
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < position:  # rotated: finish the old file, now .1, then start the new one
            old = path.with_name(path.name + ".1")
            with suppress(OSError):
                end = old.stat().st_size
                if end > position:
                    read(old, end)
            position, pending = 0, b""
        if size > position:
            read(path, size)
        time.sleep(interval)
