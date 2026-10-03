# Packaging Resume AI for Windows (PyInstaller)

The app runs fine from source (`run.ps1`). To hand someone a folder they can double-click:

```powershell
uv sync                         # dev group includes pyinstaller
uv run pyinstaller resume_ai.spec --noconfirm
```

The result is `dist\ResumeAI\ResumeAI.exe` plus its libraries (a one-folder build; about 500–600 MB because it contains Chromium). Zip the whole `dist\ResumeAI` folder.

## What the spec takes care of

- `app/automation/js/*.js` (the extractor and the ChatGPT bridge) and `app/data/alembic` (the database migration) are bundled as data files; the code finds them through `app.config.resource_path`, which reads PyInstaller's `_MEIPASS`.
- `keyring.backends.Windows` is a hidden import: keyring picks its backend at run time, so PyInstaller cannot see it.
- PySide6's own PyInstaller hooks bring QtWebEngine (`QtWebEngineProcess.exe`, `resources\`, `translations\qtwebengine_locales`). If the embedded browser shows a blank page in a packaged build, check that those three exist under `dist\ResumeAI\PySide6\`.

## Configuration for a packaged build

`.env` is read from the folder that holds `ResumeAI.exe` (and, as a fallback, from `%APPDATA%\ResumeAI\.env`). PyInstaller 6 puts bundled data under `dist\ResumeAI\_internal\`, so after building copy the template next to the exe yourself:

```powershell
copy .env.example dist\ResumeAI\.env.example
```

The user copies it to `.env` and fills in the two public Supabase values. Only public values belong there.

## Checks before giving a build to anyone

1. Start it on a PC without Python installed.
2. Sign in, open Job Search (a page renders), import a prompt, run one job in Human Check mode.
3. Open a resume's details: the PDF preview shows (this exercises QtPdf).
4. Log out and in again: the session is restored from the Windows Credential Manager.

## Notes

- Do not use `--onefile`: Chromium unpacks to a temp folder on every start, which is slow and trips some antivirus tools.
- Code signing is not set up. An unsigned exe shows a SmartScreen warning on first run.
- The data folder (`%APPDATA%\ResumeAI`) is not touched by installing a new build; the database migrates itself on start.
