"""Collaboration: projects, threaded comments, shared files and an activity feed.

Clients poll ``/api/activity?since=<timestamp>`` for near-real-time updates. The feed shows the caller's own
activity by default; administrators can pass ``mine=false`` for the whole lab's.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, send_file

from ..auth import current_user
from ..db import execute, query_all, query_one
from ..security import SHARED_EXTENSIONS, validate_upload
from .common import ApiError, int_arg, json_body, require, upload_limits

bp = Blueprint("collaboration", __name__, url_prefix="/api")

ENTITY_TYPES = {"experiment", "project", "document", "dataset", "reagent"}


@bp.get("/projects")
def list_projects():
    return jsonify(query_all(
        "SELECT p.*, pr.name AS owner_name, "
        "(SELECT COUNT(*) FROM experiments e WHERE e.project_id = p.id) AS experiment_count, "
        "(SELECT COUNT(*) FROM shared_files f WHERE f.project_id = p.id) AS file_count "
        "FROM projects p LEFT JOIN profiles pr ON pr.id = p.owner_id ORDER BY p.id"))


@bp.post("/projects")
def create_project():
    data = json_body()
    require(data, "name")
    pid = execute("INSERT INTO projects (name, description, owner_id) VALUES (?, ?, ?)",
                  (str(data["name"])[:200], data.get("description"), data.get("owner_id")))
    return jsonify(query_one("SELECT * FROM projects WHERE id = ?", (pid,))), 201


# ---------------------------------------------------------------------- comments
def build_thread(rows: list[dict]) -> list[dict]:
    by_id = {r["id"]: {**r, "replies": []} for r in rows}
    roots = []
    for r in by_id.values():
        parent = by_id.get(r["parent_id"]) if r["parent_id"] else None
        (parent["replies"] if parent else roots).append(r)
    return roots


@bp.get("/comments/<entity_type>/<int:entity_id>")
def get_comments(entity_type: str, entity_id: int):
    if entity_type not in ENTITY_TYPES:
        raise ApiError("Unknown entity type.", 404)
    rows = query_all(
        "SELECT c.*, p.name AS author_name, p.avatar_color FROM comments c LEFT JOIN profiles p ON p.id = c.author_id "
        "WHERE c.entity_type = ? AND c.entity_id = ? ORDER BY c.id", (entity_type, entity_id))
    return jsonify(build_thread(rows))


@bp.post("/comments/<entity_type>/<int:entity_id>")
def add_comment(entity_type: str, entity_id: int):
    if entity_type not in ENTITY_TYPES:
        raise ApiError("Unknown entity type.", 404)
    data = json_body()
    require(data, "body")
    parent_id = data.get("parent_id")
    if parent_id is not None:
        parent = query_one("SELECT * FROM comments WHERE id = ?", (parent_id,))
        if parent is None or parent["entity_type"] != entity_type or parent["entity_id"] != entity_id:
            raise ApiError("Parent comment does not belong to this thread.", 422)
    cid = execute("INSERT INTO comments (entity_type, entity_id, parent_id, author_id, body) VALUES (?, ?, ?, ?, ?)",
                  (entity_type, entity_id, parent_id, data.get("author_id"), str(data["body"])[:5000]))
    return jsonify(query_one("SELECT * FROM comments WHERE id = ?", (cid,))), 201


@bp.delete("/comments/<int:comment_id>")
def delete_comment(comment_id: int):
    if not query_one("SELECT id FROM comments WHERE id = ?", (comment_id,)):
        raise ApiError("Comment not found.", 404)
    execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return "", 204


# ------------------------------------------------------------------ shared files
@bp.get("/projects/<int:project_id>/files")
def list_files(project_id: int):
    return jsonify(query_all(
        "SELECT f.*, p.name AS uploader_name FROM shared_files f LEFT JOIN profiles p ON p.id = f.uploaded_by "
        "WHERE f.project_id = ? ORDER BY f.id DESC", (project_id,)))


@bp.post("/projects/<int:project_id>/files")
def upload_file(project_id: int):
    if not query_one("SELECT id FROM projects WHERE id = ?", (project_id,)):
        raise ApiError("Project not found.", 404)
    validated = validate_upload(request.files.get("file"), SHARED_EXTENSIONS, **upload_limits())
    (current_app.config["UPLOAD_DIR"] / "shared" / validated.stored_name).write_bytes(validated.data)
    uploaded_by = request.form.get("uploaded_by")
    fid = execute(
        "INSERT INTO shared_files (project_id, stored_name, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, validated.stored_name, validated.original_name, validated.mime_type, validated.size,
         int(uploaded_by) if uploaded_by and uploaded_by.isdigit() else None))
    return jsonify(query_one("SELECT * FROM shared_files WHERE id = ?", (fid,))), 201


@bp.get("/files/<int:file_id>")
def download_file(file_id: int):
    f = query_one("SELECT * FROM shared_files WHERE id = ?", (file_id,))
    if f is None:
        raise ApiError("File not found.", 404)
    path = current_app.config["UPLOAD_DIR"] / "shared" / f["stored_name"]
    if not path.exists():
        raise ApiError("Stored file is missing.", 410)
    # Always download (never render inline) to avoid serving active content from our origin.
    return send_file(path, mimetype=f["mime_type"], as_attachment=True, download_name=f["original_name"])


# ---------------------------------------------------------------------- activity
def _flag(name: str, default: bool) -> bool:
    value = request.args.get(name)
    if value is None or not value.strip():
        return default
    value = value.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise ApiError(f"{name} must be true or false.")


@bp.get("/activity")
def activity():
    """Activity feed, newest first.

    ``mine=true`` (the default) limits it to the signed-in user: audit entries recorded under their name or
    email, their comments, files they shared and reviews assigned to them. Document uploads carry no uploader,
    so they appear only in the lab view. ``mine=false`` returns the whole lab's activity and is reserved for
    administrators. With no one signed in (``SMARTLAB_AUTH_REQUIRED`` off) there is no identity to filter on,
    so the lab view is returned.
    """
    since = request.args.get("since", "1970-01-01 00:00:00")
    limit = int_arg("limit", 30, 1, 200)
    mine = _flag("mine", True)
    user = current_user()
    if not mine and user is not None and user["role"] != "admin":
        raise ApiError("Only lab administrators can view all lab activity.", 403)
    personal = mine and user is not None

    # Each branch is fixed SQL; only values travel as bind parameters.
    branches: list[tuple[str, tuple]] = [
        ("""
        SELECT 'comment' AS kind, c.id, c.created_at AS at, p.name AS actor, c.body AS summary,
               c.entity_type || ':' || c.entity_id AS ref
          FROM comments c LEFT JOIN profiles p ON p.id = c.author_id WHERE c.created_at > ?"""
         + (" AND c.author_id = ?" if personal else ""),
         (since, user["profile_id"]) if personal else (since,)),
        ("""
        SELECT 'file', f.id, f.uploaded_at, p.name, f.original_name, 'project:' || f.project_id
          FROM shared_files f LEFT JOIN profiles p ON p.id = f.uploaded_by WHERE f.uploaded_at > ?"""
         + (" AND f.uploaded_by = ?" if personal else ""),
         (since, user["profile_id"]) if personal else (since,)),
        ("""
        SELECT 'review', r.id, COALESCE(r.decided_at, r.created_at), p.name, r.checkpoint || ' - ' || r.status,
               'experiment:' || r.experiment_id
          FROM reviews r LEFT JOIN profiles p ON p.id = r.reviewer_id WHERE COALESCE(r.decided_at, r.created_at) > ?"""
         + (" AND r.reviewer_id = ?" if personal else ""),
         (since, user["profile_id"]) if personal else (since,)),
        ("""
        SELECT 'audit', a.id, a.created_at, a.actor, a.summary, a.entity_type || ':' || COALESCE(CAST(a.entity_id AS TEXT), '')
          FROM audit_log a WHERE a.created_at > ?"""
         + (" AND (a.actor = ? OR a.actor = ?)" if personal else ""),
         (since, user["name"], user["email"]) if personal else (since,)),
    ]
    if not personal:
        branches.insert(2, ("""
        SELECT 'document', d.id, d.uploaded_at, NULL, d.title, 'document:' || d.id
          FROM documents d WHERE d.uploaded_at > ?""", (since,)))
    sql = "\n        UNION ALL".join(b[0] for b in branches) + "\n        ORDER BY at DESC LIMIT ?"
    params = tuple(v for b in branches for v in b[1]) + (limit,)
    return jsonify(query_all(sql, params))
