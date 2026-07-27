# Deploy guide — Railway production setup

This guide walks through setting up the full COT Positioning Desk on Railway,
starting from the state you have now (Postgres already created, sync tested
locally, web app tested locally).

The core architecture has **two services** in the same Railway project, plus
an **optional** cron service if you'd rather not sync manually:

```
Railway Project
├── Postgres           ← already exists
├── Web Service        ← runs Flask + gunicorn (always-on); data syncs via the ⟳ SYNC button
└── Cron Service       ← OPTIONAL: runs python sync.py every Saturday at 09:00 UTC
```

All services share the same private network, so the web and cron services see
the Postgres at `postgres.railway.internal` (low latency, no public exposure).

---

## Step 1 — Push the code to GitHub

Both Railway services will deploy from the same GitHub repo. If you don't
have one yet:

```bash
cd cot_v5
git init
git add .
git commit -m "Initial commit"
git branch -M main

# Create a new repo on github.com (private if you prefer), then:
git remote add origin git@github.com:<your-user>/cot-analyzer.git
git push -u origin main
```

> Tip: `.gitignore` already excludes `__pycache__`, `.env`, etc. The
> `DATABASE_URL` is **not** stored in the repo — it lives in Railway env vars.

---

## Step 2 — Create the Web Service

In the Railway dashboard, inside the project that already contains Postgres:

1. Click **+ New** → **GitHub Repo** → pick your `cot-analyzer` repo
2. Railway will auto-detect Python via `requirements.txt` and use `Procfile`
3. Once deployed, click the service → **Variables** → add:
   - `DATABASE_URL` → click **+ Add Reference** → select Postgres → `DATABASE_URL`
     (this auto-resolves to `postgres.railway.internal:5432/...`)
   - `ANTHROPIC_API_KEY` → optional, your Claude API key for AI review
4. Go to **Settings** → **Networking** → **Generate Domain** to get a public URL
5. Visit the URL → click **⟳ SYNC** to populate the DB, then **RUN ANALYSIS**

If the page loads but says "No data in DB", just hit **⟳ SYNC** once — it
downloads 2 years of history on the first run (~30-60 seconds).

---

## Step 3 — (Optional) Create the Cron Service

Syncing is manual by default — the **⟳ SYNC** button pulls fresh CFTC data
whenever you want it (and skips the API when the DB is already current). If
you'd rather automate the weekly refresh, add a **second** service in the
same project, pointing to the same repo but with a cron schedule.

1. Click **+ New** → **GitHub Repo** → pick the same repo (Railway lets you
   deploy the same repo as multiple services)
2. Once deployed, go to the service's **Settings**:
   - **Service Name**: rename to `cron-sync` so it's distinguishable
   - **Custom Start Command**: `python sync.py`
   - **Cron Schedule**: `0 9 * * 6` (Saturday 09:00 UTC)
   - **Restart Policy**: Never (cron jobs should exit, not restart)
3. **Variables** → add `DATABASE_URL` the same way as the web service
   (reference Postgres). **No** `ANTHROPIC_API_KEY` needed here.
4. Save. Railway will redeploy with the new config.

The next Saturday at 09:00 UTC, Railway will spin up a container, run
`python sync.py`, and shut it down when done (~30-60 seconds total).

> **Alternative**: instead of configuring via dashboard, you can rename the
> bundled `railway-cron.json` → `railway.json` for the cron service only.
> But the dashboard approach is cleaner since the two services share code.

---

## Step 4 — Populate the database

The simplest path: open the web app and click **⟳ SYNC**. The first sync
downloads 2 years of history; later syncs are incremental (and skipped
entirely when the DB is already up to date).

Prefer the CLI? Run it from your laptop against Railway's Postgres:
```bash
set DATABASE_URL=postgresql://...railway-public-url...
python sync.py
```

---

## Step 5 — (If using the cron) verify it's wired up

Railway logs are your friend here. On the cron service:

- **Deployments** tab shows past cron executions
- Click any execution → see the full `sync.py` stdout
- A successful run ends with a `Done in Xs — N new rows …` line and exit code 0

If a run fails (exit code 1), Railway marks it as failed in the dashboard
and you can read the error in the logs. The next scheduled run still
happens — failures don't cascade.

---

## Monitoring

Whether you sync manually or via the optional cron, things to check:

1. **After a new CFTC release** (Friday evening US time): hit **⟳ SYNC** — the
   status line turns green ("up to date") once the latest report is stored.
2. **Visit the web app**: the "Report date" in the meta bar should advance
   by one week after each successful sync.
3. **DB row count**: grows by ~32 rows per new weekly report (one per instrument).

If the SYNC status stays "stale" after a sync:
- read the status line — it reports per-instrument errors and the error count
- check the CFTC API isn't down (rare; their uptime is very good)
- if using the cron, confirm the service is **enabled** and the schedule is `0 9 * * 6`

---

## Cost expectations

Railway free tier covers all of this comfortably:

| Service | Resource use | Cost notes |
|---|---|---|
| Postgres | <100 MB storage | Free tier covers it |
| Web | ~50 MB RAM at idle, 100 MB when handling a request | Negligible CPU |
| Cron (optional) | Runs ~60 seconds/week | Almost free |

With AI review enabled (when you turn it on), Claude API ≈ $0.04/run × 4
runs/month ≈ **$0.16/month**.

---

## Updating the code

Once both services are wired to your GitHub repo:

```bash
# On your laptop
git add .
git commit -m "Tweak threshold for momentum"
git push
```

Railway auto-redeploys **both** services on push. The cron service
respects the schedule (won't run extra times because of a redeploy).

---

## Rollback

If a deploy breaks production:

1. Railway dashboard → web service → **Deployments**
2. Find the last good deployment → click `⋯` → **Redeploy this version**

Same flow for the cron service.

The Postgres data is **independent** of code deployments — rolling back
the app doesn't touch the DB. The DB is only modified by a sync (the ⟳ SYNC
button or `python sync.py`), and even those are idempotent (upserts
overwrite, never duplicate).
