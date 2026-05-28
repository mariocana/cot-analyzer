"""
gunicorn.conf.py — production server config
Reads PORT from the environment in Python, so it works regardless of whether
the host expands $PORT in the start command (Railway, Render, Heroku, etc.).
"""
import os

# Bind to the port the platform provides, default 8000 locally.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Single worker: job state lives in process memory (see app.py).
workers = 1

# A full scrape takes ~40s; default 30s timeout would kill it.
timeout = 120

# Log to stdout/stderr so the platform captures it.
accesslog = "-"
errorlog = "-"
