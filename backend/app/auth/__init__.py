"""Authentication: server-side sessions in an httpOnly cookie, login gate for the API,
rate limiting and password-reset mail.

Session and reset tokens are 256-bit random values; only their SHA-256 hashes are stored,
so a database leak does not expose usable tokens.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import smtplib
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, g, jsonify, request

from ..db import get_db

log = logging.getLogger(__name__)

# API paths reachable without signing in.
PUBLIC_API = ("/api/health", "/api/auth/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ------------------------------------------------------------------ helpers
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_time(dt: datetime) -> str:
    """Same text format as SQLite's datetime('now'), so string comparison orders correctly."""
    return dt.isoformat(sep=" ", timespec="seconds")


def new_token() -> tuple[str, str]:
    """Return (token for the client, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "profile_id": row.get("profile_id"), "name": row["name"], "email": row["email"],
        "organization": row.get("organization"), "role": row["role"],
    }


def client_ip() -> str:
    return request.remote_addr or "unknown"


# ------------------------------------------------------------------ sessions
def create_session(conn, user_id: int) -> str:
    token, digest = new_token()
    expires = utc_now() + timedelta(days=current_app.config["SESSION_DAYS"])
    conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (fmt_time(utc_now()),))
    conn.execute("INSERT INTO auth_sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                 (user_id, digest, fmt_time(expires)))
    return token


def session_user(conn) -> dict[str, Any] | None:
    token = request.cookies.get(current_app.config["AUTH_COOKIE_NAME"])
    if not token or len(token) > 200:
        return None
    row = conn.execute(
        "SELECT u.id, u.email, u.name, u.organization, u.role, u.profile_id, s.expires_at "
        "FROM auth_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["expires_at"] < fmt_time(utc_now()):
        return None
    return row


def set_session_cookie(resp: Response, token: str) -> Response:
    cfg = current_app.config
    secure = cfg["AUTH_COOKIE_SECURE"] == "true" or (cfg["AUTH_COOKIE_SECURE"] == "auto" and request.is_secure)
    resp.set_cookie(cfg["AUTH_COOKIE_NAME"], token, max_age=cfg["SESSION_DAYS"] * 86400, httponly=True,
                    secure=secure, samesite="Lax", path="/")
    return resp


def clear_session_cookie(resp: Response) -> Response:
    resp.delete_cookie(current_app.config["AUTH_COOKIE_NAME"], path="/", samesite="Lax")
    return resp


def current_user() -> dict[str, Any] | None:
    return g.get("user")


def actor_name() -> str | None:
    """Who to record in the audit log: the signed-in user, taken from the server-side session only.

    Never read from the request body, which a client could set to any name. ``None`` when no one is
    signed in (only possible with ``SMARTLAB_AUTH_REQUIRED`` off).
    """
    user = current_user()
    return user["name"] if user else None


# ------------------------------------------------------------------ rate limiting
class RateLimiter:
    """Sliding-window counter kept in process memory (per worker). Good enough to slow down
    password guessing; put a shared limiter in front for multi-process deployments."""

    def __init__(self, window_seconds: int = 900):
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> bool:
        """Record an attempt; return False when the limit for the window is exceeded."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# ------------------------------------------------------------------ mail
def send_reset_email(app: Flask, to: str, name: str, link: str) -> None:
    """Send the reset link by SMTP when configured; otherwise write it to the server log.
    Runs in a background thread so response time does not reveal whether the account exists."""
    cfg = app.config
    subject = "Reset your SmartLab AI password"
    minutes = cfg["RESET_TOKEN_MINUTES"]
    body = (f"Hello {name},\n\nWe received a request to reset the password for your SmartLab AI account.\n"
            f"Open this link within {minutes} minutes to choose a new password:\n\n{link}\n\n"
            "If you didn't ask for this, you can ignore this email; your password stays the same.\n")
    if cfg.get("TESTING"):
        app.extensions.setdefault("smartlab_outbox", []).append({"to": to, "subject": subject, "body": body, "link": link})
        return
    if not cfg.get("SMTP_HOST"):
        log.warning("SMTP is not configured; password reset link for %s: %s", to, link)
        return

    def deliver() -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER") or "no-reply@localhost"
        msg["To"] = to
        msg.set_content(body)
        try:
            with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as smtp:
                if cfg.get("SMTP_STARTTLS"):
                    smtp.starttls()
                if cfg.get("SMTP_USER"):
                    smtp.login(cfg["SMTP_USER"], cfg.get("SMTP_PASSWORD") or "")
                smtp.send_message(msg)
        except Exception:  # noqa: BLE001 - never surface mail errors to the requester
            log.exception("Could not send password reset email to %s", to)

    threading.Thread(target=deliver, daemon=True).start()


# ------------------------------------------------------------------ request gate
def _origin_allowed(origin: str) -> bool:
    allowed = {o.strip().rstrip("/") for o in current_app.config["CORS_ORIGINS"] if isinstance(o, str) and not o.startswith("^") and o.strip()}
    own = f"{urlsplit(request.host_url).scheme}://{urlsplit(request.host_url).netloc}"
    if origin.rstrip("/") in allowed | {own}:
        return True
    try:
        parsed = urlsplit(origin)
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            return True
    except Exception:
        pass
    return False


def init_auth(app: Flask) -> None:
    app.extensions["smartlab_ratelimit"] = RateLimiter()

    if app.config["SECRET_KEY"] in ("dev-only-change-me", "change-me") and not app.config.get("TESTING"):
        log.warning("SMARTLAB_SECRET_KEY is not set; set a long random value before deploying.")

    @app.before_request
    def authenticate():
        g.user = None
        if not request.path.startswith("/api/") or request.method == "OPTIONS":
            return None
        # Cross-site request forgery guard: state-changing requests must come from an allowed origin.
        origin = request.headers.get("Origin")
        if request.method in UNSAFE_METHODS and origin and not _origin_allowed(origin):
            return jsonify({"error": "Request origin not allowed."}), 403
        g.user = session_user(get_db())
        if g.user is None and app.config["AUTH_REQUIRED"] and not request.path.startswith(PUBLIC_API):
            return jsonify({"error": "Authentication required.", "code": "auth_required"}), 401
        return None
