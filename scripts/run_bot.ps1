<#
.SYNOPSIS
Runs the bot and starts it again after a crash.

.DESCRIPTION
Exit code 1 (a crash or a failed start) starts the bot again after a delay that doubles up to
MaxDelaySeconds, so a persistent failure does not flood Telegram. Exit code 0 (stopped on
purpose) and 2 (another process owns the ledger, or SQLite cannot load) end the loop.
scripts/install_task.ps1 registers this script to run at startup.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\run_bot.ps1 -Config config\paper.yaml
#>
param(
    [Parameter(Mandatory = $true)] [string] $Config,
    [switch] $Live,
    [string] $Uv = "",
    [int] $FirstDelaySeconds = 60,
    [int] $MaxDelaySeconds = 900,
    [int] $MaxRuns = 0  # 0 runs forever
)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo  # .env, data/, logs/, and ledgers are relative to the repository
if (-not $Uv) { $Uv = (Get-Command uv -ErrorAction Stop).Source }
$text = Get-Content -Raw $Config -ErrorAction Stop
$command = if ($text -match '(?m)^mode:') { "live" } else { "paper" }
$arguments = @("run", "--frozen", "python", "-m", "tbot", $command, $Config)
if ($Live) { $arguments += "--live" }

$delay = $FirstDelaySeconds
$runs = 0
while ($true) {
    $started = Get-Date
    & $Uv @arguments
    $code = $LASTEXITCODE
    $runs += 1
    if ($code -eq 0 -or $code -eq 2) { exit $code }
    if ($MaxRuns -gt 0 -and $runs -ge $MaxRuns) { exit $code }
    if (((Get-Date) - $started).TotalHours -ge 1) { $delay = $FirstDelaySeconds }  # it was healthy
    $now = (Get-Date).ToUniversalTime().ToString("s")
    Write-Output "${now}Z tbot exited with code $code; starting again in $delay s"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxDelaySeconds)
}
