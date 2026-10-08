# Resume AI: user guide

Everything is set up. The web app is deployed, the database is migrated, and the desktop app is installed on your PC with its `.env` filled in. This guide is about *using* it.

## Starting the app

Double-click **`E:\Projects\Resume_generator_New\resume_ai_desktop\run.bat`** (or pin it to the taskbar). The first start after a reboot takes a few seconds longer while the embedded browser warms up.

Sign in with your web app account (the same e-mail and password you use at beastresumebuilder.vercel.app). The app remembers the session; you only type the password again after **Log out**.

## First-time setup (once, about 5 minutes)

1. **Settings** (gear icon next to your name, bottom of the sidebar) → **Test connection**. Expected: "Web app reachable · Signed in · AI job check available".
2. **Job Search** → click **JobRight** or **HiringCafe** → sign in to that site in the embedded browser, exactly as you would in Chrome. The login is kept.
3. **Resume Generating** → **Import Prompt** → pick your prompt file (.txt or .md). This is the text that is sent to ChatGPT with each job description. Optionally **Import Resume** (.pdf, .docx or .txt) if your prompt expects your current resume to be attached.
4. The first time you **Start** a run, the embedded browser opens chatgpt.com. Sign in there too (e-mail and password works best; Google sign-in often refuses embedded browsers). The login is kept.

Your prompt and resume stay on this PC. They are only ever sent to ChatGPT inside the message, never to the web app.

## Daily workflow

### 1. Collect jobs (Job Search tab)

Browse JobRight or HiringCafe in the embedded browser. **Every job you click is saved automatically**; a toast at the bottom confirms it:

| Toast | Meaning |
| --- | --- |
| Saved: Company · Role | Added to the queue. |
| Already saved | You opened it before. |
| Skipped: LinkedIn job | LinkedIn pages are never saved (they need their own login). |
| Skipped: no job description | The page had no readable description. |
| Not saved: page needs sign-in | Sign in on that page, then open the job again. |

You can keep browsing and clicking; nothing else is needed. Jobs open in tabs inside the app; close them when you like.

### 2. Check the queue (Saved Jobs tab)

The list of everything waiting to be generated, oldest first. Search, sort, tick rows and **Remove selected** for the ones you don't want. A **Needs attention** badge means the page could not be read automatically (sign-in wall, consent page, "verify you are human", error); open it in Job Search, fix it, and it will be retried on the next run.

**Check duplicates** (top right) cleans the list before you generate. It removes a saved job when

- the same company and role is already in the list (the oldest copy is kept, the later ones go), or
- a resume was already generated for that company and role.

"Same" ignores capitals, punctuation, "Inc"/"LLC" and notes such as "(Remote)", and reads "Sr." as "Senior"; "Senior Engineer" and "Engineer" at one company stay two jobs. A job with no company or role is never removed. When it finishes, a small message says how many went, for example **3 duplicate jobs removed (2 already saved, 1 already generated)**, or **No duplicates found**. The button is greyed out while a generation run is working. It takes well under a second even with 20,000 saved jobs.

**Shared job list (several profiles).** Every job a profile saves is also put on a shared list on the web app, and every other profile picks those jobs up into its own Saved Jobs: at each sync, and a moment after you open Saved Jobs or Resume Generating. A small message says how many arrived, and those rows carry a blue **Shared** chip. Each profile keeps its own progress:

- A job you already made a resume for in this profile (same link, or same company and role) is not added.
- **Remove** and **Check duplicates** only affect the profile you are in; a job you removed does not come back.
- When one profile makes a resume for a job, the job disappears only from that profile's list.
- Shared jobs keep the time they were first found, so every profile still generates oldest first.
- A profile reading the shared list for the first time gets the jobs found in the last 14 days.

### 3. Generate resumes (Resume Generating tab)

Press **Start** and choose a mode:

- **Human Check** (use this for your first few jobs): for each job the app opens the page, reads company, role and job description, and shows them in the review panel. Check them, correct the company or role if needed, and press **Next**. The app then pastes the prompt + job description into ChatGPT, waits for the JSON reply, validates it, and saves the resume. **Skip Job** leaves the job in the queue; **Cancel Job** removes it.
- **Automation**: the same, but each step continues by itself after a countdown (5 seconds by default, adjustable in Settings). Jobs that need a person are set aside after 20 seconds and marked **Needs attention**; the queue continues. You can leave the PC and come back.

While a run is on, the **Start** button becomes **Stop** (it stops at the next safe point; the current job finishes or is left in the queue, never half-saved). **Run log** shows what happened step by step.

What makes a run pause and wait for you, in both modes:

- ChatGPT asks you to sign in or shows a "verify you are human" check → sign in / solve it in the browser, then **Retry**.
- ChatGPT did not answer three jobs in a row → **Retry** or **Stop run**.

Keep the app window open while a run is on. You can switch tabs inside the app; the ChatGPT tab keeps working. Don't use the embedded ChatGPT tab yourself during a run.

One resume takes roughly 1–3 minutes, most of it waiting for ChatGPT.

### 4. Use the results (Generated Resumes tab)

Every generated resume, newest first. Per row:

- **PDF** / **DOCX**: downloads the formatted resume into your Downloads folder (set in Settings). It is rendered by the web app, so it looks exactly like the extension's output.
- **Copy** buttons: the ChatGPT conversation link, the job page link, or the resume JSON.
- **Status** dropdown: Generated → **Applied** → **Shortlisted** / **Rejected**. Set it when you apply and when you hear back; the Dashboard counts these.
- **Show details**: PDF preview, the job description that was used, and the JSON.

The app never applies to a job for you. Open the job link, apply as usual, then set the status to Applied.

### 5. Watch your numbers (Dashboard tab)

Generated / Applied / Shortlisted counts for today, this week or this month (your local time), an activity chart and a status breakdown. Click a card or a legend entry to jump to the matching resumes.

## Applying from the web app (Generated Resumes page)

Open https://beastresumebuilder.vercel.app/generated in Chrome with the MKResumeBuilder extension installed and signed in.

- The page lists only resumes you have **not applied with yet**. Once a resume is marked Applied (or Shortlisted / Rejected) it moves to the **Applications** page, with a small "Desktop" badge.
- **ChatGPT URL**: Copy. **JD URL**: click the address to copy it ("URL copied").
- **Apply** (right end of a row): opens the job's application page (the real form when the desktop app found it, otherwise the JD page) in a new tab, with the extension's side panel showing that resume: **Download PDF / DOCX**, **Save Resume**, the ChatGPT link and **Mark as applied**. Fill in the form, then press **Mark as applied**; the row moves to Applications. If the page itself says the application was submitted, the panel offers to mark it for you.
- **Apply jobs** (next to Import DB): starts with the oldest generated resume, the same way. After you mark it applied, the panel asks "Shall we go ahead with the next job?". **Yes** opens the next one in a new tab; **No** stops. Nothing opens without a Yes.
- If the side panel does not appear by itself, click the extension's toolbar icon on that tab: the tab already knows which resume it belongs to. If you click through to the form in a new tab, the panel follows.
- The panel also checks your application history for this job (the same link, its apply link, or the same company and role). When you already applied to it, it shows a red **You already applied to this job** with the date, and a **Remove & next job** button: it deletes this generated resume (the desktop app drops its copy at the next sync), closes the tab and opens the next generated resume. A similar but not identical role shows a yellow warning instead.
- "Not this job: open the generator" in the panel returns that tab to the normal generator.

## The web app

The resumes appear on **https://beastresumebuilder.vercel.app/generated** within seconds (sync runs after every generated resume and every status change, and every 30 minutes). Status changes made on the web come back to the desktop at the next sync. The sidebar chip at the bottom shows the sync state; click it for **Sync now** and details.

The Applications page on the web shows the extension's applications plus every desktop resume you have applied with; the ones still waiting are on Generated Resumes.

## Settings (gear icon)

| Setting | What it does |
| --- | --- |
| Web app URL | Leave as is unless you move the site. |
| Name on resumes | Your name as the web app renders it. |
| Sync interval | 5–720 minutes. |
| Automation delay | Countdown before each automatic step, 1–30 seconds. |
| Downloads folder | Where PDF/DOCX go. |
| Default dashboard period | Day / Week / Month. |
| Test connection | Checks the web app, your sign-in and the AI job check. |
| Open data folder | Your local database, browser profile and rendered documents. |

## Tips

- **Ctrl+B** collapses the sidebar.
- No internet? The app opens anyway and works on local data; it syncs when the connection is back. If a PC can never sync, use **Export DB copy…** (sync chip) and **Import DB** on the web Generated Resumes page.
- Only one window at a time: a second start says "Resume AI is already running".
- Your data: `%APPDATA%\ResumeAI\<account id>\` (database, browser logins, rendered documents). Logs: `%APPDATA%\ResumeAI\logs\resume_ai.log` (no resume or job text is ever logged).
- Admin tab (your account has the admin role): the user list and **View Profile** to see a user's dashboard, saved jobs and resumes as of their last sync, read-only.

## When something looks wrong

| Symptom | What to do |
| --- | --- |
| Wrong company or role in Human Check | Correct it in the review panel before pressing Next. If it happens on every job from one site, tell me which site and what it read; the page adapter needs adjusting. |
| "Needs attention" on many jobs | Open one in Job Search: usually the site logged you out. Sign in again. |
| ChatGPT reply rejected ("invalid resume JSON") | The prompt must make ChatGPT answer with the resume JSON only. Check the Run log and the prompt. |
| PDF/DOCX button does nothing | Settings → Test connection. The web app must be reachable and you signed in. |
| Sync chip shows an error | Click it for the reason. "Rejected" rows list resumes the web app refused and why. |
| App won't start | Run `SETUP_DESKTOP.bat` once; it repairs the environment. |

This build has not been exercised against the live JobRight, HiringCafe and ChatGPT pages yet, only against local copies of them. Run the first two or three jobs in Human Check mode and compare what the app read with the page. If anything is off, tell me what you saw and I'll adjust it.
