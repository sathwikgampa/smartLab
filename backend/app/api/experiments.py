"""Experiment planning: protocols, checklists, variable logs and expert review checkpoints."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..db import execute, get_db, query_all, query_one
from ..tenancy import team_id
from .common import ApiError, json_body, require

bp = Blueprint("experiments", __name__, url_prefix="/api/experiments")

STATUSES = {"draft", "in_progress", "in_review", "approved", "changes_requested", "completed", "archived"}
VARIABLE_KINDS = {"independent", "dependent", "controlled"}
REVIEW_DECISIONS = {"approved", "rejected", "changes_requested"}


def _experiment(exp_id: int) -> dict:
    t_id = team_id()
    exp = query_one(
        "SELECT e.*, p.name AS project_name, pr.name AS owner_name FROM experiments e "
        "LEFT JOIN projects p ON p.id = e.project_id AND p.team_id = e.team_id "
        "LEFT JOIN profiles pr ON pr.id = e.owner_id WHERE e.id = ? AND e.team_id = ?",
        (exp_id, t_id))
    if exp is None:
        raise ApiError("Experiment not found.", 404)
    return exp


def _recompute_progress(exp_id: int) -> None:
    row = query_one("SELECT COUNT(*) AS total, SUM(done) AS done FROM experiment_steps WHERE experiment_id = ?", (exp_id,))
    progress = round(100 * (row["done"] or 0) / row["total"]) if row["total"] else 0
    execute("UPDATE experiments SET progress = ?, updated_at = datetime('now') WHERE id = ?", (progress, exp_id))


def _detail(exp_id: int) -> dict:
    exp = _experiment(exp_id)
    exp["steps"] = query_all("SELECT * FROM experiment_steps WHERE experiment_id = ? ORDER BY position, id", (exp_id,))
    for s in exp["steps"]:
        s["done"] = bool(s["done"])
    exp["variables"] = query_all("SELECT * FROM experiment_variables WHERE experiment_id = ? ORDER BY id", (exp_id,))
    exp["reviews"] = query_all(
        "SELECT r.*, p.name AS reviewer_name FROM reviews r LEFT JOIN profiles p ON p.id = r.reviewer_id "
        "WHERE r.experiment_id = ? ORDER BY r.id", (exp_id,))
    return exp


@bp.get("")
def list_experiments():
    t_id = team_id()
    sql = ("SELECT e.*, p.name AS project_name, pr.name AS owner_name FROM experiments e "
           "LEFT JOIN projects p ON p.id = e.project_id AND p.team_id = e.team_id "
           "LEFT JOIN profiles pr ON pr.id = e.owner_id WHERE e.team_id = ?")
    params = [t_id]
    if request.args.get("project_id"):
        sql += " AND e.project_id = ?"
        params.append(request.args["project_id"])
    return jsonify(query_all(sql + " ORDER BY e.updated_at DESC, e.id DESC", params))


@bp.post("")
def create_experiment():
    data = json_body()
    require(data, "title")
    t_id = team_id()
    project_id = data.get("project_id")
    if project_id:
        if not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id)):
            raise ApiError("Project not found.", 404)
    owner_id = g.user.get("profile_id") if g.get("user") else data.get("owner_id")
    exp_id = execute(
        "INSERT INTO experiments (team_id, project_id, title, objective, protocol_markdown, owner_id) VALUES (?, ?, ?, ?, ?, ?)",
        (t_id, project_id, str(data["title"])[:200], data.get("objective"), data.get("protocol_markdown", ""), owner_id),
    )
    for i, text in enumerate(data.get("steps") or [], start=1):
        execute("INSERT INTO experiment_steps (experiment_id, position, text) VALUES (?, ?, ?)", (exp_id, i, str(text)))
    _recompute_progress(exp_id)
    return jsonify(_detail(exp_id)), 201


@bp.get("/<int:exp_id>")
def get_experiment(exp_id: int):
    return jsonify(_detail(exp_id))


@bp.patch("/<int:exp_id>")
def update_experiment(exp_id: int):
    _experiment(exp_id)
    data = json_body()
    allowed = {k: data[k] for k in ("title", "objective", "protocol_markdown", "status", "project_id") if k in data}
    if "status" in allowed and allowed["status"] not in STATUSES:
        raise ApiError(f"status must be one of: {', '.join(sorted(STATUSES))}.", 422)
    if "title" in allowed and not str(allowed["title"]).strip():
        raise ApiError("title cannot be empty.", 422)
    if "project_id" in allowed and allowed["project_id"] is not None:
        if not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (allowed["project_id"], team_id())):
            raise ApiError("Project not found.", 404)
    if allowed:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        execute(f"UPDATE experiments SET {sets}, updated_at = datetime('now') WHERE id = ?", [*allowed.values(), exp_id])
    return jsonify(_detail(exp_id))


@bp.delete("/<int:exp_id>")
def delete_experiment(exp_id: int):
    _experiment(exp_id)
    execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
    return "", 204


# --------------------------------------------------------------------- checklist
@bp.post("/<int:exp_id>/steps")
def add_step(exp_id: int):
    _experiment(exp_id)
    data = json_body()
    require(data, "text")
    pos = query_one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM experiment_steps WHERE experiment_id = ?", (exp_id,))["p"]
    execute("INSERT INTO experiment_steps (experiment_id, position, text) VALUES (?, ?, ?)", (exp_id, pos, str(data["text"])[:1000]))
    _recompute_progress(exp_id)
    return jsonify(_detail(exp_id)), 201


@bp.patch("/<int:exp_id>/steps/<int:step_id>")
def update_step(exp_id: int, step_id: int):
    _experiment(exp_id)
    if not query_one("SELECT id FROM experiment_steps WHERE id = ? AND experiment_id = ?", (step_id, exp_id)):
        raise ApiError("Step not found.", 404)
    data = json_body()
    if "done" in data:
        execute("UPDATE experiment_steps SET done = ? WHERE id = ?", (1 if data["done"] else 0, step_id))
    if "text" in data and str(data["text"]).strip():
        execute("UPDATE experiment_steps SET text = ? WHERE id = ?", (str(data["text"])[:1000], step_id))
    _recompute_progress(exp_id)
    return jsonify(_detail(exp_id))


@bp.delete("/<int:exp_id>/steps/<int:step_id>")
def delete_step(exp_id: int, step_id: int):
    _experiment(exp_id)
    execute("DELETE FROM experiment_steps WHERE id = ? AND experiment_id = ?", (step_id, exp_id))
    _recompute_progress(exp_id)
    return jsonify(_detail(exp_id))


@bp.put("/<int:exp_id>/steps/order")
def reorder_steps(exp_id: int):
    _experiment(exp_id)
    data = json_body()
    order = data.get("step_ids")
    if not isinstance(order, list):
        raise ApiError("step_ids must be a list.")
    existing = {r["id"] for r in query_all("SELECT id FROM experiment_steps WHERE experiment_id = ?", (exp_id,))}
    if set(order) != existing:
        raise ApiError("step_ids must contain every step of the experiment exactly once.", 422)
    db = get_db()
    for pos, sid in enumerate(order, start=1):
        db.execute("UPDATE experiment_steps SET position = ? WHERE id = ?", (pos, sid))
    db.commit()
    return jsonify(_detail(exp_id))


# --------------------------------------------------------------------- variables
@bp.post("/<int:exp_id>/variables")
def log_variable(exp_id: int):
    _experiment(exp_id)
    data = json_body()
    require(data, "name", "value")
    kind = data.get("kind", "controlled")
    if kind not in VARIABLE_KINDS:
        raise ApiError(f"kind must be one of: {', '.join(sorted(VARIABLE_KINDS))}.", 422)
    execute("INSERT INTO experiment_variables (experiment_id, name, value, unit, kind) VALUES (?, ?, ?, ?, ?)",
            (exp_id, str(data["name"])[:200], str(data["value"])[:200], data.get("unit"), kind))
    return jsonify(_detail(exp_id)), 201


# ----------------------------------------------------------------------- reviews
@bp.post("/<int:exp_id>/reviews")
def request_review(exp_id: int):
    _experiment(exp_id)
    data = json_body()
    require(data, "checkpoint")
    execute("INSERT INTO reviews (experiment_id, checkpoint, reviewer_id) VALUES (?, ?, ?)",
            (exp_id, str(data["checkpoint"])[:200], data.get("reviewer_id")))
    execute("UPDATE experiments SET status = 'in_review', updated_at = datetime('now') WHERE id = ?", (exp_id,))
    return jsonify(_detail(exp_id)), 201


@bp.post("/<int:exp_id>/reviews/<int:review_id>/decision")
def decide_review(exp_id: int, review_id: int):
    _experiment(exp_id)
    review = query_one("SELECT * FROM reviews WHERE id = ? AND experiment_id = ?", (review_id, exp_id))
    if review is None:
        raise ApiError("Review not found.", 404)
    if review["status"] != "pending":
        raise ApiError("This checkpoint has already been decided.", 409)
    data = json_body()
    decision = data.get("decision")
    if decision not in REVIEW_DECISIONS:
        raise ApiError(f"decision must be one of: {', '.join(sorted(REVIEW_DECISIONS))}.", 422)
    if decision != "approved" and not str(data.get("notes") or "").strip():
        raise ApiError("Notes are required when rejecting or requesting changes.", 422)
    execute("UPDATE reviews SET status = ?, notes = ?, decided_at = datetime('now') WHERE id = ?",
            (decision, data.get("notes"), review_id))

    statuses = [r["status"] for r in query_all("SELECT status FROM reviews WHERE experiment_id = ?", (exp_id,))]
    if any(s in ("rejected", "changes_requested") for s in statuses):
        new_status = "changes_requested"
    elif all(s == "approved" for s in statuses):
        new_status = "approved"
    else:
        new_status = "in_review"
    execute("UPDATE experiments SET status = ?, updated_at = datetime('now') WHERE id = ?", (new_status, exp_id))
    return jsonify(_detail(exp_id))
