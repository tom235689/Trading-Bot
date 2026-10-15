<#
.SYNOPSIS
Runs the bot and starts it again after a crash.

.DESCRIPTION
Exit code 1 (a crash, a failed start, or no network yet) starts the bot again after a delay
that doubles up to MaxDelaySeconds, so a persistent failure does not flood Telegram. These
end the loop: 0 (stopped on purpose, e.g. `tbot stop`), 3 (the ledger is in use by another
process, damaged, or SQLite cannot load), and 4 (the config or the command is wrong). When uv
cannot be found or started three times in a row, the loop ends with 4 too.
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
# Paths for cmdlets are absolute: in a folder whose name has [ or ], Windows PowerShell 5.1
# resolves a relative -LiteralPath wrong (and its Start-Process fails, so it is not used).
[IO.Directory]::CreateDirectory((Join-Path $repo "logs")) | Out-Null
$name = [IO.Path]::GetFileNameWithoutExtension($Config)
$journal = Join-Path $repo "logs\$name.supervisor.log"
$console = Join-Path $repo "logs\$name.console.txt"  # the bot logs to the console on stderr
$configFile = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $repo $Config }

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

function Invoke-Uv([string] $exe) {
    # Runs uv in the repository with its stderr copied to the console file; its exit code.
    $info = New-Object Diagnostics.ProcessStartInfo $exe, $line
    $info.UseShellExecute = $false
    $info.WorkingDirectory = $repo
    $info.RedirectStandardError = $true
    $file = New-Object IO.FileStream $console, ([IO.FileMode]::Create), ([IO.FileAccess]::Write),
        ([IO.FileShare]::ReadWrite), 1  # unbuffered: tbot log reads it while the bot runs
    try {
        $process = [Diagnostics.Process]::Start($info)
        $copy = $process.StandardError.BaseStream.CopyToAsync($file)
        while (-not $process.WaitForExit(1000)) {
            # A console file that cannot be written (a full disk) must not leave the bot blocked
            # on a full pipe: what it prints from then on is dropped (its JSON log has it all).
            if ($copy.IsFaulted) { $copy = $process.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null) }
        }
        try { $null = $copy.Wait(10000) } catch { }  # a process left behind may keep stderr open
        return $process.ExitCode
    } finally {
        $file.Dispose()
    }
}

try {
    $text = Get-Content -Raw -LiteralPath $configFile -ErrorAction Stop
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
$failed = 0  # starts in a row that did not happen: no uv, or uv would not start
while ($true) {
    $started = Get-Date
    $exe = Find-Uv
    $ran = $false
    $code = -1
    $last = ""
    if (-not $exe) {
        $last = "uv not found (was $Uv): install uv again, then run tbot autostart $name again"
    } else {
        if (Test-Path -LiteralPath $console) {
            Move-Item -Force -LiteralPath $console -Destination "$console.1"  # the run before stays
        }
        Write-Journal "starting: $exe $line"
        try {
            $code = Invoke-Uv $exe
            $ran = $true
            $last = Get-Content -LiteralPath $console -Tail 1 -ErrorAction SilentlyContinue
        } catch {
            $why = if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }
            $last = "could not start uv ($why): fix that, then run tbot autostart $name again"
        }
    }
    $runs += 1
    $what = if ($ran) { "tbot exited with code $code" } else { "tbot did not start" }
    if ($ran) {
        $failed = 0
    } else {
        $failed += 1
        if ($failed -ge 3) {  # this supervisor keeps its PATH: a new uv needs a new task start
            Write-Journal "$what $failed times in a row; not retrying. $last"
            exit 4
        }
    }
    if ($ran -and ($code -eq 0 -or $code -eq 3 -or $code -eq 4)) {
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
