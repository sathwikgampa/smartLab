"""Gunicorn settings for production (``gunicorn -c gunicorn.conf.py run:app``).

One process with many threads: the vector index (rag/vector_store.py) and the rate limiters
(api/ratelimit.py, auth) live in process memory, so a second worker would serve a different index and
count limits separately. The work is I/O-bound (Claude API, Supabase), so threads provide the concurrency.
Each open co-pilot stream (/api/chat/stream) occupies one thread for its duration.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "16"))
# Must exceed the longest request: AGENT_TIMEOUT_S (90 s) plus the stream's 30 s hard-limit margin.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "150"))
graceful_timeout = 30
# Longer than the edge proxy's idle timeout to the origin, so the proxy closes idle connections first.
keepalive = 75
accesslog = "-"
