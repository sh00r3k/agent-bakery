# infra/migrations

**Manual, breaking** SQL migrations that are deliberately NOT auto-run.

The first-boot bootstrap (`infra/bootstrap.sql`, mounted into
`/docker-entrypoint-initdb.d`) only runs once on an empty `pgdata` volume and is
intentionally additive/idempotent. Anything that changes credentials, ownership,
or rewrites existing tables lives here instead and must be applied by an operator
during a coordinated maintenance window.

## Migrations

### `0001_least_privilege_and_partitioning.sql` — ⚠️ BREAKING

Two changes, both requiring coordination:

1. **Per-agent least-privilege roles.** Today every agent DB is owned by the
   `appuser` **superuser** and every agent connects as it, so one leaked `.env`
   = all tenants in all DBs. The migration creates one
   `NOSUPERUSER NOCREATEDB` login role per agent DB (`monitoring_app`,
   `dashboard_app`, …) that owns only its own DB, reassigns
   object ownership, and revokes the `PUBLIC` schema grant. **After applying you
   must update each agent's `.env`** (`POSTGRES_USER=<agent>_app`,
   `POSTGRES_PASSWORD=<per-agent secret>`) and restart the agents.

2. **Monthly RANGE partitioning** of the high-churn append-only tables
   (`heartbeats`, `cost_events`, `cost_model_events`) on their `ts` column. This
   **rewrites the tables** — take an agent offline for the table it owns. A
   `pg_partman` alternative is documented inline at the bottom of the file.

**Before applying:** take a fresh backup (see `../backup/README.md`), stop the
writers, and read the `HOW TO APPLY` header in the SQL file — it takes per-agent
passwords as `psql -v` variables (never hard-coded). **Rollback** is restore
from backup; the partition conversion is not trivially reversible in place.

Extend the role list and the partition table array for any additional agent DBs
you run (`security`, `web_ext_pipeline`, `pm`, `ultraqa`).
