@echo off
setlocal
cd /d "%~dp0"

if not exist config.json (
  copy /Y config.example.json config.json >nul
  echo Created config.json from example.
  echo Edit camera_name and server addresses, then run this file again.
  pause
  exit /b 1
)

if not exist .venv (
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check -r requirements.txt
python emulator.py --config config.json
pause
