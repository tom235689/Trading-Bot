"""Whether a newer release is out: asked of GitHub or the clone's remote, never in the way of
trading. A release is the newest vX.Y.Z tag with a published GitHub release (a version whose
checks failed has a tag but no release), as `tbot update` installs it."""

import os
import re
import subprocess
from pathlib import Path

import httpx

from tbot import __version__

REPOSITORY = Path(__file__).resolve().parents[3]  # the clone tbot runs from, if it is one
TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
RELEASE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
GITHUB = re.compile(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$")


def version_key(version: str) -> tuple[int, ...]:
    """`0.8.0` -> (0, 8, 0); a suffix such as `.dev1` is ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def newest_release(repository: Path = REPOSITORY, timeout: float = 20.0) -> str | None:
    """The newest published GitHub release when origin is on GitHub; else, or when GitHub
    does not answer, the highest vX.Y.Z tag on the remote. None when neither can be asked:
    no git, no network, or not a clone."""
    if not (repository / ".git").exists():
        return None
    origin = git(repository, timeout, "remote", "get-url", "origin")
    published = published_release(origin.strip(), timeout / 2) if origin else None
    return published or newest_tag(repository, timeout / 2)


def published_release(
    url: str, timeout: float = 20.0, transport: httpx.BaseTransport | None = None
) -> str | None:
    """The highest version among a GitHub repository's published releases (no drafts or
    pre-releases), or None: origin not on GitHub, no answer, or nothing published."""
    match = GITHUB.search(url)
    if match is None:
        return None
    api = f"https://api.github.com/repos/{match[1]}/{match[2]}/releases"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(
                api, params={"per_page": 30}, headers={"Accept": "application/vnd.github+json"}
            )
            response.raise_for_status()
            found = [
                m.groups()
                for release in response.json()
                if not release.get("draft") and not release.get("prerelease")
                if (m := RELEASE.fullmatch(str(release.get("tag_name"))))
            ]
    except Exception:  # a rate limit, an odd answer: the tags still tell
        return None
    return highest(found)


def newest_tag(repository: Path, timeout: float) -> str | None:
    """The highest vX.Y.Z tag on the remote, or None when it cannot be asked."""
    listing = git(repository, timeout, "ls-remote", "--tags", "--refs", "origin", "v*")
    if listing is None:
        return None
    return highest([m.groups() for line in listing.splitlines() if (m := TAG.search(line))])


def highest(found: list[tuple[str, ...]]) -> str | None:
    if not found:
        return None
    return ".".join(max(found, key=lambda parts: tuple(int(p) for p in parts)))


def git(repository: Path, timeout: float, *args: str) -> str | None:
    """Git's output, or None when it fails or cannot run."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never wait for a password
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def update_note(latest: str | None, current: str = __version__) -> str:
    """What to tell the owner when `latest` is newer than this tbot, else ""."""
    if latest is None or version_key(latest) <= version_key(current):
        return ""
    return f"tbot {latest} is out (this is {current}): read CHANGELOG.md, then `tbot update`"
