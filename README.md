# Resume AI (desktop)

A Windows desktop app that:

1. **collects job postings** from JobRight.ai and HiringCafe.com in an embedded Chromium browser: every job you open is saved;
2. **generates a tailored resume for each saved job**, one at a time, by driving ChatGPT in that same browser with your prompt;
3. **tracks what happened next** (generated → applied → shortlisted / rejected) on a dashboard;
4. **syncs with the web app** (`resume_builder`, https://beastresumebuilder.vercel.app), where the resumes appear on the **Generated Resumes** page.

The app only reads job pages and generates resumes. It never applies to a job, never types a password, and never solves a CAPTCHA.

## 1. Before the first run: the web app

The desktop app needs the updated web app (new API routes, new page, one database migration).
Follow **`docs/WEB_DEPLOY_CHECKLIST.md`** once: run the migration in Supabase, then deploy. Until that is done the desktop app still opens and saves jobs, but validation, PDF/DOCX and sync report "This web app does not have the desktop API yet".

## 2. Setup on Windows

Requirements: Windows 10/11, Python 3.12. `uv` is recommended (https://docs.astral.sh/uv/); plain `pip` works too.

```powershell
cd E:\Projects\Resume_generator_New\resume_ai_desktop
copy .env.example .env        # then edit .env (see below)
uv sync                       # creates .venv and installs everything
.\run.bat                     # or: uv run python -m app.main
```

`run.ps1` does the same from PowerShell. If Windows refuses to run it ("running scripts is disabled"), use `run.bat`, or start it once with `powershell -ExecutionPolicy Bypass -File .\run.ps1`.

Without uv: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python -m app.main`.

### The two values in `.env`

`SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` are **the same public values the web app uses** as `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`. Copy them from either place:

- Vercel → your project → Settings → Environment Variables, or
- Supabase → Project Settings → API → *Project URL* and the *publishable* (anon) key.

Never put the service-role / secret key in `.env`. The app refuses one if it sees it, and no server secret (Groq key, service role) is ever needed on the PC.

## 3. Using it

| Tab | What it does |
| --- | --- |
| **Dashboard** | Generated / Applied / Shortlisted for Day, Week or Month (your local time), an activity chart and a status breakdown. Click a card or a legend entry to open the matching resumes. Works offline. |
| **Job Search** | JobRight.ai or HiringCafe.com in the embedded browser. Sign in to the sites yourself, once; the logins are kept. A job you click opens in a new tab and is saved; a toast says "Saved: …", "Skipped: LinkedIn job", "Skipped: no job description", "Already saved" or "Not saved: page needs sign-in". |
| **Saved Jobs** | The queue, oldest first. Search, sort, remove, "Needs attention" filter. |
| **Resume Generating** | **Import Prompt** (.txt/.md), optionally **Import Resume** (.pdf/.docx/.txt), then **Start** and pick **Automation** (continues by itself after a few seconds per step) or **Human Check** (you review company, role and job description and press Next). Sign in to ChatGPT in the browser the first time. |
| **Generated Resumes** | Every resume: copy the ChatGPT link, the JD link or the JSON; **PDF** / **DOCX** (rendered by the web app, saved to Downloads); status select; **Show details** (PDF preview, job description, JSON). |
| **Admin** | Admins only: the user list and **View Profile**, which shows one user's dashboard, saved jobs and resumes read-only as of their last sync. |

Other things worth knowing:

- **☰ / Ctrl+B** collapses the sidebar to a thin strip; the choice is remembered.
- **Sync** runs at start, every 30 minutes, after each generated resume and after each status change. The chip at the bottom of the sidebar shows the state; click it for details, **Sync now** and **Export DB copy…**.
- **No internet?** The app opens on local data and says "Offline". If sync can never be used on a PC, export a DB copy and use **Import DB** on the web app's Generated Resumes page.
- **Pages that need you.** A job page behind a sign-in, a consent wall, a "verify you are human" check or an error is never pushed through automatically. In Automation it is set aside after 20 seconds (it stays in Saved Jobs with a "Needs attention" badge) and the queue goes on; in Human Check the run waits for you to fix the page and press Retry. If ChatGPT itself asks you to sign in, the run pauses in both modes.
- **ChatGPT trouble.** If ChatGPT does not answer three jobs in a row, the run pauses and asks instead of working through the whole queue.
- **One window at a time.** A second start says "Resume AI is already running".
- **Settings** (gear next to your name): web app URL, name on resumes, sync interval, automation delay, Downloads folder, default dashboard period, Test connection, Open data folder.

### Where your data lives

`%APPDATA%\ResumeAI\<your account id>\`

- `resume_ai.db`: the local SQLite database (one per account).
- `browser\`: the embedded browser's profile (your site logins).
- `documents\`: the last rendered PDF/DOCX per resume, so re-downloading works offline.
- `..\logs\resume_ai.log`: ids, counts, lengths and timings only. Resume text, job descriptions, prompts, passwords and tokens are never logged.

Your **prompt and original resume stay on this PC**. They are never synced; they only go into the message sent to ChatGPT. The sign-in refresh token is kept in the Windows Credential Manager, not in a file.

## 4. Development

```powershell
uv sync                                  # includes the dev tools
uv run pytest                            # 436 tests, about 7 minutes (they drive a real offscreen Chromium)
uv run ruff check app tests tools
uv run ruff format app tests tools
uv run mypy                              # strict, on the non-UI modules listed in pyproject.toml
uv run python tools/preview.py preview   # PNG screenshots of the screens with sample data
```

Layout:

```
app/
  main.py            entry point (Chromium flags, qasync loop, login → main window)
  config.py          .env, data folders
  ui/                theme (tokens + QSS), icons, widgets, windows, pages, dialogs
  browser/           tabbed QtWebEngine component, profile, page-script helpers
  automation/        extractor.js + bridge.js and their Python drivers, capture rules, the pipeline
  data/              SQLAlchemy models, Store (every DB operation), Alembic migration
  sync/              web API client, sync engine
  services/          auth (Supabase), settings, documents, dashboard stats, job check, resume text
tests/               pytest (pytest-qt, respx); fixtures are local HTML pages
docs/                the UI design PDF, packaging notes, the web deployment checklist
spike/               the Phase 0 browser spike (kept for reference)
```

Design notes that matter when changing things:

- **The web app is the authority on formats.** Resume JSON is validated by `POST /api/desktop/validate-resume`, PDF/DOCX come from `POST /api/desktop/render`. Do not re-implement either here.
- **The ChatGPT bridge** (`app/automation/js/bridge.js`, `chatgpt.py`) is a port of the extension's `chatgpt-bridge.ts`. Every odd-looking step (8,000-character paste chunks, re-pasting when the composer did not grow, clearing a draft only when there is one, reading text without `innerText`) was measured on the live site. Do not simplify it.
- **Never stop the asyncio loop.** `qasync` stops by calling `QApplication.exit()`, which makes QtWebEngine tear its profile down; the next page then crashes. The app runs the loop once; the tests spin a local `QEventLoop` instead (`tests/conftest.py`).
- **A hidden page is throttled** to one timer tick per second, which stalls ChatGPT. The app starts Chromium with flags that prevent it and keeps the ChatGPT tab on screen while it is driven.
- **Sync contract.** `tools/make_contract_fixtures.py` writes this app's real sync body and a real database into the web repo's test fixtures; the web repo's `desktop-contract.test.ts` writes back what its real handlers answer (`tests/fixtures/contract/`). Re-run both when either side changes.

## 5. Known limits

- The JobRight.ai and HiringCafe.com page adapters were written without access to the signed-in sites and have not been run against them. They fall back to generic extraction; the Human Check mode shows exactly what was read. If a site changes, the place to adjust is `app/automation/js/extractor.js`.
- The ChatGPT driver has been exercised end to end against local copies of ChatGPT's page structure, and its page script is the one verified live in the Chrome extension, but this build has not itself been run against chatgpt.com.
- Google sign-in often refuses embedded browsers. Use e-mail/password or the site's own login in the embedded browser.
- Cross-origin iframes (an embedded job board inside a company page) cannot be read.
- Windows is the target. The code runs on Linux/macOS (the tests do), but only Windows was designed for.
