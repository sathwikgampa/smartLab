"""Glue between the Flask API and the orchestrator: setup, persistence and the audit log."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from flask import current_app

from ..db import dumps
from ..guardrails import GuardrailReport, sanitize_vision_result, screen_query, screen_untrusted_text
from ..security import ValidatedFile
from .orchestrator import AgentLimits, Orchestrator
from .tools import ERROR_CODE, ToolEnv

logger = logging.getLogger(__name__)

EVIDENCE_STATUS = {"supported": "grounded", "caution": "partial", "refused": "insufficient"}


def current_organization_id() -> int:
    """The organisation every query is scoped to.

    TODO(auth): there is no authentication yet, so this is the configured default organisation.
    Replace with the signed-in user's organisation once auth exists.
    """
    return int(current_app.config["DEFAULT_ORGANIZATION_ID"])


def make_orchestrator(*, on_event: Callable[[str, dict[str, Any]], None] | None = None,
                      document_ids: list[int] | None = None) -> Orchestrator:
    app = current_app._get_current_object()
    return Orchestrator(ToolEnv.from_app(app, current_organization_id()), app.extensions["smartlab"]["llm"],
                        AgentLimits.from_config(app.config), on_event=on_event, document_ids=document_ids)


def legacy_fields(result: dict[str, Any], question: str) -> dict[str, Any]:
    """Fields the pre-agent chat API returned, derived from the agent result (kept for older clients)."""
    llm_notice = next((c for c in result["cautions"] if "language model" in c), None)
    return {
        "answer": result["response"],
        "evidence_status": EVIDENCE_STATUS[result["status"]],
        "warnings": result["blockers"] + result["cautions"],
        "mode": "agent",
        "guardrails": screen_query(question).to_dict(),
        "requires_verification": result["status"] != "supported",
        "service_notice": llm_notice,
        "missing_terms": [],
        "retrieved": [],
    }


def persist_run(db, *, question: str, session_id: str, result: dict[str, Any],
                user_id: int | None = None) -> int:
    """Store the exchange in chat history and an audit record in ``agent_runs``. Returns the message id."""
    db.execute("INSERT INTO chat_messages (session_id, role, content, user_id) VALUES (?, 'user', ?, ?)",
               (session_id, question, user_id))
    meta = {k: result[k] for k in (
        "request_id", "intent", "status", "status_label", "confidence", "uncertainty_flag", "uncertainty_message",
        "blockers", "cautions", "checklist", "checklist_label", "inventory_status", "compatibility",
        "compatibility_label", "calculations", "diagnosis", "needs_confirmation", "source_issues",
        "quarantined_passages", "agent_trace")}
    meta |= {k: v for k, v in legacy_fields(result, question).items() if k in (
        "evidence_status", "warnings", "mode", "requires_verification", "service_notice", "missing_terms")}
    cur = db.execute(
        "INSERT INTO chat_messages (session_id, role, content, citations_json, meta_json, user_id) "
        "VALUES (?, 'assistant', ?, ?, ?, ?)",
        (session_id, result["response"], dumps(result["citations"]), dumps(meta), user_id))
    message_id = cur.lastrowid
    tools = [{k: t[k] for k in ("step", "tool", "ok", "duration_ms", "output_summary")} for t in result["agent_trace"]]
    citations = [{k: c[k] for k in ("chunk_id", "document", "page")} for c in result["citations"]]
    db.execute(
        "INSERT INTO agent_runs (request_id, organization_id, user_id, session_id, question, intent, status, "
        "confidence, uncertainty_flag, blockers_json, tools_json, citations_json, llm_calls, duration_ms, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (result["request_id"], current_organization_id(), None if user_id is None else str(user_id), session_id, question[:2000], result["intent"],
         result["status"], result["confidence"], bool(result["uncertainty_flag"]), dumps(result["blockers"]),
         dumps(tools), dumps(citations), result["llm_calls"], result["duration_ms"], result["started_at"]))
    db.commit()
    return message_id


def list_runs(db, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM agent_runs WHERE organization_id = ? ORDER BY id DESC LIMIT ?",
                      (current_organization_id(), limit)).fetchall()
    return [_run_row(r) for r in rows]


def get_run(db, request_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM agent_runs WHERE request_id = ? AND organization_id = ?",
                     (request_id, current_organization_id())).fetchone()
    return _run_row(row) if row else None


def _run_row(row) -> dict[str, Any]:
    r = dict(row)
    for key in ("blockers", "tools", "citations"):
        r[key] = json.loads(r.pop(f"{key}_json") or "[]")
    r["uncertainty_flag"] = bool(r["uncertainty_flag"])
    return r


# ------------------------------------------------------------------ image uploads
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,4})[- ]?(\d{2,4})(?![\w])")


def read_display(llm, image: ValidatedFile) -> dict[str, Any]:
    """Read display text and error codes from an instrument photo at upload time.

    This is the only place an image goes to the model; tools later read the stored result by
    ``upload_id``. The text is image-derived and therefore untrusted: instruction-like text is dropped,
    and codes are only ever suggestions until the user confirms one.
    """
    from ..vision.analyzer import VISION_SCHEMA, VISION_SYSTEM_PROMPT, heuristic_features
    from ..services.llm import LLMUnavailable

    features = heuristic_features(image.data)
    observed: list[str] = []
    equipment = None
    mode = "heuristic"
    if getattr(llm, "available", False):
        try:
            out = llm.complete_json(VISION_SYSTEM_PROMPT, [
                llm.image_block(image.data, image.mime_type),
                {"type": "text", "text": "Transcribe any error code and text shown on the instrument display exactly."},
            ], VISION_SCHEMA, 2000, max_retries=0, timeout=30)
            out = sanitize_vision_result(out, GuardrailReport())
            observed = [str(t)[:200] for t in (out.get("observed_text") or [])[:20]]
            equipment = str(out.get("equipment_type") or "")[:80] or None
            mode = "vision-llm"
        except (LLMUnavailable, KeyError, TypeError) as exc:
            logger.info("Display reading unavailable: %s", exc)
    flagged = [t for t in observed if screen_untrusted_text(t)]
    observed = [t for t in observed if t not in flagged]
    codes = sorted({f"{a}-{b}" for t in observed for a, b in _CODE.findall(t) if ERROR_CODE.fullmatch(f"{a}-{b}")})
    return {"observed_text": observed, "extracted_codes": codes, "equipment_type": equipment, "mode": mode,
            "image_quality": features["image_quality"], "dropped_text": len(flagged)}


def store_upload(db, image: ValidatedFile, upload_dir, reading: dict[str, Any]) -> str:
    """Save the image under its server-generated name and record what was read from it."""
    import uuid

    upload_id = uuid.uuid4().hex
    target_dir = upload_dir / "agent"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / image.stored_name).write_bytes(image.data)
    db.execute("INSERT INTO agent_uploads (upload_id, organization_id, stored_name, mime_type, size_bytes, "
               "equipment_type, observed_text_json, extracted_codes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (upload_id, current_organization_id(), image.stored_name, image.mime_type, image.size,
                reading["equipment_type"], dumps(reading["observed_text"]), dumps(reading["extracted_codes"])))
    db.commit()
    return upload_id
