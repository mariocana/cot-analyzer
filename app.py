"""
app.py — Flask web interface (Phase 3, DB-backed)
==================================================
Reads everything from the local PostgreSQL DB. Data is populated on demand
via the SYNC button (see sync.py) — no cron/batch and no live scraping.

Routes:
  GET  /                       → main analysis page
  POST /run                    → trigger analysis (now nearly instant since
                                  it just reads the DB); returns job_id for
                                  compatibility with the existing frontend
  GET  /status/<job_id>        → job progress
  GET  /result/<job_id>        → JSON result
  GET  /api/data-status        → cheap DB freshness check (no CFTC call)
  POST /api/sync               → start a manual data sync (CFTC → DB)
  GET  /api/sync/status        → poll the running/last sync
  GET  /history/<ticker>       → historical chart page for one instrument
  GET  /api/history/<ticker>   → raw JSON for chart consumption
  GET  /health                 → health check

Run locally:
    python app.py
Production (Railway):
    gunicorn -c gunicorn.conf.py app:app
"""

import threading
import uuid
from flask import Flask, jsonify, render_template, request, abort

from core import (run_analysis, history_for,
                  ai_top3_overview, ai_asset_overview)
from db import available_report_dates
from instruments import INSTRUMENTS
from sync import data_status, run_sync
from datetime import date as _date


app = Flask(__name__)

# In-memory job store (single-worker only — see gunicorn.conf.py)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Separate store + lock for the (long-running) data sync job. Only one sync
# may run at a time.
_sync_job: dict = {"state": "idle", "status_msg": "", "result": None}
_sync_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background job runner (kept for compatibility with the polling frontend;
# the analysis itself is now nearly instant)
# ---------------------------------------------------------------------------
def _run_job(job_id: str, as_of_date: _date | None) -> None:
    def progress(msg: str) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status_msg"] = msg
    try:
        result = run_analysis(progress=progress, as_of_date=as_of_date)
        with _jobs_lock:
            _jobs[job_id]["state"] = "done"
            _jobs[job_id]["result"] = result
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]["state"] = "error"
            _jobs[job_id]["result"] = {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/report-dates")
def api_report_dates():
    """Return all distinct report dates in the DB, newest first.
    Frontend uses this to populate the date dropdown."""
    try:
        dates = available_report_dates()
        return jsonify({
            "ok": True,
            "dates": [d.isoformat() for d in dates],
            "count": len(dates),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Data sync (manual, replaces the old batch.py cron)
# ---------------------------------------------------------------------------
def _run_sync_job(force_backfill: bool) -> None:
    def progress(msg: str) -> None:
        with _sync_lock:
            _sync_job["status_msg"] = msg
    try:
        result = run_sync(force_backfill=force_backfill, progress=progress)
        with _sync_lock:
            _sync_job["state"] = "done"
            _sync_job["status_msg"] = "Sync complete"
            _sync_job["result"] = result
    except Exception as e:
        with _sync_lock:
            _sync_job["state"] = "error"
            _sync_job["status_msg"] = "Sync failed"
            _sync_job["result"] = {"ok": False, "error": str(e)}


@app.route("/api/data-status")
def api_data_status():
    """Cheap freshness check (no CFTC API call). Also reports whether a sync
    is currently running so the UI can restore its state on reload."""
    try:
        status = data_status()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    with _sync_lock:
        status["syncing"] = _sync_job["state"] == "running"
    status["ok"] = True
    return jsonify(status)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Start a data sync in the background.

    By default it first checks whether the DB is already up to date and skips
    the CFTC API entirely if so. Pass {"force": true} to fetch regardless (and
    {"backfill": true} to re-download two years)."""
    body = request.json or {} if request.is_json else {}
    force = bool(body.get("force"))
    backfill = bool(body.get("backfill"))

    with _sync_lock:
        if _sync_job["state"] == "running":
            return jsonify({"ok": False, "error": "A sync is already running",
                            "state": "running"}), 409

    # Cheap up-to-date check — avoid hitting the CFTC API when not needed.
    if not force and not backfill:
        try:
            status = data_status()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if status["current"]:
            return jsonify({"ok": True, "state": "skipped",
                            "reason": "already up to date",
                            "newest": status["newest"],
                            "expected": status["expected"]})

    with _sync_lock:
        _sync_job.update({"state": "running",
                          "status_msg": "Starting sync…",
                          "result": None})
    t = threading.Thread(target=_run_sync_job, args=(backfill,), daemon=True)
    t.start()
    return jsonify({"ok": True, "state": "running"})


@app.route("/api/sync/status")
def api_sync_status():
    """Poll the running/last sync job."""
    with _sync_lock:
        return jsonify({
            "state": _sync_job["state"],
            "status_msg": _sync_job["status_msg"],
            "result": _sync_job["result"],
        })


@app.route("/run", methods=["POST"])
def run():
    as_of_date: _date | None = None
    if request.is_json:
        body = request.json or {}
        date_str = body.get("report_date")
        if date_str:
            try:
                as_of_date = _date.fromisoformat(date_str)
            except ValueError:
                return jsonify({"ok": False,
                                "error": f"Invalid date format: {date_str}"}), 400
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        # Evict old jobs to keep memory bounded
        if len(_jobs) > 20:
            for k in sorted(_jobs.keys())[:10]:
                _jobs.pop(k, None)
        _jobs[job_id] = {"state": "running",
                         "status_msg": "Starting...",
                         "result": None}
    t = threading.Thread(target=_run_job,
                         args=(job_id, as_of_date), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _parse_as_of(body: dict) -> _date | None:
    """Helper: extract optional report_date from a request body."""
    date_str = body.get("report_date")
    if not date_str:
        return None
    try:
        return _date.fromisoformat(date_str)
    except ValueError:
        return None


@app.route("/ai/top3", methods=["POST"])
def ai_top3():
    """On-demand AI overview of the top-3 setups."""
    body = request.json or {}
    as_of_date = _parse_as_of(body)
    try:
        result = ai_top3_overview(as_of_date=as_of_date)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "text": str(e), "model": None}), 500


@app.route("/ai/asset", methods=["POST"])
def ai_asset():
    """On-demand AI overview of a single instrument's COT positioning."""
    body = request.json or {}
    ticker = (body.get("ticker") or "").upper()
    if not ticker or ticker not in INSTRUMENTS:
        return jsonify({"status": "error",
                        "text": f"Unknown or missing ticker: {ticker}",
                        "model": None}), 400
    as_of_date = _parse_as_of(body)
    try:
        result = ai_asset_overview(ticker, as_of_date=as_of_date)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "text": str(e), "model": None}), 500


@app.route("/status/<job_id>")
def status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"state": "not_found"}), 404
        return jsonify({"state": job["state"],
                        "status_msg": job["status_msg"]})


@app.route("/result/<job_id>")
def result(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job["state"] == "running":
            return jsonify({"ok": False, "error": "Still running"}), 202
        return jsonify(job["result"])


# ---------------------------------------------------------------------------
# Historical chart endpoints
# ---------------------------------------------------------------------------
@app.route("/api/history/<ticker>")
def api_history(ticker):
    """Raw JSON consumed by the chart on the frontend."""
    ticker = ticker.upper()
    if ticker not in INSTRUMENTS:
        return jsonify({"ok": False,
                        "error": f"Unknown instrument: {ticker}"}), 404
    return jsonify(history_for(ticker))


@app.route("/history/<ticker>")
def history_page(ticker):
    """Full-page chart view for one instrument."""
    ticker = ticker.upper()
    if ticker not in INSTRUMENTS:
        abort(404)
    inst = INSTRUMENTS[ticker]
    return render_template("history.html",
                           ticker=ticker,
                           name=inst.name,
                           category=inst.category)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
