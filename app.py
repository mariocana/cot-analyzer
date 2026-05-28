"""
app.py — Flask web interface for the COT analyzer
==================================================
A deployable web app with a "Run Analysis" button that triggers a live
scrape in a background thread. The frontend polls /status for progress and
fetches /result when complete.

Run locally:
    python app.py
    # then open http://localhost:5000

Deploy (production):
    gunicorn -w 1 -b 0.0.0.0:8000 app:app
    # NOTE: use -w 1 (single worker) because job state is in-process memory.
    # For multi-worker, externalize job state to Redis.
"""

import threading
import uuid
from flask import Flask, jsonify, request, render_template

from core import run_analysis

app = Flask(__name__)

# In-memory job store. Single-worker only (see deploy note above).
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_job(job_id: str, run_ai: bool):
    def progress(msg):
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    """Start a new analysis job. Returns a job_id."""
    run_ai = request.json.get("run_ai", True) if request.is_json else True
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        # Avoid stacking too many old jobs in memory
        if len(_jobs) > 20:
            oldest = sorted(_jobs.keys())[:10]
            for k in oldest:
                _jobs.pop(k, None)
        _jobs[job_id] = {"state": "running", "status_msg": "Starting...",
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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
