# Deploy guide — Railway production setup

This guide walks through setting up the full COT Positioning Desk on Railway,
starting from the state you have now (Postgres already created, batch tested
locally, web app tested locally).

The final architecture has **three services** in the same Railway project:

```
Railway Project
├── Postgres           ← already exists
├── Web Service        ← runs Flask + gunicorn (always-on)
└── Cron Service       ← runs batch.py every Saturday at 09:00 UTC
```

All three share the same private network, so the web and cron services see
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
5. Visit the URL → click **RUN ANALYSIS** → you should see data from the DB

If the page loads but says "No data in DB", you just need to run the batch
manually once before the first scheduled cron (see Step 4).

---

## Step 3 — Create the Cron Service

This is a **second** service in the same project, pointing to the same repo
but with a different start command and a cron schedule.

1. Click **+ New** → **GitHub Repo** → pick the same repo (Railway lets you
   deploy the same repo as multiple services)
2. Once deployed, go to the service's **Settings**:
   - **Service Name**: rename to `cron-batch` so it's distinguishable
   - **Custom Start Command**: `python batch.py`
   - **Cron Schedule**: `0 9 * * 6` (Saturday 09:00 UTC)
   - **Restart Policy**: Never (cron jobs should exit, not restart)
3. **Variables** → add `DATABASE_URL` the same way as the web service
   (reference Postgres). **No** `ANTHROPIC_API_KEY` needed here.
4. Save. Railway will redeploy with the new config.

The next Saturday at 09:00 UTC, Railway will spin up a container, run
`python batch.py`, and shut it down when done (~30-60 seconds total).

> **Alternative**: instead of configuring via dashboard, you can rename the
> bundled `railway-cron.json` → `railway.json` for the cron service only.
> But the dashboard approach is cleaner since the two services share code.

---

## Step 4 — Manual first run (optional)

If the DB already has data from your local runs, skip this. If it's empty:

Option A — from your laptop, against Railway's Postgres:
```bash
set DATABASE_URL=postgresql://...railway-public-url...
python batch.py
```

Option B — from Railway itself. In the cron service:
- **Settings** → temporarily disable cron schedule
- **Deployments** → **Redeploy** (this triggers a one-shot run)
- After it finishes, re-enable the cron schedule

---

## Step 5 — Verify the cron is wired up

Railway logs are your friend here. On the cron service:

- **Deployments** tab shows past cron executions
- Click any execution → see the full `batch.py` stdout
- A successful run ends with `=== Done ===` and exit code 0

If a run fails (exit code 1), Railway marks it as failed in the dashboard
and you can read the error in the logs. The next scheduled run still
happens — failures don't cascade.

---

## Monitoring

After the first few weeks you'll have a rhythm. Things to check:

1. **Saturday morning**: the deployments tab should show a new green run
2. **Visit the web app**: the "Report date" in the meta bar should advance
   by one week each Saturday
3. **DB row count**: should grow by ~20 rows each week (one per instrument)

If a Saturday run doesn't appear:
- check the cron service is **enabled** (Railway sometimes pauses inactive services)
- check the schedule is still `0 9 * * 6`
- check the CFTC API isn't down (rare; their uptime is very good)

---

## Cost expectations

Railway free tier covers all of this comfortably:

| Service | Resource use | Cost notes |
|---|---|---|
| Postgres | <100 MB storage | Free tier covers it |
| Web | ~50 MB RAM at idle, 100 MB when handling a request | Negligible CPU |
| Cron | Runs ~60 seconds/week | Almost free |

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
the app doesn't touch the DB. The DB is only modified by `batch.py` runs,
and even those are idempotent (upserts overwrite, never duplicate).
