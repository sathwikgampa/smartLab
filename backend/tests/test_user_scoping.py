"""Per-account isolation: chat history and the activity feed show the caller's own records only;
administrators can opt into the whole lab's activity with ``mine=false``."""
from __future__ import annotations

import sqlite3

import pytest

from app import create_app
from app.config import TestConfig
from tests.conftest import TEST_PG_URL

GOOD = "Tr1cky-Pipette-42"


@pytest.fixture()
def auth_app(tmp_path):
    return create_app(TestConfig, DATA_DIR=tmp_path, DATABASE_PATH=tmp_path / "test.db", UPLOAD_DIR=tmp_path / "uploads",
                      INDEX_DIR=tmp_path / "index", AUTH_REQUIRED=True)


def signed_in(app, name: str, email: str):
    """A client with its own cookie jar, signed in as a new account. The first account is the admin."""
    client = app.test_client()
    r = client.post("/api/auth/signup", json={"name": name, "email": email, "organization": "Lab 2.14",
                                              "password": GOOD, "accepted": True})
    assert r.status_code == 201
    return client, r.get_json()["user"]


@pytest.fixture()
def admin(auth_app):
    return signed_in(auth_app, "Ada Lovelace", "ada@lab.org")


@pytest.fixture()
def member(auth_app, admin):
    return signed_in(auth_app, "Grace Hopper", "grace@lab.org")


# ------------------------------------------------------------------ chat history
def test_chat_sessions_are_private_to_each_account(admin, member):
    (a, _), (m, _) = admin, member
    mine = a.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()["session_id"]
    theirs = m.post("/api/chat", json={"question": "Where is the eyewash?"}).get_json()["session_id"]

    assert [s["session_id"] for s in a.get("/api/chat/sessions").get_json()] == [mine]
    assert [s["session_id"] for s in m.get("/api/chat/sessions").get_json()] == [theirs]
    # Knowing another account's session id reveals nothing and deletes nothing.
    assert m.get(f"/api/chat/{mine}").get_json() == []
    assert m.delete(f"/api/chat/{mine}").status_code == 404
    assert len(a.get(f"/api/chat/{mine}").get_json()) == 2


def test_writing_into_another_accounts_session_id_stays_separate(admin, member):
    (a, _), (m, _) = admin, member
    sid = a.post("/api/chat", json={"question": "What does error E-104 mean?"}).get_json()["session_id"]
    m.post(f"/api/chat/{sid}/messages", json={"question": "Injected?", "answer": "No"})

    session = a.get("/api/chat/sessions").get_json()[0]
    assert session["title"] == "What does error E-104 mean?" and session["message_count"] == 2
    assert [r["content"] for r in m.get(f"/api/chat/{sid}").get_json()] == ["Injected?", "No"]


# ------------------------------------------------------------------ activity feed
def test_activity_defaults_to_the_callers_own(admin, member):
    (a, admin_user), (m, member_user) = admin, member
    a.post("/api/comments/experiment/1", json={"body": "Admin note", "author_id": admin_user["profile_id"]})
    m.post("/api/comments/experiment/1", json={"body": "Member note", "author_id": member_user["profile_id"]})

    member_feed = m.get("/api/activity").get_json()
    assert [i["summary"] for i in member_feed if i["kind"] == "comment"] == ["Member note"]
    assert all(i["kind"] != "document" for i in member_feed)
    assert all(i["actor"] in (None, "Grace Hopper", "grace@lab.org") for i in member_feed if i["kind"] == "audit")
    admin_feed = a.get("/api/activity?mine=true").get_json()
    assert [i["summary"] for i in admin_feed if i["kind"] == "comment"] == ["Admin note"]


def test_lab_activity_is_for_admins_only(admin, member):
    (a, admin_user), (m, member_user) = admin, member
    a.post("/api/comments/experiment/1", json={"body": "Admin note", "author_id": admin_user["profile_id"]})
    m.post("/api/comments/experiment/1", json={"body": "Member note", "author_id": member_user["profile_id"]})

    assert m.get("/api/activity?mine=false").status_code == 403
    lab = a.get("/api/activity?mine=false").get_json()
    assert {"Admin note", "Member note"} <= {i["summary"] for i in lab if i["kind"] == "comment"}
    assert a.get("/api/activity?mine=perhaps").status_code == 400


# ------------------------------------------------------------------ upgrade
@pytest.mark.skipif(TEST_PG_URL is not None, reason="rebuilds an older SQLite layout")
def test_existing_chat_history_is_attributed_on_upgrade(auth_app, tmp_path):
    db = sqlite3.connect(tmp_path / "test.db")
    db.executescript("DROP INDEX idx_chat_user_session; ALTER TABLE chat_messages DROP COLUMN user_id;")
    db.executemany("INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
                   [("old1", "user", "Old question"), ("old1", "assistant", "Old answer"), ("anon", "user", "Hi")])
    db.execute("INSERT INTO agent_runs (request_id, user_id, session_id, question, status, started_at) "
               "VALUES ('r1', '7', 'old1', 'Old question', 'answered', '2026-01-01 00:00:00')")
    db.commit()
    db.close()

    create_app(TestConfig, DATA_DIR=tmp_path, DATABASE_PATH=tmp_path / "test.db", UPLOAD_DIR=tmp_path / "uploads",
               INDEX_DIR=tmp_path / "index", AUTH_REQUIRED=True)
    db = sqlite3.connect(tmp_path / "test.db")
    owners = dict(db.execute("SELECT session_id, MAX(user_id) FROM chat_messages GROUP BY session_id"))
    db.close()
    assert owners == {"old1": 7, "anon": None}
