"""SQLite storage. One connection per thread; WAL lets the worker write while the API reads."""
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import DB_PATH

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
  end REAL NOT NULL CHECK (end > start),
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
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (status, priority DESC, id);
CREATE INDEX IF NOT EXISTS sources_project ON sources (project_id);
CREATE INDEX IF NOT EXISTS clips_source ON clips (source_id);
"""

# Columns holding JSON documents that are always read and written whole.
JSON_COLUMNS = frozenset({"settings", "meta", "hashtags", "reasons"})

_local = threading.local()


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


def init() -> None:
    conn().executescript(SCHEMA)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    return {k: json.loads(row[k]) if k in JSON_COLUMNS else row[k] for k in row.keys()}


def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
    return [_decode(r) for r in conn().execute(sql, args)]


def one(sql: str, *args: Any) -> dict[str, Any] | None:
    r = conn().execute(sql, args).fetchone()
    return _decode(r) if r else None


def execute(sql: str, *args: Any) -> sqlite3.Cursor:
    return conn().execute(sql, args)


def insert(table: str, **values: Any) -> int:
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())).lastrowid


def update(table: str, row_id: int, **values: Any) -> bool:
    """Returns False when the row no longer exists."""
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    sets = ", ".join(f"{k} = ?" for k in values)
    return conn().execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id)).rowcount > 0


@contextmanager
def transaction() -> Iterator[None]:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def now() -> float:
    return time.time()


def record(kind: str) -> None:
    """Count a video analyzed, a clip rendered or a live moment cut."""
    execute("INSERT INTO activity (kind, at) VALUES (?, ?)", kind, now())
