@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Prepare MediaMTX first through Windows TLS / PowerShell.
if exist "%~dp0prepare_mediamtx.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_mediamtx.ps1"
  if errorlevel 1 (
    echo.
    echo ERROR: MediaMTX preparation failed.
    pause
    exit /b 1
  )
)

rem Select Python launcher.
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  set "PY_CMD=python"
)

rem HTTPS registration/telemetry is executed by windows_launcher.py through
rem the system curl.exe. On Windows curl uses Schannel and the Windows
rem certificate store; TLS verification remains enabled.
where curl.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: curl.exe was not found. Windows 10/11 normally includes it.
  pause
  exit /b 1
)

echo HTTPS transport: Windows curl.exe / Schannel
%PY_CMD% windows_launcher.py
if errorlevel 1 pause
