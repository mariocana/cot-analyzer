"""
app.py — Flask web interface (Phase 3, DB-backed)
==================================================
Reads everything from the local PostgreSQL DB (populated by batch.py).
No more live scraping.

Routes:
  GET  /                       → main analysis page
  POST /run                    → trigger analysis (now nearly instant since
                                  it just reads the DB); returns job_id for
                                  compatibility with the existing frontend
  GET  /status/<job_id>        → job progress
  GET  /result/<job_id>        → JSON result
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

from core import run_analysis, history_for
from instruments import INSTRUMENTS


app = Flask(__name__)

# In-memory job store (single-worker only — see gunicorn.conf.py)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background job runner (kept for compatibility with the polling frontend;
# the analysis itself is now nearly instant)
# ---------------------------------------------------------------------------
def _run_job(job_id: str, run_ai: bool) -> None:
    def progress(msg: str) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status_msg"] = msg
    try:
        result = run_analysis(progress=progress, run_ai=run_ai)
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


@app.route("/run", methods=["POST"])
def run():
    run_ai = True
    if request.is_json:
        run_ai = request.json.get("run_ai", True)
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        # Evict old jobs to keep memory bounded
        if len(_jobs) > 20:
            for k in sorted(_jobs.keys())[:10]:
                _jobs.pop(k, None)
        _jobs[job_id] = {"state": "running",
                         "status_msg": "Starting...",
                         "result": None}
    t = threading.Thread(target=_run_job, args=(job_id, run_ai), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


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
