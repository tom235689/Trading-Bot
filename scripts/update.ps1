<#
.SYNOPSIS
Updates the bot: stops its scheduled sessions gracefully, pulls, syncs packages, starts them again.

.DESCRIPTION
Run it from an elevated PowerShell, like scripts\install_task.ps1. Tasks that run this
repository's scripts\run_bot.ps1 are found by their action. A running one is asked to stop with
`tbot stop` (it finishes the event in progress); after the update, doctor checks its config and
the task starts again only when doctor reports no problem. Nothing is sold or cancelled.
Local changes to tracked files (an edited config, a longer trial log) are stashed for the
pull and put back; if the update changes the same lines, everything stays as it was.
A bot started by hand in a console must be stopped by hand first.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#>
param(
    [switch] $DryRun,  # only show the tasks and what would be done
    [int] $StopTimeoutSeconds = 120
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$uv = (Get-Command uv -ErrorAction Stop).Source
$runner = (Join-Path $PSScriptRoot "run_bot.ps1").ToLowerInvariant()

function Invoke-Checked([string] $exe, [string[]] $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $exe $($arguments -join ' ')" }
}

$tasks = @(Get-ScheduledTask | ForEach-Object {
    $action = $_.Actions | Where-Object {
        $_.Arguments -and $_.Arguments.ToLowerInvariant().Contains($runner)
    } | Select-Object -First 1
    if ($action) {
        $config = if ($action.Arguments -match '-Config "([^"]+)"') { $Matches[1] } else { "" }
        [pscustomobject] @{ Name = $_.TaskName; State = [string] $_.State; Config = $config }
    }
})
foreach ($task in $tasks) { Write-Output "task '$($task.Name)': $($task.State), $($task.Config)" }
if (-not $tasks) { Write-Output "no scheduled tbot task for $repo" }
$running = @($tasks | Where-Object { $_.State -eq "Running" })

$status = @(git status --porcelain --untracked-files=no)
if ($status) { Write-Output "local changes, kept across the update:`n$($status -join "`n")" }
if ($DryRun) {
    git fetch --quiet
    Write-Output "would stop: $(($running | ForEach-Object { $_.Name }) -join ', ')"
    Write-Output "would pull:"
    git log --oneline "HEAD..@{u}"
    return
}

foreach ($task in $running) {
    Write-Output "stopping '$($task.Name)'"
    & $uv run --frozen python -m tbot stop $task.Config --timeout $StopTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "'$($task.Name)' did not stop; nothing was updated" }
    $deadline = (Get-Date).AddSeconds(60)  # the supervisor ends when the bot exits with 0
    while ((Get-ScheduledTask -TaskName $task.Name).State -eq "Running") {
        if ((Get-Date) -gt $deadline) { throw "task '$($task.Name)' is still running" }
        Start-Sleep -Seconds 2
    }
}

$before = git rev-parse HEAD
$stashed = $false
if ($status) {  # edited configs, a longer trial log: set aside, put back after the pull
    Invoke-Checked "git" @("stash", "push", "--quiet", "-m", "tbot update")
    $stashed = $true
}
try {
    Invoke-Checked "git" @("pull", "--ff-only")
    if ($stashed) {
        & git stash pop --quiet
        if ($LASTEXITCODE -ne 0) { throw "the update changes the same lines as your local changes" }
        $stashed = $false
    }
    Invoke-Checked $uv @("sync", "--frozen")
} catch {
    Write-Output "update failed: $_"
    if ((git rev-parse HEAD) -ne $before) {
        Write-Output "going back to $before"
        Invoke-Checked "git" @("reset", "--hard", "--quiet", $before)  # local changes are stashed
    }
    if ($stashed) {
        Invoke-Checked "git" @("stash", "pop", "--quiet")  # applies cleanly on the old version
    }
    Invoke-Checked $uv @("sync", "--frozen")
    Write-Output "the old version and your local changes stay; starting the stopped tasks again"
}
$after = git rev-parse HEAD
if ($before -ne $after) { git log --oneline "$before..$after" } else { Write-Output "already up to date" }

foreach ($task in $running) {
    & $uv run --frozen python -m tbot doctor $task.Config
    if ($LASTEXITCODE -ne 0) {
        Write-Output "NOT started '$($task.Name)': doctor reports a problem. Fix it, then: Start-ScheduledTask -TaskName '$($task.Name)'"
        continue
    }
    Start-ScheduledTask -TaskName $task.Name
    Write-Output "started '$($task.Name)'"
}
