# Resume AI: setup, deploy and run

Follow the parts in order. Part A is done once for the web app; Part B is done once per PC for the desktop app.

Nothing in Part A has been applied to production yet. Every step there is yours to run.

| Thing | Where |
| --- | --- |
| Web app code | `E:\Projects\Resume_generator_New\resume_builder` |
| Desktop app code | `E:\Projects\Resume_generator_New\resume_ai_desktop` |
| Web app (live) | https://beastresumebuilder.vercel.app |
| Supabase project | `vmfpztidbbanemfpfnjy` |

---

## Part A. Web app (once)

### A1. Run the database migration: do this first

File: `resume_builder\supabase\migrations\20261002000000_desktop_sync.sql`

1. Open the Supabase dashboard → your project → **SQL Editor** → **New query**.
2. Paste the whole file → **Run**.

Or, if you use the Supabase CLI:

```powershell
cd E:\Projects\Resume_generator_New\resume_builder
pnpm run db:migrate
```

It only adds columns and tables, and it is safe to run twice. Deploy only after this has run: the new code reads the new columns.

### A2. Environment variables in Vercel

No new variable is needed. Check in Vercel → project → **Settings → Environment Variables** (Production) that these exist; they should already be there from the current web app:

| Variable | Needed for | Notes |
| --- | --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | everything | public |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | everything | public |
| `SUPABASE_SERVICE_ROLE_KEY` | desktop sync, admin view | secret, server only |
| `NEXT_PUBLIC_APP_URL` | links, OAuth redirects | `https://beastresumebuilder.vercel.app` |
| `GROQ_API_KEY` | AI check of company/role | optional: without it the desktop app shows "AI check unavailable" and uses the page's own values |
| `GROQ_MODEL` | | optional, default `openai/gpt-oss-20b` |
| `GROQ_TIMEOUT_MS` | | optional, default `20000` |

If you add or change a variable, redeploy afterwards.

### A3. Check the build locally (optional, recommended)

Needs Node 20+ and pnpm.

```powershell
cd E:\Projects\Resume_generator_New\resume_builder
pnpm install
pnpm run build:packages
pnpm run lint
pnpm run test
pnpm run build:web
```

Expected: 725 tests pass, and lint and the build finish without errors.

To run the web app on your PC instead of Vercel: `pnpm run dev:web` (http://localhost:3000). It uses `apps\web\.env.local`, which you already have.

### A4. Deploy

```powershell
cd E:\Projects\Resume_generator_New\resume_builder
git add -A
git commit -m "Desktop sync: API routes, Generated Resumes page, migration"
git push
```

Vercel builds from the push. (Or `vercel --prod` if you deploy from the CLI.)

Make sure `apps\web\public\sql-wasm.wasm` is included in the commit; the Import DB button needs it.

### A5. Admin role (for the desktop Admin tab)

In the Supabase SQL Editor:

```sql
select email, role from public.profiles where email = 'anthonyui.dev@gmail.com';
```

If it says `user`:

```sql
-- when no administrator exists yet
select public.bootstrap_admin('anthonyui.dev@gmail.com');

-- when another administrator already exists
select public.admin_set_user_role(
  (select id from public.profiles where email = 'anthonyui.dev@gmail.com'), 'admin');
```

### A6. Check the deploy

Open https://beastresumebuilder.vercel.app/generated. The page should load, say "Last synced from desktop: never" and show an **Import DB** button.

---

## Part B. Desktop app (each PC)

### B1. Install the requirements

- Windows 10 or 11
- Python 3.12
- uv (recommended):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal afterwards. Without uv the app still runs with plain pip (see B3).

### B2. Create `.env`

```powershell
cd E:\Projects\Resume_generator_New\resume_ai_desktop
copy .env.example .env
notepad .env
```

Fill in the two empty values and leave the rest:

| Key in `.env` | Value |
| --- | --- |
| `SUPABASE_URL` | same as the web app's `NEXT_PUBLIC_SUPABASE_URL` |
| `SUPABASE_PUBLISHABLE_KEY` | same as the web app's `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` |
| `WEB_APP_URL` | `https://beastresumebuilder.vercel.app` (already set) |
| `JOB_SEARCH_ALLOWED_HOSTS` | `jobright.ai,hiring.cafe,hiringcafe.com` (already set) |
| `AUTOMATION_STEP_SECONDS` | `5` (1–30; also in Settings) |
| `SYNC_INTERVAL_MINUTES` | `30` (5–720; also in Settings) |
| `CHATGPT_REPLY_TIMEOUT_SECONDS` | `540` |

Where to copy the two Supabase values from:

- Vercel → project → Settings → Environment Variables, or
- Supabase → Project Settings → API → **Project URL** and the **publishable** (anon) key, or
- your own `resume_builder\apps\web\.env.local`.

Only public values go in this file. Never put the service-role key or the Groq key here; the app refuses a secret key.

### B3. Install and start

```powershell
cd E:\Projects\Resume_generator_New\resume_ai_desktop
uv sync
.\run.bat
```

`run.bat` can also be double-clicked. Other ways to start:

```powershell
uv run python -m app.main
```

Without uv:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m app.main
```

### B4. First run

1. **Sign in** with your web app account (or Create account).
2. Gear icon next to your name → **Settings → Test connection**. Expected: "Web app reachable · Signed in · AI job check available".
3. **Job Search** → open JobRight or HiringCafe and sign in to the site yourself, once. Click a job; a toast says "Saved: …".
4. **Resume Generating** → **Import Prompt** (.txt or .md) → optionally **Import Resume** → **Start** → choose **Human Check** for the first run. Sign in to ChatGPT in the embedded browser when it asks.
5. Review company, role and job description, press **Next**, and let one resume finish.
6. **Generated Resumes** → the resume is listed; try **PDF**.
7. Open https://beastresumebuilder.vercel.app/generated → the same resume appears (or press **Sync now** in the sidebar chip first).

Once Human Check looks right, use **Automation** for the queue.

---

## Part C. Other commands

### Desktop: tests and checks

```powershell
cd E:\Projects\Resume_generator_New\resume_ai_desktop
uv run pytest
uv run ruff check app tests tools
uv run mypy
```

### Desktop: build an .exe folder

```powershell
uv run pyinstaller resume_ai.spec --noconfirm
copy .env.example dist\ResumeAI\.env.example
```

Result: `dist\ResumeAI\ResumeAI.exe` (about 500–600 MB). Zip the whole folder; the user copies `.env.example` to `.env` next to the exe and fills in the two values. Details: `docs\PACKAGING.md`.

### Chrome extension: rebuild (unchanged by this update)

```powershell
cd E:\Projects\Resume_generator_New\resume_builder
pnpm run build:packages
pnpm run build:extension
```

---

## Part D. Troubleshooting

| What you see | Cause and fix |
| --- | --- |
| "Setup needed: copy .env.example to .env…" on the login screen | `.env` is missing or the two Supabase values are empty. Do B2, then restart. |
| "This web app does not have the desktop API yet" | Part A is not deployed yet (or `WEB_APP_URL` points at the wrong site). |
| The Applications page on the web shows an error right after deploy | The migration (A1) was not run. Run it; no redeploy is needed. |
| "AI check unavailable" | `GROQ_API_KEY` is not set in Vercel. The app still works with the page's own company and role. |
| "running scripts is disabled on this system" | Use `run.bat`, or `powershell -ExecutionPolicy Bypass -File .\run.ps1`. |
| "Resume AI is already running" | Another window is open; check the taskbar or end `python.exe` in Task Manager. |
| "Offline" in the sidebar chip | No connection. The app keeps working on local data and syncs later. If a PC can never sync: sidebar chip → **Export DB copy…**, then **Import DB** on the web Generated Resumes page. |
| A job shows "Needs attention" | The page wanted a sign-in, a consent click or a human check. Open it in Job Search, fix it yourself, then run it again. |
| Google sign-in refuses in the embedded browser | Use e-mail and password for that site instead. |
| The Admin tab is missing | Do A5, then sign out and in again in the desktop app. |
| `uv` is not recognized | Reopen the terminal after installing uv, or use the pip commands in B3. |

Logs: `%APPDATA%\ResumeAI\logs\resume_ai.log` (ids, counts and timings only).
Your data: `%APPDATA%\ResumeAI\<account id>\` (database, browser profile, rendered documents).

## Rolling back

Roll the code back in Vercel → Deployments → previous deployment → **Promote**. Leave the new columns and tables in place; the old code ignores them.

## Not yet tested on the real sites

The app has not been run on your PC or against the live jobright.ai, hiring.cafe and chatgpt.com. Use Human Check for the first few jobs and compare what it read with the page.
