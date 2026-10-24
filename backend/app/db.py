"""Persistence layer: local SQLite or PostgreSQL (e.g. Supabase).

One connection per request (stored on ``flask.g``), rows readable by column name
or index. Application code is written against the :mod:`sqlite3` connection API
and SQLite's SQL dialect; on PostgreSQL, :class:`PgConnection` provides the same
API and :func:`translate_sql` rewrites the few dialect differences the app uses
(``?`` placeholders, ``datetime('now')``, case-insensitive ``LIKE``,
``lastrowid``). ``SCHEMA`` below is the single source of truth for both engines.
"""
from __future__ import annotations

import functools
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from flask import current_app, g

from .config import is_postgres_url, redact_url

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_type TEXT NOT NULL DEFAULT 'SOP',
    page_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'indexed',
    category TEXT NOT NULL DEFAULT 'General',
    doc_code TEXT,
    version TEXT,
    owner TEXT,
    approval_status TEXT NOT NULL DEFAULT 'unreviewed',
    reviewed_at TEXT,
    organization_id INTEGER NOT NULL DEFAULT 1,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

CREATE TABLE IF NOT EXISTS reagents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    cas_number TEXT,
    catalog_number TEXT,
    lot_number TEXT,
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT 'mL',
    reorder_threshold REAL NOT NULL DEFAULT 0,
    location TEXT,
    storage_condition TEXT,
    hazard_class TEXT,
    expiry_date TEXT,
    supplier TEXT,
    notes TEXT,
    compound_key TEXT,
    organization_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS troubleshooting (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_code TEXT,
    equipment TEXT NOT NULL,
    symptom TEXT NOT NULL,
    keywords TEXT NOT NULL,
    probable_cause TEXT NOT NULL,
    fix TEXT NOT NULL,
    source_document TEXT,
    source_page INTEGER,
    verified INTEGER NOT NULL DEFAULT 1,
    machine_model TEXT,
    ppe_warning TEXT,
    organization_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    title TEXT,
    expertise TEXT,
    avatar_color TEXT DEFAULT '#10b981'
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    owner_id INTEGER REFERENCES profiles(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    objective TEXT,
    protocol_markdown TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    progress INTEGER NOT NULL DEFAULT 0,
    owner_id INTEGER REFERENCES profiles(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiment_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiment_variables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT,
    kind TEXT NOT NULL DEFAULT 'controlled',
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    checkpoint TEXT NOT NULL,
    reviewer_id INTEGER REFERENCES profiles(id),
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    experiment_id INTEGER REFERENCES experiments(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    columns_json TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    author_id INTEGER REFERENCES profiles(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comments_entity ON comments(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS shared_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_by INTEGER REFERENCES profiles(id),
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location TEXT,
    category TEXT,
    description TEXT,
    training_steps_json TEXT NOT NULL DEFAULT '[]',
    safety_notes TEXT,
    sop_reference TEXT,
    tour_x REAL DEFAULT 0,
    tour_y REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    user_id INTEGER,  -- users.id of the account that asked; NULL for anonymous use (auth disabled)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_session_id ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS answer_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    rating TEXT NOT NULL,
    comment TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    summary TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Compound reference data used by the lab co-pilot's calculator and name resolver.
-- Shared across organisations (molecular weights do not depend on who owns the stock).
CREATE TABLE IF NOT EXISTS compounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compound_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    molecular_weight REAL,
    cas_number TEXT,
    synonyms TEXT NOT NULL DEFAULT ''
);

-- Images uploaded to the lab co-pilot. Tools only ever see ``upload_id``; the file name on disk
-- is server-generated, and the display text read from the image is stored at upload time.
CREATE TABLE IF NOT EXISTS agent_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id TEXT NOT NULL UNIQUE,
    organization_id INTEGER NOT NULL DEFAULT 1,
    stored_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    equipment_type TEXT,
    observed_text_json TEXT NOT NULL DEFAULT '[]',
    extracted_codes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Audit record of every lab co-pilot run (no secrets; tool calls are summarised).
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    organization_id INTEGER NOT NULL DEFAULT 1,
    user_id TEXT,
    session_id TEXT,
    question TEXT NOT NULL,
    intent TEXT,
    status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    uncertainty_flag INTEGER NOT NULL DEFAULT 0,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL DEFAULT '[]',
    citations_json TEXT NOT NULL DEFAULT '[]',
    llm_calls INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_org ON agent_runs(organization_id, id);

-- Authentication. Emails are stored lower-case; only salted password hashes and SHA-256 hashes of
-- session/reset tokens are kept, never the secrets themselves. Times are UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    profile_id INTEGER REFERENCES profiles(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    password_changed_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);
"""

# Indexes for the hot query paths. Created one by one after SCHEMA, so a database from an older release
# that lacks one of the columns (only MIGRATIONS columns are added automatically) still starts; that
# index is skipped and logged.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_message ON answer_feedback(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_documents_org ON documents(organization_id)",
    "CREATE INDEX IF NOT EXISTS idx_reagents_org_expiry ON reagents(organization_id, expiry_date, id)",
    "CREATE INDEX IF NOT EXISTS idx_troubleshooting_verified_codes ON troubleshooting(organization_id, error_code) "
    "WHERE verified = 1 AND error_code IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_steps_experiment ON experiment_steps(experiment_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_variables_experiment ON experiment_variables(experiment_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_experiment ON reviews(experiment_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_reviewer_pending ON reviews(reviewer_id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_experiments_owner ON experiments(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_experiments_project ON experiments(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_chat_user_session ON chat_messages(user_id, session_id, id)",
)

# Audit tables are append-only: the database itself rejects UPDATE and DELETE on them (GxP audit trail).
APPEND_ONLY_TABLES = ("audit_log", "agent_runs")

SQLITE_AUDIT_GUARD = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
""" for t in APPEND_ONLY_TABLES)

PG_AUDIT_GUARD = """
CREATE OR REPLACE FUNCTION smartlab_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
""" + "".join(
    f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{t}_append_only' AND tgrelid = '{t}'::regclass) THEN
        CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t}
            FOR EACH ROW EXECUTE FUNCTION smartlab_append_only();
    END IF;
END $$;
""" for t in APPEND_ONLY_TABLES)

# Columns added after the first release; added to existing databases on start-up.
# organization_id: there is no authentication yet, so every row belongs to the default organisation (1).
# TODO(auth): set organization_id from the signed-in user's organisation when auth is added.
MIGRATIONS = {
    "documents": {
        "category": "TEXT NOT NULL DEFAULT 'General'",
        "doc_code": "TEXT",
        "version": "TEXT",
        "owner": "TEXT",
        "approval_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
        "reviewed_at": "TEXT",
        "updated_at": "TEXT",
        "organization_id": "INTEGER NOT NULL DEFAULT 1",
    },
    "reagents": {
        "category": "TEXT",
        "compound_key": "TEXT",
        "organization_id": "INTEGER NOT NULL DEFAULT 1",
    },
    "troubleshooting": {
        "machine_model": "TEXT",
        "ppe_warning": "TEXT",
        "organization_id": "INTEGER NOT NULL DEFAULT 1",
    },
    "chat_messages": {
        "user_id": "INTEGER",
    },
}

# One-off data fixes, run only in the start-up that first adds the column: (table, column) -> (table the
# fix reads from, SQL). Chat history from before per-user scoping is attributed through agent_runs, which
# recorded the asking account per session; exchanges with no recorded account stay anonymous (NULL).
BACKFILLS = {
    ("chat_messages", "user_id"): (
        "agent_runs",
        "UPDATE chat_messages SET user_id = (SELECT CAST(r.user_id AS INTEGER) FROM agent_runs r "
        "WHERE r.session_id = chat_messages.session_id AND r.user_id IS NOT NULL ORDER BY r.id LIMIT 1)",
    ),
}


# Tables in creation order, which is also a valid foreign-key insertion order.
TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
# Tables with a generated ``id`` key (``lastrowid`` is emulated with ``RETURNING id`` for these).
TABLES_WITH_ID = frozenset(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*id INTEGER PRIMARY KEY", SCHEMA))

# ---------------------------------------------------------------- SQL dialect
# SQLite's datetime('now') is UTC text 'YYYY-MM-DD HH:MM:SS'; produce exactly the same on PostgreSQL
# so timestamp columns (TEXT on both engines) and API responses keep one format.
PG_NOW = "to_char(timezone('UTC', statement_timestamp()), 'YYYY-MM-DD HH24:MI:SS')"
_DATETIME_NOW = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_LITERAL = re.compile(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")")  # string literals and quoted identifiers
_LIKE = re.compile(r"\bLIKE\b", re.IGNORECASE)
_INSERT_INTO = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)


@functools.lru_cache(maxsize=1024)
def translate_sql(sql: str, has_params: bool = True) -> str:
    """Rewrite SQLite-dialect SQL used by the app into PostgreSQL (psycopg2 ``format`` paramstyle).

    Outside string literals: ``?`` -> ``%s`` and ``LIKE`` -> ``ILIKE`` (SQLite's LIKE is
    case-insensitive). ``datetime('now')`` becomes :data:`PG_NOW`. When parameters are passed,
    psycopg2 treats every ``%`` as a format character, so literal ``%`` is doubled.
    """
    sql = _DATETIME_NOW.sub(PG_NOW, sql)
    parts = _LITERAL.split(sql)
    for i, part in enumerate(parts):
        if has_params:
            part = part.replace("%", "%%")
        if i % 2 == 0:  # code, not a literal
            part = _LIKE.sub("ILIKE", part)
            if has_params:
                part = part.replace("?", "%s")
        parts[i] = part
    return "".join(parts)


def _pg_ddl(ddl: str) -> str:
    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl)  # PostgreSQL REAL is only 4-byte float
    return _DATETIME_NOW.sub(PG_NOW, ddl)


PG_SCHEMA = _pg_ddl(SCHEMA)


def _adapt_params(params: Iterable[Any] | None) -> tuple:
    # SQLite stores booleans as 0/1 in INTEGER columns; PostgreSQL would reject TRUE for an integer.
    return tuple(int(v) if isinstance(v, bool) else v for v in (params or ()))


# --------------------------------------------------------- PostgreSQL adapter
def _column_index(columns: Sequence[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, name in enumerate(columns):
        index.setdefault(name, i)  # like sqlite3.Row, a duplicated name resolves to its first column
    return index


class PgRow:
    """Behaves like :class:`sqlite3.Row`: ``row["id"]``, ``row[0]``, ``dict(row)``, ``row.keys()``,
    and iteration/unpacking over values (``for (doc_id,) in rows``)."""

    __slots__ = ("_columns", "_index", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[Any], index: dict[str, int] | None = None):
        self._columns = columns
        self._index = index if index is not None else _column_index(columns)
        self._values = tuple(values)

    def keys(self) -> list[str]:
        return list(self._columns)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other) -> bool:
        return isinstance(other, PgRow) and self._columns == other._columns and self._values == other._values

    __hash__ = None

    def __repr__(self) -> str:
        return f"PgRow({dict(zip(self._columns, self._values))!r})"


class PgCursor:
    """The subset of :class:`sqlite3.Cursor` the app uses."""

    def __init__(self, cursor, lastrowid: int | None = None, consumed: bool = False):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount
        self.description = cursor.description
        self._columns = None if consumed or cursor.description is None else [d[0] for d in cursor.description]
        self._index = None if self._columns is None else _column_index(self._columns)

    def fetchone(self) -> PgRow | None:
        if self._columns is None:
            return None
        row = self._cursor.fetchone()
        return None if row is None else PgRow(self._columns, row, self._index)

    def fetchall(self) -> list[PgRow]:
        if self._columns is None:
            return []
        return [PgRow(self._columns, r, self._index) for r in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    def close(self) -> None:
        self._cursor.close()


_types_registered = False


def _register_pg_types() -> None:
    """Return NUMERIC results (e.g. ``ROUND(AVG(x))``) as float, as SQLite does, so JSON stays numeric
    (Flask would serialise :class:`decimal.Decimal` as a string)."""
    global _types_registered
    if not _types_registered:
        import psycopg2.extensions as ext

        ext.register_type(ext.new_type(ext.DECIMAL.values, "SMARTLAB_NUMERIC_FLOAT",
                                       lambda value, cur: None if value is None else float(value)))
        _types_registered = True


class PgConnection:
    """Wraps a psycopg2 connection with the :class:`sqlite3.Connection` API the app relies on.

    Like sqlite3 (default isolation), statements run inside an implicit transaction that the caller
    commits; closing without committing discards the changes. A failed statement aborts the
    PostgreSQL transaction, so callers that recover from errors must ``rollback()`` (the request
    teardown always does).
    """

    engine = "postgresql"

    def __init__(self, raw, release=None):
        _register_pg_types()
        self._raw = raw
        self._release = release

    @property
    def raw(self):
        return self._raw

    @property
    def closed(self) -> bool:
        return self._raw is None or bool(self._raw.closed)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> PgCursor:
        params = _adapt_params(params)
        statement = translate_sql(sql, bool(params))
        match = _INSERT_INTO.match(statement)
        wants_id = bool(match) and match.group(1).lower() in TABLES_WITH_ID and not _RETURNING.search(statement)
        if wants_id:
            statement = statement.rstrip().rstrip(";") + " RETURNING id"
        cur = self._raw.cursor()
        cur.execute(statement, params or None)
        lastrowid = cur.fetchone()[0] if wants_id else None
        return PgCursor(cur, lastrowid, consumed=wants_id)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> PgCursor:
        from psycopg2.extras import execute_batch

        cur = self._raw.cursor()
        execute_batch(cur, translate_sql(sql, True), [_adapt_params(p) for p in seq_of_params], page_size=200)
        return PgCursor(cur)

    def executescript(self, script: str) -> PgCursor:
        """Run raw PostgreSQL (no translation or parameters), e.g. DDL."""
        cur = self._raw.cursor()
        cur.execute(script)
        return PgCursor(cur)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        raw, self._raw = self._raw, None
        if raw is None:
            return
        if self._release is None:
            raw.close()
            return
        broken = bool(raw.closed)
        if not broken:
            try:
                raw.rollback()  # discard uncommitted work before the connection is reused
            except Exception:
                broken = True
        self._release(raw, broken)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


_CONNECT_KWARGS = {
    "connect_timeout": 10,
    "application_name": "smartlab-ai",
    # Detect dropped connections (cloud load balancers close idle TCP sessions).
    "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
}


def _connect_raw(url: str):
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("A PostgreSQL URL is configured but psycopg2 is not installed "
                           "(pip install -r requirements.txt).") from exc
    return psycopg2.connect(url, **_CONNECT_KWARGS)


class PgPool:
    """Thread-safe pool of PostgreSQL connections for request handling.

    Opening a TLS connection to a hosted database costs a round trip or three, so requests reuse
    connections. When the pool is exhausted a temporary extra connection is opened instead of failing.
    """

    def __init__(self, url: str, maxconn: int = 5):
        from psycopg2.pool import ThreadedConnectionPool

        self.url = url
        self._pool = ThreadedConnectionPool(0, max(1, maxconn), url, **_CONNECT_KWARGS)

    def connect(self) -> PgConnection:
        from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
        from psycopg2.pool import PoolError

        for _ in range(3):
            try:
                raw = self._pool.getconn()
            except PoolError:
                return PgConnection(_connect_raw(self.url))  # closed (not pooled) on release
            if raw.closed or raw.get_transaction_status() == TRANSACTION_STATUS_UNKNOWN:
                self._pool.putconn(raw, close=True)
                continue
            return PgConnection(raw, release=self._putconn)
        return PgConnection(_connect_raw(self.url))

    def _putconn(self, raw, broken: bool) -> None:
        try:
            self._pool.putconn(raw, close=broken)
        except Exception:  # pool already closed
            raw.close()

    def close(self) -> None:
        self._pool.closeall()


# ------------------------------------------------------------- connections
def is_postgres(conn: Any) -> bool:
    return isinstance(conn, PgConnection)


def connect(target: Path | str) -> sqlite3.Connection | PgConnection:
    """Open a connection: a ``postgresql://`` URL selects PostgreSQL, anything else is a SQLite path."""
    if isinstance(target, str) and is_postgres_url(target):
        return PgConnection(_connect_raw(target))
    conn = sqlite3.connect(str(target), detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_readonly(target: Path | str, pool: PgPool | None = None) -> sqlite3.Connection | PgConnection:
    """Open a connection that the database itself refuses to write through.

    SQLite opens the file with ``mode=ro``; PostgreSQL marks the transaction ``READ ONLY`` (the
    connection is closed or rolled back by the caller, which ends that transaction).
    """
    if isinstance(target, str) and is_postgres_url(target):
        conn = pool.connect() if pool is not None else PgConnection(_connect_raw(target))
        conn.execute("SET TRANSACTION READ ONLY")
        return conn
    conn = sqlite3.connect(f"{Path(target).resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def database_target(config) -> Path | str:
    """The configured database: the PostgreSQL URL, or the SQLite file path."""
    return config["DATABASE_URL"] if config.get("DATABASE_ENGINE") == "postgresql" else config["DATABASE_PATH"]


def describe_database(config) -> str:
    """Human-readable database location with credentials removed (safe to log)."""
    if config.get("DATABASE_ENGINE") == "postgresql":
        return f"PostgreSQL {redact_url(config['DATABASE_URL'])}"
    return f"SQLite {config['DATABASE_PATH']}"


def get_db() -> sqlite3.Connection | PgConnection:
    if "db" not in g:
        pool = current_app.extensions.get("smartlab_db_pool")
        g.db = pool.connect() if pool is not None else connect(database_target(current_app.config))
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()  # uncommitted changes are discarded (rolled back) on both engines


_SCHEMA_LOCK = 0x5A1AB001
_SEED_LOCK = 0x5A1AB002
_USERS_LOCK = _SEED_LOCK + 1  # serialises sign-ups (first account becomes admin; unique email)
_lock_ids = {"schema": _SCHEMA_LOCK, "seed": _SEED_LOCK, "users": _USERS_LOCK}


def transaction_lock(conn: Any, name: str) -> None:
    """Serialise start-up work across processes sharing one PostgreSQL database (released at commit).

    SQLite needs nothing here. A transaction-scoped advisory lock is used because it is also safe
    behind Supabase's transaction-mode pooler.
    """
    if is_postgres(conn):
        conn.execute(f"SELECT pg_advisory_xact_lock({_lock_ids[name]})")


def init_schema(conn: sqlite3.Connection | PgConnection) -> None:
    """Create missing tables/indexes and add newer columns. Never drops or rewrites data."""
    if is_postgres(conn):
        transaction_lock(conn, "schema")
        _migrate_pg(conn)
        conn.executescript(PG_SCHEMA)
        _create_indexes(conn)
        conn.executescript(PG_AUDIT_GUARD)
        conn.commit()
        return
    # Older databases may lack the newer columns that the schema's indexes or
    # inserts rely on, so migrate tables that already exist before the full script.
    _migrate(conn)
    conn.executescript(SCHEMA)
    _create_indexes(conn)
    conn.executescript(SQLITE_AUDIT_GUARD)
    conn.commit()


def _create_indexes(conn: sqlite3.Connection | PgConnection) -> None:
    pg = is_postgres(conn)
    for ddl in INDEXES:
        try:
            if pg:  # a failed statement would abort the whole PostgreSQL transaction
                conn.executescript("SAVEPOINT smartlab_index")
                conn.executescript(ddl)
                conn.executescript("RELEASE SAVEPOINT smartlab_index")
            else:
                conn.execute(ddl)
        except (sqlite3.OperationalError, _pg_errors()) as exc:
            if pg:
                conn.executescript("ROLLBACK TO SAVEPOINT smartlab_index")
            logger.warning("Index skipped (older table layout): %s (%s)", ddl.split(" ON ")[0], exc)


def _pg_errors() -> type[BaseException] | tuple[type[BaseException], ...]:
    try:
        import psycopg2
    except ImportError:  # pragma: no cover - SQLite-only installs
        return ()
    return psycopg2.Error


def _columns(conn: sqlite3.Connection | PgConnection, table: str) -> set[str]:
    if is_postgres(conn):
        return {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = ?", (table,))}
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    added = []
    for table, columns in MIGRATIONS.items():
        existing = _columns(conn, table)
        if not existing:
            continue  # table not created yet; SCHEMA creates it with every column
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append((table, name))
    _backfill(conn, added)
    conn.commit()


def _migrate_pg(conn: PgConnection) -> None:
    added = []
    for table, columns in MIGRATIONS.items():
        existing = _columns(conn, table)
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                conn.executescript(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {_pg_ddl(ddl)}")
                added.append((table, name))
    _backfill(conn, added)


def _backfill(conn: sqlite3.Connection | PgConnection, added: list[tuple[str, str]]) -> None:
    for key in added:
        if key in BACKFILLS:
            source, sql = BACKFILLS[key]
            if _columns(conn, source):  # the source table may predate this database
                conn.execute(sql)
                logger.info("Backfilled %s.%s from %s", *key, source)


def audit(conn: sqlite3.Connection, action: str, entity_type: str, entity_id: int | None, summary: str,
          actor: str | None = None) -> None:
    """Record a privileged change (approvals, inventory updates, reviews). Caller commits."""
    conn.execute("INSERT INTO audit_log (action, entity_type, entity_id, summary, actor) VALUES (?, ?, ?, ?, ?)",
                 (action, entity_type, entity_id, summary[:500], actor))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return rows_to_dicts(get_db().execute(sql, tuple(params)).fetchall())


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return row_to_dict(get_db().execute(sql, tuple(params)).fetchone())


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute a write statement, commit, and return ``lastrowid``."""
    db = get_db()
    cur = db.execute(sql, tuple(params))
    db.commit()
    return cur.lastrowid


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
