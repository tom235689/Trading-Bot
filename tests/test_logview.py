"""`tbot log`: the JSON log as lines a person reads."""

import json
from pathlib import Path

from tbot.monitoring.logview import LEVELS, follow, format_line, tail


def record(event: str, level: str = "info", **fields: object) -> bytes:
    stamp = {"timestamp": "2026-10-08T10:06:21.714675Z"}
    return (json.dumps({"event": event, "level": level, **stamp, **fields}) + "\r\n").encode()


def test_a_record_reads_as_one_line() -> None:
    line = record("clock_synced", logger="tbot.live.clock", offset_seconds=0.305, hint="a b")
    assert format_line(line.decode()) == (
        '2026-10-08 10:06:21 info    clock_synced  offset_seconds=0.305 hint="a b"'
    )
    assert format_line("not json\n") == "not json"


def test_an_alert_shows_its_text_and_an_error_its_traceback() -> None:
    alert = record("notify", text="[paper] daily summary\nequity 1,000.00")
    assert format_line(alert.decode()) == (
        "2026-10-08 10:06:21 info    [paper] daily summary\n" + " " * 28 + "equity 1,000.00"
    )
    error = record("task_crashed", "error", task="feed", exception="Traceback\nValueError: x")
    assert format_line(error.decode()).splitlines() == [
        "2026-10-08 10:06:21 error   task_crashed  task=feed",
        " " * 28 + "Traceback",
        " " * 28 + "ValueError: x",
    ]


def test_tail_keeps_the_last_lines_at_or_above_a_level(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    lines = b"".join(record(f"e{i}", "warning" if i % 3 == 0 else "info") for i in range(10))
    path.write_bytes(lines + b'{"event": "still being wri')
    shown, size = tail(path, 2, LEVELS.index("info"))
    assert [line.split()[3] for line in shown] == ["e8", "e9"]
    assert size == len(lines)  # follow() picks up the unfinished line
    shown, _ = tail(path, 2, LEVELS.index("warning"))
    assert [line.split()[3] for line in shown] == ["e6", "e9"]
    assert tail(path, 5, LEVELS.index("error")) == ([], len(lines))


def test_follow_prints_new_lines_and_starts_over_after_a_rotation(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    old = record("old")
    path.write_bytes(old + record("new") + record("noise", "debug") + b'{"event": "par')
    seen: list[str] = []
    polls = 0

    def emit(line: str) -> None:
        seen.append(line.split()[3])
        if line.endswith("new"):
            path.write_bytes(record("rotated"))  # the handler starts a new file

    def stop() -> bool:
        nonlocal polls
        polls += 1
        return polls > 2

    follow(path, emit, start=len(old), minimum=LEVELS.index("info"), interval=0, stop=stop)
    assert seen == ["new", "rotated"]


def test_follow_finishes_the_old_file_before_the_new_one(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    first = record("first")
    path.write_bytes(first)
    seen: list[str] = []
    polls = 0

    def stop() -> bool:
        nonlocal polls
        polls += 1
        if polls == 1:  # between two polls: one more line, then the handler rotates
            path.write_bytes(first + record("last of the old"))
            path.replace(tmp_path / "paper.jsonl.1")
            path.write_bytes(record("new"))
        return polls > 1

    follow(path, lambda line: seen.append(line.split()[3]), start=len(first), interval=0, stop=stop)
    assert seen == ["last", "new"]
