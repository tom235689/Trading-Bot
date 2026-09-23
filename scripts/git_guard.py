"""Keep Hangul and secrets out of git history.

Usage:
    git_guard.py pre-commit             scan staged changes and identity
    git_guard.py commit-msg FILE        scan a commit message
    git_guard.py pre-push REMOTE [URL]  scan outgoing refs (read from stdin)
    git_guard.py audit                  scan tracked files and all history
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ZERO_SHA = "0" * 40
ALLOW_SECRET = "guard: allow-secret"
REPLACEMENT_CHAR = chr(0xFFFD)
DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "-U0", "--diff-filter=ACMRT")

# Built from code points so this file stays ASCII.
HANGUL_RANGES = (
    (0x1100, 0x11FF),  # jamo
    (0x3130, 0x318F),  # compatibility jamo
    (0x3200, 0x321E),  # parenthesized
    (0x3260, 0x327E),  # circled
    (0xA960, 0xA97F),  # jamo extended-a
    (0xAC00, 0xD7FF),  # syllables, jamo extended-b
    (0xFFA0, 0xFFDC),  # halfwidth
)
HANGUL = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in HANGUL_RANGES) + "]")

SECRETS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "telegram bot token": re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    "api key": re.compile(
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)"
        r"[A-Za-z0-9]{64}(?![A-Za-z0-9])"
    ),
    "credential assignment": re.compile(
        r"(?i)(?:api_?key|api_?secret|secret_?key|access_?token|bot_?token|password)"
        r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{16,}"
    ),
}

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SCISSORS = re.compile(r"^# -+ >8 -+$")


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str


def scan_line(where: str, text: str) -> Iterator[Finding]:
    if (match := HANGUL.search(text)) is not None:
        yield Finding(where, f"Hangul at column {match.start() + 1}")
    if REPLACEMENT_CHAR in text:
        yield Finding(where, "invalid UTF-8")
    if ALLOW_SECRET not in text:
        for name, pattern in SECRETS.items():
            if pattern.search(text):
                yield Finding(where, f"possible secret ({name})")


def scan_text(where: str, text: str) -> Iterator[Finding]:
    for number, line in enumerate(text.split("\n"), start=1):
        yield from scan_line(f"{where}:{number}", line.rstrip("\r"))


def is_blocked_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def scan_paths(paths: Iterable[str], prefix: str = "") -> Iterator[Finding]:
    for path in paths:
        if HANGUL.search(path):
            yield Finding(f"{prefix}{path}", "Hangul in file name")
        if is_blocked_path(path):
            yield Finding(f"{prefix}{path}", "secret file must not be committed")


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """Yield (path, line number, text) for each added line of a unified diff."""
    path = ""
    number = 0
    in_hunk = False
    for raw in diff.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("diff --git "):
            in_hunk = False
        elif not in_hunk and line.startswith("+++ "):
            path = line[4:].strip('"').removeprefix("b/")
        elif match := HUNK.match(line):
            in_hunk = True
            number = int(match.group(1))
        elif in_hunk and line.startswith("+"):
            yield path, number, line[1:]
            number += 1
        elif in_hunk and line.startswith(" "):
            number += 1


def scan_diff(diff: str, prefix: str = "") -> Iterator[Finding]:
    for path, number, text in added_lines(diff):
        yield from scan_line(f"{prefix}{path}:{number}", text)


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True, check=True
    )
    return result.stdout.decode("utf-8", errors="replace")


def git_ok(*args: str, cwd: Path | None = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def split_z(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


def check_staged(cwd: Path | None = None) -> Iterator[Finding]:
    names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", cwd=cwd)
    yield from scan_paths(split_z(names), "staged ")
    yield from scan_diff(git("diff", "--cached", *DIFF_OPTS, cwd=cwd), "staged ")
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        yield from scan_line(var, git("var", var, cwd=cwd))


def check_message(text: str) -> Iterator[Finding]:
    """Scan a commit message, skipping comments and anything below the scissors line."""
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if SCISSORS.match(line):
            break
        if not line.startswith("#"):
            yield from scan_line(f"commit message:{number}", line)


def check_commit(sha: str, cwd: Path | None = None) -> Iterator[Finding]:
    label = f"commit {sha[:10]}"
    identity = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>", sha, cwd=cwd)
    yield from scan_text(f"{label} identity", identity)
    yield from scan_text(f"{label} message", git("show", "-s", "--format=%B", sha, cwd=cwd))
    show = ("show", "--format=", "--diff-merges=first-parent")
    names = git(*show, "--name-only", "-z", "--diff-filter=ACMRT", sha, cwd=cwd)
    yield from scan_paths(split_z(names), f"{label} ")
    yield from scan_diff(git(*show, *DIFF_OPTS, sha, cwd=cwd), f"{label} ")


def check_tag(sha: str, cwd: Path | None = None) -> Iterator[Finding]:
    if git("cat-file", "-t", sha, cwd=cwd).strip() == "tag":
        yield from scan_text(f"tag {sha[:10]}", git("cat-file", "-p", sha, cwd=cwd))


def check_push(remote: str, lines: Iterable[str], cwd: Path | None = None) -> Iterator[Finding]:
    """Scan everything a push would send. Lines follow the pre-push stdin format."""
    seen: set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue
        yield from scan_line(f"ref {local_ref}", local_ref)
        yield from scan_line(f"ref {remote_ref}", remote_ref)
        yield from check_tag(local_sha, cwd)
        exclude = [f"--remotes={remote}"]
        if remote_sha != ZERO_SHA and git_ok("cat-file", "-e", remote_sha, cwd=cwd):
            exclude.append(remote_sha)
        for sha in git("rev-list", local_sha, "--not", *exclude, cwd=cwd).split():
            if sha not in seen:
                seen.add(sha)
                yield from check_commit(sha, cwd)


def audit(cwd: Path | None = None) -> Iterator[Finding]:
    """Scan tracked files, ref names, tags, and every commit in history."""
    root = Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
    paths = split_z(git("ls-files", "-z", cwd=cwd))
    yield from scan_paths(paths)
    for path in paths:
        file = root / path
        if not file.is_file():
            continue
        data = file.read_bytes()
        if b"\0" not in data:
            yield from scan_text(path, data.decode("utf-8", errors="replace"))
    for ref in git("for-each-ref", "--format=%(refname) %(objectname)", cwd=cwd).splitlines():
        name, sha = ref.rsplit(" ", 1)
        yield from scan_line(f"ref {name}", name)
        yield from check_tag(sha, cwd)
    for sha in git("rev-list", "--all", cwd=cwd).split():
        yield from check_commit(sha, cwd)


def report(findings: Iterable[Finding]) -> int:
    unique = list(dict.fromkeys(findings))
    if not unique:
        return 0
    print(f"git-guard: blocked, {len(unique)} issue(s) found:", file=sys.stderr)
    for finding in unique:
        print(f"  {finding.where}: {finding.kind}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(description="Keep Hangul and secrets out of git history.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pre-commit")
    commands.add_parser("commit-msg").add_argument("file", type=Path)
    push = commands.add_parser("pre-push")
    push.add_argument("remote")
    push.add_argument("url", nargs="?")
    commands.add_parser("audit")
    args = parser.parse_args(argv)

    try:
        if args.command == "pre-commit":
            return report(check_staged())
        if args.command == "commit-msg":
            return report(check_message(args.file.read_text(encoding="utf-8", errors="replace")))
        if args.command == "pre-push":
            stdin = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            return report(check_push(args.remote, stdin.splitlines()))
        return report(audit())
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        print(f"git-guard: git command failed: {exc.cmd}\n{stderr}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
