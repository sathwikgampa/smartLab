"""Authentication endpoints: sign-up, log in/out, current account, password reset."""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request

from ..auth import (clear_session_cookie, client_ip, create_session, current_user, fmt_time, new_token,
                    public_user, send_reset_email, set_session_cookie, token_hash, utc_now)
from ..auth.passwords import hash_password, password_problem, verify_password
from ..db import audit, get_db, transaction_lock
from .common import ApiError, json_body

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
NEUTRAL_RESET = "If an account exists, we've sent instructions."
INVALID_RESET = "This reset link is invalid or has expired."


def _email(data) -> str:
    email = str(data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError("Enter a valid email address.", 400, field="email")
    return email


def _limit(*keys: str) -> None:
    limiter = current_app.extensions["smartlab_ratelimit"]
    limit = current_app.config["LOGIN_ATTEMPT_LIMIT"]
    if not all(limiter.hit(k, limit) for k in keys):
        raise ApiError("Too many attempts. Wait a few minutes and try again.", 429)


@bp.post("/signup")
def signup():
    data = json_body()
    name = str(data.get("name") or "").strip()
    organization = str(data.get("organization") or "").strip() or None
    password = data.get("password")
    invite_token = str(data.get("invite_token") or data.get("invite") or "").strip() or None
    if not name or len(name) > 120:
        raise ApiError("Enter your full name (up to 120 characters).", 400, field="name")
    email = _email(data)
    if organization and len(organization) > 200:
        raise ApiError("Organization name is too long.", 400, field="organization")
    problem = password_problem(password if isinstance(password, str) else "", email, name)
    if problem:
        raise ApiError(problem, 400, field="password")
    if data.get("accepted") is not True:
        raise ApiError("Please confirm the safety and terms statement.", 400, field="accepted")
    _limit(f"signup:{client_ip()}")

    conn = get_db()
    transaction_lock(conn, "users")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise ApiError("An account with this email already exists. Log in instead.", 409, field="email")

    target_org_id = None
    target_team_id = None
    assigned_team_role = "owner"
    inv_id = None

    try:
        if invite_token:
            inv = conn.execute(
                "SELECT * FROM organization_invitations WHERE token_hash = ?",
                (token_hash(invite_token),),
            ).fetchone()
            if not inv or inv["used_at"] is not None or inv["expires_at"] < fmt_time(utc_now()):
                raise ApiError("This invitation link is invalid or has expired.", 400, field="invite_token")
            if inv["email"].lower() != email:
                raise ApiError("This invitation was issued to a different email address.", 400, field="email")
            target_org_id = inv["organization_id"]
            target_team_id = inv["team_id"]
            assigned_team_role = inv["role"] or "member"
            inv_id = inv["id"]
            org_row = conn.execute("SELECT id, name FROM organizations WHERE id = ?", (target_org_id,)).fetchone()
            if org_row:
                organization = org_row["name"]
        else:
            # Every new signup creates a separate organization/workspace and a default team.
            # Typing an existing organization name must never grant access to that organization.
            org_name = organization or f"{name}'s Lab"
            base_slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-") or "lab"
            slug = f"{base_slug}-{secrets.token_hex(3)}"
            target_org_id = conn.execute(
                "INSERT INTO organizations (name, slug) VALUES (?, ?)",
                (org_name, slug),
            ).lastrowid
            organization = org_name
            target_team_id = conn.execute(
                "INSERT INTO teams (organization_id, name, slug, is_default) VALUES (?, ?, ?, 1)",
                (target_org_id, "General", "general"),
            ).lastrowid
            assigned_team_role = "owner"

        # The first account administers the workspace; later accounts are members.
        role = "admin" if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else "member"
        # Each account gets a researcher profile so comments, files and experiments are attributed to it.
        profile_id = conn.execute(
            "INSERT INTO profiles (name, role, title, expertise, avatar_color) VALUES (?, ?, ?, ?, ?)",
            (name, "Lab admin" if role == "admin" else "Lab member", organization, None, "#6B4FD8"),
        ).lastrowid
        user_id = conn.execute(
            "INSERT INTO users (email, name, organization, organization_id, password_hash, role, profile_id, last_login_at, password_changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, name, organization, target_org_id, hash_password(password), role, profile_id, fmt_time(utc_now()), fmt_time(utc_now())),
        ).lastrowid

        if target_team_id:
            conn.execute(
                "INSERT INTO team_memberships (team_id, user_id, role) VALUES (?, ?, ?)",
                (target_team_id, user_id, assigned_team_role),
            )

        if inv_id:
            conn.execute("UPDATE organization_invitations SET used_at = ? WHERE id = ?", (fmt_time(utc_now()), inv_id))

        audit(conn, "user_created", "user", user_id, f"Account created for {email} ({role})", actor=name)
        token = create_session(conn, user_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    user = {"id": user_id, "email": email, "name": name, "organization": organization, "organization_id": target_org_id, "role": role, "profile_id": profile_id}
    return set_session_cookie(jsonify({"user": public_user(user)}), token), 201


@bp.post("/login")
def login():
    data = json_body()
    email = str(data.get("email") or "").strip().lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        raise ApiError("Enter your email and password.", 400)
    _limit(f"login-ip:{client_ip()}", f"login-email:{email}")

    conn = get_db()
    row = conn.execute("SELECT id, email, name, organization, organization_id, role, profile_id, password_hash FROM users WHERE email = ?",
                       (email,)).fetchone()
    row = dict(row) if row else None
    # verify_password runs a full hash comparison even for unknown emails (no timing difference).
    if not verify_password(row["password_hash"] if row else None, password[:1024]):
        current_app.logger.info("Failed login for %s from %s", email, client_ip())
        raise ApiError("Email or password is incorrect.", 401)
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (fmt_time(utc_now()), row["id"]))
    token = create_session(conn, row["id"])
    conn.commit()
    current_app.extensions["smartlab_ratelimit"].reset(f"login-email:{email}")
    return set_session_cookie(jsonify({"user": public_user(row)}), token)


@bp.post("/logout")
def logout():
    token = request.cookies.get(current_app.config["AUTH_COOKIE_NAME"])
    if token:
        conn = get_db()
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(token),))
        conn.commit()
    return clear_session_cookie(jsonify({"ok": True}))


@bp.get("/me")
def me():
    user = current_user()
    return jsonify({"user": public_user(user) if user else None, "auth_required": current_app.config["AUTH_REQUIRED"]})


@bp.post("/forgot-password")
def forgot_password():
    data = json_body()
    email = _email(data)
    _limit(f"forgot-ip:{client_ip()}", f"forgot-email:{email}")
    conn = get_db()
    row = conn.execute("SELECT id, name FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        token, digest = new_token()
        expires = utc_now() + timedelta(minutes=current_app.config["RESET_TOKEN_MINUTES"])
        # Only the newest link works.
        conn.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                     (fmt_time(utc_now()), row["id"]))
        conn.execute("INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                     (row["id"], digest, fmt_time(expires)))
        conn.commit()
        link = f"{current_app.config['PUBLIC_URL']}/#/reset-password?token={token}"
        send_reset_email(current_app._get_current_object(), email, row["name"], link)
    # Same answer whether or not the account exists.
    return jsonify({"ok": True, "message": NEUTRAL_RESET})


@bp.post("/reset-password")
def reset_password():
    data = json_body()
    token = str(data.get("token") or "")
    password = data.get("password")
    _limit(f"reset-ip:{client_ip()}")
    if not token or len(token) > 200:
        raise ApiError(INVALID_RESET, 400)
    conn = get_db()
    row = conn.execute(
        "SELECT r.id, r.user_id, r.expires_at, r.used_at, u.email, u.name FROM password_resets r "
        "JOIN users u ON u.id = r.user_id WHERE r.token_hash = ?", (token_hash(token),),
    ).fetchone()
    row = dict(row) if row else None
    if row is None or row["used_at"] is not None or row["expires_at"] < fmt_time(utc_now()):
        raise ApiError(INVALID_RESET, 400)
    problem = password_problem(password if isinstance(password, str) else "", row["email"], row["name"])
    if problem:
        raise ApiError(problem, 400, field="password")
    now = fmt_time(utc_now())
    conn.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                 (hash_password(password), now, row["user_id"]))
    conn.execute("UPDATE password_resets SET used_at = ? WHERE id = ?", (now, row["id"]))
    # Signing out everywhere: any session that existed before the reset is revoked.
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (row["user_id"],))
    audit(conn, "password_reset", "user", row["user_id"], f"Password reset for {row['email']}", actor=row["name"])
    conn.commit()
    return jsonify({"ok": True})
