@echo off
rem tbot from any folder: it runs in this one, where .env, config, data, and logs live.
rem "tbot setup", "tbot update", and "tbot autostart" run the scripts in scripts\.
setlocal
set "ROOT=%~dp0"
set "SCRIPT="
if /i "%~1"=="setup" set "SCRIPT=setup.ps1"
if /i "%~1"=="update" set "SCRIPT=update.ps1"
if /i "%~1"=="autostart" set "SCRIPT=install_task.ps1"
if defined SCRIPT goto script

where uv >nul 2>nul
if errorlevel 1 goto nouv
if not exist "%ROOT%.venv\pyvenv.cfg" goto nosetup
pushd "%ROOT%"
uv run --quiet --frozen python -m tbot %*
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:script
shift
set "ARGS="
:collect
if "%~1"=="" goto run
set ARGS=%ARGS% "%~1"
shift
goto collect
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\%SCRIPT%" %ARGS%
exit /b %ERRORLEVEL%

:nouv
echo uv is not installed: winget install --id astral-sh.uv -e, then open a new terminal 1>&2
exit /b 1

:nosetup
echo tbot is not set up in this folder yet: run "tbot setup" first 1>&2
exit /b 1
