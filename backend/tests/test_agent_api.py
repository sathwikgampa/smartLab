"""Lab co-pilot HTTP API: /api/chat, /api/chat/stream, /api/agent/uploads and the run audit log."""
from __future__ import annotations

import io
import json

SPEC_KEYS = {"request_id", "response", "status", "confidence", "uncertainty_flag", "uncertainty_message", "checklist",
             "inventory_status", "compatibility", "citations", "agent_trace"}


def _events(raw: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        lines = [l for l in block.splitlines() if l and not l.startswith(":")]
        if lines:
            event = next(l[7:] for l in lines if l.startswith("event: "))
            data = json.loads(next(l[6:] for l in lines if l.startswith("data: ")))
            out.append((event, data))
    return out


def test_chat_returns_the_agent_contract(client):
    body = client.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()
    assert SPEC_KEYS <= set(body)
    assert body["status"] in ("supported", "caution", "refused") and 0 <= body["confidence"] <= 1
    step = body["checklist"][0]
    assert {"step", "action", "safety_note", "chunk_ids", "unsupported"} <= set(step)
    cite = body["citations"][0]
    assert {"chunk_id", "document", "page", "snippet"} <= set(cite) and cite["chunk_id"] in step["chunk_ids"]
    assert all({"step", "tool", "input_summary", "output_summary", "duration_ms"} <= set(t) for t in body["agent_trace"])
    assert "verified" not in body["response"].lower()


def test_stream_emits_trace_steps_then_result(client):
    resp = client.post("/api/chat/stream", json={"question": "What does error E-104 mean?"})
    assert resp.status_code == 200 and resp.mimetype == "text/event-stream"
    events = _events(resp.get_data())
    kinds = [k for k, _ in events]
    assert kinds[-1] == "result" and kinds.count("trace") >= 4
    result = events[-1][1]
    assert [t["step"] for t in result["agent_trace"]] == [d["step"] for k, d in events if k == "trace"]
    assert result["message_id"] and SPEC_KEYS <= set(result)


def test_stream_failure_is_generic(client, monkeypatch):
    from app.agents.orchestrator import Orchestrator

    def boom(self, *a, **k):
        raise RuntimeError("db password is hunter2")

    monkeypatch.setattr(Orchestrator, "run", boom)
    events = _events(client.post("/api/chat/stream", json={"question": "anything"}).get_data())
    assert events[-1][0] == "error" and "hunter2" not in json.dumps(events)


def test_chat_input_validation(client):
    assert client.post("/api/chat", json={"question": "x", "upload_id": "../../etc/passwd"}).status_code == 400
    assert client.post("/api/chat", json={"question": "x", "error_code": "rm -rf /"}).status_code == 400
    assert client.post("/api/chat", json={"question": "x", "session_id": "a b/../c"}).status_code == 400
    assert client.post("/api/chat", json={"question": ["not", "a", "string"]}).status_code == 400


def test_rate_limit(app, client):
    app.config["AGENT_RATE_LIMIT_PER_MIN"] = 2
    codes = [client.post("/api/chat", json={"question": "What does error E-104 mean?"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_upload_validation(client, image_factory):
    bad = client.post("/api/agent/uploads", content_type="multipart/form-data",
                      data={"image": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "display.png")})
    assert bad.status_code == 415
    wrong_ext = client.post("/api/agent/uploads", content_type="multipart/form-data",
                            data={"image": (io.BytesIO(b"hello"), "../../evil.sh")})
    assert wrong_ext.status_code in (400, 415)


def test_upload_then_confirm_flow(client, conn, image_factory, app):
    resp = client.post("/api/agent/uploads", content_type="multipart/form-data",
                       data={"image": (io.BytesIO(image_factory(draw_red=True)), "../../display.png")})
    assert resp.status_code == 201
    up = resp.get_json()
    assert len(up["upload_id"]) == 32 and up["needs_confirmation"] is True
    stored = conn.execute("SELECT stored_name FROM agent_uploads WHERE upload_id = ?", (up["upload_id"],)).fetchone()[0]
    assert ".." not in stored and "display" not in stored  # server-generated name
    assert (app.config["UPLOAD_DIR"] / "agent" / stored).is_file()

    # Simulate the vision model having read a code (the offline test app has no vision model).
    conn.execute("UPDATE agent_uploads SET extracted_codes_json = ? WHERE upload_id = ?", ('["E-104"]', up["upload_id"]))
    conn.commit()
    ask = client.post("/api/chat", json={"question": "What is this error?", "upload_id": up["upload_id"]}).get_json()
    assert ask["needs_confirmation"]["extracted_code"] == "E-104" and ask["checklist"] == []
    assert "Is this the code on your display?" in ask["needs_confirmation"]["question"]

    confirmed = client.post("/api/chat", json={"question": "Confirmed code E-104", "upload_id": up["upload_id"],
                                               "error_code": "E-104"}).get_json()
    assert confirmed["needs_confirmation"] is None and confirmed["checklist"]
    assert confirmed["diagnosis"]["error_code"] == "E-104"


def test_runs_are_audited_and_listed(client):
    first = client.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()
    runs = client.get("/api/agent/runs").get_json()
    assert runs[0]["request_id"] == first["request_id"]
    assert runs[0]["status"] == first["status"] and runs[0]["tools"] and runs[0]["started_at"]
    detail = client.get(f"/api/agent/runs/{first['request_id']}").get_json()
    assert detail["question"] == "What does error E-104 mean?"
    assert "ANTHROPIC" not in json.dumps(detail)
    assert client.get("/api/agent/runs/nope").status_code == 404


def test_history_keeps_structured_result(client):
    body = client.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()
    history = client.get(f"/api/chat/{body['session_id']}").get_json()
    meta = history[1]["meta"]
    assert meta["status"] == body["status"] and meta["checklist"] and meta["agent_trace"]


def test_cross_org_access_is_blocked(app, client):
    app.config["DEFAULT_ORGANIZATION_ID"] = 2  # documents, stock and fixes all belong to organisation 1
    body = client.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()
    assert body["status"] == "refused" and body["citations"] == [] and body["checklist"] == []
    assert client.get("/api/agent/runs").get_json()[0]["request_id"] == body["request_id"]
    app.config["DEFAULT_ORGANIZATION_ID"] = 1
    assert all(r["request_id"] != body["request_id"] for r in client.get("/api/agent/runs").get_json())


def test_info_labels_matrix(client):
    info = client.get("/api/agent/info").get_json()
    assert info["compatibility_label"] == "Limited demo matrix, not expert-reviewed."
    assert info["compatibility_expert_reviewed"] is False


def test_persist_run_fallback_compatibility(app, conn):
    from app.agents.service import persist_run

    fake_result = {
        "request_id": "req-fake-12345",
        "intent": "general",
        "status": "supported",
        "status_label": "Grounded in verified SOP",
        "confidence": 0.95,
        "uncertainty_flag": False,
        "uncertainty_message": None,
        "blockers": [],
        "cautions": [],
        "checklist": [],
        "checklist_label": None,
        "inventory_status": "ok",
        "compatibility": None,
        "compatibility_label": None,
        "calculations": None,
        "diagnosis": None,
        "needs_confirmation": None,
        "source_issues": [],
        "quarantined_passages": [],
        "agent_trace": [],
        "response": "Test response",
        "citations": [],
        "llm_calls": 0,
        "duration_ms": 10,
        "started_at": "2026-10-24 12:00:00",
    }
    with app.app_context():
        msg_id = persist_run(conn, question="Test question?", session_id="test-session-compat", result=fake_result)
        assert msg_id is not None
        row = conn.execute("SELECT team_id FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        assert row["team_id"] == 1
