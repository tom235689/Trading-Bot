<#
.SYNOPSIS
Updates the bot: stops its scheduled sessions gracefully, pulls, syncs packages, starts them again.

.DESCRIPTION
Tasks that run this repository's scripts\run_bot.ps1 are found by their action; stopping and
starting them needs administrator rights, so with tasks, run from a normal terminal, it asks
for them and goes on in a new window. A running one is asked to stop with
`tbot stop` (it finishes the event in progress). Whatever happens afterwards, every task that was
running is started again, after `tbot doctor --offline` checks its config; the network checks are
left to the bot, which retries them. Nothing is sold or cancelled.
Local changes to tracked files (an edited config, a longer trial log) are stashed for the
pull and put back; if the update changes the same lines, or any step fails, everything stays
at the old version with your changes. Exit code 1 means the update was not applied or a task
was not started again. A bot started by hand in a console must be stopped by hand first.

.EXAMPLE
tbot update -DryRun
tbot update
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#>
[CmdletBinding()]
param(
    [switch] $DryRun,  # only show the tasks and what would be done
    [int] $StopTimeoutSeconds = 120,
    [switch] $Elevated  # set when the script elevated itself
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$runner = (Join-Path $PSScriptRoot "run_bot.ps1").ToLowerInvariant()
$venv = (Join-Path $repo ".venv").ToLowerInvariant()
$StashName = "tbot update"

function Invoke-Checked([string] $exe, [string[]] $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $exe $($arguments -join ' ')" }
}

function Invoke-Tbot([string[]] $arguments) {
    & $uv run --quiet --frozen python -m tbot @arguments | Write-Host
    return $LASTEXITCODE
}

function Get-Version {
    $ErrorActionPreference = "Continue"  # under Stop, a line on stderr would throw
    $text = & $uv run --quiet --frozen python -m tbot --version 2>$null
    if ($LASTEXITCODE -eq 0) { return [string] $text } else { return "an unknown version" }
}

function Get-VenvProcesses {
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.ToLowerInvariant().StartsWith($venv)
    })
}

function Get-OtherProcesses([string[]] $configs) {
    # .venv processes that no running task's bot accounts for: a bot started by hand,
    # `tbot log -f`. They would make the update fail after the tasks were stopped.
    @(Get-VenvProcesses | Where-Object {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue
        $line = if ($process) { [string] $process.CommandLine } else { "" }
        -not ($configs | Where-Object { $_ -and $line.ToLowerInvariant().Contains($_.ToLowerInvariant()) })
    })
}

function Format-Busy($processes) {
    "a bot or tool still runs from .venv (process $($processes[0].Id)): stop it first " +
        "(tbot stop, or Ctrl+C in its window), or run this as administrator if a scheduled task runs it"
}

$exitCode = 0
try {
    $uv = (Get-Command uv -ErrorAction Stop).Source
    $tasks = @(Get-ScheduledTask | ForEach-Object {
        $action = $_.Actions | Where-Object {
            $_.Arguments -and $_.Arguments.ToLowerInvariant().Contains($runner)
        } | Select-Object -First 1
        if ($action) {
            $config = if ($action.Arguments -match '-Config "([^"]+)"') { $Matches[1] } else { "" }
            [pscustomobject] @{ Name = $_.TaskName; State = [string] $_.State; Config = $config }
        }
    })
    if ($tasks -and -not $DryRun -and -not $Elevated -and -not (Test-Admin)) {
        exit (Invoke-Elevated $PSCommandPath $PSBoundParameters)
    }
    foreach ($task in $tasks) { Write-Output "task '$($task.Name)': $($task.State), $($task.Config)" }
    if (-not $tasks) { Write-Output "no scheduled tbot task for $repo" }
    $running = @($tasks | Where-Object { $_.State -eq "Running" })

    if (git stash list | Select-String -SimpleMatch $StashName -Quiet) {
        throw "an earlier update left your local changes in 'git stash list' ($StashName). " +
            "Put them back with 'git stash pop' (or drop them), then run this again."
    }
    $status = @(git status --porcelain --untracked-files=no)
    if ($status) { Write-Output "local changes, kept across the update:`n$($status -join "`n")" }
    $others = Get-OtherProcesses @($running | ForEach-Object { $_.Config })
    if ($DryRun) {
        git fetch --quiet
        if ($others) { Write-Output "would refuse: $(Format-Busy $others)" }
        Write-Output "would stop: $(($running | ForEach-Object { $_.Name }) -join ', ')"
        Write-Output "would pull:"
        git log --oneline "HEAD..@{u}"
        return
    }
    if ($others) { throw (Format-Busy $others) }  # before any task is stopped for nothing

    $failed = $false
    $notStarted = @()
    $before = git rev-parse HEAD
    $oldVersion = Get-Version
    try {
        foreach ($task in $running) {
            Write-Output "stopping '$($task.Name)'"
            $code = Invoke-Tbot @("stop", $task.Config, "--timeout", "$StopTimeoutSeconds")
            if ($code -ne 0) {
                $null = Invoke-Tbot @("stop", $task.Config, "--cancel")  # it keeps running
                throw "'$($task.Name)' did not stop in time"
            }
            # The bot is gone; a supervisor still running is waiting to restart it: end it.
            $deadline = (Get-Date).AddSeconds(30)
            while ((Get-ScheduledTask -TaskName $task.Name).State -eq "Running") {
                if ((Get-Date) -gt $deadline) {
                    Stop-ScheduledTask -TaskName $task.Name
                    $deadline = (Get-Date).AddSeconds(30)
                }
                Start-Sleep -Seconds 2
            }
        }
        $busy = Get-VenvProcesses
        if ($busy) { throw (Format-Busy $busy) }

        $stashed = $false
        if ($status) {  # set aside for the pull, put back last
            Invoke-Checked "git" @("stash", "push", "--quiet", "-m", $StashName)
            $stashed = $true
        }
        try {
            Invoke-Checked "git" @("pull", "--ff-only")
            Invoke-Checked $uv @("sync", "--frozen")
            if ($stashed) {
                & git stash pop --quiet
                if ($LASTEXITCODE -ne 0) { throw "the update changes the same lines as your local changes" }
                $stashed = $false
            }
        } catch {
            $failed = $true
            Write-Output "update NOT applied: $_"
            if ((git rev-parse HEAD) -ne $before) {
                Write-Output "going back to $before"
                & git reset --hard --quiet $before  # local changes are still in the stash
            }
            if ($stashed) {
                & git stash pop --quiet  # applies cleanly on the old version
                if ($LASTEXITCODE -ne 0) { Write-Output "your local changes are in 'git stash list'" }
            }
            & $uv sync --frozen
        }
    } catch {
        $failed = $true
        Write-Output "update NOT applied: $_"
    } finally {
        $after = git rev-parse HEAD
        if ($before -ne $after) { git log --oneline "$before..$after" }
        foreach ($task in $running) {
            if ((Get-ScheduledTask -TaskName $task.Name).State -eq "Running") { continue }
            $null = Invoke-Tbot @("stop", $task.Config, "--cancel")  # or the new start stops at once
            if ((Invoke-Tbot @("doctor", $task.Config, "--offline")) -ne 0) {
                Write-Output "NOT started '$($task.Name)': doctor reports a problem. Fix it, then: Start-ScheduledTask -TaskName '$($task.Name)'"
                $notStarted += $task.Name
                continue
            }
            Start-ScheduledTask -TaskName $task.Name
            Write-Output "started '$($task.Name)'"
        }
    }
    if ($failed -or $notStarted) { exit 1 }
    if ($before -eq $after) {
        Write-Output "already up to date ($oldVersion)"
    } else {
        Write-Output "updated: $oldVersion -> $(Get-Version); what changed: CHANGELOG.md"
    }
} catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    Wait-Close -Elevated:$Elevated
}
exit $exitCode
