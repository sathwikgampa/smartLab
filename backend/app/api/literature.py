"""Literature search, summarisation and cross-referencing."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..db import get_db
from ..services import literature
from ..tenancy import team_id
from .common import ApiError, int_arg, llm, rag

bp = Blueprint("literature", __name__, url_prefix="/api/literature")


@bp.get("/search")
def search():
    try:
        result = literature.search(
            get_db(), rag(), llm(), request.args.get("q", ""),
            limit=int_arg("limit", 5, 1, 20),
            online=current_app.config["LITERATURE_ONLINE"] and request.args.get("offline") != "1",
            timeout=current_app.config["LITERATURE_TIMEOUT"],
            from_year=int_arg("from_year", None, 1800, 3000),
            to_year=int_arg("to_year", None, 1800, 3000),
            sort=request.args.get("sort", "relevance"),
            team_id=team_id(),
        )
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return jsonify(result)
