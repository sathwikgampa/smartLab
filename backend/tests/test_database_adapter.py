"""Database engine selection and the PostgreSQL adapter, tested offline.

The PostgreSQL connection is replaced by a fake psycopg2 connection that records the SQL
it receives, so these tests need no database server. Real PostgreSQL behaviour is covered by
``test_postgres_integration.py`` (and by running the whole suite with SMARTLAB_TEST_DATABASE_URL).
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from app import create_app
from app.config import Config, TestConfig, normalize_postgres_url, redact_url, resolve_database
from app.db import (PG_SCHEMA, SCHEMA, TABLES, TABLES_WITH_ID, TABLES_WITHOUT_ID, PgConnection, PgPool, PgRow, connect,
                    database_target, describe_database, execute, get_db, init_schema, is_postgres, query_all,
                    query_one, translate_sql)

PG = "postgresql://user:s3cret@db.example.supabase.co:5432/postgres"


# ------------------------------------------------------------ engine selection
@pytest.mark.parametrize("env, expected", [
    ({}, ("sqlite", None)),
    ({"SMARTLAB_DATABASE_URL": "", "DATABASE_URL": "  "}, ("sqlite", None)),
    ({"DATABASE_URL": PG}, ("postgresql", PG)),
    ({"SMARTLAB_DATABASE_URL": PG}, ("postgresql", PG)),
    ({"SMARTLAB_DATABASE_URL": PG, "DATABASE_URL": "postgresql://other@h/db"}, ("postgresql", PG)),
    ({"DATABASE_URL": "postgres://u:p@h:6543/postgres"}, ("postgresql", "postgresql://u:p@h:6543/postgres")),
    ({"DATABASE_URL": "postgresql+psycopg2://u:p@h/db"}, ("postgresql", "postgresql://u:p@h/db")),
    ({"DATABASE_URL": "POSTGRES://u:p@h/db"}, ("postgresql", "postgresql://u:p@h/db")),
    # DATABASE_URL is a generic name; a non-PostgreSQL value there keeps local SQLite.
    ({"DATABASE_URL": "sqlite:///tmp/x.db"}, ("sqlite", None)),
    ({"DATABASE_URL": "mysql://u:p@h/db"}, ("sqlite", None)),
])
def test_resolve_database(env, expected):
    assert resolve_database(env) == expected


def test_explicit_non_postgres_url_is_a_configuration_error():
    with pytest.raises(ValueError, match="SMARTLAB_DATABASE_URL"):
        resolve_database({"SMARTLAB_DATABASE_URL": "mysql://u:p@h/db"})


def test_normalize_keeps_query_parameters():
    assert normalize_postgres_url(" postgres://u:p@h:5432/db?sslmode=require ") == \
        "postgresql://u:p@h:5432/db?sslmode=require"


@pytest.mark.parametrize("url, expected", [
    (PG, "postgresql://user:***@db.example.supabase.co:5432/postgres"),
    ("postgresql://u:p@ss:w@rd@h:5432/db", "postgresql://u:***@h:5432/db"),
    ("postgresql://h/db?user=u&password=pw&sslmode=require", "postgresql://h/db?user=u&password=***&sslmode=require"),
    ("postgresql://u@h/db", "postgresql://u@h/db"),
    (None, ""),
])
def test_redact_url_hides_passwords(url, expected):
    assert redact_url(url) == expected


def test_test_config_never_uses_a_cloud_database():
    assert TestConfig.DATABASE_ENGINE == "sqlite" and TestConfig.DATABASE_URL is None


def test_database_target_and_description():
    sqlite_cfg = {"DATABASE_ENGINE": "sqlite", "DATABASE_URL": None, "DATABASE_PATH": "x.db"}
    pg_cfg = {"DATABASE_ENGINE": "postgresql", "DATABASE_URL": PG, "DATABASE_PATH": "x.db"}
    assert database_target(sqlite_cfg) == "x.db" and database_target(pg_cfg) == PG
    assert "s3cret" not in describe_database(pg_cfg) and describe_database(sqlite_cfg) == "SQLite x.db"


# ----------------------------------------------------------- SQL translation
@pytest.mark.parametrize("sqlite_sql, pg_sql", [
    ("SELECT * FROM t WHERE id = ? AND name = ?", "SELECT * FROM t WHERE id = %s AND name = %s"),
    ("SELECT '?' AS q, \"a?\" FROM t WHERE x = ?", "SELECT '?' AS q, \"a?\" FROM t WHERE x = %s"),
    ("SELECT 'it''s ?' FROM t WHERE x = ?", "SELECT 'it''s ?' FROM t WHERE x = %s"),
    ("SELECT * FROM t WHERE name LIKE ? OR code like ?", "SELECT * FROM t WHERE name ILIKE %s OR code ILIKE %s"),
    ("SELECT 'LIKE' FROM t WHERE a = ?", "SELECT 'LIKE' FROM t WHERE a = %s"),
    ("SELECT * FROM t WHERE title LIKE 'SOP-%' AND id = ?", "SELECT * FROM t WHERE title ILIKE 'SOP-%%' AND id = %s"),
    ("SELECT 5 % ? FROM t", "SELECT 5 %% %s FROM t"),
])
def test_translate_with_params(sqlite_sql, pg_sql):
    assert translate_sql(sqlite_sql, True) == pg_sql


def test_translate_without_params_leaves_percent_alone():
    # psycopg2 only interprets % when parameters are passed.
    assert translate_sql("SELECT id FROM documents WHERE title LIKE 'SOP-CEN%'", False) == \
        "SELECT id FROM documents WHERE title ILIKE 'SOP-CEN%'"


def test_translate_datetime_now():
    out = translate_sql("UPDATE t SET updated_at = datetime('now'), b = DATETIME( 'now' ) WHERE id = ?", True)
    assert "datetime" not in out.lower().replace("statement_timestamp", "")
    assert out.count("to_char(timezone('UTC', statement_timestamp()), 'YYYY-MM-DD HH24:MI:SS')") == 2
    assert out.endswith("WHERE id = %s")


def test_every_app_statement_has_no_sqlite_only_constructs_left():
    """Scan the application's SQL for SQLite-only syntax the adapter does not translate."""
    from pathlib import Path

    banned = re.compile(r"COLLATE\s+NOCASE|INSERT\s+OR\s+|REPLACE\s+INTO|GROUP_CONCAT|IFNULL\(|julianday|"
                        r"strftime\(|AUTOINCREMENT|PRAGMA|sqlite_master", re.IGNORECASE)
    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {"db.py", "migrate_sqlite.py"}  # SQLite-specific code paths by design
    offenders = [f"{p.name}:{i}" for p in app_dir.rglob("*.py") if p.name not in allowed
                 for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if banned.search(line.split("#", 1)[0])]  # ignore comments
    assert offenders == []


# ------------------------------------------------------------------- schema
def test_pg_schema_is_translated():
    assert "AUTOINCREMENT" not in PG_SCHEMA and "datetime(" not in PG_SCHEMA
    assert not re.search(r"\bREAL\b", PG_SCHEMA) and "DOUBLE PRECISION" in PG_SCHEMA
    assert PG_SCHEMA.count("GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY") == SCHEMA.count("AUTOINCREMENT")


def test_tables_are_in_foreign_key_order():
    for i, table in enumerate(TABLES):
        body = SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (")[1].split(");")[0]
        for ref in re.findall(r"REFERENCES (\w+)", body):
            assert ref == table or TABLES.index(ref) < i, f"{table} references {ref} before it is created"


def test_tables_with_generated_ids():
    assert "settings" not in TABLES_WITH_ID and "team_memberships" not in TABLES_WITH_ID and {"documents", "chunks", "audit_log"} <= TABLES_WITH_ID
    assert TABLES_WITHOUT_ID == {"settings", "team_memberships"}
    assert set(TABLES_WITH_ID) | TABLES_WITHOUT_ID == set(TABLES)


# ---------------------------------------------------------- fake psycopg2
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = -1
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")
        if "RETURNING id" in sql:
            self.description, self._rows, self.rowcount = [("id",)], [(41,)], 1
        elif sql.startswith("SELECT"):
            self.description = [("id",), ("name",), ("id",)]
            self._rows = [(1, "a", 99), (2, "b", 98)]
            self.rowcount = 2
        else:
            self.rowcount = 3

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class FakeRaw:
    def __init__(self, fail_on=None, rollback_fails=False):
        self.statements: list = []
        self.closed = 0
        self.commits = self.rollbacks = 0
        self.fail_on = fail_on
        self.rollback_fails = rollback_fails

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            raise RuntimeError("connection lost")

    def close(self):
        self.closed = 1


def test_insert_returns_generated_id_like_lastrowid():
    raw = FakeRaw()
    conn = PgConnection(raw)
    cur = conn.execute("INSERT INTO reagents (name, quantity) VALUES (?, ?)", ("Ethanol", 2.5))
    assert cur.lastrowid == 41 and cur.rowcount == 1
    assert raw.statements[-1] == ("INSERT INTO reagents (name, quantity) VALUES (%s, %s) RETURNING id",
                                  ("Ethanol", 2.5))
    assert cur.fetchone() is None  # the id row was consumed, as sqlite3 returns no rows for INSERT


def test_insert_into_table_without_id_and_upsert():
    raw = FakeRaw()
    conn = PgConnection(raw)
    cur = conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                       "value = excluded.value", ("k", "v"))
    assert cur.lastrowid is None and "RETURNING" not in raw.statements[-1][0]


def test_rows_behave_like_sqlite_rows():
    conn = PgConnection(FakeRaw())
    rows = conn.execute("SELECT id, name, id FROM t").fetchall()
    first = rows[0]
    assert first["id"] == 1 and first[0] == 1 and first[1] == "a" and first[2] == 99
    assert dict(first) == {"id": 1, "name": "a"}  # duplicate name -> first column, as sqlite3.Row
    assert first.keys() == ["id", "name", "id"] and len(first) == 3
    assert [a for (a, _b, _c) in rows] == [1, 2]  # tuple unpacking iterates values
    assert [r[0] for r in conn.execute("SELECT id FROM t")] == [1, 2]  # cursor is iterable


def test_writes_report_rowcount_and_fetch_nothing():
    conn = PgConnection(FakeRaw())
    cur = conn.execute("DELETE FROM reagents WHERE id = ?", (5,))
    assert cur.rowcount == 3 and cur.fetchone() is None and cur.fetchall() == [] and cur.lastrowid is None


def test_boolean_params_become_integers():
    raw = FakeRaw()
    PgConnection(raw).execute("UPDATE experiment_steps SET done = ? WHERE id = ?", (True, 3))
    assert raw.statements[-1][1] == (1, 3) and type(raw.statements[-1][1][0]) is int


def test_statement_without_params_sends_none():
    raw = FakeRaw()
    PgConnection(raw).execute("SELECT COUNT(*) FROM chunks")
    assert raw.statements[-1] == ("SELECT COUNT(*) FROM chunks", None)


def test_context_manager_commits_or_rolls_back():
    raw = FakeRaw()
    with PgConnection(raw) as c:
        c.execute("UPDATE t SET a = ?", (1,))
    assert raw.commits == 1 and raw.rollbacks == 0
    with pytest.raises(RuntimeError):
        with PgConnection(raw) as c:
            raise RuntimeError("fail")
    assert raw.rollbacks == 1


def test_close_returns_pooled_connection_after_rollback():
    released = []
    raw = FakeRaw()
    conn = PgConnection(raw, release=lambda r, broken: released.append((r, broken)))
    conn.execute("UPDATE t SET a = ?", (1,))
    conn.close()
    assert raw.rollbacks == 1 and released == [(raw, False)] and not raw.closed
    conn.close()  # idempotent
    assert len(released) == 1 and conn.closed


def test_close_discards_broken_pooled_connection():
    released = []
    raw = FakeRaw(rollback_fails=True)
    PgConnection(raw, release=lambda r, broken: released.append(broken)).close()
    assert released == [True]


def test_unpooled_connection_is_closed():
    raw = FakeRaw()
    PgConnection(raw).close()
    assert raw.closed


def test_failed_statement_propagates():
    conn = PgConnection(FakeRaw(fail_on="bad_table"))
    with pytest.raises(RuntimeError):
        conn.execute("SELECT * FROM bad_table WHERE id = ?", (1,))


def test_pool_falls_back_to_a_direct_connection_when_exhausted(monkeypatch):
    from psycopg2.pool import PoolError

    import app.db as db

    class ExhaustedPool:
        def getconn(self):
            raise PoolError("connection pool exhausted")

    pool = PgPool.__new__(PgPool)
    pool.url, pool._pool = PG, ExhaustedPool()
    raw = FakeRaw()
    monkeypatch.setattr(db, "_connect_raw", lambda url: raw)
    conn = pool.connect()
    conn.close()
    assert raw.closed  # not returned to the pool


def test_connect_selects_engine_from_target(tmp_path, monkeypatch):
    import app.db as db

    raw = FakeRaw()
    monkeypatch.setattr(db, "_connect_raw", lambda url: raw)
    pg = connect(PG)
    assert is_postgres(pg) and pg.raw is raw
    lite = connect(tmp_path / "x.db")
    assert isinstance(lite, sqlite3.Connection) and not is_postgres(lite)
    assert lite.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    lite.close()


# ------------------------------------------------------ SQLite helpers (app)
def test_sqlite_remains_the_default(tmp_path):
    application = create_app(TestConfig, DATA_DIR=tmp_path, DATABASE_PATH=tmp_path / "d.db",
                             UPLOAD_DIR=tmp_path / "u", INDEX_DIR=tmp_path / "i")
    assert application.config["DATABASE_ENGINE"] == "sqlite" and "smartlab_db_pool" not in application.extensions
    assert application.test_client().get("/api/health").get_json()["database"] == "sqlite"
    assert (tmp_path / "d.db").is_file()


def test_helpers_share_one_request_connection_and_return_ids(app):
    with app.app_context():
        db = get_db()
        assert get_db() is db
        new_id = execute("INSERT INTO profiles (name, role) VALUES (?, ?)", ("Test User", "Scientist"))
        assert query_one("SELECT name FROM profiles WHERE id = ?", (new_id,))["name"] == "Test User"
        assert any(r["id"] == new_id for r in query_all("SELECT id FROM profiles"))
    with app.app_context():  # committed by execute()
        assert query_one("SELECT id FROM profiles WHERE id = ?", (new_id,)) is not None


def test_uncommitted_work_is_discarded_at_teardown(app):
    with app.app_context():
        get_db().execute("INSERT INTO profiles (name, role) VALUES ('Ghost', 'QA')")
    with app.app_context():
        assert query_one("SELECT id FROM profiles WHERE name = 'Ghost'") is None


def test_init_schema_is_idempotent(conn):
    before = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    init_schema(conn)
    init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == before


def test_like_search_is_case_insensitive(client):
    names = {r["name"] for r in client.get("/api/reagents?search=ETHANOL").get_json()}
    assert any("ethanol" in n.lower() for n in names)


def test_document_sort_is_case_insensitive(client):
    titles = [d["title"] for d in client.get("/api/documents?sort=title&order=asc").get_json()]
    assert titles == sorted(titles, key=str.lower)


def test_activity_feed_handles_audit_rows_without_entity_id(client):
    assert client.put("/api/settings", json={"institution_name": "Test Lab"}).status_code == 200
    refs = [i["ref"] for i in client.get("/api/activity").get_json() if i["kind"] == "audit"]
    assert "settings:" in refs


def test_postgres_app_config_is_selected_from_environment(monkeypatch):
    """Config picks PostgreSQL from the environment at import time (fresh import, nothing connects)."""
    import importlib

    import app.config as config_module

    monkeypatch.setenv("SMARTLAB_DATABASE_URL", "postgres://u:p@h:5432/db")
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.Config.DATABASE_ENGINE == "postgresql"
        assert reloaded.Config.DATABASE_URL == "postgresql://u:p@h:5432/db"
        assert reloaded.Config.INDEX_DIR.name == "index-postgres"
        assert reloaded.TestConfig.DATABASE_ENGINE == "sqlite"
    finally:
        monkeypatch.delenv("SMARTLAB_DATABASE_URL")
        importlib.reload(config_module)


def test_create_app_uses_postgres_when_configured(tmp_path, monkeypatch):
    """create_app routes start-up work and requests through PostgreSQL connections when configured."""
    import app as app_module
    import app.db as db

    opened = []

    def fake_connect(target):
        assert target == PG
        conn = PgConnection(FakeRaw())
        opened.append(conn)
        return conn

    class FakePool:
        def __init__(self, url, maxconn):
            self.url, self.maxconn = url, maxconn

    monkeypatch.setattr(app_module, "connect", fake_connect)
    monkeypatch.setattr(app_module, "init_schema", lambda conn: None)
    monkeypatch.setattr(app_module, "PgPool", FakePool)
    monkeypatch.setattr(db.PgConnection, "execute", lambda self, sql, params=(): type(
        "C", (), {"fetchone": lambda s: (0,)})())
    application = create_app(TestConfig, DATABASE_ENGINE="postgresql", DATABASE_URL=PG, SEED_DEMO_DATA=False,
                             DATA_DIR=tmp_path, UPLOAD_DIR=tmp_path / "u", INDEX_DIR=tmp_path / "i")
    assert len(opened) == 1 and opened[0].closed
    pool = application.extensions["smartlab_db_pool"]
    assert pool.url == PG and pool.maxconn == Config.DB_POOL_MAX
