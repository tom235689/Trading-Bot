import os
import subprocess
from pathlib import Path

import pytest

from tbot.live.ledger import SCHEMA_META, WRITER_META, Ledger, LedgerUnavailable
from tbot.live.release import newest_release, update_note, version_key


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


@pytest.mark.usefixtures("isolated_git")
def test_the_newest_release_is_the_highest_version_tag_on_the_remote(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
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
    for tag in ("v0.9.0", "v0.10.0", "v0.10.0-rc1", "other"):
        git(origin, "tag", tag)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert newest_release(clone) == "0.10.0"  # by number, not by text
    assert newest_release(tmp_path / "not a clone") is None


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
