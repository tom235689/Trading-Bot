"""Tests for scripts/git_guard.py. Test text is built with chr() so this file stays ASCII."""

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

import git_guard as gg

SYLLABLE = chr(0xAC00)
HANGUL_SAMPLES = [chr(c) for c in (0x1100, 0x3131, 0x3200, 0xA960, 0xAC00, 0xD7A3)]
OTHER_SAMPLES = ["plain ascii", chr(0x3042) + chr(0x4E2D), "caf" + chr(0xE9), chr(0x1F600)]


def kinds(findings: Iterable[gg.Finding]) -> list[str]:
    return [finding.kind for finding in findings]


@pytest.mark.parametrize("char", HANGUL_SAMPLES)
def test_hangul_detected(char: str) -> None:
    assert kinds(gg.scan_line("x", f"abc {char}")) == ["Hangul at column 5"]


@pytest.mark.parametrize("text", OTHER_SAMPLES)
def test_other_text_passes(text: str) -> None:
    assert list(gg.scan_line("x", text)) == []


def test_invalid_utf8_flagged() -> None:
    assert kinds(gg.scan_line("x", "bad " + chr(0xFFFD))) == ["invalid UTF-8"]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("-----BEGIN " + "RSA PRIVATE KEY-----", "private key"),
        ("123456789:" + "A" * 35, "telegram bot token"),
        ("key " + "aB3" * 21 + "x", "api key"),
        ("BINANCE_API_KEY=" + "x" * 20, "credential assignment"),
        ('"api_secret": "' + "y" * 20 + '"', "credential assignment"),
    ],
)
def test_secret_detected(text: str, name: str) -> None:
    assert f"possible secret ({name})" in kinds(gg.scan_line("x", text))


@pytest.mark.parametrize(
    "text",
    [
        "sha256:" + "a1" * 32,
        "password_hash = compute(value)",
        "api_key = os.environ['BINANCE_API_KEY']",
    ],
)
def test_non_secret_passes(text: str) -> None:
    assert list(gg.scan_line("x", text)) == []


def test_allow_pragma_skips_secrets_only() -> None:
    text = "BINANCE_API_KEY=" + "x" * 20 + f" {SYLLABLE}  # " + gg.ALLOW_SECRET
    assert kinds(gg.scan_line("x", text)) == ["Hangul at column 38"]


@pytest.mark.parametrize(
    ("path", "blocked"),
    [(".env", True), ("config/.env.local", True), (".env.example", False), ("env.py", False)],
)
def test_blocked_paths(path: str, blocked: bool) -> None:
    assert gg.is_blocked_path(path) is blocked


def test_added_lines_tracks_paths_and_numbers() -> None:
    diff = "\n".join(
        [
            "diff --git a/a.py b/a.py",
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1,0 +2,2 @@",
            "+first",
            "+++ looks like a header",
            "@@ -9 +10 @@",
            "-old",
            "+new",
            "diff --git a/b.txt b/b.txt",
            "--- /dev/null",
            "+++ b/b.txt",
            "@@ -0,0 +1 @@",
            "+only",
            "\\ No newline at end of file",
        ]
    )
    assert list(gg.added_lines(diff)) == [
        ("a.py", 2, "first"),
        ("a.py", 3, "++ looks like a header"),
        ("a.py", 10, "new"),
        ("b.txt", 1, "only"),
    ]


def test_message_skips_comments_and_scissors() -> None:
    message = "\n".join(
        [
            "Add feature",
            f"# {SYLLABLE} comment",
            "# ------------------------ >8 ------------------------",
            f"+{SYLLABLE} diff below scissors",
        ]
    )
    assert list(gg.check_message(message)) == []
    assert [f.where for f in gg.check_message(f"Title\n\nBody {SYLLABLE}")] == ["commit message:3"]


# Integration tests against real temporary repositories.


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Hooks export GIT_DIR and friends; never let tests touch the real repository.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "tester")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tester@example.com")


def run(repo: Path, *args: str, stdin: str | None = None) -> str:
    data = stdin.encode() if stdin is not None else None
    result = subprocess.run(["git", *args], cwd=repo, input=data, capture_output=True, check=True)
    return result.stdout.decode().strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_bytes(content.encode())
    run(repo, "add", name)
    run(repo, "commit", "-q", "-F", "-", stdin=message)
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-q", "-b", "main")
    return path


def push_line(local_sha: str, remote_sha: str = gg.ZERO_SHA) -> str:
    return f"refs/heads/main {local_sha} refs/heads/main {remote_sha}"


def test_pre_commit_blocks_staged_hangul(repo: Path) -> None:
    (repo / "ok.py").write_bytes(b"x = 1\n")
    (repo / "bad.py").write_bytes(f"x = 1\ny = '{SYLLABLE}'\n".encode())
    run(repo, "add", ".")
    assert [f.where for f in gg.check_staged(repo)] == ["staged bad.py:2"]


def test_pre_commit_blocks_hangul_file_name(repo: Path) -> None:
    (repo / f"{SYLLABLE}.txt").write_bytes(b"clean\n")
    run(repo, "add", ".")
    assert kinds(gg.check_staged(repo)) == ["Hangul in file name"]


def test_pre_push_scans_unpushed_commits(repo: Path) -> None:
    first = commit(repo, "a.txt", "clean\n", "Clean commit")
    second = commit(repo, "b.txt", "clean\n", f"Message {SYLLABLE}")
    third = commit(repo, "c.txt", "clean\n", "Clean again")

    new_branch = list(gg.check_push("origin", [push_line(third)], repo))
    assert [f.where for f in new_branch] == [f"commit {second[:10]} message:1"]

    already_pushed = list(gg.check_push("origin", [push_line(third, second)], repo))
    assert already_pushed == []

    assert list(gg.check_push("origin", [push_line(first)], repo)) == []


def test_pre_push_scans_file_content(repo: Path) -> None:
    sha = commit(repo, "a.txt", f"line\n{SYLLABLE}\n", "Add file")
    assert [f.where for f in gg.check_push("origin", [push_line(sha)], repo)] == [
        f"commit {sha[:10]} a.txt:2"
    ]


def test_pre_push_ignores_branch_deletion(repo: Path) -> None:
    commit(repo, "a.txt", f"{SYLLABLE}\n", "Add file")
    line = f"(delete) {gg.ZERO_SHA} refs/heads/old {'1' * 40}"
    assert list(gg.check_push("origin", [line], repo)) == []


def test_pre_push_scans_annotated_tag(repo: Path) -> None:
    commit(repo, "a.txt", "clean\n", "Clean commit")
    run(repo, "tag", "-a", "v1", "-F", "-", stdin=f"Release {SYLLABLE}")
    tag_sha = run(repo, "rev-parse", "v1")
    line = f"refs/tags/v1 {tag_sha} refs/tags/v1 {gg.ZERO_SHA}"
    assert kinds(gg.check_push("origin", [line], repo)) == ["Hangul at column 9"]


def test_audit_scans_tree_and_history(repo: Path) -> None:
    commit(repo, "a.txt", f"{SYLLABLE}\n", "Add file")
    sha = commit(repo, "a.txt", "clean\n", "Fix file")
    wheres = [f.where for f in gg.audit(repo)]
    assert wheres == [f"commit {run(repo, 'rev-parse', f'{sha}~1')[:10]} a.txt:1"]
