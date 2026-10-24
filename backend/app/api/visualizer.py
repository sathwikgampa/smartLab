"""SmartLab Visualizer: generate scientific diagrams from text and use them in the workspace.

Every generation attempt, including refused and failed ones, is written to ``generated_images`` (append-only)
and to the audit log, with the signed-in account taken from the server-side session. Images are readable by
any signed-in member of the lab, like experiments and project files, so a figure inserted into a protocol
renders for the whole team; the history list shows the caller's own images by default.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from ..auth import actor_name, current_user
from ..db import audit, get_db, query_all, query_one
from ..services.imagegen import (ASPECT_RATIOS, MAX_PROMPT_CHARS, STYLES, ImageGenerator, ImageGenRejected,
                                 ImageGenUnavailable, sanitize_prompt)
from .common import ApiError, int_arg, json_body, require
from .ratelimit import rate_limited

bp = Blueprint("visualizer", __name__, url_prefix="/api/visualizer")

AI_NOTICE = "AI-generated illustration for communication only; it is not experimental data."


def _generator() -> ImageGenerator:
    return current_app.extensions["smartlab"]["imagegen"]


def _image_dir() -> Path:
    return current_app.config["UPLOAD_DIR"] / "generated"


def _public(row: dict) -> dict:
    ok = row["status"] == "succeeded"
    keys = ("id", "user_id", "actor", "project_id", "prompt", "final_prompt", "style", "aspect_ratio", "provider",
            "model", "status", "error", "width", "height", "size_bytes", "sha256", "duration_ms", "created_at")
    return {**{k: row[k] for k in keys}, "notice": AI_NOTICE,
            "image_url": f"/api/visualizer/images/{row['id']}/file" if ok else None}


def _image(image_id: int) -> dict:
    row = query_one("SELECT * FROM generated_images WHERE id = ?", (image_id,))
    if row is None:
        raise ApiError("Image not found.", 404)
    return row


def _stored_image(image_id: int) -> tuple[dict, Path]:
    row = _image(image_id)
    if row["status"] != "succeeded":
        raise ApiError("This generation did not produce an image.", 409)
    path = _image_dir() / row["stored_name"]
    if not path.is_file():
        raise ApiError("Stored image is missing.", 410)
    return row, path


def _record(fields: dict) -> dict:
    """Insert one generation attempt and its audit entry in a single commit."""
    user = current_user()
    fields = {"organization_id": current_app.config["DEFAULT_ORGANIZATION_ID"],
              "user_id": user["id"] if user else None, "actor": actor_name(), **fields}
    db = get_db()
    cols = ", ".join(fields)
    image_id = db.execute(f"INSERT INTO generated_images ({cols}) VALUES ({', '.join('?' for _ in fields)})",
                          tuple(fields.values())).lastrowid
    audit(db, f"image.{fields['status']}", "generated_image", image_id,
          f"Visualizer ({fields['status']}): {fields['prompt'][:300]}", fields["actor"])
    db.commit()
    return _image(image_id)


def _alt_text(prompt: str) -> str:
    """The prompt made safe for Markdown image alt text (no brackets, emphasis or line breaks)."""
    return re.sub(r"[\[\]()<>*_`!\\|#]", "", prompt)[:120].strip() or "Generated figure"


@bp.get("/info")
def info():
    gen = _generator()
    return jsonify({"enabled": gen.available, "provider": gen.provider if gen.available else None,
                    "model": gen.model, "styles": list(STYLES), "aspect_ratios": list(ASPECT_RATIOS),
                    "max_prompt_chars": MAX_PROMPT_CHARS, "notice": AI_NOTICE})


@bp.post("/generate")
@rate_limited("visualizer", "IMAGE_RATE_LIMIT_PER_MIN")
def generate():
    data = json_body()
    require(data, "prompt")
    style = data.get("style") or "diagram"
    aspect_ratio = data.get("aspect_ratio") or "1:1"
    if style not in STYLES:
        raise ApiError(f"style must be one of: {', '.join(STYLES)}.", 422)
    if aspect_ratio not in ASPECT_RATIOS:
        raise ApiError(f"aspect_ratio must be one of: {', '.join(ASPECT_RATIOS)}.", 422)
    project_id = data.get("project_id")
    if project_id is not None and (not isinstance(project_id, int)
                                   or not query_one("SELECT id FROM projects WHERE id = ?", (project_id,))):
        raise ApiError("Project not found.", 404)
    gen = _generator()
    if not gen.available:
        raise ApiError("Image generation is not configured on this server.", 503)

    raw_prompt = str(data["prompt"])
    base = {"prompt": raw_prompt[:MAX_PROMPT_CHARS], "style": style, "aspect_ratio": aspect_ratio,
            "project_id": project_id, "provider": gen.provider, "model": gen.model}
    try:
        prompt = sanitize_prompt(raw_prompt)
    except ImageGenRejected as exc:
        row = _record({**base, "status": "blocked", "error": str(exc)})
        raise ApiError(str(exc), 422, image=_public(row)) from exc

    # Do not hold a database transaction open across the slow provider call.
    get_db().commit()
    started = time.monotonic()
    try:
        result = gen.generate(prompt, style, aspect_ratio)
    except (ImageGenRejected, ImageGenUnavailable) as exc:
        blocked = isinstance(exc, ImageGenRejected)
        row = _record({**base, "prompt": prompt, "status": "blocked" if blocked else "failed", "error": str(exc),
                       "duration_ms": int((time.monotonic() - started) * 1000)})
        raise ApiError(str(exc), 422 if blocked else 502, image=_public(row)) from exc

    stored_name = f"{uuid.uuid4().hex}.png"
    (_image_dir() / stored_name).write_bytes(result.png)
    row = _record({**base, "prompt": prompt, "final_prompt": result.final_prompt, "status": "succeeded",
                   "stored_name": stored_name, "mime_type": "image/png", "width": result.width,
                   "height": result.height, "size_bytes": len(result.png),
                   "sha256": hashlib.sha256(result.png).hexdigest(),
                   "duration_ms": int((time.monotonic() - started) * 1000)})
    return jsonify(_public(row)), 201


@bp.get("/images")
def list_images():
    """Generation history, newest first. ``mine=false`` (the whole lab's) is for administrators;
    ``all=true`` includes refused and failed attempts."""
    limit = int_arg("limit", 24, 1, 100)
    mine = request.args.get("mine", "true").lower() != "false"
    user = current_user()
    if not mine and user is not None and user["role"] != "admin":
        raise ApiError("Only lab administrators can view the whole lab's generations.", 403)
    where, params = [], []
    if mine and user is not None:
        where.append("user_id = ?")
        params.append(user["id"])
    if request.args.get("all", "false").lower() != "true":
        where.append("status = 'succeeded'")
    sql = "SELECT * FROM generated_images" + (f" WHERE {' AND '.join(where)}" if where else "")
    return jsonify([_public(r) for r in query_all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))])


@bp.get("/images/<int:image_id>")
def get_image(image_id: int):
    return jsonify(_public(_image(image_id)))


@bp.get("/images/<int:image_id>/file")
def image_file(image_id: int):
    row, path = _stored_image(image_id)
    download = request.args.get("download") == "1"
    resp = send_file(path, mimetype="image/png", as_attachment=download,
                     download_name=f"smartlab-visual-{row['id']}.png", max_age=86400)
    # The file is a PNG re-encoded by the server; these headers keep it from ever being treated as a page.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp.headers["Cache-Control"] = "private, max-age=86400, immutable"
    return resp


@bp.post("/images/<int:image_id>/save-to-project")
def save_to_project(image_id: int):
    """Copy the image into a project's shared files, where the whole team can find and download it."""
    row, path = _stored_image(image_id)
    data = json_body()
    require(data, "project_id")
    project = query_one("SELECT id, name FROM projects WHERE id = ?", (data["project_id"],))
    if project is None:
        raise ApiError("Project not found.", 404)
    user = current_user()
    stored_name = f"{uuid.uuid4().hex}.png"
    shutil.copyfile(path, current_app.config["UPLOAD_DIR"] / "shared" / stored_name)
    slug = re.sub(r"[^a-z0-9]+", "-", row["prompt"].lower()).strip("-")[:60] or "figure"
    db = get_db()
    file_id = db.execute(
        "INSERT INTO shared_files (project_id, stored_name, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project["id"], stored_name, f"visualizer-{row['id']}-{slug}.png", "image/png", row["size_bytes"],
         user["profile_id"] if user else None)).lastrowid
    audit(db, "image.save_to_project", "generated_image", row["id"],
          f"Saved Visualizer image #{row['id']} to project '{project['name']}' (file #{file_id}).", actor_name())
    db.commit()
    return jsonify(query_one("SELECT * FROM shared_files WHERE id = ?", (file_id,))), 201


@bp.post("/images/<int:image_id>/attach-to-experiment")
def attach_to_experiment(image_id: int):
    """Append the image to an experiment's protocol notes as a Markdown figure with an AI-generated caption."""
    row, _ = _stored_image(image_id)
    data = json_body()
    require(data, "experiment_id")
    exp = query_one("SELECT id, title, protocol_markdown FROM experiments WHERE id = ?", (data["experiment_id"],))
    if exp is None:
        raise ApiError("Experiment not found.", 404)
    figure = (f"![{_alt_text(row['prompt'])}](/api/visualizer/images/{row['id']}/file)\n\n"
              f"*Figure: SmartLab Visualizer #{row['id']} ({row['model']}, {row['created_at']} UTC). {AI_NOTICE}*")
    markdown = f"{(exp['protocol_markdown'] or '').rstrip()}\n\n{figure}\n".lstrip()
    db = get_db()
    db.execute("UPDATE experiments SET protocol_markdown = ?, updated_at = datetime('now') WHERE id = ?",
               (markdown, exp["id"]))
    audit(db, "image.attach_to_experiment", "experiment", exp["id"],
          f"Inserted Visualizer image #{row['id']} into the notes of '{exp['title']}'.", actor_name())
    db.commit()
    return jsonify({"experiment_id": exp["id"], "protocol_markdown": markdown})
