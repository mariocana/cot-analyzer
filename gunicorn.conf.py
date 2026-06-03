"""
gunicorn.conf.py — production server config
Reads PORT from env in Python, so $PORT expansion issues never bite us.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1   # single worker: in-process job state
timeout = 120
accesslog = "-"
errorlog = "-"
