"""Unified reagent + document + troubleshooting search."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import get_db
from ..rag.engine import _snippet
from ..services import inventory, troubleshooting
from ..tenancy import team_id
from .common import ApiError, rag

bp = Blueprint("search", __name__, url_prefix="/api/search")


@bp.get("/unified")
def unified():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        raise ApiError("Query parameter 'q' must be at least 2 characters.")
    q = q[:500]
    conn = get_db()
    engine = rag()
    t_id = team_id()

    documents = [
        {**e.to_dict(), "snippet": _snippet(e.text, q)}
        for e in engine.retrieve(conn, q, k=6, team_id=t_id) if e.score >= engine.min_score
    ]
    for d in documents:
        d.pop("text", None)

    nl = inventory.natural_language_query(conn, q, team_id=t_id)
    reagents = nl["results"] or inventory.list_reagents(conn, search=q, team_id=t_id)
    fixes = troubleshooting.match(conn, q, limit=4, team_id=t_id)
    return jsonify({
        "query": q,
        "documents": documents,
        "reagents": reagents[:10],
        "inventory_answer": nl["answer"] if nl["results"] else None,
        "troubleshooting": fixes,
        "counts": {"documents": len(documents), "reagents": len(reagents[:10]), "troubleshooting": len(fixes)},
    })
