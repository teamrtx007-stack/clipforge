# Database: SQLite now, PostgreSQL (Supabase) later

ClipForge keeps its records (projects, videos, clips, the job queue, stored-file records, the activity chart) in a database. There are two options, and **SQLite is the default**:

| | SQLite (default) | PostgreSQL / Supabase |
|---|---|---|
| Turned on by | nothing: `DATABASE_URL` is unset | `DATABASE_URL=postgresql://...` in `.env` |
| Where the data is | `clipforge.sqlite3` in the `/data` Docker volume | the `clipforge` schema of the database |
| Tables created by | ClipForge, on start | the migration in `supabase/migrations/*_clipforge_schema.sql`, applied by hand. ClipForge refuses to start on a database without them and never creates them itself |
| Several workers | one machine | several machines: jobs are claimed with a row lock, so none is taken twice |

The code is the same for both: `backend/app/db.py` translates the few differences (placeholders, the reserved word `end`, new row ids, on/off flags, row locks).

## Back up SQLite

A consistent copy, safe while ClipForge is running. The original is only read:

```
docker compose exec clipforge python -m app.db backup
```

It prints where the copy went, `/data/backups/clipforge-<date>-<time>.sqlite3`. To copy the backups out of Docker, to a folder outside OneDrive:

```
docker cp clipforge-clipforge-1:/data/backups "%USERPROFILE%\clipforge-db-backups"
```

If Docker named the container differently, `docker compose ps` shows its name.

## Switching to PostgreSQL (later phases, not now)

1. Back up SQLite (above).
2. Apply the migration to a **test copy** first, never straight to production.
3. Copy the data with the migration tool (phase M4), dry run first, and compare row counts.
4. Set `DATABASE_URL` and restart.

Until then, `DATABASE_URL` stays unset.

## Rollback

- **PostgreSQL misbehaves:** remove `DATABASE_URL` from `.env` and restart (`docker compose up -d --force-recreate`). ClipForge is back on SQLite at once, with all its data: PostgreSQL mode never writes to the SQLite file.
- **A code change misbehaves:** revert its pull request on GitHub, then `git pull` and `docker compose up -d --build`. The SQLite file is not changed by this code, so nothing needs repairing.
- **The SQLite file itself is damaged:** stop ClipForge, copy a backup back into the volume as `/data/clipforge.sqlite3`, and start it again.

## Tests

The whole backend suite runs on SQLite by default. To run it on PostgreSQL, point `CLIPFORGE_TEST_DATABASE_URL` at a **throwaway** database: the tests delete its `clipforge` schema first. A `DATABASE_URL` in the environment is ignored by the tests, so they can never touch a real database.
