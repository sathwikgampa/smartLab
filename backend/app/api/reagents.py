"""Reagent inventory endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..auth import actor_name
from ..db import get_db
from ..services import inventory
from ..services.inventory import InventoryError
from ..tenancy import require_team_access, team_id
from .common import ApiError, int_arg, json_body, require

bp = Blueprint("reagents", __name__, url_prefix="/api/reagents")


@bp.get("")
def list_reagents():
    return jsonify(inventory.list_reagents(
        get_db(),
        search=request.args.get("search"),
        status=request.args.get("status"),
        location=request.args.get("location"),
        category=request.args.get("category"),
        expiring_within=int_arg("expiring_within", None, 0, 3650),
        sort=request.args.get("sort", "name"),
        order=request.args.get("order", "asc"),
        team_id=team_id(),
    ))


@bp.get("/categories")
def categories():
    rows = get_db().execute(
        "SELECT DISTINCT category FROM reagents WHERE category IS NOT NULL AND team_id = ? ORDER BY category",
        (team_id(),),
    )
    return jsonify([r[0] for r in rows])


@bp.get("/summary")
def summary():
    return jsonify(inventory.summary(get_db(), team_id=team_id()))


@bp.post("/query")
def nl_query():
    data = json_body()
    require(data, "question")
    return jsonify(inventory.natural_language_query(get_db(), str(data["question"])[:500], team_id=team_id()))


@bp.post("")
def create():
    try:
        return jsonify(inventory.create_reagent(get_db(), json_body(), actor_name(), team_id=team_id())), 201
    except InventoryError as exc:
        raise ApiError(str(exc), 422) from exc


@bp.get("/<int:reagent_id>")
def get(reagent_id: int):
    reagent = inventory.get_reagent(get_db(), reagent_id, team_id=team_id())
    if reagent is None:
        raise ApiError("Reagent not found.", 404)
    return jsonify(reagent)


@bp.patch("/<int:reagent_id>")
def update(reagent_id: int):
    try:
        reagent = inventory.update_reagent(get_db(), reagent_id, json_body(), actor_name(), team_id=team_id())
    except InventoryError as exc:
        raise ApiError(str(exc), 422) from exc
    if reagent is None:
        raise ApiError("Reagent not found.", 404)
    return jsonify(reagent)


@bp.post("/<int:reagent_id>/adjust")
def adjust(reagent_id: int):
    data = json_body()
    require(data, "delta")
    try:
        delta = float(data["delta"])
    except (TypeError, ValueError) as exc:
        raise ApiError("delta must be a number.") from exc
    try:
        reagent = inventory.adjust_stock(get_db(), reagent_id, delta, actor_name(), team_id=team_id())
    except InventoryError as exc:
        raise ApiError(str(exc), 422) from exc
    if reagent is None:
        raise ApiError("Reagent not found.", 404)
    return jsonify(reagent)


@bp.delete("/<int:reagent_id>")
@require_team_access("admin")
def delete(reagent_id: int):
    if not inventory.delete_reagent(get_db(), reagent_id, actor_name(), team_id=team_id()):
        raise ApiError("Reagent not found.", 404)
    return "", 204
