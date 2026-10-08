<#
.SYNOPSIS
Removes a scheduled task registered by scripts\install_task.ps1, stopping the bot gracefully first.

.DESCRIPTION
Run it from an elevated PowerShell. The bot finishes the event in progress (`tbot stop`); nothing
is sold, exchange stops stay in place, and the ledger, data, and logs are kept.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\remove_task.ps1 -Config config\paper.yaml
#>
param(
    [Parameter(Mandatory = $true)] [string] $Config,
    [string] $Name = ""
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
if (-not $Name) { $Name = "tbot " + [IO.Path]::GetFileNameWithoutExtension($Config) }

$task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "no task '$Name'"
    return
}
if ($task.State -eq "Running") {
    $uv = (Get-Command uv -ErrorAction Stop).Source
    & $uv run --frozen python -m tbot stop $Config
    if ($LASTEXITCODE -ne 0) {
        & $uv run --frozen python -m tbot stop $Config --cancel | Out-Null  # it keeps running
        throw "the bot did not stop in time; the task and the bot are left as they were"
    }
    # The bot is gone; a supervisor still waiting to restart it is ended with the task.
    Stop-ScheduledTask -TaskName $Name
}
Unregister-ScheduledTask -TaskName $Name -Confirm:$false
Write-Output "removed '$Name'; the ledger, data, and logs are kept"
