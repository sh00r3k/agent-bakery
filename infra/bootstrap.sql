-- Bootstrap per-agent databases on the shared agent_backend Postgres
-- (pgvector/pgvector:pg16, superuser: appuser).
--
-- Run on the host:
--   docker exec -i agent-postgres-1 psql -U appuser -d postgres < infra/bootstrap.sql
--
-- Idempotent: CREATE DATABASE is guarded via \gexec, extensions use IF NOT EXISTS.
-- One database per agent keeps app tables isolated and makes per-agent
-- backup/restore trivial.

SELECT 'CREATE DATABASE monitoring OWNER appuser'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'monitoring')\gexec
SELECT 'CREATE DATABASE dashboard OWNER appuser'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dashboard')\gexec

-- pgvector in each agent DB (RAG embeddings / similarity). Connect + enable.
\connect monitoring
CREATE EXTENSION IF NOT EXISTS vector;
\connect dashboard
CREATE EXTENSION IF NOT EXISTS vector;
