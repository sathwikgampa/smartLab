-- =====================================================================================================
-- 002_rls.sql: row-level security for team isolation, enforced by PostgreSQL itself.
--
-- Requires 001_tenancy.sql. Idempotent. Run as the table owner (Supabase: postgres).
--
-- Who is the caller?
--   * The Flask app connects as role smartlab_app (NOBYPASSRLS, not the table owner) and declares the
--     signed-in account per transaction:
--         SELECT set_config('smartlab.user_id', '42', true), set_config('smartlab.team_id', '7', true);
--     (is_local = true: the setting ends with the transaction, so it is safe behind Supabase's
--     transaction-mode pooler and can never leak to the next request on a pooled connection).
--     Only sessions logged in as smartlab_app may assert a user id this way.
--   * Supabase Auth clients (supabase-js / PostgREST with a user JWT) are identified by auth.uid(),
--     mapped through users.auth_user_id. No policy changes are needed to adopt Supabase Auth later.
--   * The table owner (postgres: migrations, init_schema, seeding) bypasses RLS, as owners do.
--     FORCE ROW LEVEL SECURITY is deliberately not used, so maintenance keeps working.
--
-- Isolation rule: a row is visible when its team is one of the caller's teams AND, if the transaction
-- pinned an active team (smartlab.team_id), it is that team. With no identity, nothing is visible.
-- =====================================================================================================
BEGIN;
SET LOCAL lock_timeout = '5s';

-- ------------------------------------------------------------------------------- application role
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'smartlab_app') THEN
        -- The password is set outside version control:  ALTER ROLE smartlab_app PASSWORD '...';
        CREATE ROLE smartlab_app LOGIN NOINHERIT NOBYPASSRLS;
    END IF;
END $$;
-- Bound runaway queries. idle_in_transaction_session_timeout is intentionally not set: a co-pilot request
-- may hold its transaction open while waiting up to AGENT_TIMEOUT_S (90 s) for the Claude API.
ALTER ROLE smartlab_app SET statement_timeout = '15s';

-- ------------------------------------------------------------------------------- identity helpers
CREATE OR REPLACE FUNCTION smartlab.role_rank(p_role text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE p_role WHEN 'viewer' THEN 0 WHEN 'member' THEN 1 WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END
$$;

-- Supabase Auth: the account linked to the JWT subject. Dynamic SQL so the script also installs on plain
-- PostgreSQL, where the auth schema does not exist.
CREATE OR REPLACE FUNCTION smartlab.auth_account_id() RETURNS integer
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_id integer;
BEGIN
    IF to_regprocedure('auth.uid()') IS NULL THEN
        RETURN NULL;
    END IF;
    EXECUTE 'SELECT u.id FROM public.users u WHERE u.auth_user_id = auth.uid()' INTO v_id;
    RETURN v_id;
END $$;

-- The calling account. session_user (the login role) is unchanged inside SECURITY DEFINER functions and
-- cannot be altered by SET ROLE from a less privileged role, so other roles cannot spoof the setting.
CREATE OR REPLACE FUNCTION smartlab.current_user_id() RETURNS integer
LANGUAGE plpgsql STABLE SET search_path = '' AS $$
BEGIN
    IF session_user = 'smartlab_app' THEN
        RETURN NULLIF(current_setting('smartlab.user_id', true), '')::integer;
    END IF;
    RETURN smartlab.auth_account_id();
END $$;

-- The active team pinned for this transaction, or NULL. Trusted from anyone: it can only narrow what
-- smartlab.team_ids() returns, never widen it.
CREATE OR REPLACE FUNCTION smartlab.current_team_id() RETURNS integer
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT NULLIF(current_setting('smartlab.team_id', true), '')::integer
$$;

-- Teams the caller holds at least p_min_role in (restricted to the pinned team, if any).
-- SECURITY DEFINER: reads memberships without recursing into their own RLS policies.
-- Returned as an array so policies can use  team_id = ANY ((SELECT smartlab.team_ids(...))::integer[]) : the
-- scalar sub-select is evaluated once per statement (an InitPlan), not once per row, and the
-- comparison can use the (team_id, ...) indexes.
CREATE OR REPLACE FUNCTION smartlab.team_ids(p_min_role text DEFAULT 'viewer') RETURNS integer[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT COALESCE(array_agg(t.id ORDER BY t.id), '{}')
      FROM public.teams t
      JOIN public.users u ON u.id = smartlab.current_user_id() AND u.organization_id = t.organization_id
      LEFT JOIN public.team_memberships m ON m.team_id = t.id AND m.user_id = u.id
     WHERE t.archived_at IS NULL
       AND (smartlab.current_team_id() IS NULL OR t.id = smartlab.current_team_id())
       AND smartlab.role_rank(CASE WHEN u.role = 'admin' THEN 'owner' ELSE m.role END)
           >= smartlab.role_rank(p_min_role)
$$;

-- The caller's effective role in one team (org admins are owners), or NULL.
CREATE OR REPLACE FUNCTION smartlab.team_role(p_team integer) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT CASE WHEN u.role = 'admin' THEN 'owner' ELSE m.role END
      FROM public.teams t
      JOIN public.users u ON u.id = smartlab.current_user_id() AND u.organization_id = t.organization_id
      LEFT JOIN public.team_memberships m ON m.team_id = t.id AND m.user_id = u.id
     WHERE t.id = p_team AND t.archived_at IS NULL
$$;

CREATE OR REPLACE FUNCTION smartlab.current_org_id() RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT u.organization_id FROM public.users u WHERE u.id = smartlab.current_user_id()
$$;

CREATE OR REPLACE FUNCTION smartlab.is_org_admin(p_org integer DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT EXISTS (SELECT 1 FROM public.users u
                    WHERE u.id = smartlab.current_user_id() AND u.role = 'admin'
                      AND (p_org IS NULL OR u.organization_id = p_org))
$$;

-- Sign-up: put a brand-new account (no memberships yet) into its organization's default team. This is the
-- only way smartlab_app adds a membership without being a team admin, and it cannot target another team.
CREATE OR REPLACE FUNCTION smartlab.enroll_in_default_team(p_user integer) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_team integer;
    v_role text;
BEGIN
    IF EXISTS (SELECT 1 FROM public.team_memberships WHERE user_id = p_user) THEN
        RETURN NULL;
    END IF;
    SELECT t.id, CASE WHEN u.role = 'admin' THEN 'owner' ELSE 'member' END INTO v_team, v_role
      FROM public.users u
      JOIN public.teams t ON t.organization_id = u.organization_id AND t.is_default = 1 AND t.archived_at IS NULL
     WHERE u.id = p_user;
    IF v_team IS NOT NULL THEN
        INSERT INTO public.team_memberships (team_id, user_id, role) VALUES (v_team, p_user, v_role);
    END IF;
    RETURN v_team;
END $$;

-- Team creation: any authenticated member can create a team in their own organization.
-- Created atomically with the caller as its owner.
CREATE OR REPLACE FUNCTION smartlab.create_team(p_name text, p_slug text, p_description text DEFAULT NULL) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user integer;
    v_org integer;
    v_team integer;
BEGIN
    v_user := smartlab.current_user_id();
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'Authentication required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT u.organization_id INTO v_org FROM public.users u WHERE u.id = v_user;
    IF v_org IS NULL THEN
        RAISE EXCEPTION 'User has no organization' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO public.teams (organization_id, name, slug, description, is_default)
    VALUES (v_org, p_name, p_slug, p_description, 0)
    RETURNING id INTO v_team;
    INSERT INTO public.team_memberships (team_id, user_id, role)
    VALUES (v_team, v_user, 'owner');
    RETURN v_team;
END $$;

-- ------------------------------------------------------------------------------- column defaults
-- An INSERT that omits team_id lands in the transaction's active team rather than silently in team 1.
-- Owner connections (seeding, and today's app before it becomes team-aware) have no active team and keep
-- the old default of 1. Existing rows are untouched. audit_log included: until the app passes team_id,
-- its audit rows must stay in team 1, not become team-less (which only org admins can read). Team-less
-- events (sign-up) are written with an explicit NULL.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['documents', 'reagents', 'troubleshooting', 'equipment', 'projects', 'experiments',
                             'datasets', 'comments', 'shared_files', 'chat_messages', 'agent_uploads',
                             'agent_runs', 'audit_log'] LOOP
        EXECUTE format('ALTER TABLE public.%I ALTER COLUMN team_id SET DEFAULT COALESCE(smartlab.current_team_id(), 1)', t);
    END LOOP;
END $$;

-- ------------------------------------------------------------------------------- team-owned tables
-- (table, minimum role to insert/update, minimum role to delete). Finer rules, such as who may approve
-- a document, stay in the Flask layer (@require_team_access).
DO $$
DECLARE s record;
BEGIN
    FOR s IN SELECT * FROM (VALUES
        ('documents',       'member', 'admin'),
        ('reagents',        'member', 'admin'),
        ('troubleshooting', 'member', 'admin'),
        ('equipment',       'member', 'admin'),
        ('projects',        'member', 'admin'),
        ('experiments',     'member', 'member'),
        ('datasets',        'member', 'member'),
        ('comments',        'member', 'member'),
        ('shared_files',    'member', 'member'),
        ('chat_messages',   'viewer', 'member'),  -- viewers may ask the co-pilot questions
        ('agent_uploads',   'viewer', 'member')
    ) AS v(tbl, write_role, delete_role) LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_select', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_insert', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_update', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_delete', s.tbl);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT '
                       'USING (team_id = ANY ((SELECT smartlab.team_ids(''viewer''))::integer[]))', s.tbl || '_select', s.tbl);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR INSERT '
                       'WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids(%L))::integer[]))', s.tbl || '_insert', s.tbl, s.write_role);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR UPDATE '
                       'USING (team_id = ANY ((SELECT smartlab.team_ids(%L))::integer[])) '
                       'WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids(%L))::integer[]))',
                       s.tbl || '_update', s.tbl, s.write_role, s.write_role);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR DELETE '
                       'USING (team_id = ANY ((SELECT smartlab.team_ids(%L))::integer[]))', s.tbl || '_delete', s.tbl, s.delete_role);
    END LOOP;
END $$;

-- ------------------------------------------------------------------------------- append-only audit
-- SELECT + INSERT only: there is no UPDATE or DELETE policy, so RLS refuses both (and the PG_AUDIT_GUARD
-- triggers refuse them for the owner too). Team-less rows (sign-up, password reset) are org-admin only.
ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_log_select ON public.audit_log;
DROP POLICY IF EXISTS audit_log_insert ON public.audit_log;
CREATE POLICY audit_log_select ON public.audit_log FOR SELECT
    USING (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[])
           OR (team_id IS NULL AND (SELECT smartlab.is_org_admin())));
CREATE POLICY audit_log_insert ON public.audit_log FOR INSERT
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[])
                OR (team_id IS NULL AND session_user = 'smartlab_app'));

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_runs_select ON public.agent_runs;
DROP POLICY IF EXISTS agent_runs_insert ON public.agent_runs;
CREATE POLICY agent_runs_select ON public.agent_runs FOR SELECT
    USING (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[]));
CREATE POLICY agent_runs_insert ON public.agent_runs FOR INSERT
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[]));

-- ------------------------------------------------------------------------------- child tables
-- Scoped through their parent row (no team_id of their own). Each EXISTS probe is a primary-key lookup.
-- Referential cascades (ON DELETE CASCADE) bypass RLS, so deleting a parent still removes its children.
-- One policy per command, never FOR ALL: a FOR ALL policy also applies to SELECT, and PostgreSQL ORs
-- permissive policies, so every invisible row would be probed twice.
DO $$
DECLARE
    s record;
    probe text;
BEGIN
    FOR s IN SELECT * FROM (VALUES
        ('chunks',               'documents',   'document_id'),
        ('experiment_steps',     'experiments', 'experiment_id'),
        ('experiment_variables', 'experiments', 'experiment_id'),
        ('reviews',              'experiments', 'experiment_id')
    ) AS v(tbl, parent, fk) LOOP
        -- EXISTS (SELECT 1 FROM <parent> p WHERE p.id = <tbl>.<fk> AND p.team_id = ANY (<caller's teams>))
        probe := format('EXISTS (SELECT 1 FROM public.%I p WHERE p.id = %I.%I '
                        'AND p.team_id = ANY ((SELECT smartlab.team_ids(%%L))::integer[]))', s.parent, s.tbl, s.fk);
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_select', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_insert', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_update', s.tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', s.tbl || '_delete', s.tbl);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT USING (%s)',
                       s.tbl || '_select', s.tbl, format(probe, 'viewer'));
        EXECUTE format('CREATE POLICY %I ON public.%I FOR INSERT WITH CHECK (%s)',
                       s.tbl || '_insert', s.tbl, format(probe, 'member'));
        EXECUTE format('CREATE POLICY %I ON public.%I FOR UPDATE USING (%s) WITH CHECK (%s)',
                       s.tbl || '_update', s.tbl, format(probe, 'member'), format(probe, 'member'));
        EXECUTE format('CREATE POLICY %I ON public.%I FOR DELETE USING (%s)',
                       s.tbl || '_delete', s.tbl, format(probe, 'member'));
    END LOOP;
END $$;

-- Viewers may rate answers (insert/replace their open flag); resolving a flag needs a team admin.
ALTER TABLE public.answer_feedback ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answer_feedback_select ON public.answer_feedback;
DROP POLICY IF EXISTS answer_feedback_insert ON public.answer_feedback;
DROP POLICY IF EXISTS answer_feedback_update ON public.answer_feedback;
DROP POLICY IF EXISTS answer_feedback_delete ON public.answer_feedback;
CREATE POLICY answer_feedback_select ON public.answer_feedback FOR SELECT
    USING (EXISTS (SELECT 1 FROM public.chat_messages c WHERE c.id = answer_feedback.message_id
                     AND c.team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[])));
CREATE POLICY answer_feedback_insert ON public.answer_feedback FOR INSERT
    WITH CHECK (EXISTS (SELECT 1 FROM public.chat_messages c WHERE c.id = answer_feedback.message_id
                          AND c.team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[])));
CREATE POLICY answer_feedback_update ON public.answer_feedback FOR UPDATE
    USING (EXISTS (SELECT 1 FROM public.chat_messages c WHERE c.id = answer_feedback.message_id
                     AND c.team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])))
    WITH CHECK (EXISTS (SELECT 1 FROM public.chat_messages c WHERE c.id = answer_feedback.message_id
                          AND c.team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])));
CREATE POLICY answer_feedback_delete ON public.answer_feedback FOR DELETE
    USING (status = 'open' AND EXISTS (SELECT 1 FROM public.chat_messages c WHERE c.id = answer_feedback.message_id
                                         AND c.team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[])));

-- ------------------------------------------------------------------------------- tenancy tables
ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS organizations_select ON public.organizations;
DROP POLICY IF EXISTS organizations_app ON public.organizations;
CREATE POLICY organizations_select ON public.organizations FOR SELECT
    USING (id = (SELECT smartlab.current_org_id()));
CREATE POLICY organizations_app ON public.organizations TO smartlab_app
    USING (true) WITH CHECK (true);

ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS teams_select ON public.teams;
DROP POLICY IF EXISTS teams_insert ON public.teams;
DROP POLICY IF EXISTS teams_update ON public.teams;
DROP POLICY IF EXISTS teams_app ON public.teams;
-- Org admins also see their org's archived teams, and a team they have just created: the app appends
-- RETURNING id to INSERTs, and team_ids() is computed when the statement starts, before the row exists.
CREATE POLICY teams_select ON public.teams FOR SELECT
    USING (id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[]) OR smartlab.is_org_admin(organization_id));
CREATE POLICY teams_insert ON public.teams FOR INSERT
    WITH CHECK (smartlab.is_org_admin(organization_id) AND is_default = 0);
CREATE POLICY teams_app ON public.teams TO smartlab_app FOR INSERT
    WITH CHECK (true);
CREATE POLICY teams_update ON public.teams FOR UPDATE
    USING (id = ANY ((SELECT smartlab.team_ids('admin'))::integer[]))
    WITH CHECK (id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])
                AND organization_id = (SELECT smartlab.current_org_id()));
-- No DELETE policy: archive (archived_at) instead.

ALTER TABLE public.team_memberships ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_memberships_select ON public.team_memberships;
DROP POLICY IF EXISTS team_memberships_insert ON public.team_memberships;
DROP POLICY IF EXISTS team_memberships_update ON public.team_memberships;
DROP POLICY IF EXISTS team_memberships_delete ON public.team_memberships;
CREATE POLICY team_memberships_select ON public.team_memberships FOR SELECT
    USING (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[]) OR user_id = (SELECT smartlab.current_user_id()));
-- Team admins manage members, and can never grant a role above their own.
CREATE POLICY team_memberships_insert ON public.team_memberships FOR INSERT
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])
                AND smartlab.role_rank(role) <= smartlab.role_rank(smartlab.team_role(team_id))
                AND EXISTS (SELECT 1 FROM public.users u WHERE u.id = team_memberships.user_id
                              AND u.organization_id = (SELECT smartlab.current_org_id())));
CREATE POLICY team_memberships_update ON public.team_memberships FOR UPDATE
    USING (team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])
           AND smartlab.role_rank(role) <= smartlab.role_rank(smartlab.team_role(team_id)))
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])
                AND smartlab.role_rank(role) <= smartlab.role_rank(smartlab.team_role(team_id)));
-- Admins remove members (not ones ranked above them); anyone may leave a team themselves.
CREATE POLICY team_memberships_delete ON public.team_memberships FOR DELETE
    USING ((team_id = ANY ((SELECT smartlab.team_ids('admin'))::integer[])
            AND smartlab.role_rank(role) <= smartlab.role_rank(smartlab.team_role(team_id)))
           OR user_id = (SELECT smartlab.current_user_id()));

-- ------------------------------------------------------------------------------- auth & global tables
-- Login, sign-up and session checks run before anyone is identified, so the authentication tables are
-- open to the app role only. Supabase Data API roles get nothing (see grants below).
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'auth_sessions', 'password_resets', 'organization_invitations', 'profiles', 'settings', 'compounds'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_app', t);
        EXECUTE format('CREATE POLICY %I ON public.%I TO smartlab_app USING (true) WITH CHECK (true)', t || '_app', t);
    END LOOP;
END $$;

-- ------------------------------------------------------------------------------- privileges
GRANT USAGE ON SCHEMA public, smartlab TO smartlab_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smartlab_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smartlab_app;
-- Belt and braces on top of RLS: the audit trail is append-only, tenants are archived not deleted.
REVOKE UPDATE, DELETE ON public.audit_log, public.agent_runs FROM smartlab_app;
REVOKE UPDATE, DELETE ON public.organizations FROM smartlab_app;
REVOKE DELETE ON public.teams FROM smartlab_app;
-- Tables that init_schema() creates in future releases get the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO smartlab_app;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA smartlab FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA smartlab TO smartlab_app;

-- Supabase exposes the public schema over its Data API with the anon key, and grants anon/authenticated
-- full table privileges by default. Nothing in SmartLab is meant to be reached that way (yet).
-- To adopt Supabase Auth later: GRANT USAGE ON SCHEMA smartlab and EXECUTE on its functions, plus
-- table privileges, to authenticated. The policies above already handle auth.uid().
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
        END IF;
    END LOOP;
END $$;

COMMIT;
