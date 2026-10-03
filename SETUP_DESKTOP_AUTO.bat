@echo off
rem Resume AI: unattended setup. Installs uv (official installer, user-level) if missing,
rem installs the packages (uv downloads Python 3.12 itself), creates .env, starts the app.
rem Progress goes to setup.log next to this file.
setlocal
cd /d "%~dp0"
set LOG=%~dp0setup.log
echo == %date% %time% > "%LOG%"

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if not errorlevel 1 goto haveuv
echo [1/4] Installing uv ...
echo -- install uv >> "%LOG%"
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
where uv >nul 2>nul
if errorlevel 1 (echo uv installed but not on PATH >> "%LOG%" & goto fail)
:haveuv
echo -- uv version >> "%LOG%"
uv --version >> "%LOG%" 2>&1

echo [2/4] Installing packages (uv sync, downloads Python 3.12 the first time) ...
echo -- uv sync >> "%LOG%"
uv sync >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [3/4] Creating .env ...
echo -- make_env >> "%LOG%"
uv run python tools\make_env.py >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [4/4] Checks ...
echo -- import check >> "%LOG%"
uv run python -c "import app.main, PySide6.QtWebEngineWidgets; import sys; print('python', sys.version)" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo -- starting app >> "%LOG%"
echo == SETUP OK >> "%LOG%"
echo Starting Resume AI ...
start "" uv run python -m app.main
timeout /t 3 >nul
exit /b 0

:fail
echo == FAILED (see above) >> "%LOG%"
echo FAILED. See setup.log
timeout /t 10 >nul
exit /b 1
