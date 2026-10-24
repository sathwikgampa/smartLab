-- =====================================================================================================
-- 004_pgvector.sql (optional): chunk embeddings in PostgreSQL, searched under the same RLS as everything else.
--
-- Today the vectors live in a FAISS/NumPy index on local disk (rag/vector_store.py), rebuilt on every
-- Render deploy and team-filtered through a per-process cache. Storing them here lets one SQL query
-- apply team isolation and similarity ranking together, and survives restarts.
--
-- Requires 001 and 002, and pgvector >= 0.8.0 (iterative index scans; Supabase ships 0.8). Idempotent.
-- Dimension 384 = sentence-transformers/all-MiniLM-L6-v2 (the production EMBEDDING_MODEL). The 2048-dim
-- offline "hashing" embedder used by the tests exceeds HNSW's 2000-dimension limit for vector columns and
-- stays on the in-process index. Changing the model means a new column and a re-embed, and the model
-- column records which model produced each row.
-- =====================================================================================================
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE SCHEMA IF NOT EXISTS extensions;  -- present on Supabase already
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

CREATE TABLE IF NOT EXISTS public.chunk_embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES public.chunks(id) ON DELETE CASCADE,
    -- Copied from the chunk's document by trigger, so the vector index can be filtered without a join.
    -- documents.team_id is immutable (001), so this copy can never drift.
    team_id INTEGER NOT NULL REFERENCES public.teams(id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    embedding extensions.vector(384) NOT NULL
);

CREATE OR REPLACE FUNCTION smartlab.chunk_embedding_team() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    SELECT d.team_id INTO NEW.team_id
      FROM public.chunks c JOIN public.documents d ON d.id = c.document_id
     WHERE c.id = NEW.chunk_id;
    IF NEW.team_id IS NULL THEN
        RAISE EXCEPTION 'chunk % does not exist', NEW.chunk_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS chunk_embeddings_team ON public.chunk_embeddings;
-- BEFORE triggers run before RLS WITH CHECK, so the policy below checks the real (derived) team.
CREATE TRIGGER chunk_embeddings_team BEFORE INSERT OR UPDATE ON public.chunk_embeddings
    FOR EACH ROW EXECUTE FUNCTION smartlab.chunk_embedding_team();

ALTER TABLE public.chunk_embeddings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunk_embeddings_select ON public.chunk_embeddings;
DROP POLICY IF EXISTS chunk_embeddings_insert ON public.chunk_embeddings;
DROP POLICY IF EXISTS chunk_embeddings_update ON public.chunk_embeddings;
DROP POLICY IF EXISTS chunk_embeddings_delete ON public.chunk_embeddings;
-- Per command rather than FOR ALL, so similarity searches evaluate only the SELECT policy (see 002).
CREATE POLICY chunk_embeddings_select ON public.chunk_embeddings FOR SELECT
    USING (team_id = ANY ((SELECT smartlab.team_ids('viewer'))::integer[]));
CREATE POLICY chunk_embeddings_insert ON public.chunk_embeddings FOR INSERT
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('member'))::integer[]));
CREATE POLICY chunk_embeddings_update ON public.chunk_embeddings FOR UPDATE
    USING (team_id = ANY ((SELECT smartlab.team_ids('member'))::integer[]))
    WITH CHECK (team_id = ANY ((SELECT smartlab.team_ids('member'))::integer[]));
CREATE POLICY chunk_embeddings_delete ON public.chunk_embeddings FOR DELETE
    USING (team_id = ANY ((SELECT smartlab.team_ids('member'))::integer[]));

GRANT SELECT, INSERT, UPDATE, DELETE ON public.chunk_embeddings TO smartlab_app;

-- Cosine distance, matching the normalised embeddings the app produces. m / ef_construction are
-- pgvector's defaults, a good balance for corpora up to a few million chunks.
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_hnsw ON public.chunk_embeddings
    USING hnsw (embedding extensions.vector_cosine_ops) WITH (m = 16, ef_construction = 64);
-- Lets the planner choose an exact scan for a small team instead of walking the global graph.
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_team ON public.chunk_embeddings (team_id);

-- Top-k chunks for a query vector, within the caller's (pinned) team. SECURITY INVOKER: the caller's RLS
-- applies to every table read here.
--
-- Two strategies, picked per call:
--   * Small scope (a team under 10,000 vectors, or an explicit document list): exact search. The team
--     index narrows to the team's rows and they are ranked directly, which is exact and fast.
--   * Large scope: HNSW. The graph is global and filtered after neighbours are found, so for a team
--     holding a small share of all vectors a plain scan returns fewer than k rows. iterative_scan keeps
--     walking until k rows pass the filter. relaxed_order may emit rows slightly out of order, hence
--     the outer ORDER BY.
-- Measured on PostgreSQL 16 + pgvector 0.8.7, 20,000 vectors, k = 10: a 12-vector team took 69 ms
-- (cold) through HNSW alone, against 16 ms cold / 8 ms warm with this split; the 20,000-vector team
-- runs at 8 ms warm through HNSW.
CREATE OR REPLACE FUNCTION smartlab.match_chunks(
    p_query extensions.vector(384),
    p_count integer DEFAULT 5,
    p_document_ids integer[] DEFAULT NULL
) RETURNS TABLE (chunk_id integer, document_id integer, similarity double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = ''
SET hnsw.iterative_scan = 'relaxed_order'
SET hnsw.ef_search = '100'
AS $$
#variable_conflict use_column
DECLARE
    v_teams integer[] := smartlab.team_ids('viewer');
    v_limit integer := LEAST(GREATEST(p_count, 1), 50);
BEGIN
    -- Capped count: reads at most 10,001 entries of idx_chunk_embeddings_team, however large the team.
    IF p_document_ids IS NOT NULL
       OR (SELECT count(*) FROM (SELECT 1 FROM public.chunk_embeddings e
                                  WHERE e.team_id = ANY (v_teams) LIMIT 10001) s) <= 10000 THEN
        RETURN QUERY
        SELECT e.chunk_id, c.document_id, 1 - (e.embedding OPERATOR(extensions.<=>) p_query)
          FROM public.chunk_embeddings e
          JOIN public.chunks c ON c.id = e.chunk_id
         WHERE e.team_id = ANY (v_teams)
           AND (p_document_ids IS NULL OR c.document_id = ANY (p_document_ids))
         -- "+ 0" keeps the planner off the HNSW index, which only matches the bare distance expression.
         ORDER BY (e.embedding OPERATOR(extensions.<=>) p_query) + 0
         LIMIT v_limit;
    ELSE
        RETURN QUERY
        SELECT m.chunk_id, m.document_id, 1 - m.distance
          FROM (SELECT e.chunk_id, c.document_id, e.embedding OPERATOR(extensions.<=>) p_query AS distance
                  FROM public.chunk_embeddings e
                  JOIN public.chunks c ON c.id = e.chunk_id
                 WHERE e.team_id = ANY (v_teams)
                 ORDER BY e.embedding OPERATOR(extensions.<=>) p_query
                 LIMIT v_limit) m
         ORDER BY m.distance;
    END IF;
END $$;

REVOKE ALL ON FUNCTION smartlab.match_chunks(extensions.vector, integer, integer[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION smartlab.match_chunks(extensions.vector, integer, integer[]) TO smartlab_app;
GRANT USAGE ON SCHEMA extensions TO smartlab_app;

COMMIT;
