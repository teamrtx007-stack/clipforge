"""The database layer on both SQLite (default) and PostgreSQL (CLIPFORGE_TEST_DATABASE_URL). Every test here runs
on whichever database the suite uses; PostgreSQL-only and SQLite-only checks say so."""
import ast
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, files, jobs, main, pipeline  # noqa: F401  (pipeline registers the job kinds)
from app.config import settings

POSTGRES = bool(settings.database_url)
APP = Path(__file__).resolve().parents[1] / "app"
MIGRATION = next((APP.parents[1] / "supabase" / "migrations").glob("*_clipforge_schema.sql"))


@pytest.fixture
def clean():
    for table in ("jobs", "activity", "files", "clips", "sources", "channels", "projects"):
        db.execute(f"DELETE FROM {table}")
    yield
    for table in ("jobs", "activity", "files"):
        db.execute(f"DELETE FROM {table}")


def test_tests_never_use_a_database_from_the_environment():
    assert settings.database_url == (os.environ.get("CLIPFORGE_TEST_DATABASE_URL") or None)
    assert db.kind() == ("postgres" if POSTGRES else "sqlite")


class TestPlaceholders:
    def test_question_marks_become_psycopg_placeholders(self):
        assert db.to_postgres("SELECT * FROM t WHERE a = ? AND b IN (?, ?)") == \
            "SELECT * FROM t WHERE a = %s AND b IN (%s, %s)"

    def test_quoted_text_comments_and_percent_signs_are_left_alone(self):
        assert db.to_postgres("SELECT 'why?' AS q, \"odd?col\" FROM t WHERE a = ? -- really?\n") == \
            "SELECT 'why?' AS q, \"odd?col\" FROM t WHERE a = %s -- really?\n"
        assert db.to_postgres("SELECT '50%' WHERE name LIKE ? AND x = 'it''s ?'") == \
            "SELECT '50%%' WHERE name LIKE %s AND x = 'it''s ?'"

    def test_column_names_are_checked(self):
        with pytest.raises(ValueError):
            db.update("clips", 1, **{"title = 'x'; --": "y"})


class TestSameResultsOnBothDatabases:
    def test_insert_update_and_read_back(self, clean):
        pid = db.insert("projects", name="Trip", settings={"length": "short", "nested": {"ok": True}}, created_at=1.5)
        ch = db.insert("channels", project_id=pid, platform="twitch", url="https://twitch.tv/x", name="x",
                       template="viral", enabled=True, chat=False, created_at=2.0)
        sid = db.insert("sources", project_id=pid, channel_id=ch, kind="upload", name="run", ext=".mp4",
                        status="ready", meta={"peak": 12.5}, created_at=3.0)
        cid = db.insert("clips", source_id=sid, start=1.0, end=26.5, score=71, title="Héllo ‘you’ — 50% ?",
                        hashtags=["#a", "#ब"], reasons=[], template="viral", status="draft", created_at=4.0)
        clip = db.one("SELECT * FROM clips WHERE id = ?", cid)
        assert (clip["start"], clip["end"], clip["score"]) == (1.0, 26.5, 71.0)
        assert clip["title"] == "Héllo ‘you’ — 50% ?" and clip["hashtags"] == ["#a", "#ब"] and clip["peak"] is None
        assert db.one("SELECT settings FROM projects WHERE id = ?", pid)["settings"] == {"length": "short", "nested": {"ok": True}}
        flags = db.one("SELECT enabled, chat, auto_record FROM channels WHERE id = ?", ch)
        assert flags == {"enabled": 1, "chat": 0, "auto_record": 1}, "on/off flags read back as 1 and 0 on both"

        assert db.update("clips", cid, end=30.0, status="ready", hashtags=["#b"])
        assert db.one('SELECT "end", status, hashtags FROM clips WHERE id = ?', cid) == \
            {"end": 30.0, "status": "ready", "hashtags": ["#b"]}
        assert db.update("clips", 999999, title="gone") is False
        assert db.execute("UPDATE clips SET progress = ? WHERE source_id = ?", 0.5, sid).rowcount == 1
        assert db.rows("SELECT id FROM clips WHERE source_id IN (?, ?) ORDER BY id", sid, -1) == [{"id": cid}]

        db.execute("DELETE FROM projects WHERE id = ?", pid)  # cascades, as before
        assert db.one("SELECT COUNT(*) AS n FROM clips")["n"] == 0
        assert db.one("SELECT channel_id FROM sources WHERE id = ?", sid) is None

    def test_a_failed_transaction_changes_nothing(self, clean):
        pid = db.insert("projects", name="keep", settings={}, created_at=1.0)
        with pytest.raises(RuntimeError), db.transaction():
            db.update("projects", pid, name="changed")
            db.insert("projects", name="new", settings={}, created_at=2.0)
            raise RuntimeError("boom")
        assert db.rows("SELECT name FROM projects") == [{"name": "keep"}]
        with db.transaction():
            db.update("projects", pid, name="changed")
        assert db.one("SELECT name FROM projects WHERE id = ?", pid)["name"] == "changed"

    def test_storage_usage_is_a_whole_number_and_the_free_limit_still_stops(self, clean):
        assert files.usage("r2") == 0 and isinstance(files.usage("r2"), int)
        for i in range(3):
            db.execute("""INSERT INTO files (owner_kind, owner_id, file_type, storage_provider, object_key, file_size,
                          upload_status, created_at, updated_at) VALUES ('clip', ?, 'clip_video', 'r2', ?, ?, 'uploaded', 1, 1)""",
                       i, f"clips/{i}/clip.mp4", 4_000_000_000)
        used = files.usage("r2")
        assert used == 12_000_000_000 and isinstance(used, int)
        assert used > 0.8 * 10 * 1000**3  # the comparisons quota() and _check_room() make

    def test_the_dashboard_counts_days_in_the_viewers_time_zone(self, clean):
        now = db.now()
        local = datetime.fromtimestamp(now + 19800, timezone.utc)  # India, UTC+5:30
        midnight = now - (local.hour * 3600 + local.minute * 60 + local.second) - 1  # 1 s before today, India time
        for at, kind in [(now, "clip"), (now - 60, "clip"), (midnight, "video"), (midnight - 86400, "moment")]:
            db.execute("INSERT INTO activity (kind, at) VALUES (?, ?)", kind, at)
        with TestClient(main.create_app(background=False), base_url="http://localhost") as api:
            days = api.get("/api/stats?days=3&tz_offset=-330").json()["days"]
        assert [(d["clips"], d["videos"], d["moments"]) for d in days] == [(0, 0, 1), (0, 1, 0), (2, 0, 0)]


class TestJobQueue:
    def test_no_job_is_ever_claimed_twice(self, clean):
        for target in range(200):
            jobs.enqueue("render_clip", target)
        claimed: list[int] = []
        lock = threading.Lock()

        def worker():
            while (job := jobs.claim()) is not None:
                with lock:
                    claimed.append(job["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(claimed) == 200 and len(set(claimed)) == 200
        assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'")["n"] == 200

    def test_the_same_work_queued_at_once_is_queued_once(self, clean):
        errors: list[Exception] = []
        for target in range(40):  # the race is rare, so try it many times
            barrier = threading.Barrier(8)

            def worker(target=target, barrier=barrier):
                barrier.wait()
                try:
                    jobs.enqueue("render_clip", target)
                except Exception as exc:  # e.g. a unique-index violation that was not absorbed
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert errors == []
        counts = db.rows("SELECT target_id, COUNT(*) AS n FROM jobs WHERE kind = 'render_clip' GROUP BY target_id")
        assert len(counts) == 40 and all(r["n"] == 1 for r in counts)


class TestSchema:
    def test_no_sql_uses_the_reserved_word_end_unquoted(self):
        """`end` is reserved in PostgreSQL: the clips.end column must always be written "end"."""
        found = []
        for path in sorted(APP.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and re.search(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", node.value) \
                        and re.search(r'(?<!")\bend\b(?!")', node.value.replace("ELSE 'draft' END", "")):
                    found.append(f"{path.name}:{node.lineno}")
        assert found == []

    def test_the_supabase_migration_contains_the_schema_exactly(self):
        text = MIGRATION.read_text()
        body = text.split("-- BEGIN schema_postgres.sql\n", 1)[1].split("-- END schema_postgres.sql", 1)[0]
        assert body == (APP / "schema_postgres.sql").read_text()
        assert text.count("enable row level security") == 7

    @pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
    def test_postgres_has_the_same_tables_and_columns_as_sqlite(self, tmp_path):
        lite = sqlite3.connect(tmp_path / "x.sqlite3")
        lite.executescript(db.SCHEMA)
        expected = {t: [c[1] for c in lite.execute(f"PRAGMA table_info({t})")]
                    for (t,) in lite.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        actual: dict[str, list[str]] = {}
        for r in db.rows("""SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = ?
                            ORDER BY table_name, ordinal_position""", db.POSTGRES_SCHEMA):
            actual.setdefault(r["table_name"], []).append(r["column_name"])
        assert actual == expected

    @pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
    def test_the_migration_applies_cleanly_twice_and_locks_the_tables(self):
        with db._postgres_pool().connection() as c:
            c.execute(MIGRATION.read_text())
            c.execute(MIGRATION.read_text())  # safe to run again
        locked = db.rows("""SELECT relname FROM pg_class JOIN pg_namespace n ON n.oid = relnamespace
                            WHERE nspname = ? AND relkind = 'r' AND relrowsecurity""", db.POSTGRES_SCHEMA)
        assert len(locked) == 7

    @pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
    def test_a_database_without_the_tables_is_refused_not_changed(self, monkeypatch):
        monkeypatch.setattr(db, "POSTGRES_SCHEMA", "clipforge_missing")
        with pytest.raises(RuntimeError, match="no clipforge_missing tables yet"):
            db.init()
        assert db.one("SELECT to_regnamespace(?) AS s", "clipforge_missing")["s"] is None, "nothing was created"


# A SQLite database exactly as the code before this change creates it: the only difference is `end` unquoted.
OLD_SCHEMA = db.SCHEMA.replace('"end" REAL NOT NULL CHECK ("end" > start)', "end REAL NOT NULL CHECK (end > start)")

_EXERCISE = """
import sys
from fastapi.testclient import TestClient
from app import db, main
with TestClient(main.create_app(background=False), base_url="http://localhost") as api:
    for path in ("/api/projects", "/api/projects/1", "/api/clips?limit=5", "/api/clips/1", "/api/jobs",
                 "/api/system", "/api/stats?days=7", "/api/channels"):
        assert api.get(path).status_code == 200, path
print(db.backup())
"""


@pytest.mark.skipif(POSTGRES, reason="SQLite only")
class TestExistingSqliteDatabase:
    def test_an_existing_database_is_unchanged_by_the_new_code(self, tmp_path):
        assert OLD_SCHEMA != db.SCHEMA
        data = tmp_path / "data"
        data.mkdir()
        path = data / "clipforge.sqlite3"
        old = sqlite3.connect(path)
        old.executescript(OLD_SCHEMA)
        old.executescript("""
            INSERT INTO projects (name, settings, created_at) VALUES ('Trip', '{"length": "short"}', 1700000000.5);
            INSERT INTO sources (project_id, kind, name, ext, status, meta, created_at)
              VALUES (1, 'upload', 'run', '.mp4', 'ready', '{}', 1700000001);
            INSERT INTO clips (source_id, start, end, score, title, template, status, created_at, rendered_at)
              VALUES (1, 1.5, 26.5, 70, 'Clip ? — 50%', 'viral', 'ready', 1700000002, 1700000003);
            INSERT INTO activity (kind, at) VALUES ('clip', 1700000003);
        """)
        old.commit()
        old.close()
        dump_before = list(sqlite3.connect(path).iterdump())

        env = {**os.environ, "DATA_DIR": str(data), "LIVE_ENABLED": "0"}
        env.pop("DATABASE_URL", None)
        out = subprocess.run([sys.executable, "-c", _EXERCISE], cwd=APP.parent, env=env, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-2000:]

        # The file's bytes may differ only because SQLite switches it to WAL mode on open, as the old code did.
        assert list(sqlite3.connect(path).iterdump()) == dump_before, "every table and row is unchanged"
        copy = Path(out.stdout.strip().splitlines()[-1])
        assert copy.parent == data / "backups" and list(sqlite3.connect(copy).iterdump()) == dump_before

    def test_backup_refuses_to_overwrite_and_needs_a_database(self, tmp_path, monkeypatch):
        dest = db.backup(tmp_path / "b.sqlite3")
        with pytest.raises(FileExistsError):
            db.backup(dest)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing.sqlite3")
        with pytest.raises(RuntimeError, match="no database"):
            db.backup(tmp_path / "c.sqlite3")
