-- =====================================================================================================
-- 003_indexes.sql: foreign-key validation and team-leading indexes, without blocking writes.
--
-- Requires 001_tenancy.sql. Idempotent. Must run OUTSIDE a transaction (CREATE INDEX CONCURRENTLY):
--     psql "$OWNER_URL" -f backend/migrations/003_indexes.sql
-- The Supabase SQL editor wraps a run in one transaction; there, run the statements one at a time.
--
-- Every index leads with team_id: under RLS every query on these tables carries
-- "team_id = ANY (<caller's teams>)", so team_id is the most selective prefix, followed by the column
-- each endpoint sorts or filters on (named per index).
-- =====================================================================================================

-- Check existing rows against the foreign keys added NOT VALID in 001. SHARE UPDATE EXCLUSIVE lock:
-- reads and writes continue while it scans.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
              WHERE NOT convalidated AND contype = 'f'
                AND connamespace = 'public'::regnamespace
                AND (conname LIKE '%\_team\_id\_fkey' OR conname = 'users_organization_id_fkey') LOOP
        EXECUTE format('ALTER TABLE %s VALIDATE CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- documents: list (ORDER BY updated_at), activity feed (uploaded_at > ?), safety (category),
-- version history (doc_code).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_team_updated  ON public.documents (team_id, updated_at DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_team_uploaded ON public.documents (team_id, uploaded_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_team_category ON public.documents (team_id, category);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_team_code     ON public.documents (team_id, doc_code) WHERE doc_code IS NOT NULL;

-- reagents: inventory list / co-pilot stock checks (ORDER BY expiry_date, id).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reagents_team_expiry ON public.reagents (team_id, expiry_date, id);

-- troubleshooting: exact error-code lookups by the co-pilot (verified entries only).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_troubleshooting_team_codes ON public.troubleshooting (team_id, error_code)
    WHERE verified = 1 AND error_code IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_equipment_team ON public.equipment (team_id, id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_projects_team  ON public.projects (team_id, id);

-- experiments: list (ORDER BY updated_at DESC, id DESC).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_experiments_team_updated ON public.experiments (team_id, updated_at DESC, id DESC);

-- datasets: list (ORDER BY id DESC).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_datasets_team ON public.datasets (team_id, id DESC);

-- comments: thread per entity (ORDER BY id) and the activity feed (created_at > ?).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_comments_team_entity  ON public.comments (team_id, entity_type, entity_id, id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_comments_team_created ON public.comments (team_id, created_at);

-- shared_files: files per project (ORDER BY id DESC) and the activity feed (uploaded_at > ?).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shared_files_team_project  ON public.shared_files (team_id, project_id, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shared_files_team_uploaded ON public.shared_files (team_id, uploaded_at);

-- chat_messages: one conversation (session_id ... ORDER BY id) and the session list (GROUP BY session_id).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_team_session ON public.chat_messages (team_id, session_id, id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_uploads_team ON public.agent_uploads (team_id);

-- agent_runs: the run log (ORDER BY id DESC).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_runs_team ON public.agent_runs (team_id, id DESC);

-- audit_log: the activity feed (created_at > ?). Per-entity history keeps idx_audit_entity from db.py.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_team_created ON public.audit_log (team_id, created_at);

-- users: the organization_id foreign key, used by every team_ids() call.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_organization ON public.users (organization_id);

-- Refresh planner statistics so the new indexes are used straight away.
ANALYZE public.documents, public.reagents, public.troubleshooting, public.experiments, public.chat_messages,
        public.comments, public.shared_files, public.audit_log, public.agent_runs, public.team_memberships,
        public.teams, public.users;

-- -----------------------------------------------------------------------------------------------------
-- Optional, once tables grow past ~10k rows: the search boxes run "LIKE '%term%'" (ILIKE on PostgreSQL),
-- which a B-tree cannot serve. Trigram GIN indexes can.
--   CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_title_trgm
--       ON public.documents USING gin (title extensions.gin_trgm_ops);
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reagents_name_trgm
--       ON public.reagents USING gin (name extensions.gin_trgm_ops);
-- -----------------------------------------------------------------------------------------------------
