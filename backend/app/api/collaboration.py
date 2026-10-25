"""Collaboration: projects, threaded comments, shared files and an activity feed.

Clients poll ``/api/activity?since=<timestamp>`` for near-real-time updates.
"""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request, send_file

from ..db import execute, query_all, query_one
from ..security import SHARED_EXTENSIONS, validate_upload
from ..tenancy import team_id
from .common import ApiError, int_arg, json_body, require, upload_limits

bp = Blueprint("collaboration", __name__, url_prefix="/api")

ENTITY_TYPES = {"experiment", "project", "document", "dataset", "reagent"}


def _validate_entity_access(entity_type: str, entity_id: int) -> None:
    if entity_type not in ENTITY_TYPES:
        raise ApiError("Unknown entity type.", 404)
    table_map = {
        "experiment": "experiments",
        "project": "projects",
        "document": "documents",
        "dataset": "datasets",
        "reagent": "reagents",
    }
    table = table_map[entity_type]
    t_id = team_id()
    if not query_one(f"SELECT id FROM {table} WHERE id = ? AND team_id = ?", (entity_id, t_id)):
        raise ApiError("Target entity not found.", 404)


@bp.get("/projects")
def list_projects():
    t_id = team_id()
    return jsonify(query_all(
        "SELECT p.*, pr.name AS owner_name, "
        "(SELECT COUNT(*) FROM experiments e WHERE e.project_id = p.id AND e.team_id = p.team_id) AS experiment_count, "
        "(SELECT COUNT(*) FROM shared_files f WHERE f.project_id = p.id AND f.team_id = p.team_id) AS file_count "
        "FROM projects p LEFT JOIN profiles pr ON pr.id = p.owner_id WHERE p.team_id = ? ORDER BY p.id",
        (t_id,)))


@bp.post("/projects")
def create_project():
    data = json_body()
    require(data, "name")
    t_id = team_id()
    owner_id = g.user.get("profile_id") if g.get("user") else data.get("owner_id")
    pid = execute("INSERT INTO projects (team_id, name, description, owner_id) VALUES (?, ?, ?, ?)",
                  (t_id, str(data["name"])[:200], data.get("description"), owner_id))
    return jsonify(query_one("SELECT * FROM projects WHERE id = ? AND team_id = ?", (pid, t_id))), 201


@bp.get("/projects/<int:project_id>")
def get_project(project_id: int):
    t_id = team_id()
    p = query_one(
        "SELECT p.*, pr.name AS owner_name, "
        "(SELECT COUNT(*) FROM experiments e WHERE e.project_id = p.id AND e.team_id = p.team_id) AS experiment_count, "
        "(SELECT COUNT(*) FROM shared_files f WHERE f.project_id = p.id AND f.team_id = p.team_id) AS file_count "
        "FROM projects p LEFT JOIN profiles pr ON pr.id = p.owner_id WHERE p.id = ? AND p.team_id = ?",
        (project_id, t_id),
    )
    if p is None:
        raise ApiError("Project not found.", 404)
    return jsonify(p)


@bp.delete("/projects/<int:project_id>")
def delete_project(project_id: int):
    t_id = team_id()
    if not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id)):
        raise ApiError("Project not found.", 404)
    execute("DELETE FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id))
    return "", 204


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
    _validate_entity_access(entity_type, entity_id)
    t_id = team_id()
    rows = query_all(
        "SELECT c.*, p.name AS author_name, p.avatar_color FROM comments c LEFT JOIN profiles p ON p.id = c.author_id "
        "WHERE c.team_id = ? AND c.entity_type = ? AND c.entity_id = ? ORDER BY c.id", (t_id, entity_type, entity_id))
    return jsonify(build_thread(rows))


@bp.post("/comments/<entity_type>/<int:entity_id>")
def add_comment(entity_type: str, entity_id: int):
    _validate_entity_access(entity_type, entity_id)
    t_id = team_id()
    data = json_body()
    require(data, "body")
    parent_id = data.get("parent_id")
    if parent_id is not None:
        parent = query_one("SELECT * FROM comments WHERE id = ? AND team_id = ?", (parent_id, t_id))
        if parent is None or parent["entity_type"] != entity_type or parent["entity_id"] != entity_id:
            raise ApiError("Parent comment does not belong to this thread.", 422)
    author_id = g.user.get("profile_id") if g.get("user") else data.get("author_id")
    cid = execute(
        "INSERT INTO comments (team_id, entity_type, entity_id, parent_id, author_id, body) VALUES (?, ?, ?, ?, ?, ?)",
        (t_id, entity_type, entity_id, parent_id, author_id, str(data["body"])[:5000]),
    )
    return jsonify(query_one("SELECT * FROM comments WHERE id = ? AND team_id = ?", (cid, t_id))), 201


@bp.delete("/comments/<int:comment_id>")
def delete_comment(comment_id: int):
    t_id = team_id()
    if not query_one("SELECT id FROM comments WHERE id = ? AND team_id = ?", (comment_id, t_id)):
        raise ApiError("Comment not found.", 404)
    execute("DELETE FROM comments WHERE id = ? AND team_id = ?", (comment_id, t_id))
    return "", 204


# ------------------------------------------------------------------ shared files
@bp.get("/projects/<int:project_id>/files")
def list_files(project_id: int):
    t_id = team_id()
    if not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id)):
        raise ApiError("Project not found.", 404)
    return jsonify(query_all(
        "SELECT f.*, p.name AS uploader_name FROM shared_files f LEFT JOIN profiles p ON p.id = f.uploaded_by "
        "WHERE f.team_id = ? AND f.project_id = ? ORDER BY f.id DESC", (t_id, project_id)))


@bp.post("/projects/<int:project_id>/files")
def upload_file(project_id: int):
    t_id = team_id()
    if not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id)):
        raise ApiError("Project not found.", 404)
    validated = validate_upload(request.files.get("file"), SHARED_EXTENSIONS, **upload_limits())
    (current_app.config["UPLOAD_DIR"] / "shared" / validated.stored_name).write_bytes(validated.data)
    uploaded_by = g.user.get("profile_id") if g.get("user") else (request.form.get("uploaded_by") or None)
    fid = execute(
        "INSERT INTO shared_files (team_id, project_id, stored_name, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (t_id, project_id, validated.stored_name, validated.original_name, validated.mime_type, validated.size, uploaded_by))
    return jsonify(query_one("SELECT * FROM shared_files WHERE id = ? AND team_id = ?", (fid, t_id))), 201


@bp.get("/files/<int:file_id>")
def download_file(file_id: int):
    t_id = team_id()
    f = query_one("SELECT * FROM shared_files WHERE id = ? AND team_id = ?", (file_id, t_id))
    if f is None:
        raise ApiError("File not found.", 404)
    path = current_app.config["UPLOAD_DIR"] / "shared" / f["stored_name"]
    if not path.exists():
        raise ApiError("Stored file is missing.", 410)
    # Always download (never render inline) to avoid serving active content from our origin.
    return send_file(path, mimetype=f["mime_type"], as_attachment=True, download_name=f["original_name"])


# ---------------------------------------------------------------------- activity
@bp.get("/activity")
def activity():
    t_id = team_id()
    since = request.args.get("since", "1970-01-01 00:00:00")
    limit = int_arg("limit", 30, 1, 200)
    items = query_all(
        """
        SELECT 'comment' AS kind, c.id, c.created_at AS at, p.name AS actor, c.body AS summary,
               c.entity_type || ':' || c.entity_id AS ref
          FROM comments c LEFT JOIN profiles p ON p.id = c.author_id WHERE c.team_id = ? AND c.created_at > ?
        UNION ALL
        SELECT 'file', f.id, f.uploaded_at, p.name, f.original_name, 'project:' || f.project_id
          FROM shared_files f LEFT JOIN profiles p ON p.id = f.uploaded_by WHERE f.team_id = ? AND f.uploaded_at > ?
        UNION ALL
        SELECT 'document', d.id, d.uploaded_at, NULL, d.title, 'document:' || d.id
          FROM documents d WHERE d.team_id = ? AND d.uploaded_at > ?
        UNION ALL
        SELECT 'review', r.id, COALESCE(r.decided_at, r.created_at), p.name, r.checkpoint || ' - ' || r.status,
               'experiment:' || r.experiment_id
          FROM reviews r JOIN experiments e ON e.id = r.experiment_id LEFT JOIN profiles p ON p.id = r.reviewer_id
         WHERE e.team_id = ? AND COALESCE(r.decided_at, r.created_at) > ?
        UNION ALL
        SELECT 'audit', a.id, a.created_at, a.actor, a.summary, a.entity_type || ':' || COALESCE(CAST(a.entity_id AS TEXT), '')
          FROM audit_log a WHERE a.team_id = ? AND a.created_at > ?
        ORDER BY at DESC LIMIT ?
        """, (t_id, since, t_id, since, t_id, since, t_id, since, t_id, since, limit))
    return jsonify(items)
