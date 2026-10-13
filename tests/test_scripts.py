"""The Windows scripts, run by Windows PowerShell 5.1 itself; nothing is registered,
elevated, or stopped: the scheduled-task and elevation commands are stubbed."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.ps1"))

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell scripts")


def powershell(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_script_parses(script: Path) -> None:
    check = (
        "$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errors); $errors.Count"
    )
    assert powershell("-Command", check).stdout.strip() == "0"


def test_arguments_reach_the_elevated_window_unchanged(tmp_path: Path) -> None:
    child = tmp_path / "child.ps1"
    child.write_text(
        "param([Parameter(Position = 0)] [string] $Config, [switch] $SkipDoctor,"  # as install_task
        " [switch] $Busy, [string] $Uv)\n"
        "@{ Config = $Config; SkipDoctor = [bool] $SkipDoctor; Busy = [bool] $Busy;"
        " Uv = $Uv } | ConvertTo-Json -Compress\n",
        encoding="ascii",
    )
    parent = (
        f". '{ROOT / 'scripts' / 'common.ps1'}'\n"
        "$bound = [ordered] @{ Config = 'my \"odd\" folder\\'; Uv = 'C:\\Program Files\\uv.exe' }\n"
        "$bound['SkipDoctor'] = [switch] $true  # set by the script, as install_task does\n"
        "$bound['Busy'] = $true  # a plain bool, too\n"
        f"exit (Invoke-Elevated '{child}' $bound -Here)\n"
    )
    script = tmp_path / "parent.ps1"
    script.write_text(parent, encoding="ascii")
    result = powershell("-File", str(script))
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Config": 'my "odd" folder\\',
        "SkipDoctor": True,
        "Busy": True,
        "Uv": "C:\\Program Files\\uv.exe",
    }


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


def test_autostart_hands_its_checks_to_the_elevated_window(tmp_path: Path) -> None:
    # 0.6.0 handed over `-SkipDoctor "True"`, which the elevated window could not parse.
    (tmp_path / "scripts").mkdir()
    for name in ("install_task.ps1", "common.ps1", "run_bot.ps1"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    with (tmp_path / "scripts" / "common.ps1").open("a", encoding="ascii") as handle:
        handle.write(STUBS)
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "paper.yaml", tmp_path / "config" / "paper.yaml")
    uv = tmp_path / "uv.cmd"  # doctor passes; `tbot stop --cancel` does nothing
    uv.write_text("@echo OK   strategies: donchian_trend 4h\r\n@exit /b 0\r\n", encoding="ascii")
    script = tmp_path / "scripts" / "install_task.ps1"
    result = powershell("-File", str(script), "paper", "-Uv", str(uv))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    child = next(line for line in out.splitlines() if line.startswith("CHILD "))
    assert " -SkipDoctor " in child
    assert "True" not in child
    assert "REGISTERED tbot paper as " in out
    assert "registered and started 'tbot paper'" in out
