"""Copies of a session ledger, the book of record: one per day, the oldest removed."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import structlog

from tbot.live.ledger import Ledger
from tbot.monitoring.telegram import Notifier

log = structlog.get_logger(__name__)
BACKUP_SECONDS = 6 * 3600  # today's copy is at most this old
BACKUP_DIR = "backups"


def backup_dir(ledger: Path) -> Path:
    return ledger.parent / BACKUP_DIR


def daily_backups(ledger: Path) -> list[Path]:
    """The rotated copies, oldest first. Copies made with `tbot backup` are not among them."""
    return sorted(backup_dir(ledger).glob(f"{ledger.stem}-{'[0-9]' * 8}.sqlite"))


def backup_ledger(ledger: Ledger, path: Path, now: datetime, keep: int) -> Path:
    """Copy the ledger to backups/<name>-<yyyymmdd>.sqlite and keep the newest `keep` days."""
    target = backup_dir(path) / f"{path.stem}-{now:%Y%m%d}.sqlite"
    ledger.backup(target)
    for old in daily_backups(path)[:-keep]:
        old.unlink(missing_ok=True)
    return target


async def backup_loop(
    ledger: Ledger,
    path: Path,
    keep: int,
    lock: asyncio.Lock,
    notifier: Notifier,
    label: str,
    clock: Callable[[], datetime],
) -> None:
    failing = False
    while True:
        try:
            async with lock:  # never in the middle of an event's writes
                target = backup_ledger(ledger, path, clock(), keep)
            log.info("ledger_backup", path=str(target))
            failing = False
        except Exception as exc:  # a backup must never stop trading
            log.error("backup_failed", error=repr(exc))
            if not failing:
                await notifier.send(f"[{label}] ledger backup failed: {exc!r}")
            failing = True
        await asyncio.sleep(BACKUP_SECONDS)
