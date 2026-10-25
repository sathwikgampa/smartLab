"""Researcher profiles with progress tracking, and the virtual lab tour equipment guides."""
from __future__ import annotations

from flask import Blueprint, jsonify

from ..db import loads, query_all, query_one
from ..tenancy import team_id
from .common import ApiError

bp = Blueprint("profiles", __name__, url_prefix="/api")


@bp.get("/profiles")
def list_profiles():
    t_id = team_id()
    return jsonify(query_all(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM experiments e WHERE e.owner_id = p.id AND e.team_id = ?) AS experiment_count, "
        "(SELECT ROUND(AVG(progress)) FROM experiments e WHERE e.owner_id = p.id AND e.team_id = ?) AS avg_progress, "
        "(SELECT COUNT(*) FROM reviews r JOIN experiments e ON e.id = r.experiment_id WHERE r.reviewer_id = p.id AND r.status = 'pending' AND e.team_id = ?) AS pending_reviews "
        "FROM profiles p ORDER BY p.id", (t_id, t_id, t_id)))


@bp.get("/profiles/<int:profile_id>")
def get_profile(profile_id: int):
    t_id = team_id()
    profile = query_one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
    if profile is None:
        raise ApiError("Profile not found.", 404)
    profile["experiments"] = query_all(
        "SELECT id, title, status, progress, updated_at FROM experiments WHERE owner_id = ? AND team_id = ? ORDER BY updated_at DESC",
        (profile_id, t_id))
    profile["review_queue"] = query_all(
        "SELECT r.*, e.title AS experiment_title FROM reviews r JOIN experiments e ON e.id = r.experiment_id "
        "WHERE r.reviewer_id = ? AND r.status = 'pending' AND e.team_id = ?", (profile_id, t_id))
    return jsonify(profile)


def _equipment(row: dict) -> dict:
    row["training_steps"] = loads(row.pop("training_steps_json"), [])
    return row


@bp.get("/equipment")
def list_equipment():
    return jsonify([_equipment(r) for r in query_all("SELECT * FROM equipment ORDER BY id")])


@bp.get("/equipment/<int:equipment_id>")
def get_equipment(equipment_id: int):
    row = query_one("SELECT * FROM equipment WHERE id = ?", (equipment_id,))
    if row is None:
        raise ApiError("Equipment not found.", 404)
    item = _equipment(row)
    # Link the guide to the indexed SOP so trainees can open the source document.
    if item.get("sop_reference"):
        item["sop_documents"] = query_all("SELECT id, title FROM documents WHERE title LIKE ?",
                                          (f"{item['sop_reference']}%",))
    return jsonify(item)
