"""Document library: upload/index PDFs, list, delete, page previews with highlights."""
from __future__ import annotations

import io

from flask import Blueprint, current_app, jsonify, request, send_file

from ..auth import actor_name
from ..db import audit, get_db, query_all, query_one
from ..rag.engine import APPROVAL_STATUSES, DuplicateDocumentError, clean_metadata
from ..rag.pdf_extractor import render_page_png
from ..security import DOCUMENT_EXTENSIONS, validate_upload
from .common import ApiError, int_arg, json_body, rag, upload_limits

bp = Blueprint("documents", __name__, url_prefix="/api/documents")


def _get_doc(doc_id: int) -> dict:
    doc = query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if doc is None:
        raise ApiError("Document not found.", 404)
    return doc


SORTABLE = {"title", "doc_type", "category", "version", "owner", "approval_status", "updated_at", "uploaded_at",
            "page_count"}


@bp.get("")
def list_documents():
    """List documents. Filters: ``q`` (title/code/owner), ``category``, ``status``, ``doc_type``;
    ``sort`` (allow-listed column) and ``order``."""
    sql = "SELECT * FROM documents WHERE 1=1"
    params: list = []
    if request.args.get("q"):
        sql += " AND (title LIKE ? OR doc_code LIKE ? OR owner LIKE ? OR original_name LIKE ?)"
        params += [f"%{request.args['q']}%"] * 4
    for arg, col in (("category", "category"), ("status", "approval_status"), ("doc_type", "doc_type")):
        if request.args.get(arg):
            sql += f" AND {col} = ?"
            params.append(request.args[arg])
    sort = request.args.get("sort", "updated_at")
    sort = sort if sort in SORTABLE else "updated_at"
    order = "ASC" if request.args.get("order") == "asc" else "DESC"
    # Case-insensitive text sort on both SQLite and PostgreSQL (COLLATE NOCASE is SQLite-only).
    sort_expr = sort if sort == "page_count" else f"LOWER({sort})"
    return jsonify(query_all(f"{sql} ORDER BY {sort_expr} {order}, id DESC", params))


@bp.get("/facets")
def facets():
    """Distinct categories/types for filter controls, plus the allowed approval statuses."""
    return jsonify({
        "categories": [r["category"] for r in query_all("SELECT DISTINCT category FROM documents ORDER BY category")],
        "doc_types": [r["doc_type"] for r in query_all("SELECT DISTINCT doc_type FROM documents ORDER BY doc_type")],
        "approval_statuses": list(APPROVAL_STATUSES),
    })


@bp.post("")
def upload_document():
    validated = validate_upload(request.files.get("file"), DOCUMENT_EXTENSIONS, **upload_limits())
    form = request.form
    metadata = {k: form[k] for k in ("category", "doc_code", "version", "owner", "approval_status") if k in form}
    metadata.setdefault("approval_status", "unreviewed")  # nothing is approved implicitly on upload
    db = get_db()
    try:
        doc = rag().ingest(db, validated, current_app.config["UPLOAD_DIR"], title=form.get("title"),
                           doc_type=form.get("doc_type") or "SOP", metadata=metadata)
    except DuplicateDocumentError as exc:
        raise ApiError(str(exc), 409, document=exc.existing) from exc
    except ValueError as exc:
        raise ApiError(str(exc), 422) from exc
    audit(db, "upload", "document", doc["id"], f"Uploaded and indexed '{doc['title']}' ({doc['page_count']} pages)",
          actor_name())
    db.commit()
    return jsonify(doc), 201


@bp.patch("/<int:doc_id>")
def update_document(doc_id: int):
    """Edit metadata. Approval status changes are recorded in the audit log."""
    doc = _get_doc(doc_id)
    data = json_body()
    try:
        meta = clean_metadata(data)
    except ValueError as exc:
        raise ApiError(str(exc), 422) from exc
    for key in ("title", "doc_type"):
        if key in data:
            value = str(data[key] or "").strip()
            if not value:
                raise ApiError(f"{key} cannot be empty.", 422)
            meta[key] = value[:200]
    if not meta:
        return jsonify(doc)
    db = get_db()
    sets = ", ".join(f"{k} = ?" for k in meta)
    extra = ", reviewed_at = datetime('now')" if "approval_status" in meta else ""
    db.execute(f"UPDATE documents SET {sets}{extra}, updated_at = datetime('now') WHERE id = ?", [*meta.values(), doc_id])
    if meta.get("approval_status") and meta["approval_status"] != doc["approval_status"]:
        audit(db, "status_change", "document", doc_id,
              f"'{doc['title']}' {doc['approval_status']} -> {meta['approval_status']}", actor_name())
    else:
        audit(db, "edit", "document", doc_id, f"Edited metadata of '{doc['title']}'", actor_name())
    db.commit()
    return jsonify(_get_doc(doc_id))


@bp.post("/<int:doc_id>/reindex")
def reindex_document(doc_id: int):
    _get_doc(doc_id)
    db = get_db()
    try:
        doc = rag().reindex(db, doc_id, current_app.config["UPLOAD_DIR"])
    except FileNotFoundError as exc:
        raise ApiError(str(exc), 410) from exc
    except ValueError as exc:
        raise ApiError(str(exc), 422) from exc
    audit(db, "reindex", "document", doc_id, f"Re-indexed '{doc['title']}' ({doc['chunk_count']} chunks)",
          actor_name())
    db.commit()
    return jsonify(doc)


@bp.get("/<int:doc_id>")
def get_document(doc_id: int):
    doc = _get_doc(doc_id)
    doc["pages"] = query_all(
        "SELECT page, COUNT(*) AS chunks FROM chunks WHERE document_id = ? GROUP BY page ORDER BY page", (doc_id,))
    doc["other_versions"] = query_all(
        "SELECT id, title, version, approval_status, updated_at FROM documents "
        "WHERE doc_code = ? AND id != ? ORDER BY updated_at DESC", (doc["doc_code"], doc_id)) if doc["doc_code"] else []
    doc["history"] = query_all(
        "SELECT action, summary, actor, created_at FROM audit_log WHERE entity_type = 'document' AND entity_id = ? "
        "ORDER BY id DESC LIMIT 20", (doc_id,))
    return jsonify(doc)


@bp.delete("/<int:doc_id>")
def delete_document(doc_id: int):
    doc = _get_doc(doc_id)
    db = get_db()
    rag().delete(db, doc_id, current_app.config["UPLOAD_DIR"])
    audit(db, "delete", "document", doc_id, f"Removed '{doc['title']}' from the knowledge base", actor_name())
    db.commit()
    return "", 204


@bp.get("/<int:doc_id>/file")
def download_document(doc_id: int):
    doc = _get_doc(doc_id)
    path = current_app.config["UPLOAD_DIR"] / doc["stored_name"]
    if not path.exists():
        raise ApiError("Stored file is missing.", 410)
    return send_file(path, mimetype="application/pdf", download_name=doc["original_name"])


@bp.get("/<int:doc_id>/pages/<int:page>")
def page_text(doc_id: int, page: int):
    doc = _get_doc(doc_id)
    if not 1 <= page <= doc["page_count"]:
        raise ApiError(f"Page must be between 1 and {doc['page_count']}.", 404)
    chunks = query_all("SELECT id, chunk_index, text FROM chunks WHERE document_id = ? AND page = ? ORDER BY chunk_index",
                       (doc_id, page))
    return jsonify({"document_id": doc_id, "title": doc["title"], "page": page, "page_count": doc["page_count"],
                    "chunks": chunks})


@bp.get("/<int:doc_id>/pages/<int:page>/preview")
def page_preview(doc_id: int, page: int):
    """Render a page as PNG. ``highlight`` (repeatable) phrases are highlighted in emerald."""
    doc = _get_doc(doc_id)
    zoom = (int_arg("zoom", 150, 50, 300) or 150) / 100
    phrases: list[str] = []
    for text in request.args.getlist("highlight")[:5]:
        # search_for matches within lines, so split long snippets into short word runs.
        words = text.split()
        phrases += [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
    path = current_app.config["UPLOAD_DIR"] / doc["stored_name"]
    try:
        png = render_page_png(str(path), page, phrases, zoom)
    except IndexError as exc:
        raise ApiError(str(exc), 404) from exc
    return send_file(io.BytesIO(png), mimetype="image/png", max_age=0)
