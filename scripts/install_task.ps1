<#
.SYNOPSIS
Runs a session at every boot, restarted after crashes: registers a scheduled task and starts it.

.DESCRIPTION
The task runs scripts/run_bot.ps1, which starts the bot again after a crash, whether anyone is
logged on or not. Tasks that start at boot need administrator rights: run from a normal
terminal, it asks for them and goes on in a new window. It runs `tbot doctor` first and refuses
to register while doctor reports a problem. The task is started at once, unless -NoStart or a
session already runs on the config (then it starts at the next boot). -Remove stops the bot
gracefully and removes the task (scripts\remove_task.ps1); with -DryRun it only says what it
would do.

.PARAMETER Config
A config file or a name from config\.

.PARAMETER Live
Required for a config with mode: live.

.PARAMETER Name
The task name; default "tbot <config name>".

.PARAMETER SkipDoctor
Register without running tbot doctor first.

.PARAMETER NoStart
Register only; the session starts at the next boot.

.PARAMETER Remove
Stop the session gracefully and remove its task.

.PARAMETER DryRun
Only show what would be done; changes nothing and asks for no rights.

.PARAMETER Elevated
Internal: set when the script restarted itself with administrator rights.

.EXAMPLE
tbot autostart paper
tbot autostart live -Live
tbot autostart paper -Remove
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 paper -DryRun
#>
param(
    [Parameter(Position = 0)] [string] $Config = "paper",
    [switch] $Live,
    [string] $Name = "",
    [switch] $SkipDoctor,
    [switch] $NoStart,
    [switch] $Remove,
    [switch] $DryRun,
    [switch] $Elevated
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    & (Join-Path $PSScriptRoot "remove_task.ps1") -Config $Config -Name $Name -DryRun:$DryRun
    exit $LASTEXITCODE
}
if (-not $DryRun -and -not $Elevated -and -not (Test-Admin)) {
    exit (Invoke-Elevated $PSCommandPath $PSBoundParameters)
}

$code = 0
try {
    $path = Resolve-Config $repo $Config
    $text = Get-Content -Raw $path
    $isLive = $text -match '(?m)^mode:\s*["'']?live["'']?\s*(#.*)?$'
    if ($isLive -and -not $Live) { throw "$Config trades real money: add -Live to confirm" }
    if ($Live -and -not $isLive) { throw "-Live is only for a config with mode: live" }
    $stem = [IO.Path]::GetFileNameWithoutExtension($path)
    if (-not $Name) { $Name = "tbot $stem" }
    $uv = (Get-Command uv).Source  # the task may not see the same PATH

    $busy = $false
    if (-not $SkipDoctor) {
        $report = @(& $uv --directory $repo run --quiet --frozen python -m tbot doctor $path)
        $doctorCode = $LASTEXITCODE
        $report | ForEach-Object { Write-Output $_ }
        if ($doctorCode -eq 4) { throw "$Config is no valid session config: see the error above" }
        if ($doctorCode -ne 0) { throw "doctor found problems: fix the FAIL lines, or pass -SkipDoctor" }
        $busy = [bool] ($report | Where-Object { $_ -match "process: a session is running" })
    }

    $runner = Join-Path $PSScriptRoot "run_bot.ps1"
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Config `"$path`" -Uv `"$uv`""
    if ($Live) { $arguments += " -Live" }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U  # no stored password
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Trading bot ($stem), restarted after crashes"

    if ($DryRun) {
        Write-Output "task '$Name' as $user at startup: powershell.exe $arguments"
    } else {
        $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($existing) {
            $other = Get-TaskRunner $existing
            if (-not $other) { throw "a task '$Name' exists and is no tbot task: pass -Name with another name" }
            if (-not (Test-OwnTask $other $runner)) {
                throw "'$Name' runs the bot of another folder ($other): pass -Name with another name"
            }
        }
        Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
        if ($existing -and $existing.State -eq "Running") {
            Write-Output "updated '$Name'; it is running, and the new settings apply at its next start"
        } elseif ($NoStart) {
            Write-Output "registered '$Name': it starts at the next boot, or now with: Start-ScheduledTask -TaskName '$Name'"
        } elseif ($busy) {
            Write-Output ("registered '$Name'. A session started by hand already runs on $stem, so the task " +
                "starts at the next boot. To hand over now: tbot stop $stem, then Start-ScheduledTask -TaskName '$Name'")
        } else {
            # A `tbot stop` from before would end the new start at once: withdraw it.
            $withdrawn = @(& $uv --directory $repo run --quiet --frozen python -m tbot stop $path --cancel)
            $withdrawn | Where-Object { $_ -match "withdrawn" } | ForEach-Object { Write-Output $_ }
            Start-ScheduledTask -TaskName $Name
            Write-Output "registered and started '$Name': it runs at every boot. Watch it: tbot status, tbot log $stem -f"
        }
    }
} catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    $code = 1
}
Wait-Close -Elevated:$Elevated
exit $code
