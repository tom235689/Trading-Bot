<#
.SYNOPSIS
Registers a scheduled task that runs the bot at startup, whether anyone is logged on or not.

.DESCRIPTION
The task runs scripts/run_bot.ps1, which starts the bot again after a crash. Run this from an
elevated PowerShell: tasks that start at boot need administrator rights. It runs
`tbot doctor` first and refuses to register while doctor reports a problem.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Config config\paper.yaml
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Config config\live.yaml -Live
Start-ScheduledTask -TaskName "tbot paper"; Unregister-ScheduledTask -TaskName "tbot paper"
#>
param(
    [Parameter(Mandatory = $true)] [string] $Config,
    [switch] $Live,
    [string] $Name = "",
    [switch] $SkipDoctor,
    [switch] $DryRun
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$path = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $repo $Config }
if (-not (Test-Path $path -PathType Leaf)) { throw "no config at $path" }
$text = Get-Content -Raw $path
$isLive = $text -match '(?m)^mode:\s*live\s*$'
if ($isLive -and -not $Live) { throw "$Config trades real money: add -Live to confirm" }
if ($Live -and -not $isLive) { throw "-Live is only for a config with mode: live" }
if (-not $Name) { $Name = "tbot " + [IO.Path]::GetFileNameWithoutExtension($Config) }
$uv = (Get-Command uv).Source  # the task may not see the same PATH

if (-not $SkipDoctor) {
    & $uv --directory $repo run --frozen python -m tbot doctor $path
    if ($LASTEXITCODE -ne 0) { throw "doctor found problems: fix the FAIL lines, or pass -SkipDoctor" }
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
    -Principal $principal -Description "Trading bot ($Config), restarted after crashes"

if ($DryRun) {
    Write-Output "task '$Name' as $user at startup: powershell.exe $arguments"
    return
}
Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
Write-Output "registered '$Name'. Start it now: Start-ScheduledTask -TaskName '$Name'"
