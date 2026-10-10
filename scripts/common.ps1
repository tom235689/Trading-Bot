# Helpers for install_task.ps1, remove_task.ps1, and update.ps1 (dot-sourced).

function Resolve-Config([string] $repo, [string] $config) {
    # A config file, or a name from config\ such as paper; returns the full path.
    $path = if ([IO.Path]::IsPathRooted($config)) { $config } else { Join-Path $repo $config }
    if (-not (Test-Path $path -PathType Leaf) -and -not [IO.Path]::GetExtension($config) -and
        $config -notmatch '[\\/]') {
        $path = Join-Path $repo "config\$config.yaml"
        if (-not (Test-Path $path -PathType Leaf)) { $path = Join-Path $repo "config\$config.yml" }
    }
    if (-not (Test-Path $path -PathType Leaf)) {
        $names = @(Get-ChildItem (Join-Path $repo "config") -File |
            Where-Object { $_.Extension -in ".yaml", ".yml" } |
            ForEach-Object { $_.BaseName } | Sort-Object) -join ", "
        throw "no config '$config' (names in config\: $names)"
    }
    return (Resolve-Path $path).Path
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
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { "-$key" }
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
    if ($Here) {
        $process = Start-Process powershell.exe -ArgumentList $line -NoNewWindow -Wait -PassThru
        return $process.ExitCode
    }
    Write-Host "administrator rights needed: answer the Windows prompt; the script runs in a new window"
    try {
        $process = Start-Process powershell.exe -ArgumentList "$line -Elevated" -Verb RunAs -Wait -PassThru
    } catch {
        Write-Host "not done: administrator rights were not given ($($_.Exception.Message))"
        return 1
    }
    Write-Host "the administrator window finished with exit code $($process.ExitCode)"
    return $process.ExitCode
}

function Wait-Close([switch] $Elevated) {
    # An elevated window closes when its script ends: keep it open to be read.
    if ($Elevated) { $null = Read-Host "press Enter to close this window" }
}
