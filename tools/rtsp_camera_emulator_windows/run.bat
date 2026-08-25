@echo off
setlocal EnableExtensions EnableDelayedExpansion
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

rem Python's bundled OpenSSL on some Windows installations has no usable
rem default CA file. Use certifi's current Mozilla CA bundle instead of
rem disabling TLS verification. This is required for HTTPS registration and
rem telemetry to the central RobotLiDAR server.
%PY_CMD% -c "import certifi" >nul 2>nul
if errorlevel 1 (
  echo Installing Python CA bundle ^(certifi^)...
  %PY_CMD% -m pip install --user --upgrade certifi
  if errorlevel 1 (
    echo.
    echo ERROR: Failed to install certifi.
    pause
    exit /b 1
  )
)

for /f "usebackq delims=" %%I in (`%PY_CMD% -c "import certifi; print(certifi.where())"`) do set "SSL_CERT_FILE=%%I"
if not defined SSL_CERT_FILE (
  echo ERROR: Could not determine certifi CA bundle path.
  pause
  exit /b 1
)
if not exist "%SSL_CERT_FILE%" (
  echo ERROR: CA bundle does not exist: %SSL_CERT_FILE%
  pause
  exit /b 1
)

echo Python CA bundle: %SSL_CERT_FILE%
%PY_CMD% emulator.py
if errorlevel 1 pause
