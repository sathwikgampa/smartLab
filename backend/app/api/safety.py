"""Safety resources and workspace settings.

Safety content comes only from approved records already in the system: documents in
the "Safety" category, reagent hazard classes, and emergency contacts entered by the
institution in Settings. Nothing here is generated - the assistant supplements, and
does not replace, official safety procedures.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..auth import actor_name
from ..db import audit, get_db, get_setting, query_all, set_setting
from ..rag.engine import _snippet
from .common import ApiError, json_body, llm, rag

bp = Blueprint("safety", __name__, url_prefix="/api")

SAFETY_CATEGORY = "Safety"
EDITABLE_SETTINGS = {"institution_name": 120, "emergency_contacts": 2000, "safety_office": 500}


@bp.get("/safety")
def overview():
    db = get_db()
    return jsonify({
        "documents": query_all(
            "SELECT id, title, doc_code, version, approval_status, reviewed_at, updated_at, page_count FROM documents "
            "WHERE category = ? ORDER BY approval_status = 'approved' DESC, title", (SAFETY_CATEGORY,)),
        "hazardous_reagents": query_all(
            "SELECT id, name, cas_number, hazard_class, location, storage_condition FROM reagents "
            "WHERE hazard_class IS NOT NULL AND hazard_class != '' ORDER BY name"),
        "emergency_contacts": get_setting(db, "emergency_contacts", ""),
        "safety_office": get_setting(db, "safety_office", ""),
        "disclaimer": "SmartLab AI supplements, and does not replace, your institution's official emergency procedures, "
                      "safety data sheets and qualified safety personnel.",
    })


@bp.get("/safety/search")
def search():
    """Retrieve passages from safety-category documents only (no generated text)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        raise ApiError("Query parameter 'q' must be at least 2 characters.")
    db = get_db()
    ids = [r["id"] for r in query_all("SELECT id FROM documents WHERE category = ?", (SAFETY_CATEGORY,))]
    engine = rag()
    hits = engine.retrieve(db, q[:500], k=6, document_ids=ids) if ids else []
    return jsonify({
        "query": q,
        "searched_documents": len(ids),
        "results": [{**{k: v for k, v in h.to_dict().items() if k != "text"}, "snippet": _snippet(h.text, q)}
                    for h in hits if h.score >= engine.min_score],
    })


@bp.get("/settings")
def get_settings():
    db = get_db()
    engine = rag()
    client = llm()
    return jsonify({
        "editable": {k: get_setting(db, k, "") for k in EDITABLE_SETTINGS},
        "ai": {
            "enabled": client.available,
            "model": client.model if client.available else None,
            "provider": "Anthropic Claude API" if client.available else None,
            "disclosure": ("Questions, retrieved document passages and attached images are sent to the Anthropic API "
                           "to generate answers.") if client.available else
                          ("No external AI service is configured. Answers are assembled from your documents on this "
                           "server and images get a basic colour/brightness analysis only."),
        },
        "retrieval": {
            "embeddings": engine.embedder.name,
            "vector_store": engine.store.backend,
            "min_evidence_score": engine.min_score,
            "chunk_size": engine.chunk_size,
            "top_k": engine.top_k,
            "indexed_chunks": len(engine.store),
        },
        "demo_data": {
            "documents": get_setting(db, "demo_documents") == "true",
            "records": get_setting(db, "demo_records") == "true",
        },
    })


@bp.put("/settings")
def update_settings():
    data = json_body()
    unknown = set(data) - set(EDITABLE_SETTINGS) - {"actor"}
    if unknown:
        raise ApiError(f"Unknown setting(s): {', '.join(sorted(unknown))}.", 422)
    db = get_db()
    for key, limit in EDITABLE_SETTINGS.items():
        if key in data:
            value = str(data[key] or "")
            if len(value) > limit:
                raise ApiError(f"{key} is limited to {limit} characters.", 422)
            set_setting(db, key, value.strip())
    audit(db, "edit", "settings", None, "Updated " + ", ".join(k for k in data if k in EDITABLE_SETTINGS),
          actor_name())
    db.commit()
    return get_settings()
