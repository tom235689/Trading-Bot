<#
.SYNOPSIS
Updates the bot to the newest release: stops its scheduled sessions gracefully, updates, syncs
packages, checks the new version, and starts the sessions again.

.DESCRIPTION
The newest release is the newest vX.Y.Z tag with a published GitHub release (a version whose
checks failed has a tag but no release); when GitHub cannot be asked, the newest vX.Y.Z tag.
Before anything is stopped, the update must be able to apply: this clone is on a branch, the
release follows from it (no commits of its own), and no untracked file is in the way of one
the release adds.
Tasks that run this repository's scripts\run_bot.ps1 are found by their action; stopping and
starting them needs administrator rights, so with tasks, run from a normal terminal, it asks
for them and goes on in a new window. A running one is asked to stop with
`tbot stop` (it finishes the event in progress). Nothing is sold or cancelled.
After the packages are synced, the new version must load on this PC, and `tbot doctor
--offline` must still pass on every config whose task was stopped; otherwise everything goes
back to the old version. Whatever happens afterwards, every task that was running is started
again after doctor checks its config (offline, it fails only on what stops a start; the
network checks are left to the bot, which retries them), and checked to run a little later.
Local changes to tracked files (an edited config, a longer trial log) are stashed for the
update and put back; if the update changes the same lines, or any step fails, everything stays
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
$doctors = @{}  # doctor's exit code by commit and config

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

function Test-Config([string] $config, [switch] $Quiet) {
    # `tbot doctor --offline` on the version checked out now, once per version; it shows its
    # warnings and failures (-Quiet: only when it fails).
    $key = "$(git rev-parse HEAD) $config"
    if (-not $doctors.ContainsKey($key)) {
        $report = @(& $uv run --quiet --frozen python -m tbot doctor $config --offline)
        $doctors[$key] = $LASTEXITCODE
        if (-not $Quiet -or $LASTEXITCODE -ne 0) {
            Write-Host "doctor --offline $($config):"
            $report | Where-Object { $_ -notmatch '^OK\s' } | Write-Host
        }
    }
    return $doctors[$key]
}

function Find-Release {
    # The tag to update to: the newest vX.Y.Z with a published GitHub release, else the newest vX.Y.Z.
    Invoke-Checked "git" @("fetch", "--quiet", "--tags", "origin")
    $tags = @(git tag --list "v*" | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } |
        Sort-Object { [version] $_.Substring(1) } -Descending)
    if (-not $tags) { throw "origin has no release tag (vX.Y.Z) to update to" }
    $published = Get-PublishedTags ([string] (git remote get-url origin))
    $released = @($tags | Where-Object { $null -ne $published -and $published -contains $_ })
    if ($released) { return $released[0] }
    $why = if ($null -eq $published) {
        "cannot tell which releases are published (origin is not on GitHub, or GitHub did not answer)"
    } else {
        "GitHub lists no published release of these tags"
    }
    Write-Host "$($why): taking the newest tag, $($tags[0])"
    return $tags[0]
}

function Get-Refusal([string] $tag) {
    # Why the update to $tag cannot apply, or "": asked before anything is stopped.
    $null = git symbolic-ref -q HEAD
    if ($LASTEXITCODE -ne 0) {
        return ("this clone is not on a branch (as after git checkout <tag>): run " +
            "git switch main, then this again")
    }
    $null = git merge-base --is-ancestor HEAD $tag
    if ($LASTEXITCODE -ne 0) {
        return ("this clone has commits that $tag does not have (git log --oneline $tag..HEAD), " +
            "so it cannot simply move to $($tag): merge it by hand (git merge $tag)")
    }
    $added = @(git -c core.quotepath=off diff --name-only --diff-filter=A HEAD $tag)
    $untracked = @(git -c core.quotepath=off ls-files --others --exclude-standard)
    $inWay = @($added | Where-Object { $untracked -contains $_ })
    if ($inWay) {
        return ("$tag adds files that are here already but not tracked by git: " +
            "$($inWay -join ', '). Move or rename them, then run this again")
    }
    return ""
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
            "account: tbot stop <config> for each session, git merge --ff-only <newest tag>, " +
            "uv sync --frozen, then tbot autostart <config>")
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

    $null = git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "git cannot use $repo as $([Environment]::UserName) (see above)" }
    if (git stash list | Select-String -SimpleMatch $StashName -Quiet) {
        throw "an earlier update left your local changes in 'git stash list' ($StashName). " +
            "Put them back with 'git stash pop' (or drop them), then run this again."
    }
    $status = @(git status --porcelain --untracked-files=no)
    if ($status) { Write-Output "local changes, kept across the update:`n$($status -join "`n")" }
    $others = Get-OtherProcesses @($running | ForEach-Object { $_.Config })

    $tag = Find-Release
    $null = git merge-base --is-ancestor $tag HEAD
    if ($LASTEXITCODE -eq 0) {
        Write-Output "already up to date: $tag is in this clone ($(Get-Version))"
        return
    }
    $refusal = Get-Refusal $tag
    if ($DryRun) {
        if ($refusal) { Write-Output "would refuse: $refusal" }
        if ($others) { Write-Output "would refuse: $(Format-Busy $others)" }
        $names = ($running | ForEach-Object { $_.Name }) -join ', '
        Write-Output "would stop: $(if ($names) { $names } else { 'nothing (no task runs)' })"
        Write-Output "would update to $($tag):"
        git log --oneline "HEAD..$tag" | ForEach-Object { Write-Output "  $_" }
        return
    }
    if ($refusal) { throw "nothing was stopped or changed: $refusal" }
    if ($others) { throw (Format-Busy $others) }  # before any task is stopped for nothing
    Assert-EnvReadable
    # A session whose doctor passes now and fails on the new version sends the update back.
    $passed = @{}
    foreach ($task in $running) { $passed[$task.Config] = (Test-Config $task.Config -Quiet) -eq 0 }

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
        if ($status) {  # set aside for the update, put back last
            Invoke-Checked "git" @("stash", "push", "--quiet", "-m", $StashName)
            $stashed = $true
        }
        try {
            Invoke-Checked "git" @("merge", "--ff-only", "--quiet", $tag)
            Invoke-Checked $uv @("sync", "--frozen")
            if ($stashed) {  # kept in the stash until the new version passed its checks
                & git stash apply --quiet
                if ($LASTEXITCODE -ne 0) {
                    throw ("the update changes the same lines as your local changes; nothing was " +
                        "changed. To take it: note your edits (git diff), undo them with 'git checkout " +
                        "-- <file>', run tbot update, then make them again in the updated file")
                }
            }
            $problem = Test-Packages $uv
            if ($problem) { throw "the new version does not work on this PC: $problem" }
            foreach ($task in $running) {
                if ($passed[$task.Config] -and (Test-Config $task.Config) -ne 0) {
                    throw "doctor --offline passed on $($task.Config) before the update and fails after it (see above)"
                }
            }
            if ($stashed) {
                Invoke-Checked "git" @("stash", "drop", "--quiet")
                $stashed = $false
            }
        } catch {
            $failed = $true
            Write-Output "update NOT applied: $_"
            if ($stashed -or (git rev-parse HEAD) -ne $before) {
                Write-Output "going back to the old version, $oldVersion ($before)"
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
            if ((Test-Config $task.Config) -ne 0) {
                Write-Output ("NOT started '$($task.Name)': it could not start (see the FAIL lines). " +
                    "Fix it, then: tbot autostart $($task.Config)")
                $notStarted += $task.Name
                continue
            }
            $stem = [IO.Path]::GetFileNameWithoutExtension($task.Config)
            $problem = Start-TaskChecked $task.Name (Join-Path $repo "logs\$stem.supervisor.log")
            if ($problem) {
                Write-Output "'$($task.Name)' was started again, but $problem"
                $notStarted += $task.Name
            } else {
                Write-Output "started '$($task.Name)'"
            }
        }
    }
    if ($failed -or $notStarted) { exit 1 }
    Write-Output "updated to $($tag): $oldVersion -> $(Get-Version); what changed: CHANGELOG.md"
} catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    Wait-Close -Elevated:$Elevated
}
exit $exitCode
