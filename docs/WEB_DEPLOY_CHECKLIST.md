# Web app: deployment checklist for the desktop update

Everything here is run **by you**. Nothing has been applied to production.

The code is already in `resume_builder` (new API routes under `/api/desktop`, the admin read route, the `/generated` page with Import DB, and one migration).

## 1. Run the database migration (Supabase) — do this FIRST

File: `resume_builder/supabase/migrations/20261002000000_desktop_sync.sql`

Supabase dashboard → SQL Editor → paste the whole file → Run. (Or `supabase db push` if you use the CLI.)

What it does:

- adds to `public.applications`: `source` (`'extension'` by default, or `'desktop'`), `client_id`, `jd_url`, `apply_url`, `shortlisted_at`, `rejected_at`, `status_changed_at`, with their checks and two indexes;
- creates `public.desktop_saved_jobs`, `public.desktop_tombstones`, `public.desktop_sync_status` (row level security on, owner-only).

It only adds things. Existing rows keep working (every new column is nullable or has a default), and it is safe to run twice. It was tested on a fresh PostgreSQL 16 with all earlier migrations applied: it applies cleanly, twice, and existing rows stay valid.

**Order matters:** the new code reads the new columns (the Applications page filters on `source`), so deploy only after the migration has run.

## 2. Environment variables (Vercel)

No new variable is required. The desktop job check uses the Groq settings the web app already has:

- `GROQ_API_KEY` — if it is set, role/company are checked by the model; if it is not, the check falls back to the page's own values and the desktop app shows "AI check unavailable".
- `GROQ_MODEL` (default `openai/gpt-oss-20b`) and `GROQ_TIMEOUT_MS` are optional.

Confirm in Vercel → Settings → Environment Variables that `GROQ_API_KEY` exists for Production if you want the AI check.

## 3. Verify locally (optional but recommended)

```powershell
cd E:\Projects\Resume_generator_New\resume_builder
pnpm install            # one new dependency in apps/web: sql.js (for Import DB)
pnpm run build:packages
pnpm run lint
pnpm run test           # 725 tests
pnpm run build:web
```

## 4. Deploy

Commit and push as usual (Vercel builds from the repository), or `vercel --prod`.

`apps/web/public/sql-wasm.wasm` must be committed: Import DB loads it from the app's own origin. A test fails if it ever differs from the installed `sql.js` package.

## 5. After the deploy

1. Open `https://beastresumebuilder.vercel.app/generated`: the page loads, shows "Last synced from desktop: never" and an **Import DB** button.
2. In the desktop app: Settings → **Test connection** → "Web app reachable · Signed in · AI job check available".
3. Generate one resume on the desktop → it appears on `/generated` within a few seconds (or press **Sync now**).
4. Change its status on the web page → **Sync now** on the desktop → the status changes there too.

## 6. Admin role for anthonyui.dev@gmail.com

The desktop Admin tab shows for accounts whose web profile has `role = 'admin'` (the server checks it again on every admin request). To check, in the Supabase SQL Editor:

```sql
select email, role from public.profiles where email = 'anthonyui.dev@gmail.com';
```

If it says `user`:

```sql
-- when no administrator exists yet
select public.bootstrap_admin('anthonyui.dev@gmail.com');

-- when another administrator already exists (bootstrap_admin refuses then)
select public.admin_set_user_role((select id from public.profiles where email = 'anthonyui.dev@gmail.com'), 'admin');
```

Sign out and in again in the desktop app afterwards.

## Rolling back

The code can be rolled back in Vercel at any time; the added columns and tables are harmless to the old code. Do not drop the columns while desktop records exist.
