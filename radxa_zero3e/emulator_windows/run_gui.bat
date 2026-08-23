@echo off
setlocal
cd /d %~dp0

where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py -3
) else (
  set PY=python
)

if not exist .venv (
  %PY% -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python gui.py
