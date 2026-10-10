<#
.SYNOPSIS
Removes a scheduled task registered by scripts\install_task.ps1, stopping the bot gracefully first.

.DESCRIPTION
Needs administrator rights: run from a normal terminal, it asks for them and goes on in a new
window. The bot finishes the event in progress (`tbot stop`); nothing is sold, exchange stops
stay in place, and the ledger, data, and logs are kept.

.PARAMETER Config
A config file or a name from config\.

.PARAMETER Name
The task name; default "tbot <config name>".

.PARAMETER DryRun
Only say what would be done; changes nothing and asks for no rights.

.PARAMETER Elevated
Internal: set when the script restarted itself with administrator rights.

.EXAMPLE
tbot autostart paper -Remove
powershell -ExecutionPolicy Bypass -File scripts\remove_task.ps1 paper
#>
param(
    [Parameter(Position = 0)] [string] $Config = "paper",
    [string] $Name = "",
    [switch] $DryRun,
    [switch] $Elevated
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$named = [bool] $Name  # a task named outright may belong to no config here
if (-not $Name) { $Name = "tbot " + [IO.Path]::GetFileNameWithoutExtension($Config) }

$task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
$runner = Join-Path $PSScriptRoot "run_bot.ps1"
if ($task) {
    $other = Get-TaskRunner $task
    if (-not $other -or -not (Test-OwnTask $other $runner)) {
        $owner = if ($other) { "the bot of another folder ($other)" } else { "no tbot task" }
        Write-Output "'$Name' is $($owner): remove it from there, or in Task Scheduler"
        exit 1
    }
} elseif (-not $named) {
    try { $null = Resolve-Config $repo $Config } catch {
        # A typo: no prompt for it. A task Task Scheduler lists can still be removed elevated.
        Write-Output "no task '$Name' visible here, and $($_.Exception.Message)"
        Write-Output "(a task Task Scheduler lists: run this from an administrator terminal)"
        exit 1
    }
}
if ($DryRun) {
    if ($task) {
        Write-Output "would stop the bot gracefully and remove '$Name' ($($task.State))"
    } else {
        Write-Output "no task '$Name' visible here (an elevated terminal may see more)"
    }
    exit 0
}
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
        # The session the task runs, which may differ from the name given here.
        $action = @($task.Actions)[0]
        $session = if ($action.Arguments -match '-Config "([^"]+)"') { $Matches[1] } else { $Config }
        $uv = (Get-Command uv -ErrorAction Stop).Source
        & $uv run --quiet --frozen python -m tbot stop $session
        if ($LASTEXITCODE -ne 0) {
            & $uv run --quiet --frozen python -m tbot stop $session --cancel | Out-Null  # it keeps running
            throw "the bot did not stop in time; the task and the bot are left as they were"
        }
        # The bot is gone; a supervisor still waiting to restart it is ended with the task,
        # and the request left for that restart is withdrawn.
        Stop-ScheduledTask -TaskName $Name
        & $uv run --quiet --frozen python -m tbot stop $session --cancel | Out-Null
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Output "removed '$Name'; the ledger, data, and logs are kept"
} catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    $code = 1
}
Wait-Close -Elevated:$Elevated
exit $code
