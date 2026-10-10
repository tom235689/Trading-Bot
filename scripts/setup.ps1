<#
.SYNOPSIS
Sets up the bot on Windows: Python environment, git hooks, .env, the tbot command, market data,
and a doctor run.

.DESCRIPTION
Safe to run again at any time; every step skips what is already done. While a bot runs from
.venv, the Python and package steps are skipped (rebuilding them under a running bot breaks it)
and everything else runs.
The repository folder is added to your user PATH (-NoPath skips it), so `tbot` (tbot.cmd)
works in any new terminal.
With Smart App Control on, unsigned Python files can be blocked, so the environment is built
on Python signed by the Python Software Foundation: an existing install, or the official
"python" package from nuget.org, unpacked to PythonHome after its signature is checked.

.PARAMETER Python
A python.exe to build the environment on.

.PARAMETER PythonVersion
The Python version of the nuget package, used when no signed Python is found.

.PARAMETER PythonHome
Where the nuget Python goes; default %LOCALAPPDATA%\tbot\python-<version>.

.PARAMETER SkipDownload
No market data download (a few minutes on the first run); sessions download what they need.

.PARAMETER NoPath
Leave the user PATH alone; run .\tbot from this folder.

.PARAMETER Config
The config doctor checks at the end.

.EXAMPLE
.\tbot setup
.\tbot setup -SkipDownload
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [string] $Python = "",
    [string] $PythonVersion = "3.12.10",
    [string] $PythonHome = "",
    [switch] $SkipDownload,
    [switch] $NoPath,
    [string] $Config = "config\paper.yaml"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Step([string] $text) { Write-Output "== $text" }

function Test-Signed([string] $exe) {
    if (-not (Test-Path $exe -PathType Leaf)) { return $false }
    $signature = Get-AuthenticodeSignature $exe
    return $signature.Status -eq "Valid" -and
        $signature.SignerCertificate.Subject -match "O=Python Software Foundation"
}

function Invoke-Checked([string] $exe, [string[]] $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $exe $($arguments -join ' ')" }
}

function Add-UserPath([string] $folder, [string] $variable = "Path") {
    # Appends the folder to the user's PATH, keeping %VARIABLES% and the registry value type.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
    try {
        $raw = [string] $key.GetValue($variable, "",
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $entries = @($raw -split ";" | Where-Object { $_ })
        $expanded = @($entries | ForEach-Object {
            [Environment]::ExpandEnvironmentVariables($_).Trim('"').TrimEnd("\")
        })
        if ($expanded -contains $folder.TrimEnd("\")) { return "already on your PATH" }
        $other = $expanded | Where-Object {
            try { [IO.File]::Exists([IO.Path]::Combine($_, "tbot.cmd")) } catch { $false }
        } | Select-Object -First 1
        if ($other) { return "another tbot folder is on your PATH ($other): left as it is; use .\tbot here" }
        $kind = if ($raw) { $key.GetValueKind($variable) } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
        $key.SetValue($variable, ((@($entries) + $folder) -join ";"), $kind)
    } finally {
        $key.Close()
    }
    # Changing a user variable this way tells Windows to hand programs started from now on the new PATH.
    [Environment]::SetEnvironmentVariable("TBOT_SETUP", "1", "User")
    [Environment]::SetEnvironmentVariable("TBOT_SETUP", $null, "User")
    return "added $folder to your PATH: tbot works in every new terminal (in this one: .\tbot)"
}

Step "uv"
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw "uv is not installed. Install it with: winget install --id astral-sh.uv -e " +
        "(or see https://docs.astral.sh/uv/), then open a new PowerShell and run this again."
}
$uv = $uvCommand.Source
Write-Output (& $uv --version)

$venv = (Join-Path $repo ".venv").ToLowerInvariant()
$busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.ToLowerInvariant().StartsWith($venv)
})
$run = @("run", "--frozen")
if ($busy) {  # rebuilding or syncing the environment under a running bot breaks it
    Write-Output ("a bot or tool runs from .venv (process $($busy[0].Id)): the Python and " +
        "package steps are skipped; to redo them, stop it (tbot stop) and run this again")
    $run = @("run", "--frozen", "--no-sync")
}

Step "Python"
if ($busy) { Write-Output "skipped: .venv is in use" }
$smartAppControl = 0
try {
    $policy = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop
    if ($null -ne $policy.VerifiedAndReputablePolicyState) {
        $smartAppControl = [int] $policy.VerifiedAndReputablePolicyState
    }
} catch { }
if (-not $PythonHome) { $PythonHome = Join-Path $env:LOCALAPPDATA "tbot\python-$PythonVersion" }
if (-not $busy -and -not $Python -and $smartAppControl -ne 0) {
    Write-Output "Smart App Control is on: using Python signed by the Python Software Foundation"
    $short = ($PythonVersion -split "\.")[0..1] -join ""
    $candidates = @(
        (Join-Path $PythonHome "python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$short\python.exe"),
        (Join-Path $env:ProgramFiles "Python$short\python.exe")
    )
    $Python = $candidates | Where-Object { Test-Signed $_ } | Select-Object -First 1
    if (-not $Python) {
        $url = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
        Write-Output "downloading Python $PythonVersion from $url"
        $temp = Join-Path ([IO.Path]::GetTempPath()) ("tbot-python-" + [Guid]::NewGuid())
        New-Item -ItemType Directory -Force $temp | Out-Null
        try {
            $archive = Join-Path $temp "python.zip"
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
            Expand-Archive -Path $archive -DestinationPath (Join-Path $temp "package")
            $unpacked = Join-Path $temp "package\tools"
            if (-not (Test-Signed (Join-Path $unpacked "python.exe"))) {
                throw "the downloaded python.exe is not signed by the Python Software Foundation"
            }
            New-Item -ItemType Directory -Force (Split-Path -Parent $PythonHome) | Out-Null
            if (Test-Path $PythonHome) { Remove-Item -Recurse -Force $PythonHome }
            Move-Item $unpacked $PythonHome
        } finally {
            Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
        }
        $Python = Join-Path $PythonHome "python.exe"
    }
}
if (-not $busy -and $Python) {
    if (-not (Test-Path $Python -PathType Leaf)) { throw "no python.exe at $Python" }
    $wanted = Split-Path -Parent (Resolve-Path $Python).Path
    $current = ""
    if (Test-Path ".venv\pyvenv.cfg") {
        $line = Get-Content ".venv\pyvenv.cfg" | Where-Object { $_ -match "^home\s*=" } | Select-Object -First 1
        if ($line) { $current = ($line -split "=", 2)[1].Trim() }
    }
    if ($current -ne $wanted) {
        Write-Output "building .venv on $Python"
        Invoke-Checked $uv @("venv", "--python", $Python, "--clear")
    } else {
        Write-Output ".venv already uses $Python"
    }
}

Step "packages"
if ($busy) {
    Write-Output "skipped: .venv is in use"
} else {
    Invoke-Checked $uv @("sync", "--frozen")
    Invoke-Checked $uv @("run", "--frozen", "python", "-c", "import sqlite3, polars, pydantic_core, tbot")
}
Invoke-Checked $uv ($run + @("python", "-m", "tbot", "--version"))

Step "git hooks"
if (Test-Path ".git") {
    Invoke-Checked "git" @("config", "core.hooksPath", ".githooks")
    Write-Output "hooks from .githooks"
} else {
    Write-Output "not a git clone: skipped"
}

Step "tbot command"
if ($NoPath) {
    Write-Output "skipped (-NoPath): run .\tbot in this folder"
} else {
    Write-Output (Add-UserPath $repo)
}

Step ".env"
if (Test-Path ".env") {
    Write-Output ".env exists: left as it is"
} else {
    Copy-Item ".env.example" ".env"
    Write-Output "created .env from .env.example; tbot notify sets up Telegram alerts"
}

Step "market data"
if ($SkipDownload) {
    Write-Output "skipped (-SkipDownload); sessions download what they need at start"
} else {
    Invoke-Checked $uv ($run + @("python", "-m", "tbot", "download"))
}

Step "doctor $Config"
& $uv @run --quiet python -m tbot doctor $Config
if ($LASTEXITCODE -ne 0) {
    Write-Output "doctor reports a problem: fix the FAIL lines above, then run tbot doctor again"
    exit 1
}
Write-Output ""
Write-Output "Ready. Next (README, Everyday use):"
Write-Output "  tbot notify    Telegram alerts, guided (optional; WARN telegram above until then)"
Write-Output "  tbot paper     paper trading until you stop it with Ctrl+C"
Write-Output "  tbot status    every session at a glance, from another terminal"
