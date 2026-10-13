@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found: .venv\Scripts\python.exe
  echo Create .venv and install requirements.txt first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.desktop
if errorlevel 1 pause
