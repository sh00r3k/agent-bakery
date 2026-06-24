-- =============================================================================
-- 0001 — least-privilege per-agent roles + time-range partitioning
--
--   ⚠️  APPLY MANUALLY — BREAKING, requires a coordinated migration window. ⚠️
--
-- This migration is NOT auto-run. It is deliberately kept out of
-- /docker-entrypoint-initdb.d (that path only runs `infra/bootstrap.sql`, and
-- ONLY on a first-boot empty data dir). Running this changes connection
-- credentials and rewrites large tables, so it must be coordinated with a
-- rollout of new per-agent .env values and a maintenance window.
--
-- WHAT IT FIXES
--   1. Today every agent DB is OWNED by the `appuser` SUPERUSER and every agent
--      connects AS that superuser. One leaked .env = full control of EVERY
--      tenant's data across EVERY DB. We create one NON-superuser, NON-createdb
--      login role per agent DB that owns (and can touch) ONLY its own DB.
--   2. The high-churn append-only tables (heartbeats, cost_events,
--      cost_model_events) grow unbounded. We convert them to monthly RANGE
--      partitions on their timestamp column so old data can be dropped by
--      detaching a partition (instant) instead of a giant DELETE + VACUUM.
--
-- ─────────────────────────────────────────────────────────────────────────────
-- HOW TO APPLY (per DB; repeat for each agent DB you run)
--
--   0. TAKE A BACKUP FIRST (see infra/backup/README.md) and stop the agent so
--      no writer holds the table while it is rewritten.
--   1. Set the per-agent passwords as psql variables and run this file against
--      the postgres maintenance DB AS the `appuser` superuser:
--
--        docker exec -i agent-postgres-1 \
--          psql -U appuser -d postgres \
--               -v ON_ERROR_STOP=1 \
--               -v monitoring_pw="$MONITORING_DB_PASSWORD" \
--               -v dashboard_pw="$DASHBOARD_DB_PASSWORD" \
--               < infra/migrations/0001_least_privilege_and_partitioning.sql
--
--   2. Update each agent's .env: POSTGRES_USER=<agent>_app and
--      POSTGRES_PASSWORD=<the per-agent password>. Roll out and restart agents.
--   3. Verify each agent connects and writes. Then, and only then, consider
--      tightening `appuser` (out of scope here — keep it as the migration/owner
--      escape hatch).
--
-- ROLLBACK: restore from the pre-migration backup. The partition conversion is a
-- table rewrite and is NOT trivially reversible in place.
--
-- NOTE: this file is a TEMPLATE for the two shipped agents (monitoring,
-- dashboard). Extend the role list / partition blocks for any other agent DBs
-- you run.
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- PART A — per-agent least-privilege login roles
--
-- Each role: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE. It becomes the owner
-- of its DB and of the public schema therein, so it can DDL its own tables but
-- cannot reach any other DB's objects. Passwords come from psql vars (see
-- header); they are NEVER hard-coded here.
-- -----------------------------------------------------------------------------

-- monitoring -----------------------------------------------------------------
SELECT format('CREATE ROLE monitoring_app LOGIN PASSWORD %L '
              'NOSUPERUSER NOCREATEDB NOCREATEROLE', :'monitoring_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'monitoring_app')\gexec
ALTER DATABASE monitoring OWNER TO monitoring_app;

-- dashboard ------------------------------------------------------------------
SELECT format('CREATE ROLE dashboard_app LOGIN PASSWORD %L '
              'NOSUPERUSER NOCREATEDB NOCREATEROLE', :'dashboard_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_app')\gexec
ALTER DATABASE dashboard OWNER TO dashboard_app;

-- Reassign in-DB object ownership + schema rights. Run the same block in each
-- agent DB so the new role owns the existing tables/indexes/extension objects.
\connect monitoring
REASSIGN OWNED BY appuser TO monitoring_app;
ALTER SCHEMA public OWNER TO monitoring_app;
GRANT ALL ON SCHEMA public TO monitoring_app;
-- Lock down PUBLIC: no implicit cross-role access on the public schema.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

\connect dashboard
REASSIGN OWNED BY appuser TO dashboard_app;
ALTER SCHEMA public OWNER TO dashboard_app;
GRANT ALL ON SCHEMA public TO dashboard_app;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- PART B — monthly RANGE partitioning for high-churn time-series tables
--
-- Tables: heartbeats, cost_events, cost_model_events (all in the `monitoring`
-- DB today). They are append-only and queried by a recent time window, so RANGE
-- partitioning by their `ts` (TIMESTAMPTZ) column lets us:
--   * keep indexes/working set small (partition pruning on the time predicate),
--   * expire history by DETACH + DROP of an old partition (no bulk DELETE).
--
-- Strategy: rename the existing table aside, create the partitioned parent with
-- the same columns, create partitions covering existing + near-future data, copy
-- rows, then drop the old table. THIS REWRITES THE TABLE — do it offline.
--
-- The column list below is the documented assumption; reconcile against the live
-- \d before running. If your churn is high enough to want automatic partition
-- creation, prefer pg_partman instead (see the pg_partman note at the bottom).
-- -----------------------------------------------------------------------------

\connect monitoring

-- Helper: convert <tbl> (with TIMESTAMPTZ column <ts_col>) to a RANGE-partitioned
-- table with one partition per calendar month spanning the existing data plus
-- the next month, and a catch-all DEFAULT partition.
DO $mig$
DECLARE
    tbl        TEXT;
    ts_col     TEXT;
    tbls       TEXT[][] := ARRAY[
                   ARRAY['heartbeats',        'ts'],
                   ARRAY['cost_events',       'ts'],
                   ARRAY['cost_model_events', 'ts']
               ];
    i          INT;
    min_ts     TIMESTAMPTZ;
    max_ts     TIMESTAMPTZ;
    cur        DATE;
    last       DATE;
    part_name  TEXT;
BEGIN
    FOR i IN 1 .. array_length(tbls, 1) LOOP
        tbl    := tbls[i][1];
        ts_col := tbls[i][2];

        -- Skip if the table is absent (agent may not have created it yet) or is
        -- already partitioned (idempotent re-run).
        IF NOT EXISTS (
            SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = tbl AND c.relkind = 'r'
        ) THEN
            RAISE NOTICE 'skip %, not a plain table (absent or already partitioned)', tbl;
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE public.%I RENAME TO %I', tbl, tbl || '_legacy');

        -- New partitioned parent cloned from the legacy table's structure.
        EXECUTE format(
            'CREATE TABLE public.%I (LIKE public.%I INCLUDING DEFAULTS '
            'INCLUDING CONSTRAINTS INCLUDING INDEXES) PARTITION BY RANGE (%I)',
            tbl, tbl || '_legacy', ts_col);

        -- Span of existing data; default to current month if the table is empty.
        EXECUTE format('SELECT min(%I), max(%I) FROM public.%I',
                       ts_col, ts_col, tbl || '_legacy')
            INTO min_ts, max_ts;
        cur  := date_trunc('month', COALESCE(min_ts, now()))::date;
        last := date_trunc('month', COALESCE(max_ts, now()))::date
                + INTERVAL '1 month';

        WHILE cur <= last LOOP
            part_name := format('%s_%s', tbl, to_char(cur, 'YYYYMM'));
            EXECUTE format(
                'CREATE TABLE public.%I PARTITION OF public.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                part_name, tbl, cur, (cur + INTERVAL '1 month')::date);
            cur := (cur + INTERVAL '1 month')::date;
        END LOOP;

        -- Catch-all so an out-of-range insert never fails the writer.
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.%I DEFAULT',
            tbl || '_default', tbl);

        -- Copy the data into the partitioned parent, then drop the legacy table.
        EXECUTE format('INSERT INTO public.%I SELECT * FROM public.%I',
                       tbl, tbl || '_legacy');
        EXECUTE format('DROP TABLE public.%I', tbl || '_legacy');

        EXECUTE format('ALTER TABLE public.%I OWNER TO monitoring_app', tbl);
        RAISE NOTICE 'partitioned % by % (months % .. %)', tbl, ts_col,
                     to_char(date_trunc('month', COALESCE(min_ts, now())), 'YYYYMM'),
                     to_char(last - INTERVAL '1 month', 'YYYYMM');
    END LOOP;
END
$mig$;

-- -----------------------------------------------------------------------------
-- ONGOING MAINTENANCE
--   * Create next month's partition before it is needed (cron, ~25th):
--       CREATE TABLE public.heartbeats_YYYYMM PARTITION OF public.heartbeats
--         FOR VALUES FROM ('YYYY-MM-01') TO ('YYYY-(MM+1)-01');
--   * Expire old data instantly:
--       ALTER TABLE public.heartbeats DETACH PARTITION public.heartbeats_YYYYMM;
--       DROP TABLE public.heartbeats_YYYYMM;
--
-- pg_partman alternative (recommended if you don't want a monthly cron):
--   CREATE EXTENSION pg_partman;  -- requires the extension installed in the image
--   SELECT partman.create_parent(
--     p_parent_table => 'public.heartbeats',
--     p_control      => 'ts',
--     p_type         => 'range',
--     p_interval     => '1 month');
--   UPDATE partman.part_config SET retention = '6 months',
--          retention_keep_table = false WHERE parent_table = 'public.heartbeats';
--   -- then schedule SELECT partman.run_maintenance(); via pg_cron or an external cron.
-- The stock pgvector/pgvector:pg16 image does NOT ship pg_partman; switch to an
-- image that bundles it (or build it) before taking this path.
-- =============================================================================
