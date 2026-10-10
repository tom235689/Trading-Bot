@echo off
rem tbot from any folder: it runs in this one, where .env, config, data, and logs live.
rem "tbot setup", "tbot update", and "tbot autostart" run the scripts in scripts\.
setlocal
set "ROOT=%~dp0"
set "SCRIPT="
if /i "%~1"=="setup" set "SCRIPT=setup.ps1"
if /i "%~1"=="update" set "SCRIPT=update.ps1"
if /i "%~1"=="autostart" set "SCRIPT=install_task.ps1"
if not defined SCRIPT goto tbot
if "%~2"=="-h" goto scripthelp
if "%~2"=="--help" goto scripthelp
if "%~2"=="-?" goto scripthelp
goto script

:tbot

where uv >nul 2>nul
if errorlevel 1 goto nouv
if not exist "%ROOT%.venv\pyvenv.cfg" goto nosetup
pushd "%ROOT%"
uv run --quiet --frozen python -m tbot %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:script
rem The rest of the line as typed: everything after the first word.
set "ALL=%*"
call set "ARGS=%%ALL:*%~1=%%"
rem One line: "tbot update" may replace this file, and cmd reads a file again after each
rem command. "call" reads ERRORLEVEL when the script has ended; no delayed expansion, so "!"
rem in a value stays.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\%SCRIPT%" %ARGS% & call exit /b %%ERRORLEVEL%%

:scripthelp
set "HELP_SCRIPT=%ROOT%scripts\%SCRIPT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Help -Detailed -Name $env:HELP_SCRIPT"
exit /b 0

:nouv
echo uv is not installed: winget install --id astral-sh.uv -e, then open a new terminal 1>&2
exit /b 1

:nosetup
echo tbot is not set up in this folder yet: run "tbot setup" first 1>&2
exit /b 1
