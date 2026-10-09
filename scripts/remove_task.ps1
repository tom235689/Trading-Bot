<#
.SYNOPSIS
Removes a scheduled task registered by scripts\install_task.ps1, stopping the bot gracefully first.

.DESCRIPTION
Needs administrator rights: run from a normal terminal, it asks for them and goes on in a new
window. The bot finishes the event in progress (`tbot stop`); nothing is sold, exchange stops
stay in place, and the ledger, data, and logs are kept.

.EXAMPLE
tbot autostart paper -Remove
powershell -ExecutionPolicy Bypass -File scripts\remove_task.ps1 paper
#>
param(
    [Parameter(Position = 0)] [string] $Config = "paper",  # a config file or a name from config\
    [string] $Name = "",
    [switch] $Elevated  # set when the script elevated itself
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
if (-not $Name) { $Name = "tbot " + [IO.Path]::GetFileNameWithoutExtension($Config) }

$task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if (-not $task -and -not $Elevated -and -not (Test-Admin)) {
    # An unelevated terminal may not see a task that an administrator registered.
    exit (Invoke-Elevated $PSCommandPath $PSBoundParameters)
}
if (-not $task) {
    Write-Output "no task '$Name'"
    Wait-Close -Elevated:$Elevated
    exit 0
}
if (-not $Elevated -and -not (Test-Admin)) {
    exit (Invoke-Elevated $PSCommandPath $PSBoundParameters)
}

$code = 0
try {
    if ($task.State -eq "Running") {
        $uv = (Get-Command uv -ErrorAction Stop).Source
        & $uv run --quiet --frozen python -m tbot stop $Config
        if ($LASTEXITCODE -ne 0) {
            & $uv run --quiet --frozen python -m tbot stop $Config --cancel | Out-Null  # it keeps running
            throw "the bot did not stop in time; the task and the bot are left as they were"
        }
        # The bot is gone; a supervisor still waiting to restart it is ended with the task.
        Stop-ScheduledTask -TaskName $Name
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Output "removed '$Name'; the ledger, data, and logs are kept"
} catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    $code = 1
}
Wait-Close -Elevated:$Elevated
exit $code
