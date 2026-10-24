"""Streaming, ingestion and audit-trail behaviour introduced by the performance hardening."""
from __future__ import annotations

import threading
import time

import pytest

from app import create_app
from app.config import TestConfig
from app.db import audit, connect, database_target

from .test_auth import ORIGIN, signup


def _run_count(app) -> int:
    c = connect(database_target(app.config))
    try:
        return c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
    finally:
        c.close()


def test_stream_run_is_recorded_after_client_disconnects(app, client, monkeypatch):
    from app.agents.orchestrator import Orchestrator

    gate = threading.Event()
    original = Orchestrator.run

    def held(self, *args, **kwargs):
        gate.wait(10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "run", held)
    before = _run_count(app)
    resp = client.post("/api/chat/stream", json={"question": "What does error E-104 mean?"}, buffered=False)
    body = iter(resp.response)
    assert next(body).startswith(b": stream open")
    resp.close()  # the browser goes away before the run finishes
    gate.set()

    deadline = time.monotonic() + 15
    while _run_count(app) == before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _run_count(app) == before + 1


def test_stream_headers_disable_buffering(client):
    resp = client.post("/api/chat/stream", json={"question": "What does error E-104 mean?"})
    assert resp.headers["Cache-Control"] == "no-cache, no-transform"
    assert resp.headers["X-Accel-Buffering"] == "no"


@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET summary = 'rewritten'",
    "DELETE FROM audit_log",
    "UPDATE agent_runs SET status = 'supported'",
    "DELETE FROM agent_runs",
])
def test_audit_tables_are_append_only(conn, statement):
    audit(conn, "edit", "settings", None, "baseline entry")
    conn.execute("INSERT INTO agent_runs (request_id, question, status, started_at) VALUES ('r-1', 'q', 'refused', 'x')")
    conn.commit()
    with pytest.raises(Exception, match="append-only"):
        conn.execute(statement)
    conn.rollback()


def test_audit_actor_comes_from_the_session_not_the_body(tmp_path):
    app = create_app(TestConfig, DATA_DIR=tmp_path, DATABASE_PATH=tmp_path / "test.db",
                     UPLOAD_DIR=tmp_path / "uploads", INDEX_DIR=tmp_path / "index", AUTH_REQUIRED=True)
    web = app.test_client()
    assert signup(web, name="Ada Lovelace").status_code == 201
    created = web.post("/api/reagents", json={"name": "Audit buffer", "quantity": 5, "unit": "mL",
                                              "actor": "Mallory"}, headers=ORIGIN)
    assert created.status_code == 201
    c = connect(database_target(app.config))
    try:
        row = c.execute("SELECT actor FROM audit_log WHERE entity_type = 'reagent' AND entity_id = ?",
                        (created.get_json()["id"],)).fetchone()
    finally:
        c.close()
    assert row["actor"] == "Ada Lovelace"


def test_batched_ingest_maps_every_chunk_to_its_own_page(client, conn, pdf_factory):
    import io

    # Several chunks per page: chunk_index restarts on every page, so (page, chunk_index) repeats per index.
    pages = [" ".join(f"Step {i} on page {n}: check centrifuge rotor number {n} before the run." for i in range(1, 30))
             for n in range(1, 4)]
    resp = client.post("/api/documents", data={"file": (io.BytesIO(pdf_factory(pages)), "multi.pdf")},
                       content_type="multipart/form-data")
    assert resp.status_code == 201
    doc_id = resp.get_json()["id"]
    rows = conn.execute("SELECT page, chunk_index, text FROM chunks WHERE document_id = ? ORDER BY id",
                        (doc_id,)).fetchall()
    assert len({(r["page"], r["chunk_index"]) for r in rows}) == len(rows) > 3
    assert all(f"page {r['page']}:" in r["text"] for r in rows)
    # Each stored vector belongs to its own chunk: searching a chunk's exact text returns that chunk first.
    engine = client.application.extensions["smartlab"]["rag"]
    for r in conn.execute("SELECT id, text FROM chunks WHERE document_id = ?", (doc_id,)).fetchall():
        assert engine.store.search(engine.embedder.embed([r["text"]])[0], 1)[0][0] == r["id"]


def test_hashed_assets_are_immutable_and_index_revalidates(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(TestConfig, DATA_DIR=tmp_path, DATABASE_PATH=tmp_path / "test.db",
                     UPLOAD_DIR=tmp_path / "uploads", INDEX_DIR=tmp_path / "index", FRONTEND_DIST=dist)
    web = app.test_client()
    asset = web.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    page = web.get("/")
    assert page.status_code == 200 and page.headers["Cache-Control"] == "no-cache"
