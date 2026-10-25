"""Lab co-pilot chat (agent runs, plain JSON and Server-Sent Events), conversation history, answer
feedback and raw vector search."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid

from flask import Blueprint, Response, current_app, g, jsonify, request

from ..agents.service import current_organization_id, legacy_fields, make_orchestrator, persist_run
from ..agents.tools import ERROR_CODE, UPLOAD_ID
from ..auth import actor_name
from ..db import audit, dumps, execute, get_db, loads, query_all, query_one
from ..tenancy import organization_id, team_id
from .common import ApiError, int_arg, json_body, rag, require
from .ratelimit import rate_limited

logger = logging.getLogger(__name__)

bp = Blueprint("chat", __name__, url_prefix="/api")

MAX_QUESTION_CHARS = 2000
FEEDBACK_RATINGS = {"helpful", "incorrect", "unsafe", "outdated"}
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _chat_input(data: dict) -> dict:
    """Validate a chat request. Error messages name the field only; nothing internal is echoed."""
    require(data, "question")
    if not isinstance(data["question"], str):
        raise ApiError("question must be a string.")
    question = data["question"].strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(f"Question exceeds {MAX_QUESTION_CHARS} characters.")
    session_id = str(data.get("session_id") or uuid.uuid4().hex)[:64]
    if not _SESSION_ID.fullmatch(session_id):
        raise ApiError("session_id is invalid.")
    doc_ids = data.get("document_ids") or None
    if doc_ids is not None and (not isinstance(doc_ids, list) or len(doc_ids) > 50
                                or not all(isinstance(i, int) and not isinstance(i, bool) for i in doc_ids)):
        raise ApiError("document_ids must be a list of integers.")
    upload_id = data.get("upload_id") or None
    if upload_id is not None and (not isinstance(upload_id, str) or not UPLOAD_ID.fullmatch(upload_id)):
        raise ApiError("upload_id is invalid.")
    error_code = data.get("error_code") or None
    if error_code is not None and (not isinstance(error_code, str) or not ERROR_CODE.fullmatch(error_code.strip())):
        raise ApiError("error_code must look like E-104 or IMB-01.")
    return {"question": question, "session_id": session_id, "document_ids": doc_ids, "upload_id": upload_id,
            "error_code": error_code.strip() if error_code else None}


def _response(inp: dict, result: dict, message_id: int) -> dict:
    return {"session_id": inp["session_id"], "message_id": message_id, **result,
            **legacy_fields(result, inp["question"])}


@bp.post("/chat")
@rate_limited("agent")
def chat():
    """Run the lab co-pilot and return its structured result (plus the pre-agent fields)."""
    inp = _chat_input(json_body())
    orchestrator = make_orchestrator(document_ids=inp["document_ids"])
    result = orchestrator.run(inp["question"], upload_id=inp["upload_id"], error_code=inp["error_code"])
    try:
        active_team_id = team_id()
    except Exception:
        active_team_id = int(current_app.config.get("DEFAULT_TEAM_ID", 1))
    msg_id = persist_run(get_db(), question=inp["question"], session_id=inp["session_id"], result=result,
                         user_id=_user_id(), team_id=active_team_id, organization_id=current_organization_id())
    return jsonify(_response(inp, result, msg_id))


def _user_id() -> int | None:
    return (g.get("user") or {}).get("id")


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@bp.post("/chat/stream")
@rate_limited("agent")
def chat_stream():
    """Same run as /api/chat, streamed: one ``trace`` event per completed step, then ``result`` (or ``error``).

    The run is executed *and persisted* on a worker thread with its own app context, so the audit record in
    ``agent_runs`` is written even if the client disconnects mid-stream. The response generator holds no
    request context and no database connection while it waits (the request's pooled connection is returned
    as soon as this view returns).
    """
    inp = _chat_input(json_body())
    app = current_app._get_current_object()
    user_id = _user_id()
    try:
        active_team_id = team_id()
    except Exception:
        active_team_id = int(app.config.get("DEFAULT_TEAM_ID", 1))
    org_id = current_organization_id()
    events: queue.Queue = queue.Queue()
    orchestrator = make_orchestrator(on_event=lambda kind, payload: events.put((kind, payload)),
                                     document_ids=inp["document_ids"])
    hard_limit = float(app.config.get("AGENT_TIMEOUT_S", 90)) + 30

    def work() -> None:
        with app.app_context():  # own pooled connection, released by close_db when the context ends
            try:
                result = orchestrator.run(inp["question"], upload_id=inp["upload_id"], error_code=inp["error_code"])
                msg_id = persist_run(get_db(), question=inp["question"], session_id=inp["session_id"],
                                     result=result, user_id=user_id, team_id=active_team_id, organization_id=org_id)
                events.put(("result", _response(inp, result, msg_id)))
            except Exception:  # noqa: BLE001 - details go to the log only
                logger.exception("Streaming agent run failed")
                events.put(("error", {"error": "The co-pilot could not complete this request."}))

    threading.Thread(target=work, daemon=True, name="agent-run").start()

    def generate():
        started = time.monotonic()
        yield ": stream open\n\n"
        try:
            while True:
                try:
                    kind, payload = events.get(timeout=10)
                except queue.Empty:
                    if time.monotonic() - started > hard_limit:
                        yield _sse("error", {"error": "The co-pilot timed out."})
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(kind, payload)
                if kind in ("result", "error"):
                    return
        except GeneratorExit:  # client went away; the worker still finishes and records the run
            logger.info("Client left the co-pilot stream; the run continues and will be recorded.")
            raise

    # no-transform: intermediaries must not buffer or re-encode the event stream.
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@bp.post("/chat/<session_id>/messages")
def record_message(session_id: str):
    """Persist a client-composed exchange (used for image questions answered by /api/vision)."""
    data = json_body()
    require(data, "question", "answer")
    sid = session_id[:64]
    t_id = team_id()
    execute("INSERT INTO chat_messages (team_id, session_id, role, content, meta_json) VALUES (?, ?, 'user', ?, ?)",
            (t_id, sid, str(data["question"])[:MAX_QUESTION_CHARS], dumps({"attachment": data.get("attachment")})))
    msg_id = execute(
        "INSERT INTO chat_messages (team_id, session_id, role, content, citations_json, meta_json) VALUES (?, ?, 'assistant', ?, ?, ?)",
        (t_id, sid, str(data["answer"])[:20000], dumps(data.get("citations") or []), dumps(data.get("meta") or {})))
    return jsonify({"session_id": sid, "message_id": msg_id}), 201


@bp.get("/chat/sessions")
def sessions():
    """Recent conversations, newest first, titled by their first question."""
    t_id = team_id()
    limit = int_arg("limit", 20, 1, 100)
    return jsonify(query_all(
        """
        SELECT session_id,
               (SELECT content FROM chat_messages f WHERE f.session_id = m.session_id AND f.role = 'user' AND f.team_id = m.team_id
                 ORDER BY f.id LIMIT 1) AS title,
               COUNT(*) AS message_count, MIN(created_at) AS started_at, MAX(created_at) AS updated_at, MAX(id) AS last_id
          FROM chat_messages m WHERE m.team_id = ? GROUP BY session_id ORDER BY last_id DESC LIMIT ?
        """, (t_id, limit)))


@bp.get("/chat/<session_id>")
def history(session_id: str):
    t_id = team_id()
    rows = query_all("SELECT * FROM chat_messages WHERE session_id = ? AND team_id = ? ORDER BY id", (session_id[:64], t_id))
    feedback = {r["message_id"]: r for r in query_all(
        "SELECT * FROM answer_feedback WHERE message_id IN (SELECT id FROM chat_messages WHERE session_id = ? AND team_id = ?)",
        (session_id[:64], t_id))}
    for r in rows:
        r["citations"] = loads(r.pop("citations_json"), [])
        r["meta"] = loads(r.pop("meta_json"), {})
        r["feedback"] = feedback.get(r["id"])
    return jsonify(rows)


@bp.delete("/chat/<session_id>")
def delete_session(session_id: str):
    t_id = team_id()
    if not query_one("SELECT 1 AS x FROM chat_messages WHERE session_id = ? AND team_id = ?", (session_id[:64], t_id)):
        raise ApiError("Conversation not found.", 404)
    execute("DELETE FROM chat_messages WHERE session_id = ? AND team_id = ?", (session_id[:64], t_id))
    return "", 204


# ---------------------------------------------------------------------- feedback
@bp.post("/messages/<int:message_id>/feedback")
def give_feedback(message_id: int):
    """Rate an answer. 'incorrect', 'unsafe' and 'outdated' ratings enter the expert review queue."""
    t_id = team_id()
    msg = query_one("SELECT * FROM chat_messages WHERE id = ? AND role = 'assistant' AND team_id = ?", (message_id, t_id))
    if msg is None:
        raise ApiError("Answer not found.", 404)
    data = json_body()
    rating = data.get("rating")
    if rating not in FEEDBACK_RATINGS:
        raise ApiError(f"rating must be one of: {', '.join(sorted(FEEDBACK_RATINGS))}.", 422)
    comment = str(data.get("comment") or "").strip()[:2000] or None
    if rating != "helpful" and not comment:
        raise ApiError("Please describe what is wrong so a reviewer can check it.", 422)
    db = get_db()
    db.execute("DELETE FROM answer_feedback WHERE message_id = ? AND status = 'open'", (message_id,))
    status = "closed" if rating == "helpful" else "open"
    cur = db.execute("INSERT INTO answer_feedback (message_id, rating, comment, status) VALUES (?, ?, ?, ?)",
                     (message_id, rating, comment, status))
    if status == "open":
        audit(db, "flag", "answer", message_id, f"Answer flagged as {rating}: {comment}", actor_name())
    db.commit()
    return jsonify(query_one("SELECT * FROM answer_feedback WHERE id = ?", (cur.lastrowid,))), 201


@bp.get("/feedback")
def feedback_queue():
    t_id = team_id()
    status = request.args.get("status", "open")
    rows = query_all(
        """
        SELECT f.*, a.content AS answer, a.session_id, a.citations_json,
               (SELECT q.content FROM chat_messages q WHERE q.session_id = a.session_id AND q.role = 'user'
                  AND q.id < a.id ORDER BY q.id DESC LIMIT 1) AS question
          FROM answer_feedback f JOIN chat_messages a ON a.id = f.message_id
         WHERE f.rating != 'helpful' AND (? = 'all' OR f.status = ?) AND a.team_id = ?
         ORDER BY f.id DESC
        """, (status, status, t_id))
    for r in rows:
        r["citations"] = loads(r.pop("citations_json"), [])
    return jsonify(rows)


@bp.post("/feedback/<int:feedback_id>/resolve")
def resolve_feedback(feedback_id: int):
    t_id = team_id()
    item = query_one(
        "SELECT f.* FROM answer_feedback f JOIN chat_messages a ON a.id = f.message_id WHERE f.id = ? AND a.team_id = ?",
        (feedback_id, t_id))
    if item is None:
        raise ApiError("Feedback item not found.", 404)
    if item["status"] != "open":
        raise ApiError("This item has already been resolved.", 409)
    data = json_body()
    resolution = str(data.get("resolution") or "").strip()
    if not resolution:
        raise ApiError("A resolution note is required.", 422)
    db = get_db()
    db.execute("UPDATE answer_feedback SET status = 'resolved', resolution = ?, resolved_at = datetime('now') WHERE id = ?",
               (resolution[:2000], feedback_id))
    audit(db, "resolve", "answer", item["message_id"], f"Review resolved: {resolution[:200]}", actor_name())
    db.commit()
    return jsonify(query_one("SELECT * FROM answer_feedback WHERE id = ?", (feedback_id,)))


@bp.post("/search/vector")
def vector_search():
    data = json_body()
    require(data, "query")
    k = data.get("k", 5)
    if not isinstance(k, int) or not 1 <= k <= 50:
        raise ApiError("k must be an integer between 1 and 50.")
    engine = rag()
    hits = engine.retrieve(get_db(), str(data["query"]), k=k, team_id=team_id())
    return jsonify({"query": data["query"], "min_score": engine.min_score,
                    "results": [h.to_dict() | {"is_evidence": h.score >= engine.min_score} for h in hits]})
