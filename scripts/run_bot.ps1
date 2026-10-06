<#
.SYNOPSIS
Runs the bot and starts it again after a crash.

.DESCRIPTION
Exit code 1 (a crash, a failed start, or no network yet) starts the bot again after a delay
that doubles up to MaxDelaySeconds, so a persistent failure does not flood Telegram. These
end the loop: 0 (stopped on purpose, e.g. `tbot stop`), 3 (the ledger is in use by another
process, damaged, or SQLite cannot load), and 4 (the config or the command is wrong).
The last run's console output is in logs\console.txt, and every start and exit in
logs\supervisor.log. scripts/install_task.ps1 registers this script to run at startup.

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
New-Item -ItemType Directory -Force logs | Out-Null
$journal = Join-Path $repo "logs\supervisor.log"
$console = Join-Path $repo "logs\console.txt"  # the bot logs to the console on stderr

function Write-Journal([string] $text) {
    $line = (Get-Date).ToUniversalTime().ToString("s") + "Z " + $text
    Write-Output $line
    Add-Content -Path $journal -Value $line -Encoding UTF8
}

if (-not $Uv) { $Uv = (Get-Command uv -ErrorAction Stop).Source }
$text = Get-Content -Raw $Config -ErrorAction Stop
$command = if ($text -match '(?m)^mode:') { "live" } else { "paper" }
$arguments = @("run", "--frozen", "python", "-m", "tbot", $command, $Config)
if ($Live) { $arguments += "--live" }
$line = ($arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join " "

$delay = $FirstDelaySeconds
$runs = 0
while ($true) {
    $started = Get-Date
    Write-Journal "starting: uv $line"
    $process = Start-Process -FilePath $Uv -ArgumentList $line -NoNewWindow -PassThru `
        -RedirectStandardError $console
    $null = $process.Handle  # keeps the exit code readable after the wait
    $process.WaitForExit()
    $code = $process.ExitCode
    $runs += 1
    $last = Get-Content -Path $console -Tail 1 -ErrorAction SilentlyContinue
    if ($code -eq 0 -or $code -eq 3 -or $code -eq 4) {
        Write-Journal "tbot exited with code $code; not restarting. $last"
        exit $code
    }
    if ($MaxRuns -gt 0 -and $runs -ge $MaxRuns) {
        Write-Journal "tbot exited with code $code; run limit reached. $last"
        exit $code
    }
    if (((Get-Date) - $started).TotalHours -ge 1) { $delay = $FirstDelaySeconds }  # it was healthy
    Write-Journal "tbot exited with code $code; starting again in $delay s. $last"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxDelaySeconds)
}
