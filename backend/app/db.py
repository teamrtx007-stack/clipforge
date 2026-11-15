"""ClipForge's records: SQLite by default, PostgreSQL when DATABASE_URL is set.

SQLite: one connection per thread; WAL lets the worker write while the API reads. This is the default and
behaves exactly as before.
PostgreSQL (for Supabase): a small shared connection pool, tables in the `clipforge` schema. Callers write
SQLite-style SQL with `?` placeholders; this module translates it. The schema is not created on startup:
it comes from supabase/migrations (or create_postgres_schema() for tests and staging), so pointing
DATABASE_URL at a database never changes its structure by surprise.

Run `python -m app.db backup` for a consistent copy of the SQLite database, safe while ClipForge runs.
"""
import json
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DB_PATH, settings

POSTGRES_SCHEMA = "clipforge"
POSTGRES_DDL = Path(__file__).with_name("schema_postgres.sql")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse ids: files and queued jobs refer to them
  name TEXT NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  auto_record INTEGER NOT NULL DEFAULT 1,
  auto_render INTEGER NOT NULL DEFAULT 1,
  chat INTEGER NOT NULL DEFAULT 1,
  speech INTEGER NOT NULL DEFAULT 1,
  sensitivity TEXT NOT NULL DEFAULT 'medium',
  template TEXT NOT NULL,
  buffer_minutes INTEGER NOT NULL DEFAULT 5,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK (kind IN ('upload', 'live')),
  name TEXT NOT NULL,
  ext TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('uploading', 'queued', 'processing', 'ready', 'failed')),
  stage TEXT NOT NULL DEFAULT '',
  progress REAL NOT NULL DEFAULT 0,
  duration REAL,
  width INTEGER,
  height INTEGER,
  fps REAL,
  meta TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  start REAL NOT NULL,
  "end" REAL NOT NULL CHECK ("end" > start),
  peak REAL,
  score REAL NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  hashtags TEXT NOT NULL DEFAULT '[]',
  reasons TEXT NOT NULL DEFAULT '[]',
  template TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft', 'queued', 'rendering', 'ready', 'failed')),
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL,
  rendered_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
-- One row per finished piece of work, for the dashboard chart. It outlives the videos and clips it counts.
CREATE TABLE IF NOT EXISTS activity (
  kind TEXT NOT NULL CHECK (kind IN ('video', 'clip', 'moment')),
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_at ON activity (at);
-- Every stored copy of a file in object storage: the primary copy (R2) and its backup (B2) are separate rows,
-- so a file is always fetched from a provider that recorded it, and cleanup can tell whether a copy is the last.
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,                -- file_id
  user_id TEXT NOT NULL DEFAULT 'local',               -- one shared account today
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('source', 'clip')),
  owner_id INTEGER NOT NULL,
  file_type TEXT NOT NULL,                             -- source_video, transcript, loudness, clip_video, thumbnail
  storage_provider TEXT NOT NULL,                      -- r2 or b2
  object_key TEXT NOT NULL,
  file_size INTEGER NOT NULL DEFAULT 0,
  md5 TEXT,
  upload_status TEXT NOT NULL CHECK (upload_status IN ('pending', 'uploaded', 'failed', 'deleting')),
  backup_status TEXT NOT NULL DEFAULT 'none' CHECK (backup_status IN ('none', 'pending', 'verified', 'failed', 'skipped')),
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  expires_at REAL,
  UNIQUE (storage_provider, object_key)
);
CREATE INDEX IF NOT EXISTS files_owner ON files (owner_kind, owner_id, file_type);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (status, priority DESC, id);
CREATE INDEX IF NOT EXISTS sources_project ON sources (project_id);
CREATE INDEX IF NOT EXISTS clips_source ON clips (source_id);
"""

# Columns holding JSON documents that are always read and written whole.
JSON_COLUMNS = frozenset({"settings", "meta", "hashtags", "reasons"})

_local = threading.local()
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def kind() -> str:
    """Which database is in use: "sqlite" or "postgres". Never the address or password."""
    return "postgres" if settings.database_url else "sqlite"


@dataclass
class Result:
    """What a statement returned, already read: rows as dicts, and how many rows it changed."""
    rows: list[dict[str, Any]] = field(default_factory=list)
    rowcount: int = 0


def _quote(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"not a column name: {name!r}")
    return f'"{name}"'  # "end" is a reserved word in PostgreSQL; quoting works in both databases


# --- SQLite -------------------------------------------------------------------------------------------------

def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def _sqlite_run(sql: str, args: tuple) -> Result:
    cur = conn().execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()] if cur.description else []
    return Result(rows, cur.rowcount)


# --- PostgreSQL ---------------------------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()
# A quoted string or identifier, a -- comment, a ? placeholder, or a % sign.
_PG_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|\?|%")


def to_postgres(sql: str) -> str:
    """SQLite-style `?` placeholders to psycopg's `%s`. A `?` inside quotes or a comment stays as it is;
    every literal `%` is doubled, because psycopg reads `%` as the start of a placeholder."""
    def swap(m: re.Match) -> str:
        token = m.group()
        if token == "?":
            return "%s"
        return token.replace("%", "%%")
    return _PG_TOKENS.sub(swap, sql)


def _postgres_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            def configure(c) -> None:
                c.execute(f"SET search_path TO {POSTGRES_SCHEMA}")

            _pool = ConnectionPool(settings.database_url, min_size=1, max_size=settings.db_pool_size, timeout=30,
                                   kwargs={"autocommit": True, "row_factory": dict_row}, configure=configure,
                                   name="clipforge", open=True)
        return _pool


def close() -> None:
    """Close the PostgreSQL pool (tests, shutdown). SQLite connections close with their threads."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def _postgres_run(sql: str, args: tuple) -> Result:
    query = to_postgres(sql) if args else sql
    # SQLite stores True/False as 1/0 in its integer flag columns; PostgreSQL refuses booleans there.
    params = tuple(int(a) if isinstance(a, bool) else a for a in args) or None

    def run(c) -> Result:
        cur = c.execute(query, params)
        return Result(list(cur.fetchall()) if cur.description else [], cur.rowcount)

    pinned = getattr(_local, "pg", None)  # inside transaction(): every statement uses its connection
    if pinned is not None:
        return run(pinned)
    with _postgres_pool().connection() as c:
        return run(c)


def create_postgres_schema() -> None:
    """Create the clipforge schema and tables if missing. For tests and a staging copy only: on Supabase the
    migration in supabase/migrations does this, applied by hand."""
    with _postgres_pool().connection() as c:
        c.execute(POSTGRES_DDL.read_text())


# --- Both ---------------------------------------------------------------------------------------------------

def _run(sql: str, args: tuple) -> Result:
    return _postgres_run(sql, args) if settings.database_url else _sqlite_run(sql, args)


def init() -> None:
    if settings.database_url:
        found = one("SELECT to_regclass(?) AS t", f"{POSTGRES_SCHEMA}.files")
        if not found or found["t"] is None:
            raise RuntimeError(f"PostgreSQL has no {POSTGRES_SCHEMA} tables yet. Apply the ClipForge migration in "
                               "supabase/migrations first (see docs/DATABASE.md), or remove DATABASE_URL to use SQLite.")
        return
    conn().executescript(SCHEMA)


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    return {k: json.loads(v) if k in JSON_COLUMNS and v is not None else v for k, v in row.items()}


def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
    return [_decode(r) for r in _run(sql, args).rows]


def one(sql: str, *args: Any) -> dict[str, Any] | None:
    found = _run(sql, args).rows
    return _decode(found[0]) if found else None


def execute(sql: str, *args: Any) -> Result:
    return _run(sql, args)


def insert(table: str, **values: Any) -> int:
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    cols = ", ".join(_quote(k) for k in values)
    marks = ", ".join("?" for _ in values)
    sql = f"INSERT INTO {_quote(table)} ({cols}) VALUES ({marks})"
    if settings.database_url:
        return _postgres_run(sql + " RETURNING id", tuple(values.values())).rows[0]["id"]
    return conn().execute(sql, tuple(values.values())).lastrowid


def update(table: str, row_id: int, **values: Any) -> bool:
    """Returns False when the row no longer exists."""
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    sets = ", ".join(f"{_quote(k)} = ?" for k in values)
    return _run(f"UPDATE {_quote(table)} SET {sets} WHERE id = ?", (*values.values(), row_id)).rowcount > 0


@contextmanager
def transaction() -> Iterator[None]:
    if settings.database_url:
        with _postgres_pool().connection() as c, c.transaction():
            _local.pg = c
            try:
                yield
            finally:
                _local.pg = None
        return
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def skip_locked() -> str:
    """Row locking for a job claim, so two workers never take the same job. SQLite has a single writer
    and no such clause."""
    return "FOR UPDATE SKIP LOCKED" if settings.database_url else ""


def on_conflict_ignore() -> str:
    """Skip an insert that would break a unique index (PostgreSQL's queued-job index)."""
    return "ON CONFLICT DO NOTHING" if settings.database_url else ""


def now() -> float:
    return time.time()


def record(kind: str) -> None:
    """Count a video analyzed, a clip rendered or a live moment cut."""
    execute("INSERT INTO activity (kind, at) VALUES (?, ?)", kind, now())


def backup(dest: Path | None = None) -> Path:
    """A consistent copy of the SQLite database (SQLite's online backup), safe while ClipForge runs. The
    original is only read."""
    if settings.database_url:
        raise RuntimeError("Backups here are for SQLite. Supabase databases are backed up with pg_dump.")
    if not DB_PATH.exists():
        raise RuntimeError(f"There is no database at {DB_PATH} yet.")
    dest = dest or DB_PATH.parent / "backups" / time.strftime("clipforge-%Y%m%d-%H%M%S.sqlite3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


if __name__ == "__main__":
    if sys.argv[1:] != ["backup"]:
        print("Usage: python -m app.db backup")
        raise SystemExit(2)
    print(f"Backed up to {backup()}")
