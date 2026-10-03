@echo off
rem Resume AI: one-time setup, then start. Double-click this file.
rem   1. installs the packages   2. creates .env   3. starts the app
setlocal
cd /d "%~dp0"
title Resume AI setup

where uv >nul 2>nul
if errorlevel 1 goto pip

echo [1/3] Installing packages (uv sync). The first time takes a few minutes...
call uv sync
if errorlevel 1 goto fail
echo.
echo [2/3] Creating .env ...
call uv run python tools\make_env.py
if errorlevel 1 goto envfail
echo.
echo [3/3] Starting Resume AI ...
call uv run python -m app.main
if errorlevel 1 goto fail
goto done

:pip
where python >nul 2>nul
if errorlevel 1 goto nopython
echo uv was not found, using pip instead.
echo [1/3] Installing packages. The first time takes a few minutes...
if not exist .venv python -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo [2/3] Creating .env ...
.venv\Scripts\python.exe tools\make_env.py
if errorlevel 1 goto envfail
echo.
echo [3/3] Starting Resume AI ...
.venv\Scripts\python.exe -m app.main
if errorlevel 1 goto fail
goto done

:nopython
echo.
echo Neither uv nor Python was found.
echo Install uv, then run this file again:
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
pause
exit /b 1

:envfail
echo.
echo .env could not be created automatically (see the message above).
echo Copy .env.example to .env, fill in SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY, then run this file again.
pause
exit /b 1

:fail
echo.
echo Something failed (see the message above). Nothing else was changed.
pause
exit /b 1

:done
endlocal
