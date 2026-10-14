<#
.SYNOPSIS
Runs the bot and starts it again after a crash.

.DESCRIPTION
Exit code 1 (a crash, a failed start, or no network yet) starts the bot again after a delay
that doubles up to MaxDelaySeconds, so a persistent failure does not flood Telegram. These
end the loop: 0 (stopped on purpose, e.g. `tbot stop`), 3 (the ledger is in use by another
process, damaged, or SQLite cannot load), and 4 (the config or the command is wrong).
Each config has its own logs: logs\<config>.supervisor.log records every start and exit,
logs\<config>.console.txt the last run's console. `tbot autostart <config>`
(scripts/install_task.ps1) registers this script to run at startup.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\run_bot.ps1 -Config config\paper.yaml
#>
param(
    [Parameter(Mandatory = $true)] [string] $Config,
    [switch] $Live,
    [string] $Uv = "",
    [int] $FirstDelaySeconds = 60,
    [int] $MaxDelaySeconds = 900,
    [int] $MaxRuns = 0,  # 0 runs forever
    [int] $MaxJournalBytes = 5MB  # then the journal moves to .1 and starts again
)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo  # .env, data/, logs/, and ledgers are relative to the repository
$env:TBOT_SUPERVISED = "1"  # a restart obeys a `tbot stop` left for it; a start by hand does not
New-Item -ItemType Directory -Force logs | Out-Null
$name = [IO.Path]::GetFileNameWithoutExtension($Config)
$journal = Join-Path $repo "logs\$name.supervisor.log"
$console = Join-Path $repo "logs\$name.console.txt"  # the bot logs to the console on stderr

function Write-Journal([string] $text) {
    if ((Test-Path -LiteralPath $journal) -and (Get-Item -LiteralPath $journal).Length -gt $MaxJournalBytes) {
        Move-Item -Force -LiteralPath $journal -Destination "$journal.1"
    }
    $line = (Get-Date).ToUniversalTime().ToString("s") + "Z " + $text
    Write-Output $line
    Add-Content -LiteralPath $journal -Value $line -Encoding UTF8
}

function Find-Uv {
    if ($Uv -and (Test-Path -LiteralPath $Uv -PathType Leaf)) { return $Uv }
    $found = Get-Command uv -ErrorAction SilentlyContinue  # moved or reinstalled
    if ($found) { return $found.Source }
    return ""
}

try {
    $text = Get-Content -Raw -LiteralPath $Config -ErrorAction Stop
} catch {  # renamed or deleted since the task was registered: say so where it is looked for
    Write-Journal "cannot read $Config ($($_.Exception.Message)); not starting. Register it again: tbot autostart <config>"
    exit 4
}
$command = if ($text -match '(?m)^mode:') { "live" } else { "paper" }
$arguments = @("run", "--frozen", "python", "-m", "tbot", $command, $Config)
if ($Live) { $arguments += "--live" }
$line = ($arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join " "

$delay = $FirstDelaySeconds
$runs = 0
$missing = 0  # starts in a row without uv
while ($true) {
    $started = Get-Date
    $exe = Find-Uv
    $code = -1
    $last = ""
    if (-not $exe) {
        $missing += 1
        $last = "uv not found (was $Uv): install uv again, then run tbot autostart $name again"
        if ($missing -ge 3) {  # this supervisor keeps its PATH: a new uv needs a new task start
            Write-Journal "$last; not retrying"
            exit 4
        }
    } else {
        $missing = 0
        if (Test-Path -LiteralPath $console) {
            Move-Item -Force -LiteralPath $console -Destination "$console.1"  # the run before stays
        }
        Write-Journal "starting: $exe $line"
        try {
            $process = Start-Process -FilePath $exe -ArgumentList $line -NoNewWindow -PassThru `
                -RedirectStandardError $console
            $null = $process.Handle  # keeps the exit code readable after the wait
            $process.WaitForExit()
            $code = $process.ExitCode
            $last = Get-Content -LiteralPath $console -Tail 1 -ErrorAction SilentlyContinue
        } catch {
            $last = "could not start uv: $_"
        }
    }
    $runs += 1
    $what = if ($code -eq -1) { "tbot did not start" } else { "tbot exited with code $code" }
    if ($code -eq 0 -or $code -eq 3 -or $code -eq 4) {
        Write-Journal "$what; not restarting. $last"
        exit $code
    }
    if ($MaxRuns -gt 0 -and $runs -ge $MaxRuns) {
        Write-Journal "$what; run limit reached. $last"
        exit 1
    }
    if (((Get-Date) - $started).TotalHours -ge 1) { $delay = $FirstDelaySeconds }  # it was healthy
    Write-Journal "$what; starting again in $delay s. $last"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxDelaySeconds)
}
