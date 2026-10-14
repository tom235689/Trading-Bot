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

.PARAMETER DryRun
Only show the tasks and what would be done; changes nothing and asks for no rights.

.PARAMETER StopTimeoutSeconds
How long a running session gets to finish its event and stop.

.PARAMETER Elevated
Internal: set when the script restarted itself with administrator rights.

.PARAMETER Uv
Internal: uv as found before the script elevated itself (an administrator account may not have it).

.EXAMPLE
tbot update -DryRun
tbot update
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#>
[CmdletBinding()]
param(
    [switch] $DryRun,
    [int] $StopTimeoutSeconds = 120,
    [switch] $Elevated,
    [string] $Uv = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo
# Elevated with another administrator's password, this run is that account: git refuses a
# repository another account owns unless it is named safe (as `git -c` would).
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "safe.directory"
$env:GIT_CONFIG_VALUE_0 = $repo -replace '\\', '/'

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

function Get-Incoming {
    # Commits a pull would bring, or $null when that cannot be known (no upstream).
    $ErrorActionPreference = "Continue"  # under Stop, a line on stderr would throw
    $lines = @(git log --oneline "HEAD..@{u}" 2>$null)
    if ($LASTEXITCODE -ne 0) { return $null }
    return ,$lines
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

function Assert-EnvReadable {
    # The restart checks each config with .env; an account that cannot read it (setup lets
    # only the owner) would stop every session and start none again.
    $file = Join-Path $repo ".env"
    if (-not (Test-Path -LiteralPath $file)) { return }
    try {
        [IO.File]::OpenRead($file).Dispose()
    } catch {
        throw ("$([Environment]::UserName) cannot read .env, which only the account that set up " +
            "the bot may read, so its sessions would stop and not start again. Update as that " +
            "account: tbot stop <config> for each session, git pull, uv sync --frozen, then " +
            "tbot autostart <config>")
    }
}

function Format-Busy($processes) {
    "a bot or tool still runs from .venv (process $($processes[0].Id)): stop it first " +
        "(tbot stop, or Ctrl+C in its window), or run this as administrator if a scheduled task runs it"
}

$exitCode = 0
try {
    if (-not $Uv) { $Uv = (Get-Command uv -ErrorAction Stop).Source }
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
        $PSBoundParameters["Uv"] = $Uv
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
        $fetched = $LASTEXITCODE -eq 0
        if ($others) { Write-Output "would refuse: $(Format-Busy $others)" }
        $names = ($running | ForEach-Object { $_.Name }) -join ', '
        Write-Output "would stop: $(if ($names) { $names } else { 'nothing (no task runs)' })"
        $incoming = Get-Incoming
        if ($null -eq $incoming) {
            Write-Output "cannot tell what would be pulled: this branch has no upstream to pull from"
        } elseif (-not $fetched) {
            Write-Output "cannot tell what would be pulled: git fetch failed (see above)"
        } elseif ($incoming) {
            Write-Output "would pull:"
            $incoming | ForEach-Object { Write-Output "  $_" }
        } else {
            Write-Output "nothing to pull: already at $(Get-Version)"
        }
        return
    }
    if ($others) { throw (Format-Busy $others) }  # before any task is stopped for nothing
    Assert-EnvReadable
    $null = git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "git cannot use $repo as $([Environment]::UserName) (see above)" }

    $failed = $false
    $notStarted = @()
    $before = git rev-parse HEAD
    $oldVersion = Get-Version
    try {
        foreach ($task in $running) {
            Write-Output "stopping '$($task.Name)'"
            $code = Invoke-Tbot @("stop", $task.Config, "--timeout", "$StopTimeoutSeconds")
            if ($code -eq 4) {
                throw "cannot ask '$($task.Name)' to stop: its config $($task.Config) does not load (see above)"
            }
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
                if ($LASTEXITCODE -ne 0) {
                    throw ("the update changes the same lines as your local changes; nothing was " +
                        "changed. To take it: note your edits (git diff), undo them with 'git checkout " +
                        "-- <file>', run tbot update, then make them again in the updated file")
                }
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
            # Offline, doctor fails only on what stops a start; its warnings are shown.
            if ((Invoke-Tbot @("doctor", $task.Config, "--offline")) -ne 0) {
                Write-Output ("NOT started '$($task.Name)': it could not start (see the FAIL lines). " +
                    "Fix it, then: tbot autostart $($task.Config)")
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
