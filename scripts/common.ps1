# Helpers for install_task.ps1, remove_task.ps1, update.ps1, and setup.ps1 (dot-sourced).
# Paths handed to cmdlets are absolute: in a folder whose name has [ or ], Windows
# PowerShell 5.1 resolves a relative -LiteralPath wrong.

function Resolve-Config([string] $repo, [string] $config) {
    # A config file, or a name from config\ such as paper; returns the full path.
    $path = if ([IO.Path]::IsPathRooted($config)) { $config } else { Join-Path $repo $config }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -and -not [IO.Path]::GetExtension($config) -and
        $config -notmatch '[\\/]') {
        $path = Join-Path $repo "config\$config.yaml"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $path = Join-Path $repo "config\$config.yml" }
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $names = @(Get-ChildItem -LiteralPath (Join-Path $repo "config") -File |
            Where-Object { $_.Extension -in ".yaml", ".yml" } |
            ForEach-Object { $_.BaseName } | Sort-Object) -join ", "
        throw "no config '$config' (names in config\: $names)"
    }
    return (Resolve-Path -LiteralPath $path).Path
}

function Get-TaskRunner($task) {
    # The run_bot.ps1 a scheduled task runs, or "" for a task that is no tbot task.
    foreach ($action in @($task.Actions)) {
        if ($action.Arguments -match '-File "([^"]*run_bot\.ps1)"') { return $Matches[1] }
    }
    return ""
}

function Test-SameFile([string] $a, [string] $b) {
    return [IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [IO.Path]::GetFullPath($b).TrimEnd('\')
}

function Test-OwnTask([string] $other, [string] $runner) {
    # A task runs this folder's bot, or the bot of a folder that is gone (moved or renamed).
    return (Test-SameFile $other $runner) -or -not (Test-Path -LiteralPath $other)
}

function Test-Admin {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-Argument([string] $value) {
    # Quoted so that Windows splits the command line back into this exact value:
    # backslashes before a quote or at the end are doubled, and quotes are escaped.
    $escaped = [regex]::Replace($value, '(\\*)"', { param($m) $m.Groups[1].Value * 2 + '\"' })
    $escaped = [regex]::Replace($escaped, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
    return '"' + $escaped + '"'
}

function Format-Arguments([System.Collections.IDictionary] $bound) {
    # Bound script parameters as a powershell.exe command line.
    $parts = foreach ($key in $bound.Keys) {
        $value = $bound[$key]
        if ($value -is [System.Management.Automation.SwitchParameter] -or $value -is [bool]) {
            if ([bool] $value) { "-$key" }  # a switch set by the script itself is a bool
        } else {
            "-$key"
            ConvertTo-Argument ([string] $value)
        }
    }
    return ($parts -join " ")
}

function Invoke-Elevated([string] $script, [System.Collections.IDictionary] $bound, [switch] $Here) {
    # Runs the script again as administrator in a new window and returns its exit code.
    # -Here runs it unelevated in this console instead (for tests).
    $line = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" $(Format-Arguments $bound)"
    $how = @{ Verb = "RunAs" }
    if ($Here) {
        $how = @{ NoNewWindow = $true }
    } else {
        $line += " -Elevated"
        Write-Host "administrator rights needed: answer the Windows prompt; the script runs in a new window"
    }
    # Windows PowerShell 5.1's Start-Process fails in a folder whose name has [ or ].
    Push-Location -LiteralPath $env:SystemRoot
    try {
        $process = Start-Process powershell.exe -ArgumentList $line @how -Wait -PassThru
    } catch {
        if ($Here) { throw }
        Write-Host "not done: administrator rights were not given ($($_.Exception.Message))"
        return 1
    } finally {
        Pop-Location
    }
    if (-not $Here) { Write-Host "the administrator window finished with exit code $($process.ExitCode)" }
    return $process.ExitCode
}

function Start-TaskChecked([string] $name, [string] $journal) {
    # Starts a scheduled task; "" once it runs the bot, else why not. A start that fails at
    # once ends the task, or shows in the supervisor's journal while it waits to retry.
    $seen = if (Test-Path -LiteralPath $journal) { @(Get-Content -LiteralPath $journal).Count } else { 0 }
    Start-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 10
    $lines = if (Test-Path -LiteralPath $journal) { @(Get-Content -LiteralPath $journal) } else { @() }
    $new = if ($lines.Count -ge $seen) { @($lines | Select-Object -Skip $seen) } else { $lines }  # moved to .1
    $state = [string] (Get-ScheduledTask -TaskName $name).State
    if ($state -ne "Running") {
        $last = if ($lines) { $lines[-1] } else { "no journal yet" }
        return "it is not running ($state): $last"
    }
    $failed = @($new | Where-Object { $_ -match '^\S+ tbot (did not start|exited with code)' })
    if ($failed) { return "the bot did not start: $($failed[-1])" }
    return ""
}

function Test-Packages([string] $uv) {
    # "" when the packages load, else why not. On a processor without AVX2, FMA, and BMI
    # polars only warns that it will likely crash: here that warning is an error.
    $ErrorActionPreference = "Continue"  # under Stop, a line on stderr would throw
    $output = @(& $uv run --quiet --frozen python -W "error:Missing required CPU features:RuntimeWarning" `
        -c "import sqlite3, polars, pydantic_core, tbot" 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { [string] $_ }
        })
    if ($LASTEXITCODE -eq 0) { return "" }
    $text = $output -join "`n"
    if ($text -match "Missing required CPU features") {
        $missing = if ($text -match 'not detected:\s*\n\s*(\S[^\n]*)') { " ($($Matches[1].Trim()))" } else { "" }
        return ("this PC's processor lacks features$missing that polars, the data library, needs, " +
            "so tbot would crash on it. Run tbot on a newer PC: most made since 2015 have them")
    }
    $tail = ($output | Where-Object { $_ } | Select-Object -Last 3) -join " "
    return "the packages do not load (exit $LASTEXITCODE): $tail"
}

function Get-PublishedTags([string] $url) {
    # The tags of a GitHub repository's published releases (no drafts or pre-releases), or
    # $null when origin is not on GitHub or GitHub does not answer.
    if ($url -notmatch 'github\.com[/:]([^/]+)/([^/]+?)(\.git)?/?$') { return $null }
    $api = "https://api.github.com/repos/$($Matches[1])/$($Matches[2])/releases?per_page=30"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
            [Net.SecurityProtocolType]::Tls12
        $releases = Invoke-RestMethod -Uri $api -UseBasicParsing -TimeoutSec 20 `
            -Headers @{ Accept = "application/vnd.github+json" }
    } catch {
        Write-Host "GitHub did not answer ($($_.Exception.Message))"
        return $null
    }
    return ,@($releases | Where-Object { -not $_.draft -and -not $_.prerelease } |
        ForEach-Object { [string] $_.tag_name })
}

function Wait-Close([switch] $Elevated) {
    # An elevated window closes when its script ends: keep it open to be read.
    if ($Elevated) { $null = Read-Host "press Enter to close this window" }
}
