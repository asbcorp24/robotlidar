@echo off
setlocal
cd /d "%~dp0"

if not exist "runtime\mediamtx.exe" (
  echo Preparing MediaMTX...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_mediamtx.ps1"
  if errorlevel 1 (
    echo.
    echo Failed to download MediaMTX through Windows TLS.
    echo Check Windows date/time and certificate store, then try again.
    echo You can also manually place mediamtx.exe into:
    echo   %~dp0runtime\mediamtx.exe
    pause
    exit /b 1
  )
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 emulator.py
) else (
  python emulator.py
)
if errorlevel 1 pause
