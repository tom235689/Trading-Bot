"""Whether a newer release is out: asked of the clone's remote, never in the way of trading."""

import os
import re
import subprocess
from pathlib import Path

from tbot import __version__

REPOSITORY = Path(__file__).resolve().parents[3]  # the clone tbot runs from, if it is one
TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")


def version_key(version: str) -> tuple[int, ...]:
    """`0.8.0` -> (0, 8, 0); a suffix such as `.dev1` is ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def newest_release(repository: Path = REPOSITORY, timeout: float = 20.0) -> str | None:
    """The highest vX.Y.Z tag on the remote, or None when it cannot be asked: no git, no
    network, or not a clone."""
    if not (repository / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "origin", "v*"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never wait for a password
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    found = [m.groups() for line in result.stdout.splitlines() if (m := TAG.search(line))]
    if not found:
        return None
    return ".".join(max(found, key=lambda parts: tuple(int(p) for p in parts)))


def update_note(latest: str | None, current: str = __version__) -> str:
    """What to tell the owner when `latest` is newer than this tbot, else ""."""
    if latest is None or version_key(latest) <= version_key(current):
        return ""
    return f"tbot {latest} is out (this is {current}): read CHANGELOG.md, then `tbot update`"
