"""Small in-memory sliding-window rate limiter for the lab co-pilot endpoints.

Limits are per client address and per endpoint group, kept in this process's memory. With several
server processes each one counts separately; a shared store would be needed for a strict global
limit (no new service is added here).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from functools import wraps

from flask import current_app, request

from .common import ApiError


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > window_s:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # forget idle clients
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


def rate_limited(group: str, limit_key: str = "AGENT_RATE_LIMIT_PER_MIN"):
    """Reject with 429 once a client exceeds ``limit_key`` (a config value) requests per minute in ``group``."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            limiter = current_app.extensions.setdefault("smartlab_rate_limiter", RateLimiter())
            limit = int(current_app.config.get(limit_key, 20))
            if limit > 0 and not limiter.allow(f"{group}:{request.remote_addr}", limit):
                raise ApiError("Too many requests. Please wait a minute and try again.", 429)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
