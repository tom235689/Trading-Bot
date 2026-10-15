"""The Windows scripts, run by Windows PowerShell 5.1 itself; nothing is registered,
elevated, or stopped: the scheduled-task and elevation commands are stubbed. Most run in a
folder whose name has [ and ], which Windows PowerShell 5.1 reads as a wildcard."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.ps1"))
BRACKETS = "Trading-Bot [x]"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell scripts")


def clean_env(**extra: str) -> dict[str, str]:
    """This environment without the variables git hooks export, which point git at the
    real repository."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**env, **extra}


def powershell(
    *args: str, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def cmd_file(path: Path, text: str) -> Path:
    path.write_bytes(text.replace("\n", "\r\n").encode("ascii"))  # cmd.exe wants CRLF
    return path


def copy_scripts(repo: Path, names: tuple[str, ...], stubs: str = "") -> None:
    (repo / "scripts").mkdir(parents=True)
    for name in names:
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    with (repo / "scripts" / "common.ps1").open("a", encoding="ascii") as handle:
        handle.write(stubs)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_script_parses(script: Path) -> None:
    check = (
        "$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errors); $errors.Count"
    )
    assert powershell("-Command", check).stdout.strip() == "0"


def test_arguments_reach_the_elevated_window_unchanged(tmp_path: Path) -> None:
    here = tmp_path / BRACKETS  # Start-Process failed in such a folder
    here.mkdir()
    child = here / "child.ps1"
    child.write_text(
        "param([Parameter(Position = 0)] [string] $Config, [switch] $SkipDoctor,"  # as install_task
        " [switch] $Busy, [string] $Uv)\n"
        "@{ Config = $Config; SkipDoctor = [bool] $SkipDoctor; Busy = [bool] $Busy;"
        " Uv = $Uv } | ConvertTo-Json -Compress\n",
        encoding="ascii",
    )
    parent = (
        f". '{ROOT / 'scripts' / 'common.ps1'}'\n"
        f"Set-Location -LiteralPath '{here}'  # as update.ps1 and remove_task.ps1 do\n"
        "$bound = [ordered] @{ Config = 'my \"odd\" folder\\'; Uv = 'C:\\Program Files\\uv.exe' }\n"
        "$bound['SkipDoctor'] = [switch] $true  # set by the script, as install_task does\n"
        "$bound['Busy'] = $true  # a plain bool, too\n"
        f"exit (Invoke-Elevated '{child}' $bound -Here)\n"
    )
    script = here / "parent.ps1"
    script.write_text(parent, encoding="ascii")
    result = powershell("-File", str(script), cwd=here)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Config": 'my "odd" folder\\',
        "SkipDoctor": True,
        "Busy": True,
        "Uv": "C:\\Program Files\\uv.exe",
    }, result.stdout + result.stderr


def test_a_config_name_or_a_typo(tmp_path: Path) -> None:
    common = ROOT / "scripts" / "common.ps1"
    found = powershell("-Command", f". '{common}'; Resolve-Config '{ROOT}' 'paper'")
    assert Path(found.stdout.strip()) == ROOT / "config" / "paper.yaml"
    typo = powershell("-Command", f". '{common}'; Resolve-Config '{ROOT}' 'papr'")
    assert "no config 'papr' (names in config\\:" in typo.stderr + typo.stdout


def test_dry_runs_change_nothing() -> None:
    install = powershell(
        "-File", str(ROOT / "scripts" / "install_task.ps1"), "paper", "-DryRun", "-SkipDoctor"
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert "task 'tbot paper' as " in install.stdout
    remove = powershell("-File", str(ROOT / "scripts" / "remove_task.ps1"), "paper", "-DryRun")
    assert remove.returncode == 0, remove.stdout + remove.stderr


STUBS = r"""
# Test stubs: nothing is registered, started, or elevated.
Import-Module ScheduledTasks  # its functions would replace the stubs when it loads
function Test-Admin { return $false }
function Wait-Close([switch] $Elevated) { }
function Invoke-Elevated([string] $script, $bound, [switch] $Here) {
    $arguments = Format-Arguments $bound
    $line = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" $arguments -Elevated"
    Write-Host "CHILD $line"  # Write-Output would become part of the exit code
    $process = Start-Process powershell.exe -ArgumentList $line -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}
function Register-ScheduledTask { param($TaskName, $InputObject, [switch] $Force)
    Write-Host "REGISTERED $TaskName as $($InputObject.Principal.UserId)" }
function Start-ScheduledTask { param($TaskName) Write-Host "STARTED $TaskName" }
function Get-ScheduledTask { param($TaskName, $ErrorAction)
    if ($script:asked) { [pscustomobject] @{ State = "Running"; Actions = @() } }
    else { $script:asked = $true; $null } }
function Start-Sleep { param($Seconds) }
"""


def task_folder(folder: Path, stubs: str) -> tuple[Path, Path]:
    """A repository with the task scripts, stubbed, and a uv whose doctor passes."""
    copy_scripts(folder, ("install_task.ps1", "common.ps1", "run_bot.ps1"), stubs)
    (folder / "config").mkdir()
    shutil.copy(ROOT / "config" / "paper.yaml", folder / "config" / "paper.yaml")
    uv = cmd_file(folder / "uv.cmd", "@echo OK   strategies: donchian_trend 4h\n@exit /b 0\n")
    return folder, uv


def test_autostart_hands_its_checks_to_the_elevated_window(tmp_path: Path) -> None:
    # 0.6.0 handed over `-SkipDoctor "True"`, which the elevated window could not parse.
    repo, uv = task_folder(tmp_path, STUBS)
    (repo / "logs").mkdir()  # a failed start of an earlier run is no failure of this one
    (repo / "logs" / "paper.supervisor.log").write_text(
        "2026-10-01T00:00:00Z tbot did not start; starting again in 60 s. could not start uv\n"
    )
    script = repo / "scripts" / "install_task.ps1"
    result = powershell("-File", str(script), "paper", "-Uv", str(uv))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    child = next(line for line in out.splitlines() if line.startswith("CHILD "))
    assert " -SkipDoctor " in child
    assert "True" not in child
    assert "REGISTERED tbot paper as " in out
    assert "registered and started 'tbot paper'" in out


TASK_STUBS = r"""
# Test stubs: a task 'tbot paper' of this folder that runs config\paper.yaml.
Import-Module ScheduledTasks
function Wait-Close([switch] $Elevated) { }
function Get-ScheduledTask { param($TaskName, $ErrorAction)
    $root = Split-Path -Parent $PSScriptRoot
    $runner = Join-Path $PSScriptRoot "run_bot.ps1"
    $config = Join-Path $root "config\paper.yaml"
    $action = [pscustomobject] @{ Arguments = "-NoProfile -File `"$runner`" -Config `"$config`"" }
    $state = if ($script:started) { "Running" } else { "Ready" }
    [pscustomobject] @{ TaskName = $TaskName; State = $state; Actions = @($action) }
}
function Register-ScheduledTask { param($TaskName, $InputObject, [switch] $Force)
    Write-Host "REGISTERED $TaskName" }
function Start-ScheduledTask { param($TaskName)  # runs, but the supervisor cannot start uv
    $script:started = $true
    $journal = Join-Path (Split-Path -Parent $PSScriptRoot) "logs\paper.supervisor.log"
    Add-Content -LiteralPath $journal -Value ("2026-10-15T00:00:10Z tbot did not start; " +
        "starting again in 60 s. could not start uv (no such program)") }
function Start-Sleep { param($Seconds) }
"""


def test_autostart_keeps_the_task_of_another_config_and_names_a_failed_start(
    tmp_path: Path,
) -> None:
    repo, uv = task_folder(tmp_path / BRACKETS, TASK_STUBS)
    script = str(repo / "scripts" / "install_task.ps1")
    mine = tmp_path / "mine" / "paper.yaml"  # the same file name, in another folder
    mine.parent.mkdir()
    shutil.copy(ROOT / "config" / "paper.yaml", mine)
    other = powershell("-File", script, str(mine), "-Uv", str(uv), "-Elevated", cwd=repo)
    assert other.returncode == 1, other.stdout + other.stderr
    assert f"runs {repo / 'config' / 'paper.yaml'}, not {mine}: pass -Name" in other.stdout
    assert "REGISTERED" not in other.stdout

    (repo / "logs").mkdir()
    (repo / "logs" / "paper.supervisor.log").write_text("2026-10-15T00:00:00Z starting: uv\n")
    same = powershell("-File", script, "paper", "-Uv", str(uv), "-Elevated", cwd=repo)
    assert same.returncode == 1, same.stdout + same.stderr
    assert "REGISTERED tbot paper" in same.stdout
    assert (
        "registered 'tbot paper', but the bot did not start: 2026-10-15T00:00:10Z tbot did "
        "not start; starting again in 60 s. could not start uv (no such program)"
    ) in same.stdout


UV_CODES = r"""@echo off
rem uv for run_bot.ps1: each start exits with the next code of codes.txt.
setlocal enabledelayedexpansion
set /p CODES=<"%~dp0codes.txt"
for /f "tokens=1* delims=," %%a in ("!CODES!") do (
  set FIRST=%%a
  set REST=%%b
)
>"%~dp0codes.txt" echo !REST!
echo uv-stub exiting with !FIRST! 1>&2
exit /b !FIRST!
"""


def run_bot(repo: Path, uv: Path) -> subprocess.CompletedProcess[str]:
    if not (repo / "scripts").exists():
        copy_scripts(repo, ("run_bot.ps1", "common.ps1"))
        (repo / "config").mkdir()
        (repo / "config" / "paper.yaml").write_text("initial_cash: 1\n")
    return powershell(
        "-File",
        str(repo / "scripts" / "run_bot.ps1"),
        "-Config",
        "config\\paper.yaml",  # as typed by hand, relative to the repository
        "-Uv",
        str(uv),
        "-FirstDelaySeconds",
        "0",
        "-MaxDelaySeconds",
        "0",
        "-MaxRuns",
        "6",
        cwd=repo,
    )


def journal(repo: Path) -> str:
    return (repo / "logs" / "paper.supervisor.log").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("last", [3, 4])
def test_the_supervisor_starts_a_crashed_bot_again_until_3_or_4(tmp_path: Path, last: int) -> None:
    repo = tmp_path / (BRACKETS + " caf" + chr(0xE9))
    repo.mkdir()
    uv = cmd_file(repo / "uv.cmd", UV_CODES)
    (repo / "codes.txt").write_text(f"1,-1,{last}\n")
    result = run_bot(repo, uv)
    assert result.returncode == last, result.stdout + result.stderr
    text = journal(repo)
    assert text.count(" starting: ") == 3
    assert "tbot exited with code 1; starting again in 0 s. uv-stub exiting with 1" in text
    assert "tbot exited with code -1; starting again in 0 s. uv-stub exiting with -1" in text
    assert f"tbot exited with code {last}; not restarting. uv-stub exiting with {last}" in text
    logs = repo / "logs"  # the last run's stderr, and the run's before
    assert (logs / "paper.console.txt").read_text().strip() == f"uv-stub exiting with {last}"
    assert (logs / "paper.console.txt.1").read_text().strip() == "uv-stub exiting with -1"


def test_the_supervisor_ends_after_three_starts_that_failed(tmp_path: Path) -> None:
    repo = tmp_path / BRACKETS
    repo.mkdir()
    uv = repo / "uv.exe"
    uv.write_bytes(b"no program")
    result = run_bot(repo, uv)
    assert result.returncode == 4, result.stdout + result.stderr
    text = journal(repo)
    assert text.count("tbot did not start; starting again in 0 s. could not start uv (") == 2
    assert "tbot did not start 3 times in a row; not retrying. could not start uv (" in text
    assert "then run tbot autostart paper again" in text


def test_setup_works_in_a_folder_with_brackets(tmp_path: Path) -> None:
    repo = tmp_path / BRACKETS
    copy_scripts(repo, ("setup.ps1", "common.ps1"))
    (repo / ".env.example").write_text("TBOT_X=1\n")
    python = tmp_path / "python" / "python.exe"  # never run: the stub uv builds nothing
    python.parent.mkdir()
    python.write_bytes(b"")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text(f"home = {python.parent}\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    cmd_file(tools / "uv.cmd", "@echo uv-stub %*\n@exit /b 0\n")
    env = clean_env(PATH=f"{tools};{os.environ['PATH']}")
    git(repo, env, "init", "-q")
    setup = (str(repo / "scripts" / "setup.ps1"), "-Python", str(python), "-SkipDownload")
    first = powershell("-File", *setup, "-NoPath", cwd=repo, env=env)  # PATH stays as it is
    out = first.stdout + first.stderr
    assert first.returncode == 0, out
    assert f".venv already uses {python}" in out
    assert "hooks from .githooks" in out
    assert git(repo, env, "config", "core.hooksPath") == ".githooks"
    assert "created .env from .env.example" in out
    (repo / ".env").write_text("TBOT_X=my key\n")
    again = powershell("-File", *setup, "-NoPath", cwd=repo, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert ".env exists: left as it is" in again.stdout
    assert (repo / ".env").read_text() == "TBOT_X=my key\n"


def test_setup_says_when_the_processor_is_too_old_for_polars(tmp_path: Path) -> None:
    uv = cmd_file(
        tmp_path / "uv.cmd",
        "@echo off\n"
        "echo Traceback (most recent call last): 1>&2\n"
        "echo RuntimeWarning: Missing required CPU features. 1>&2\n"
        "echo. 1>&2\n"
        "echo The following required CPU features were not detected: 1>&2\n"
        "echo     avx2, fma, bmi1 1>&2\n"
        "exit /b 1\n",
    )
    common = ROOT / "scripts" / "common.ps1"
    result = powershell("-Command", f". '{common}'; Test-Packages '{uv}'")
    assert result.stdout.strip().startswith(
        "this PC's processor lacks features (avx2, fma, bmi1) that polars, the data library, needs"
    ), result.stdout + result.stderr


def test_the_import_check_makes_polars_processor_warning_an_error() -> None:
    # polars warns, and goes on to crash: the option in common.ps1 must still stop it.
    option = re.search(r'-W "([^"]+)"', (ROOT / "scripts" / "common.ps1").read_text())
    assert option is not None
    probe = (
        "import polars._cpu_check as cpu; cpu._read_cpu_flags = lambda: {'avx2': False}; "
        "cpu.check_cpu_flags('+avx2')"
    )
    result = subprocess.run(
        [sys.executable, "-W", option[1], "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "RuntimeWarning: Missing required CPU features" in result.stderr


UPDATE_STUBS = r"""
# Test stubs: a running task 'tbot paper' for config\paper.yaml; nothing is elevated.
Import-Module ScheduledTasks
function Test-Admin { return $true }
function Wait-Close([switch] $Elevated) { }
$box = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # uv.cmd and its marks
function Get-ScheduledTask { param($TaskName, $ErrorAction)
    $runner = Join-Path $PSScriptRoot "run_bot.ps1"
    $config = Join-Path (Split-Path -Parent $PSScriptRoot) "config\paper.yaml"
    $action = [pscustomobject] @{ Arguments = "-NoProfile -File `"$runner`" -Config `"$config`"" }
    $state = if (Test-Path -LiteralPath (Join-Path $box "stopped")) { "Ready" } else { "Running" }
    [pscustomobject] @{ TaskName = "tbot paper"; State = $state; Actions = @($action) }
}
function Start-ScheduledTask { param($TaskName)
    Remove-Item -LiteralPath (Join-Path $box "stopped") -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath (Join-Path $box "crashes")) {  # the bot ends at once
        $logs = Join-Path (Split-Path -Parent $PSScriptRoot) "logs"
        [IO.Directory]::CreateDirectory($logs) | Out-Null
        Add-Content -LiteralPath (Join-Path $logs "paper.supervisor.log") -Value (
            "2026-10-15T00:00:10Z tbot exited with code 1; starting again in 60 s. no network")
    }
    Write-Host "STARTED $TaskName" }
function Stop-ScheduledTask { param($TaskName) Write-Host "STOPPED $TaskName" }
function Start-Sleep { param($Seconds) }
function Get-PublishedTags([string] $url) {  # published.txt stands for GitHub's answer
    $file = Join-Path $box "published.txt"
    if (Test-Path -LiteralPath $file) { return ,@(Get-Content -LiteralPath $file) }
    return $null
}
"""

UV_UPDATE = r"""@echo off
rem uv for update.ps1, run in the repository; doctor fails while doctor_fails is there.
>>"%~dp0uv.log" echo %*
if "%1"=="sync" exit /b 0
echo %* | findstr /c:" --version" >nul && (echo tbot stub& exit /b 0)
echo %* | findstr /c:" --cancel" >nul && exit /b 0
echo %* | findstr /c:" stop " >nul && (type nul > "%~dp0stopped" & exit /b 0)
echo %* | findstr /c:" doctor " >nul || exit /b 0
if exist doctor_fails (echo FAIL config: refused by this version & exit /b 1)
echo OK   config: fine
exit /b 0
"""

LOCAL = b"initial_cash: 500\nfee: 1\n"  # the owner's own edit of a tracked config


class Sandbox:
    """A remote with release v1.0.0, a developer's clone, and the owner's clone, all in a
    folder whose name has brackets; uv and the scheduled-task commands are stubbed."""

    def __init__(self, tmp_path: Path) -> None:
        self.box = tmp_path / BRACKETS
        self.box.mkdir()
        config = tmp_path / "gitconfig"
        config.write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n"
        )
        self.env = clean_env(GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1")
        git(self.box, self.env, "init", "-q", "--bare", "remote.git")
        git(self.box, self.env, "clone", "-q", "remote.git", "dev")
        self.dev = self.box / "dev"
        copy_scripts(self.dev, ("update.ps1", "common.ps1", "run_bot.ps1"), UPDATE_STUBS)
        (self.dev / "config").mkdir()
        (self.dev / "config" / "paper.yaml").write_bytes(b"initial_cash: 10000\nfee: 1\n")
        self.release("v1.0.0", "README.md", b"v1\n")
        git(self.box, self.env, "clone", "-q", "remote.git", "owner")
        self.owner = self.box / "owner"
        self.uv = cmd_file(self.box / "uv.cmd", UV_UPDATE)

    def git(self, *args: str) -> str:
        return git(self.owner, self.env, *args)

    def release(self, tag: str, path: str, content: bytes) -> None:
        (self.dev / path).write_bytes(content)
        git(self.dev, self.env, "add", "-A")
        git(self.dev, self.env, "commit", "-q", "-m", tag)
        git(self.dev, self.env, "tag", "-a", tag, "-m", tag)
        git(self.dev, self.env, "push", "-q", "origin", "main", tag)

    def update(self) -> subprocess.CompletedProcess[str]:
        script = str(self.owner / "scripts" / "update.ps1")
        return powershell("-File", script, "-Uv", str(self.uv), cwd=self.owner, env=self.env)

    def uv_calls(self) -> str:
        log = self.box / "uv.log"
        return log.read_text(errors="replace") if log.exists() else ""


def test_update_takes_the_newest_published_release_and_keeps_local_edits(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    sandbox.release("v1.1.0", "README.md", b"v1.1\n")
    sandbox.release("v1.2.0", "README.md", b"v1.2\n")  # its checks failed: a tag, no release
    (sandbox.box / "published.txt").write_text("v1.1.0\nv1.0.0\n")
    (sandbox.owner / "config" / "paper.yaml").write_bytes(LOCAL)
    result = sandbox.update()
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert sandbox.git("describe", "--tags") == "v1.1.0"
    assert sandbox.git("symbolic-ref", "--short", "HEAD") == "main"  # still on its branch
    assert (sandbox.owner / "config" / "paper.yaml").read_bytes() == LOCAL
    assert sandbox.git("stash", "list") == ""
    assert "stopping 'tbot paper'" in out
    assert "started 'tbot paper'" in out
    assert "updated to v1.1.0: tbot stub -> tbot stub" in out
    assert "error:Missing required CPU features" in sandbox.uv_calls()  # the import check


def test_update_refuses_before_stopping_anything_when_it_cannot_apply(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    sandbox.release("v1.1.0", "config/mine.yaml", b"shipped: 1\n")
    (sandbox.owner / "README.md").write_bytes(b"my notes\n")
    sandbox.git("commit", "-q", "-am", "my own commit")
    diverged = sandbox.update()
    assert diverged.returncode == 1, diverged.stdout + diverged.stderr
    assert "taking the newest tag, v1.1.0" in diverged.stdout  # GitHub could not be asked
    assert (
        "nothing was stopped or changed: this clone has commits that v1.1.0 does not have"
    ) in diverged.stdout

    sandbox.git("reset", "-q", "--hard", "v1.0.0")
    (sandbox.owner / "config" / "mine.yaml").write_bytes(b"initial_cash: 1\n")
    in_the_way = sandbox.update()
    assert in_the_way.returncode == 1, in_the_way.stdout + in_the_way.stderr
    assert (
        "v1.1.0 adds files that are here already but not tracked by git: config/mine.yaml"
    ) in in_the_way.stdout
    assert " stop " not in sandbox.uv_calls()
    assert not (sandbox.box / "stopped").exists()
    assert (sandbox.owner / "config" / "mine.yaml").read_bytes() == b"initial_cash: 1\n"


def test_update_goes_back_when_doctor_fails_on_the_new_version(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    sandbox.release("v1.1.0", "doctor_fails", b"")
    (sandbox.owner / "config" / "paper.yaml").write_bytes(LOCAL)
    before = sandbox.git("rev-parse", "HEAD")
    result = sandbox.update()
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "update NOT applied: doctor --offline passed on" in out
    assert "FAIL config: refused by this version" in out
    assert "going back to the old version, tbot stub" in out
    assert "started 'tbot paper'" in out  # on the old version
    assert sandbox.git("rev-parse", "HEAD") == before
    assert not (sandbox.owner / "doctor_fails").exists()
    assert (sandbox.owner / "config" / "paper.yaml").read_bytes() == LOCAL
    assert sandbox.git("stash", "list") == ""
    assert sandbox.uv_calls().count("sync --frozen") == 2


def test_update_says_when_a_session_does_not_run_after_it(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    sandbox.release("v1.1.0", "README.md", b"v1.1\n")
    (sandbox.box / "crashes").write_text("")
    result = sandbox.update()
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert sandbox.git("describe", "--tags") == "v1.1.0"  # the update itself is kept
    assert (
        "'tbot paper' was started again, but the bot did not start: 2026-10-15T00:00:10Z "
        "tbot exited with code 1; starting again in 60 s. no network"
    ) in out
