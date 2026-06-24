# Postgres backups & restore

Scheduled logical backups of the shared `agent-postgres-1` (one archive per
agent DB), produced by the `postgres-backup` service in the root
`docker-compose.yml` under the **`backup` profile**
(`prodrigestivill/postgres-backup-local`, pinned to the `:16` major to match
`pgvector/pgvector:pg16`).

## Enable

```bash
# one-off / verify
docker compose --profile backup up -d postgres-backup
docker compose logs -f postgres-backup
```

Knobs (set in the root `.env`; see `env.example`):

| Var                 | Default                                | Meaning                                  |
| ------------------- | -------------------------------------- | ---------------------------------------- |
| `BACKUP_DIR`        | `./infra/backup/dumps`                 | host path the dumps are written to       |
| `BACKUP_DBS`        | all agent DBs (`monitoring`, `dashboard`) | comma-separated DBs to dump           |
| `BACKUP_SCHEDULE`   | `@daily`                               | `@daily`/`@weekly` or a 5-field cron     |
| `BACKUP_KEEP_DAYS`  | `7`                                    | daily dumps retained                     |
| `BACKUP_KEEP_WEEKS` | `4`                                    | weekly dumps retained                    |
| `BACKUP_KEEP_MONTHS`| `6`                                    | monthly dumps retained                   |

In production point `BACKUP_DIR` at an **absolute host path** on a volume with
its own snapshot/offsite story (e.g. `/var/backups/agent-bakery`). Dumps are
gzipped `pg_dump` archives written as `<db>/<db>-<timestamp>.sql.gz`, plus a
`latest` symlink per DB.

> The dumps contain every tenant's data. Treat the backup directory as a secret:
> restrict it to `root`/the postgres role and encrypt at rest / in transit.

## Restore a single DB

`POSTGRES_EXTRA_OPTS=--clean --if-exists` means each dump already drops &
recreates its objects, so a restore is idempotent. Restore into the running
Postgres:

```bash
# pick the archive you want (or use the `latest` symlink)
gunzip -c infra/backup/dumps/monitoring/monitoring-latest.sql.gz \
  | docker exec -i agent-postgres-1 psql -U appuser -d monitoring
```

Restore **all** DBs (e.g. onto a fresh volume):

```bash
for db in monitoring dashboard; do
  gunzip -c "infra/backup/dumps/$db/$db-latest.sql.gz" \
    | docker exec -i agent-postgres-1 psql -U appuser -d "$db"
done
```

If the target DB does not exist yet (fresh `pgdata`), run `infra/bootstrap.sql`
first to create the empty DBs + the `vector` extension, then restore.

## Disaster recovery (fresh host)

1. Bring up just Postgres: `docker compose up -d postgres` (wait for healthy).
2. `docker exec -i agent-postgres-1 psql -U appuser -d postgres < infra/bootstrap.sql`.
3. Copy the dump tree to the new host and run the "restore all DBs" loop above.
4. Start the rest: `docker compose up -d`.

## Related

- **Least-privilege roles + partitioning:** `infra/migrations/README.md` and
  `infra/migrations/0001_least_privilege_and_partitioning.sql` — a **manual,
  breaking** migration off the single-superuser model. Apply it during a
  coordinated maintenance window, ideally right after taking a fresh backup with
  the procedure above.
