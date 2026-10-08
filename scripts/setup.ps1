<#
.SYNOPSIS
Sets up the bot on Windows: Python environment, git hooks, .env, market data, and a doctor run.

.DESCRIPTION
Safe to run again at any time; every step skips what is already done.
With Smart App Control on, unsigned Python files can be blocked, so the environment is built
on Python signed by the Python Software Foundation: an existing install, or the official
"python" package from nuget.org, unpacked to PythonHome after its signature is checked.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipDownload
#>
param(
    [string] $Python = "",  # a python.exe to build the environment on
    [string] $PythonVersion = "3.12.10",
    [string] $PythonHome = "",  # where the nuget Python goes; default %LOCALAPPDATA%\tbot\python-<version>
    [switch] $SkipDownload,  # no market data download (a few minutes on the first run)
    [string] $Config = "config\paper.yaml"  # the config doctor checks at the end
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

Step "uv"
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw "uv is not installed. Install it with: winget install --id astral-sh.uv -e " +
        "(or see https://docs.astral.sh/uv/), then open a new PowerShell and run this again."
}
$uv = $uvCommand.Source
Write-Output (& $uv --version)

Step "Python"
$smartAppControl = 0
try {
    $policy = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop
    if ($null -ne $policy.VerifiedAndReputablePolicyState) {
        $smartAppControl = [int] $policy.VerifiedAndReputablePolicyState
    }
} catch { }
if (-not $PythonHome) { $PythonHome = Join-Path $env:LOCALAPPDATA "tbot\python-$PythonVersion" }
if (-not $Python -and $smartAppControl -ne 0) {
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
if ($Python) {
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
Invoke-Checked $uv @("sync", "--frozen")
Invoke-Checked $uv @("run", "--frozen", "python", "-c", "import sqlite3, polars, pydantic_core, tbot")
Invoke-Checked $uv @("run", "--frozen", "python", "-m", "tbot", "--version")

Step "git hooks"
if (Test-Path ".git") {
    Invoke-Checked "git" @("config", "core.hooksPath", ".githooks")
    Write-Output "hooks from .githooks"
} else {
    Write-Output "not a git clone: skipped"
}

Step ".env"
if (Test-Path ".env") {
    Write-Output ".env exists: left as it is"
} else {
    Copy-Item ".env.example" ".env"
    Write-Output "created .env from .env.example: add the Telegram and heartbeat settings (README)"
}

Step "market data"
if ($SkipDownload) {
    Write-Output "skipped (-SkipDownload); sessions download what they need at start"
} else {
    Invoke-Checked $uv @("run", "--frozen", "python", "-m", "tbot", "download")
}

Step "doctor $Config"
& $uv run --frozen python -m tbot doctor $Config
if ($LASTEXITCODE -ne 0) {
    Write-Output "doctor reports a problem: fix the FAIL lines above, then run it again"
    exit 1
}
Write-Output "ready: uv run python -m tbot paper $Config (README, Paper trading)"
