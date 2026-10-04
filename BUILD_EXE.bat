@echo off
rem Resume AI: build the Windows app (dist\ResumeAI\ResumeAI.exe). Double-click this file.
rem Nothing in the app changes; this only packages it. Progress goes to build_exe.log.
setlocal
cd /d "%~dp0"
set LOG=%~dp0build_exe.log
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
echo == %date% %time% > "%LOG%"
echo Building ResumeAI.exe (takes a few minutes). Log: %LOG%

where uv >nul 2>nul
if errorlevel 1 (echo uv not found >> "%LOG%" & goto fail)

echo [1/4] packages
echo -- uv sync >> "%LOG%"
uv sync >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [2/4] pyinstaller
echo -- pyinstaller >> "%LOG%"
uv run pyinstaller resume_ai.spec --noconfirm >> "%LOG%" 2>&1
if errorlevel 1 goto fail
if not exist "dist\ResumeAI\ResumeAI.exe" (echo exe missing >> "%LOG%" & goto fail)

echo [3/4] settings
echo -- settings >> "%LOG%"
copy /y ".env.example" "dist\ResumeAI\.env.example" >> "%LOG%" 2>&1
if exist ".env" copy /y ".env" "dist\ResumeAI\.env" >> "%LOG%" 2>&1
if exist "dist\ResumeAI\.env" (echo .env copied >> "%LOG%") else (echo NO .env: run SETUP_DESKTOP.bat first >> "%LOG%")

echo [4/4] check
echo -- check >> "%LOG%"
dir "dist\ResumeAI\ResumeAI.exe" | find "ResumeAI.exe" >> "%LOG%" 2>&1
if exist "dist\ResumeAI\_internal\PySide6\QtWebEngineProcess.exe" (echo webengine process: yes >> "%LOG%") else (echo webengine process: MISSING >> "%LOG%")
if exist "dist\ResumeAI\_internal\PySide6\resources" (echo webengine resources: yes >> "%LOG%") else (echo webengine resources: MISSING >> "%LOG%")
if exist "dist\ResumeAI\_internal\app\automation\js\bridge.js" (echo page scripts: yes >> "%LOG%") else (echo page scripts: MISSING >> "%LOG%")
if exist "dist\ResumeAI\_internal\app\data\alembic" (echo migrations: yes >> "%LOG%") else (echo migrations: MISSING >> "%LOG%")

echo == BUILD OK >> "%LOG%"
echo Done: dist\ResumeAI\ResumeAI.exe
timeout /t 5 >nul
exit /b 0

:fail
echo == FAILED (see above) >> "%LOG%"
echo FAILED. See build_exe.log
timeout /t 10 >nul
exit /b 1
