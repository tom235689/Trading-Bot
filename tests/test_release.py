import os
import subprocess
from pathlib import Path

import httpx
import pytest

from tbot.live.ledger import SCHEMA_META, WRITER_META, Ledger, LedgerUnavailable
from tbot.live.release import newest_release, published_release, update_note, version_key


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Hooks export GIT_DIR and friends; never let tests touch the real repository.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def clone_with_tags(folder: Path, *tags: str) -> Path:
    origin = folder / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(
        origin,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    for tag in tags:
        git(origin, "tag", tag)
    clone = folder / "clone"
    git(folder, "clone", "-q", str(origin), str(clone))
    return clone


@pytest.mark.usefixtures("isolated_git")
def test_the_newest_release_is_the_highest_version_tag_on_the_remote(tmp_path: Path) -> None:
    clone = clone_with_tags(tmp_path, "v0.9.0", "v0.10.0", "v0.10.0-rc1", "other")
    assert newest_release(clone) == "0.10.0"  # by number, not by text
    assert newest_release(tmp_path / "not a clone") is None


@pytest.mark.usefixtures("isolated_git")
def test_a_published_release_counts_before_a_newer_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = clone_with_tags(tmp_path, "v0.9.0", "v0.9.1")
    asked: list[str] = []

    def published(url: str, timeout: float) -> str | None:
        asked.append(url)
        return "0.9.0"

    monkeypatch.setattr("tbot.live.release.published_release", published)
    assert newest_release(clone) == "0.9.0"  # 0.9.1 failed its checks: a tag, no release
    assert Path(asked[0]) == tmp_path / "origin"
    monkeypatch.setattr("tbot.live.release.published_release", lambda url, timeout: None)
    assert newest_release(clone) == "0.9.1"  # GitHub cannot tell: the tags do


def answering(answer: object, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/bot/releases"
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(handle)


def test_the_published_release_skips_drafts_prereleases_and_other_tags() -> None:
    answer = [
        {"tag_name": "v0.11.0", "draft": True, "prerelease": False},
        {"tag_name": "v0.10.1", "draft": False, "prerelease": True},
        {"tag_name": "v0.9.3", "draft": False, "prerelease": False},
        {"tag_name": "v0.10.0", "draft": False, "prerelease": False},
        {"tag_name": "nightly", "draft": False, "prerelease": False},
    ]
    for url in (
        "https://github.com/owner/bot.git",
        "git@github.com:owner/bot.git",
        "https://github.com/owner/bot/",
    ):
        assert published_release(url, transport=answering(answer)) == "0.10.0"


def test_no_published_release_when_github_cannot_tell() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    url = "https://github.com/owner/bot.git"
    assert published_release(url, transport=httpx.MockTransport(offline)) is None
    assert published_release(url, transport=answering({"message": "rate limit"}, 403)) is None
    assert published_release(url, transport=answering({"message": "an odd answer"})) is None
    assert published_release(url, transport=answering([])) is None
    # Not on GitHub: nothing is asked.
    assert published_release("D:/remote.git", transport=httpx.MockTransport(offline)) is None


def test_an_update_is_named_only_when_newer() -> None:
    assert version_key("0.10.0") > version_key("0.9.3")
    assert update_note("0.8.0", "0.7.0").startswith("tbot 0.8.0 is out (this is 0.7.0)")
    assert update_note("0.7.0", "0.7.0") == ""
    assert update_note(None, "0.7.0") == ""


def test_a_ledger_written_by_a_newer_tbot_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite"
    with Ledger(path) as ledger:
        ledger.mark_written()
        assert ledger.get_meta(SCHEMA_META) == "1"
        ledger.set_meta(SCHEMA_META, "2")
        ledger.set_meta(WRITER_META, "9.9.0")
    with pytest.raises(LedgerUnavailable, match=r"written by tbot 9\.9\.0, newer than this"):
        Ledger(path)
