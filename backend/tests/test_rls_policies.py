"""Team isolation enforced by PostgreSQL row-level security (backend/migrations/*.sql).

Needs a *disposable* PostgreSQL 16 cluster with pgvector >= 0.8 and a superuser URL, because the
migrations create cluster-wide roles. For example:

    docker run -d --name smartlab-rls -e POSTGRES_PASSWORD=pw -p 55432:5432 pgvector/pgvector:pg16
    SMARTLAB_TEST_PG_ADMIN_URL=postgresql://postgres:pw@localhost:55432/postgres pytest tests/test_rls_policies.py

The module builds today's schema with the app itself (create_app + demo seed), simulates Supabase (a
non-superuser owner, anon/authenticated roles, auth.uid()), applies the migrations twice, then queries as
the restricted application role exactly as the Flask app would.
"""
from __future__ import annotations

import os
import random
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ADMIN_URL = os.getenv("SMARTLAB_TEST_PG_ADMIN_URL", "").strip() or None
pytestmark = pytest.mark.skipif(ADMIN_URL is None, reason="SMARTLAB_TEST_PG_ADMIN_URL not set")

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
OWNER, OWNER_PW = "smartlab_rls_owner", "owner-pw"
APP_PW = "app-pw"
DIM = 384


def _url(user: str, password: str, dbname: str) -> str:
    parts = urlsplit(ADMIN_URL)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{user}:{password}@{host}", f"/{dbname}", parts.query, ""))


def _statements(sql: str) -> list[str]:
    """Split a script into statements, keeping $$-quoted bodies intact (for CONCURRENTLY outside a transaction)."""
    out, buf, in_body = [], [], False
    for line in sql.splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf.append(line)
        if line.count("$$") % 2:
            in_body = not in_body
        if not in_body and line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    return out


def apply_migrations(owner_url: str) -> None:
    import psycopg2

    conn = psycopg2.connect(owner_url)
    conn.autocommit = True
    try:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            if "CONCURRENTLY" in sql:  # must run statement by statement, outside a transaction
                for stmt in _statements(sql):
                    conn.cursor().execute(stmt)
            else:
                conn.cursor().execute(sql)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    import psycopg2

    from app import create_app
    from app.config import TestConfig

    dbname = f"smartlab_rls_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(ADMIN_URL)
    admin.autocommit = True
    cur = admin.cursor()
    # Supabase-like cluster: the owner is not a superuser; anon/authenticated exist.
    for role, ddl in ((OWNER, f"LOGIN CREATEROLE PASSWORD '{OWNER_PW}'"), ("anon", "NOLOGIN"),
                      ("authenticated", "NOLOGIN")):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {role} {ddl}")
    cur.execute(f"CREATE DATABASE {dbname} OWNER {OWNER}")

    sup = psycopg2.connect(_url(urlsplit(ADMIN_URL).username, urlsplit(ADMIN_URL).password, dbname))
    sup.autocommit = True
    s = sup.cursor()
    # What Supabase pre-installs: extensions schema with pgvector, auth.uid() from the JWT, and default
    # grants of every new public table to the Data API roles.
    s.execute(f"CREATE SCHEMA extensions AUTHORIZATION {OWNER}; CREATE EXTENSION vector WITH SCHEMA extensions")
    s.execute("CREATE SCHEMA auth; CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS "
              "$$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$")
    s.execute(f"GRANT USAGE ON SCHEMA auth TO {OWNER}, anon, authenticated")
    s.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated")
    sup.close()

    owner_url = _url(OWNER, OWNER_PW, dbname)
    tmp = tmp_path_factory.mktemp("rls")
    app = create_app(TestConfig, DATABASE_ENGINE="postgresql", DATABASE_URL=owner_url, DATA_DIR=tmp,
                     DATABASE_PATH=tmp / "unused.db", UPLOAD_DIR=tmp / "uploads", INDEX_DIR=tmp / "index")
    app.extensions["smartlab_db_pool"].close()

    own = psycopg2.connect(owner_url)
    own.autocommit = True
    o = own.cursor()
    # Accounts that exist before the migration: the backfill must put them in the default team.
    o.execute("INSERT INTO users (email, name, password_hash, role) VALUES "
              "('ada@lab.org', 'Ada', 'x', 'admin'), ('lena@lab.org', 'Lena', 'x', 'member') RETURNING id")
    ada, lena = (r[0] for r in o.fetchall())

    apply_migrations(owner_url)
    apply_migrations(owner_url)  # idempotent
    cur.execute(f"ALTER ROLE smartlab_app PASSWORD '{APP_PW}'")

    o.execute("INSERT INTO teams (organization_id, name, slug) VALUES (1, 'Team Windows', 'windows') RETURNING id")
    windows = o.fetchone()[0]
    o.execute("INSERT INTO organizations (name, slug) VALUES ('Other Lab', 'other-lab') RETURNING id")
    other_org = o.fetchone()[0]
    o.execute("INSERT INTO teams (organization_id, name, slug, is_default) VALUES (%s, 'Main', 'main', 1) RETURNING id",
              (other_org,))
    other_team = o.fetchone()[0]

    def user(email, role="member", org=1, auth=None):
        o.execute("INSERT INTO users (email, name, password_hash, role, organization_id, auth_user_id) "
                  "VALUES (%s, %s, 'x', %s, %s, %s) RETURNING id", (email, email.split("@")[0], role, org, auth))
        return o.fetchone()[0]

    carol_auth = str(uuid.uuid4())
    ids = {"ada": ada, "lena": lena, "bob": user("bob@lab.org"), "carol": user("carol@lab.org", auth=carol_auth),
           "dave": user("dave@lab.org"), "erin": user("erin@lab.org"), "zed": user("zed@other.org", "admin", other_org)}
    o.executemany("INSERT INTO team_memberships (team_id, user_id, role) VALUES (%s, %s, %s)", [
        (windows, ids["bob"], "member"), (windows, ids["carol"], "viewer"),
        (windows, ids["dave"], "admin"), (windows, ids["erin"], "owner")])

    # Team Windows data.
    o.execute("INSERT INTO documents (stored_name, original_name, title, sha256, team_id) "
              "VALUES ('w.pdf', 'w.pdf', 'Windows SOP', 'sha-windows', %s) RETURNING id", (windows,))
    windows_doc = o.fetchone()[0]
    o.executemany("INSERT INTO chunks (document_id, page, chunk_index, text) VALUES (%s, 1, %s, %s)",
                  [(windows_doc, i, f"windows chunk {i}") for i in range(12)])
    o.execute("INSERT INTO reagents (name, quantity, unit, team_id) VALUES ('Windows buffer', 5, 'mL', %s)", (windows,))
    o.execute("INSERT INTO experiments (title, team_id) VALUES ('Windows assay', %s) RETURNING id", (windows,))
    windows_exp = o.fetchone()[0]
    o.execute("INSERT INTO experiment_steps (experiment_id, position, text) VALUES (%s, 1, 'Pipette')", (windows_exp,))

    # Embeddings: every chunk gets a random unit vector (deterministic seed).
    rng = random.Random(7)
    o.execute("SELECT id FROM chunks ORDER BY id")
    for (chunk_id,) in o.fetchall():
        vec = [rng.gauss(0, 1) for _ in range(DIM)]
        norm = sum(v * v for v in vec) ** 0.5
        o.execute("INSERT INTO chunk_embeddings (chunk_id, team_id, model, embedding) VALUES (%s, 1, 'test', %s)",
                  (chunk_id, "[" + ",".join(f"{v / norm:.6f}" for v in vec) + "]"))
    own.close()

    yield {"app_url": _url("smartlab_app", APP_PW, dbname), "owner_url": owner_url, "windows": windows,
           "windows_doc": windows_doc, "windows_exp": windows_exp, "other_team": other_team, "carol_auth": carol_auth,
           **ids}

    admin.cursor().execute(f"DROP DATABASE {dbname} WITH (FORCE)")
    admin.close()


@contextmanager
def as_app(db, user=None, team=None):
    """A transaction on the application role with the identity the Flask app would declare. Rolled back."""
    import psycopg2

    conn = psycopg2.connect(db["app_url"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('smartlab.user_id', %s, true), set_config('smartlab.team_id', %s, true)",
                    ("" if user is None else str(db[user]), "" if team is None else str(team)))
        yield cur
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def as_owner(db):
    import psycopg2

    conn = psycopg2.connect(db["owner_url"])
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def team_ids_of(cur, table):
    cur.execute(f"SELECT DISTINCT team_id FROM {table} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


# ------------------------------------------------------------------------------- migration
def test_existing_accounts_were_backfilled_into_the_default_team(db):
    with as_owner(db) as o:
        o.execute("SELECT user_id, role FROM team_memberships WHERE team_id = 1 ORDER BY user_id")
        assert o.fetchall() == [(db["ada"], "owner"), (db["lena"], "member")]
        # Rows seeded before the migration were assigned team 1 by the column default, without an UPDATE.
        assert team_ids_of(o, "reagents") == [1, db["windows"]]
        assert one(o, "SELECT COUNT(*) FROM agent_runs WHERE team_id IS DISTINCT FROM 1") == 0
        assert one(o, "SELECT COUNT(*) FROM pg_constraint WHERE conname LIKE '%%team_id_fkey' AND NOT convalidated") == 0


def test_current_app_keeps_working_on_the_migrated_database(db, tmp_path):
    """Migrations first, code later: today's app (owner connection, no team awareness) must boot and serve."""
    from app import create_app
    from app.config import TestConfig

    app = create_app(TestConfig, DATABASE_ENGINE="postgresql", DATABASE_URL=db["owner_url"], DATA_DIR=tmp_path,
                     DATABASE_PATH=tmp_path / "unused.db", UPLOAD_DIR=tmp_path / "uploads", INDEX_DIR=tmp_path / "index")
    try:
        client = app.test_client()
        assert client.get("/api/documents").status_code == 200
        created = client.post("/api/reagents", json={"name": "Post-migration buffer", "quantity": 1, "unit": "mL"})
        assert created.status_code == 201
        assert client.delete(f"/api/reagents/{created.get_json()['id']}").status_code == 204
    finally:
        app.extensions["smartlab_db_pool"].close()
    with as_owner(db) as o:  # the old code never names team_id, so new rows take the default team
        assert one(o, "SELECT team_id FROM audit_log WHERE summary LIKE 'Added reagent ''Post-migration%%'") == 1


# ------------------------------------------------------------------------------- isolation
def test_without_an_identity_nothing_is_visible(db):
    with as_app(db) as cur:
        for table in ("documents", "reagents", "chunks", "experiments", "experiment_steps", "teams", "audit_log"):
            assert one(cur, f"SELECT COUNT(*) FROM {table}") == 0, table


def test_member_sees_only_their_team(db):
    with as_app(db, "bob") as cur:
        assert team_ids_of(cur, "documents") == [db["windows"]]
        assert team_ids_of(cur, "reagents") == [db["windows"]]
        assert one(cur, "SELECT COUNT(*) FROM chunks") == 12
        assert one(cur, "SELECT COUNT(*) FROM experiment_steps") == 1
        cur.execute("SELECT slug FROM teams")
        assert cur.fetchall() == [("windows",)]


def test_id_guessing_returns_nothing(db):
    with as_owner(db) as o:
        general_doc = one(o, "SELECT MIN(id) FROM documents WHERE team_id = 1")
    with as_app(db, "bob") as cur:
        assert one(cur, "SELECT COUNT(*) FROM documents WHERE id = %s", (general_doc,)) == 0
        cur.execute("UPDATE documents SET title = 'pwned' WHERE id = %s", (general_doc,))
        assert cur.rowcount == 0


def test_pinned_team_narrows_an_org_admins_view(db):
    with as_app(db, "ada") as cur:
        assert team_ids_of(cur, "documents") == [1, db["windows"]]
    with as_app(db, "ada", team=db["windows"]) as cur:
        assert team_ids_of(cur, "documents") == [db["windows"]]


def test_pinning_a_foreign_team_shows_nothing(db):
    with as_app(db, "bob", team=1) as cur:
        assert one(cur, "SELECT COUNT(*) FROM documents") == 0


def test_other_organizations_are_invisible_even_to_their_admins(db):
    with as_app(db, "zed") as cur:
        assert one(cur, "SELECT COUNT(*) FROM documents") == 0
        cur.execute("SELECT id FROM teams")
        assert cur.fetchall() == [(db["other_team"],)]


# ------------------------------------------------------------------------------- writes
def test_writing_into_a_foreign_team_is_rejected(db):
    import psycopg2

    with as_app(db, "bob") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege, match="row-level security"):
        cur.execute("INSERT INTO reagents (name, quantity, unit, team_id) VALUES ('x', 1, 'mL', 1)")


def test_insert_without_team_id_lands_in_the_active_team(db):
    with as_app(db, "bob", team=db["windows"]) as cur:
        cur.execute("INSERT INTO reagents (name, quantity, unit) VALUES ('Fresh', 1, 'mL') RETURNING team_id")
        assert cur.fetchone()[0] == db["windows"]


def test_team_id_can_never_change(db):
    import psycopg2

    with as_owner(db) as o, pytest.raises(psycopg2.errors.CheckViolation, match="cannot change"):
        o.execute("UPDATE documents SET team_id = 1 WHERE id = %s", (db["windows_doc"],))


def test_viewers_are_read_only_but_may_ask_questions(db):
    import psycopg2

    with as_app(db, "carol") as cur:
        assert one(cur, "SELECT COUNT(*) FROM documents") == 1
        cur.execute("INSERT INTO chat_messages (session_id, role, content, team_id) VALUES ('s1', 'user', 'hi', %s)",
                    (db["windows"],))
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO reagents (name, quantity, unit, team_id) VALUES ('x', 1, 'mL', %s)", (db["windows"],))


def test_child_rows_follow_their_parents_team_and_role(db):
    import psycopg2

    insert_step = "INSERT INTO experiment_steps (experiment_id, position, text) VALUES (%s, 2, 'Spin')"
    with as_app(db, "bob") as cur:
        cur.execute(insert_step, (db["windows_exp"],))
        with as_owner(db) as o:
            general_exp = one(o, "SELECT MIN(id) FROM experiments WHERE team_id = 1")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(insert_step, (general_exp,))
    with as_app(db, "carol") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute(insert_step, (db["windows_exp"],))


def test_deleting_documents_needs_a_team_admin(db):
    with as_app(db, "bob") as cur:
        cur.execute("DELETE FROM documents WHERE id = %s", (db["windows_doc"],))
        assert cur.rowcount == 0
    with as_app(db, "dave") as cur:
        cur.execute("DELETE FROM documents WHERE id = %s", (db["windows_doc"],))
        assert cur.rowcount == 1
        assert one(cur, "SELECT COUNT(*) FROM chunks") == 0  # cascade still works under RLS


# ------------------------------------------------------------------------------- audit trail
def test_audit_log_is_append_only_for_the_app(db):
    import psycopg2

    with as_app(db, "bob") as cur:
        cur.execute("INSERT INTO audit_log (action, entity_type, summary, team_id) VALUES ('x', 'y', 'z', %s)",
                    (db["windows"],))
        cur.execute("INSERT INTO audit_log (action, entity_type, summary, team_id) VALUES ('signup', 'user', 'no team', NULL)")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_log SET summary = 'rewritten'")
    with as_app(db, "bob") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM audit_log")


def test_team_less_audit_rows_are_for_org_admins_only(db):
    with as_owner(db) as o:
        o.execute("INSERT INTO audit_log (action, entity_type, summary, team_id) VALUES ('signup', 'user', 'org event', NULL)")
        o.connection.commit()
    with as_app(db, "bob") as cur:
        assert one(cur, "SELECT COUNT(*) FROM audit_log WHERE team_id IS NULL") == 0
    with as_app(db, "ada") as cur:
        assert one(cur, "SELECT COUNT(*) FROM audit_log WHERE team_id IS NULL") >= 1


# ------------------------------------------------------------------------------- memberships
def test_admins_cannot_grant_above_their_own_role(db):
    import psycopg2

    with as_app(db, "dave") as cur:
        cur.execute("UPDATE team_memberships SET role = 'member' WHERE team_id = %s AND user_id = %s",
                    (db["windows"], db["carol"]))
        assert cur.rowcount == 1
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO team_memberships (team_id, user_id, role) VALUES (%s, %s, 'owner')",
                        (db["windows"], db["lena"]))


def test_members_cannot_manage_memberships(db):
    import psycopg2

    with as_app(db, "bob") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("INSERT INTO team_memberships (team_id, user_id, role) VALUES (%s, %s, 'member')",
                    (db["windows"], db["lena"]))


def test_accounts_from_another_organization_cannot_be_added(db):
    import psycopg2

    with as_app(db, "ada") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("INSERT INTO team_memberships (team_id, user_id, role) VALUES (%s, %s, 'member')",
                    (db["windows"], db["zed"]))


def test_a_team_keeps_its_last_owner(db):
    import psycopg2

    with as_owner(db) as o, pytest.raises(psycopg2.errors.CheckViolation, match="at least one owner"):
        o.execute("DELETE FROM team_memberships WHERE team_id = %s AND user_id = %s", (db["windows"], db["erin"]))


def test_signup_enrolls_into_the_default_team_once(db):
    with as_owner(db) as o:
        o.execute("INSERT INTO users (email, name, password_hash) VALUES ('new@lab.org', 'New', 'x') RETURNING id")
        new_id = o.fetchone()[0]
        o.connection.commit()
    with as_app(db) as cur:
        assert one(cur, "SELECT smartlab.enroll_in_default_team(%s)", (new_id,)) == 1
        assert one(cur, "SELECT smartlab.enroll_in_default_team(%s)", (new_id,)) is None


def test_org_admins_create_teams_members_cannot(db):
    import psycopg2

    with as_app(db, "ada") as cur:
        cur.execute("INSERT INTO teams (organization_id, name, slug) VALUES (1, 'Team Linux', 'linux') RETURNING id")
        assert cur.fetchone()[0] > 0
    with as_app(db, "bob") as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("INSERT INTO teams (organization_id, name, slug) VALUES (1, 'Rogue', 'rogue')")


# ------------------------------------------------------------------------------- identity
def test_only_the_app_role_can_assert_a_user_id(db):
    """A Supabase Auth client is identified by its JWT; setting smartlab.user_id does nothing for it."""
    import psycopg2

    admin = urlsplit(ADMIN_URL)
    conn = psycopg2.connect(_url(admin.username, admin.password, urlsplit(db["app_url"]).path.lstrip("/")))
    try:
        cur = conn.cursor()
        cur.execute("GRANT USAGE ON SCHEMA smartlab TO authenticated; "
                    "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA smartlab TO authenticated; SET ROLE authenticated")
        cur.execute("SELECT set_config('smartlab.user_id', %s, true), set_config('request.jwt.claim.sub', %s, true)",
                    (str(db["ada"]), db["carol_auth"]))
        assert one(cur, "SELECT smartlab.current_user_id()") == db["carol"]
    finally:
        conn.rollback()
        conn.close()


def test_data_api_roles_have_no_table_access(db):
    with as_owner(db) as o:
        assert one(o, "SELECT has_table_privilege('anon', 'public.documents', 'SELECT')") is False
        assert one(o, "SELECT has_table_privilege('authenticated', 'public.users', 'SELECT')") is False


# ------------------------------------------------------------------------------- performance
def test_policies_evaluate_membership_once_per_statement(db):
    with as_app(db, "bob") as cur:
        cur.execute("EXPLAIN SELECT * FROM documents")
        plan = "\n".join(r[0] for r in cur.fetchall())
        assert "InitPlan" in plan and re.search(r"team_id = ANY \(\$?\w*\)", plan), plan


def test_child_table_reads_probe_the_parent_once_per_row(db):
    """Write policies are per command: a SELECT must not also evaluate an OR'd write policy."""
    with as_app(db, "bob") as cur:
        cur.execute("EXPLAIN SELECT * FROM chunks")
        plan = "\n".join(r[0] for r in cur.fetchall())
        assert len(re.findall(r"^\s*SubPlan \d+", plan, re.M)) == 1, plan


# ------------------------------------------------------------------------------- vector search
def test_vector_search_is_scoped_and_returns_k_rows(db):
    with as_owner(db) as o:
        o.execute("SELECT embedding::text FROM chunk_embeddings ce JOIN chunks c ON c.id = ce.chunk_id "
                  "WHERE c.document_id = %s ORDER BY c.id LIMIT 1", (db["windows_doc"],))
        query = o.fetchone()[0]
        o.execute("SELECT id FROM chunks WHERE document_id = %s", (db["windows_doc"],))
        windows_chunks = {r[0] for r in o.fetchall()}
    with as_app(db, "bob") as cur:
        cur.execute("SET LOCAL enable_seqscan = off")  # force the HNSW path even on this small table
        cur.execute("SELECT chunk_id, similarity FROM smartlab.match_chunks(%s::extensions.vector, 10)", (query,))
        hits = cur.fetchall()
        # Windows holds 12 of ~all chunks; iterative scanning still fills k from the team's own chunks.
        assert len(hits) == 10 and {h[0] for h in hits} <= windows_chunks
        assert hits[0][1] == pytest.approx(1.0, abs=1e-4)  # the query vector itself ranks first
    with as_app(db, "ada", team=1) as cur:
        cur.execute("SELECT chunk_id FROM smartlab.match_chunks(%s::extensions.vector, 10)", (query,))
        assert not {r[0] for r in cur.fetchall()} & windows_chunks


def test_embedding_team_is_derived_from_the_document(db):
    """A caller cannot file an embedding under a team other than its chunk's document."""
    with as_owner(db) as o:
        o.execute("SELECT c.id FROM chunks c WHERE c.document_id = %s LIMIT 1", (db["windows_doc"],))
        chunk = o.fetchone()[0]
        o.execute("DELETE FROM chunk_embeddings WHERE chunk_id = %s", (chunk,))
        o.execute("INSERT INTO chunk_embeddings (chunk_id, team_id, model, embedding) "
                  "VALUES (%s, 1, 'test', array_fill(0.1, ARRAY[%s])::extensions.vector) RETURNING team_id", (chunk, DIM))
        assert o.fetchone()[0] == db["windows"]
