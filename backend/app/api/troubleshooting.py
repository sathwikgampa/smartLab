"""Error-to-fix troubleshooting endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify

from ..db import get_db
from ..services import troubleshooting as ts
from ..tenancy import team_id
from .common import ApiError, json_body, require

bp = Blueprint("troubleshooting", __name__, url_prefix="/api/troubleshooting")


@bp.get("")
def list_entries():
    return jsonify(ts.list_entries(get_db(), team_id=team_id()))


@bp.post("")
def create_entry():
    try:
        return jsonify(ts.create_entry(get_db(), json_body(), team_id=team_id())), 201
    except ValueError as exc:
        raise ApiError(str(exc), 422) from exc


@bp.post("/match")
def match():
    data = json_body()
    require(data, "query")
    query = str(data["query"])[:1000]
    return jsonify({
        "query": query,
        "error_codes": ts.extract_error_codes(query),
        "matches": ts.match(get_db(), query, include_unverified=bool(data.get("include_unverified")), team_id=team_id()),
    })
