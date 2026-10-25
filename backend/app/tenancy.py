"""Team tenancy: the active team for every API request, verified against memberships.

The client names its team in the ``X-Team-ID`` header, or the ``team_id`` query parameter for URLs
the browser loads itself (``<img src>``, file links). That value is a request, not a fact: it is
checked against ``team_memberships`` (or org-admin rights) before ``g.team`` is set. Routes read the
team with :func:`team_id` and never from the request payload.
"""
from __future__ import annotations

import functools
from typing import Any

from flask import Flask, current_app, g, jsonify, request

from .api.common import ApiError
from .db import get_db, is_postgres

TEAM_HEADER = "X-Team-ID"
# Not team data: reachable without an active team.
TEAM_FREE_API = ("/api/health", "/api/auth/", "/api/teams", "/api/invitations")
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}

# Teams the user may open: explicit memberships, plus every team of their organisation for org admins.
_TEAMS_SQL = (
    "SELECT t.id, t.organization_id, t.name, t.slug, t.description, t.is_default, "
    "CASE WHEN u.role = 'admin' THEN 'owner' ELSE m.role END AS role, "
    "t.created_at, t.archived_at "
    "FROM teams t JOIN users u ON u.id = ? AND u.organization_id = t.organization_id "
    "LEFT JOIN team_memberships m ON m.team_id = t.id AND m.user_id = u.id "
    "WHERE t.archived_at IS NULL AND (m.user_id IS NOT NULL OR u.role = 'admin')"
)


def user_teams(conn, user_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_TEAMS_SQL + " ORDER BY t.is_default DESC, t.id ASC", (user_id,))]


def team_for_user(conn, user_id: int, team_id: int) -> dict[str, Any] | None:
    row = conn.execute(_TEAMS_SQL + " AND t.id = ?", (user_id, team_id)).fetchone()
    return dict(row) if row else None


def team_id() -> int:
    """The verified active team. Raises outside a team-scoped request so a missed route fails closed."""
    team = g.get("team")
    if team is None:
        raise RuntimeError("team_id() used on a request without a resolved team")
    return int(team["id"])


def organization_id() -> int:
    team = g.get("team")
    if team and "organization_id" in team:
        return int(team["organization_id"])
    user = g.get("user")
    if user and user.get("organization_id"):
        return int(user["organization_id"])
    return int(current_app.config.get("DEFAULT_ORGANIZATION_ID", 1))


def require_team_access(min_role: str = "member"):
    """Raise the minimum team role for a route (the resolver already guarantees membership)."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            team = g.get("team")
            if team is None or ROLE_RANK.get(team.get("role", ""), -1) < ROLE_RANK.get(min_role, 1):
                raise ApiError(f"This action needs the team {min_role} role.", 403, code="team_role")
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def init_tenancy(app: Flask) -> None:
    """Register after init_auth(): Flask runs before_request hooks in registration order."""

    @app.before_request
    def resolve_team():
        g.team = None
        path = request.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return None

        user = g.get("user")
        conn = get_db()

        # On PostgreSQL, establish caller user_id for RLS whenever user is known
        if user and is_postgres(conn):
            conn.execute("SELECT set_config('smartlab.user_id', ?, true)", (str(user["id"]),))

        if path.startswith(TEAM_FREE_API):
            return None

        if user is None:
            # Only reachable with SMARTLAB_AUTH_REQUIRED off (tests, local demo): fallback to default team
            g.team = {
                "id": app.config.get("DEFAULT_TEAM_ID", 1),
                "organization_id": app.config.get("DEFAULT_ORGANIZATION_ID", 1),
                "name": "General",
                "slug": "general",
                "role": "owner",
            }
            return None

        raw = request.headers.get(TEAM_HEADER) or request.args.get("team_id")
        if raw is None or raw == "":
            teams = user_teams(conn, user["id"])
            g.team = teams[0] if teams else None
        elif str(raw).isdigit():
            g.team = team_for_user(conn, user["id"], int(raw))

        if g.team is None:
            return jsonify({"error": "You don't have access to this team.", "code": "team_forbidden"}), 403

        # On PostgreSQL, set the active team context for RLS
        if is_postgres(conn):
            conn.execute("SELECT set_config('smartlab.team_id', ?, true)", (str(g.team["id"]),))

        return None
