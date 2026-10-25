"""Teams of the signed-in user's organisation, team membership, and invitations."""
from __future__ import annotations

import re
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request

from ..auth import actor_name, current_user, fmt_time, new_token, token_hash, utc_now
from ..db import audit, get_db, is_postgres
from ..tenancy import ROLE_RANK, team_for_user, user_teams
from .common import ApiError, json_body, require

bp = Blueprint("teams", __name__, url_prefix="/api/teams")


def _user():
    user = current_user()
    if user is None:
        raise ApiError("Authentication required.", 401, code="auth_required")
    return user


def _team(team_id: int, min_role: str = "viewer") -> dict:
    user = _user()
    team = team_for_user(get_db(), user["id"], team_id)
    if team is None:
        raise ApiError("Team not found.", 404)
    if ROLE_RANK.get(team["role"], -1) < ROLE_RANK.get(min_role, 0):
        raise ApiError(f"This action needs the team {min_role} role.", 403, code="team_role")
    return team


@bp.get("")
def my_teams():
    user = _user()
    return jsonify({
        "teams": user_teams(get_db(), user["id"]),
        "can_create": bool(current_app.config.get("MULTI_TEAM", True)),
    })


@bp.post("")
def create_team():
    user = _user()
    if not current_app.config.get("MULTI_TEAM", True):
        raise ApiError("Team creation is disabled.", 403)
    data = json_body()
    require(data, "name")
    name = str(data["name"]).strip()[:80]
    if not name:
        raise ApiError("Team name cannot be empty.", 422, field="name")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "team"
    conn = get_db()
    org_row = conn.execute("SELECT organization_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not org_row or not org_row[0]:
        raise ApiError("User has no organization.", 400)
    org_id = org_row[0]

    # Check slug uniqueness within organization
    if conn.execute("SELECT 1 FROM teams WHERE organization_id = ? AND slug = ?", (org_id, slug)).fetchone():
        raise ApiError("A team with this name already exists in your organization.", 409, field="name")

    # Atomically create team and creator ownership
    try:
        if is_postgres(conn):
            tid = conn.execute(
                "SELECT smartlab.create_team(?, ?, ?)",
                (name, slug, data.get("description")),
            ).fetchone()[0]
        else:
            tid = conn.execute(
                "INSERT INTO teams (organization_id, name, slug, description, is_default) VALUES (?, ?, ?, ?, 0)",
                (org_id, name, slug, data.get("description")),
            ).lastrowid
            conn.execute("INSERT INTO team_memberships (team_id, user_id, role) VALUES (?, ?, 'owner')", (tid, user["id"]))
        audit(conn, "create", "team", tid, f"Created team '{name}'", actor_name())
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    created = team_for_user(conn, user["id"], tid)
    return jsonify(created), 201


@bp.get("/<int:team_id>")
def get_team(team_id: int):
    return jsonify(_team(team_id, "viewer"))


@bp.post("/<int:team_id>/archive")
@bp.patch("/<int:team_id>")
def archive_team(team_id: int):
    team = _team(team_id, "owner")
    if team.get("is_default") == 1:
        raise ApiError("The default team cannot be archived.", 400)
    conn = get_db()
    now_str = fmt_time(utc_now())
    conn.execute("UPDATE teams SET archived_at = ? WHERE id = ?", (now_str, team_id))
    audit(conn, "archive", "team", team_id, f"Archived team '{team['name']}'", actor_name())
    conn.commit()
    team["archived_at"] = now_str
    return jsonify(team)


@bp.get("/<int:team_id>/members")
def members(team_id: int):
    _team(team_id, "viewer")
    rows = get_db().execute(
        "SELECT u.id, u.name, u.email, m.role, m.created_at FROM team_memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.team_id = ? ORDER BY u.name",
        (team_id,),
    )
    return jsonify([dict(r) for r in rows])


@bp.route("/<int:team_id>/members", methods=["POST", "PUT"])
def upsert_member(team_id: int):
    """Add an existing account of the same organisation, or change its role."""
    team = _team(team_id, "admin")
    caller = _user()
    data = json_body()
    require(data, "email")
    role = data.get("role", "member")
    if role not in ROLE_RANK:
        raise ApiError(f"role must be one of: {', '.join(ROLE_RANK.keys())}.", 422, field="role")
    if ROLE_RANK[role] > ROLE_RANK[team["role"]]:
        raise ApiError("You can't grant a role above your own.", 403)

    conn = get_db()
    target = conn.execute(
        "SELECT id, organization_id FROM users WHERE email = ?",
        (str(data["email"]).strip().lower(),),
    ).fetchone()
    if target is None:
        raise ApiError("No account with this email exists. Send an invitation instead.", 404, field="email")
    if target["organization_id"] != team["organization_id"]:
        raise ApiError("Cross-organization membership is not allowed.", 403, field="email")

    target_id = target["id"]
    if target_id == caller["id"] and ROLE_RANK[role] > ROLE_RANK[team["role"]]:
        raise ApiError("You cannot promote yourself.", 403)

    # Protect last-owner invariant if modifying an existing owner
    existing = conn.execute(
        "SELECT role FROM team_memberships WHERE team_id = ? AND user_id = ?",
        (team_id, target_id),
    ).fetchone()
    if existing and existing["role"] == "owner" and role != "owner":
        other_owners = conn.execute(
            "SELECT COUNT(*) FROM team_memberships WHERE team_id = ? AND role = 'owner' AND user_id != ?",
            (team_id, target_id),
        ).fetchone()[0]
        if other_owners == 0:
            raise ApiError("A team needs at least one owner.", 409)

    conn.execute(
        "INSERT INTO team_memberships (team_id, user_id, role) VALUES (?, ?, ?) "
        "ON CONFLICT (team_id, user_id) DO UPDATE SET role = excluded.role",
        (team_id, target_id, role),
    )
    audit(conn, "member_role", "team", team_id, f"Set {data['email']} to {role}", actor_name())
    conn.commit()
    return members(team_id)


@bp.delete("/<int:team_id>/members/<int:user_id>")
def remove_member(team_id: int, user_id: int):
    caller = _user()
    team = _team(team_id, "viewer")

    # Either caller is admin/owner, or user is removing themselves
    is_self = caller["id"] == user_id
    if not is_self and ROLE_RANK.get(team["role"], -1) < ROLE_RANK["admin"]:
        raise ApiError("This action needs the team admin role.", 403, code="team_role")

    conn = get_db()
    target_mem = conn.execute(
        "SELECT role FROM team_memberships WHERE team_id = ? AND user_id = ?",
        (team_id, user_id),
    ).fetchone()
    if target_mem is None:
        raise ApiError("Member not found in this team.", 404)

    # Admin cannot remove someone ranked above them (e.g. admin cannot remove owner)
    if not is_self and ROLE_RANK.get(target_mem["role"], -1) > ROLE_RANK.get(team["role"], -1):
        raise ApiError("You cannot remove a member ranked above your role.", 403)

    # Protect last-owner invariant
    if target_mem["role"] == "owner":
        owners = conn.execute(
            "SELECT COUNT(*) FROM team_memberships WHERE team_id = ? AND role = 'owner' AND user_id <> ?",
            (team_id, user_id),
        ).fetchone()[0]
        if owners == 0:
            raise ApiError("A team needs at least one owner.", 409)

    conn.execute("DELETE FROM team_memberships WHERE team_id = ? AND user_id = ?", (team_id, user_id))
    audit(conn, "member_removed", "team", team_id, f"Removed user {user_id}", actor_name())
    conn.commit()
    return "", 204


# ---------------------------------------------------------------------- invitations
@bp.get("/<int:team_id>/invitations")
def list_invitations(team_id: int):
    _team(team_id, "admin")
    conn = get_db()
    now_str = fmt_time(utc_now())
    rows = conn.execute(
        "SELECT i.id, i.email, i.role, i.created_at, i.expires_at, u.name AS invited_by_name "
        "FROM organization_invitations i LEFT JOIN users u ON u.id = i.invited_by "
        "WHERE i.team_id = ? AND i.used_at IS NULL AND i.expires_at > ? ORDER BY i.id DESC",
        (team_id, now_str),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.post("/<int:team_id>/invitations")
def create_invitation(team_id: int):
    team = _team(team_id, "admin")
    caller = _user()
    data = json_body()
    require(data, "email")
    email = str(data["email"]).strip().lower()
    if "@" not in email:
        raise ApiError("Enter a valid email address.", 400, field="email")

    role = data.get("role", "member")
    if role not in ROLE_RANK:
        raise ApiError(f"role must be one of: {', '.join(ROLE_RANK.keys())}.", 422, field="role")
    if ROLE_RANK[role] > ROLE_RANK[team["role"]]:
        raise ApiError("You can't grant a role above your own.", 403)

    conn = get_db()
    # Check if user is already in this team
    existing = conn.execute(
        "SELECT 1 FROM team_memberships m JOIN users u ON u.id = m.user_id WHERE m.team_id = ? AND u.email = ?",
        (team_id, email),
    ).fetchone()
    if existing:
        raise ApiError("This user is already a member of this team.", 409, field="email")

    token, digest = new_token()
    days = current_app.config.get("INVITATION_EXPIRES_DAYS", 7)
    expires = utc_now() + timedelta(days=days)
    now_str = fmt_time(utc_now())
    exp_str = fmt_time(expires)

    # Invalidate any older unused invite for same email and team
    conn.execute(
        "UPDATE organization_invitations SET used_at = ? WHERE team_id = ? AND email = ? AND used_at IS NULL",
        (now_str, team_id, email),
    )
    iid = conn.execute(
        "INSERT INTO organization_invitations (organization_id, team_id, email, role, token_hash, invited_by, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (team_id if "organization_id" not in team else team["organization_id"], team_id, email, role, digest, caller["id"], exp_str),
    ).lastrowid
    audit(conn, "invite_created", "team", team_id, f"Invited {email} as {role}", actor_name())
    conn.commit()

    invite_url = f"{current_app.config['PUBLIC_URL']}/#/signup?invite={token}"
    return jsonify({
        "id": iid,
        "token": token,
        "email": email,
        "role": role,
        "expires_at": exp_str,
        "invite_url": invite_url,
    }), 201


@bp.delete("/<int:team_id>/invitations/<int:invite_id>")
def cancel_invitation(team_id: int, invite_id: int):
    _team(team_id, "admin")
    conn = get_db()
    cur = conn.execute("DELETE FROM organization_invitations WHERE id = ? AND team_id = ?", (invite_id, team_id))
    if cur.rowcount == 0:
        raise ApiError("Invitation not found.", 404)
    audit(conn, "invite_cancelled", "team", team_id, f"Cancelled invitation #{invite_id}", actor_name())
    conn.commit()
    return "", 204


# ---------------------------------------------------------------------- Public invitations blueprint
invitations_bp = Blueprint("invitations", __name__, url_prefix="/api/invitations")


@invitations_bp.get("/<token>")
def invitation_details(token: str):
    conn = get_db()
    inv = conn.execute(
        "SELECT i.id, i.organization_id, i.team_id, i.email, i.role, i.expires_at, i.used_at, "
        "o.name as organization_name, t.name as team_name "
        "FROM organization_invitations i "
        "JOIN organizations o ON o.id = i.organization_id "
        "JOIN teams t ON t.id = i.team_id "
        "WHERE i.token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if not inv or inv["used_at"] is not None or inv["expires_at"] < fmt_time(utc_now()):
        raise ApiError("This invitation link is invalid or has expired.", 404)
    return jsonify({
        "id": inv["id"],
        "organization_id": inv["organization_id"],
        "organization_name": inv["organization_name"],
        "team_id": inv["team_id"],
        "team_name": inv["team_name"],
        "email": inv["email"],
        "role": inv["role"],
        "expires_at": inv["expires_at"],
    })


@invitations_bp.post("/<token>/accept")
def accept_invitation(token: str):
    user = _user()
    conn = get_db()
    inv = conn.execute(
        "SELECT * FROM organization_invitations WHERE token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if not inv or inv["used_at"] is not None or inv["expires_at"] < fmt_time(utc_now()):
        raise ApiError("This invitation link is invalid or has expired.", 400)
    if inv["email"].lower() != user["email"].lower():
        raise ApiError("This invitation was issued to a different email address.", 403)
    if user.get("organization_id") and user["organization_id"] != inv["organization_id"]:
        raise ApiError("This account belongs to a different organization.", 403)

    try:
        conn.execute(
            "INSERT INTO team_memberships (team_id, user_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT (team_id, user_id) DO UPDATE SET role = excluded.role",
            (inv["team_id"], user["id"], inv["role"] or "member"),
        )
        conn.execute("UPDATE organization_invitations SET used_at = ? WHERE id = ?", (fmt_time(utc_now()), inv["id"]))
        audit(conn, "invite_accepted", "team", inv["team_id"], f"Accepted invitation to team {inv['team_id']}", actor_name())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"status": "accepted", "team_id": inv["team_id"]})
