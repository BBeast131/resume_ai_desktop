@echo off
rem Resume AI: start the app from source (double-click).
cd /d "%~dp0"
where uv >nul 2>nul
if %errorlevel%==0 (
  uv sync
  uv run python -m app.main
) else (
  if not exist .venv python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  .venv\Scripts\python.exe -m app.main
)
if %errorlevel% neq 0 pause
