@echo off
rem Phase 0 spike: sets up Python packages on first run, then opens the test window.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo Setting up (first run only, this downloads about 250 MB)...
where uv >nul 2>nul
if %errorlevel%==0 (
    uv venv --python 3.12 .venv || goto fail
    uv pip install --python .venv\Scripts\python.exe "PySide6>=6.8" || goto fail
) else (
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv || goto fail
    .venv\Scripts\python.exe -m pip install --upgrade pip || goto fail
    .venv\Scripts\python.exe -m pip install "PySide6>=6.8" || goto fail
)

:run
.venv\Scripts\python.exe spike\browser_spike.py %*
goto end

:fail
echo.
echo Setup failed. Python 3.12 or newer is needed: https://www.python.org/downloads/
echo Copy the messages above and send them to Claude.
pause

:end
