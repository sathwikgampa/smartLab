"""Multimodal image-to-document RAG endpoint."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import get_db
from ..security import IMAGE_EXTENSIONS, validate_upload
from ..tenancy import team_id
from ..vision.analyzer import analyze_image
from .common import llm, rag, upload_limits

bp = Blueprint("vision", __name__, url_prefix="/api/vision")


@bp.post("/analyze")
def analyze():
    image = validate_upload(request.files.get("image"), IMAGE_EXTENSIONS, **upload_limits())
    return jsonify(analyze_image(get_db(), rag(), llm(), image, request.form.get("note", ""), team_id=team_id()))
