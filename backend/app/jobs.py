"""Background job queue in the database (SQLite, or PostgreSQL with row locks). Only this module knows how jobs are
stored and claimed."""
import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from . import db

log = logging.getLogger("clipforge.jobs")

MAX_ATTEMPTS = 2  # one retry after a crash; a file that crashes the process twice is skipped
LIVE_PRIORITY = 10  # live clips jump ahead of uploads so they appear while the stream is still on


@dataclass(frozen=True)
class Handler:
    run: Callable[[int], None]
    fail: Callable[[int, str], None]


HANDLERS: dict[str, Handler] = {}
_wake = threading.Event()


def register(kind: str, run: Callable[[int], None], fail: Callable[[int, str], None]) -> None:
    HANDLERS[kind] = Handler(run, fail)


class Gone(Exception):
    """Raised by a handler whose target was deleted mid-job. The job ends quietly, not as a failure."""


def enqueue(kind: str, target_id: int, priority: int = 0) -> None:
    """Queue work for a target. A job already waiting for the same target absorbs the request.
    One statement, so callers can run it inside their own transaction."""
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind!r}")
    t = db.now()
    db.execute(
        """INSERT INTO jobs (kind, target_id, status, priority, created_at, updated_at)
           SELECT ?, ?, 'queued', ?, ?, ?
           WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE kind = ? AND target_id = ? AND status = 'queued')
           """ + db.on_conflict_ignore(),
        kind, target_id, priority, t, t, kind, target_id,
    )
    _wake.set()


def claim(min_priority: int = 0) -> dict | None:
    # One statement, so two workers can never take the same job. rows() drains the cursor,
    # which ends the statement and commits the claim before the job starts. On PostgreSQL the row lock
    # (SKIP LOCKED) does the same for workers on other machines.
    claimed = db.rows(
        f"""UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
           WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND priority >= ?
                       ORDER BY priority DESC, id LIMIT 1 {db.skip_locked()})
           RETURNING *""",
        db.now(), min_priority,
    )
    return claimed[0] if claimed else None


def _finish(job: dict, error: str | None) -> None:
    db.update("jobs", job["id"], status="failed" if error else "done", error=error, updated_at=db.now())


def _fail(job: dict, error: str) -> None:
    _finish(job, error)
    handler = HANDLERS.get(job["kind"])
    if handler:
        handler.fail(job["target_id"], error)


def recover_stale() -> None:
    """At startup, nothing is running yet, so any 'running' job was cut off by a crash or restart.
    Finished jobs older than a week are deleted, so the queue page stays fast."""
    db.execute("DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?", db.now() - 7 * 86400)
    for job in db.rows("SELECT * FROM jobs WHERE status = 'running'"):
        if job["attempts"] >= MAX_ATTEMPTS:
            _fail(job, "stopped after repeated crashes")
        else:
            db.update("jobs", job["id"], status="queued", updated_at=db.now())


def run_one(min_priority: int = 0) -> bool:
    job = claim(min_priority)
    if job is None:
        return False
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise RuntimeError(f"no handler for job kind {job['kind']!r}")
        handler.run(job["target_id"])
    except Gone:
        _finish(job, None)
    except Exception as exc:  # a failed job must never kill the worker thread
        log.error("job %s (%s #%s) failed:\n%s", job["id"], job["kind"], job["target_id"], traceback.format_exc())
        _fail(job, str(exc) or type(exc).__name__)
    else:
        _finish(job, None)
    return True


def _worker(stop: threading.Event, min_priority: int) -> None:
    while not stop.is_set():
        try:
            if run_one(min_priority):
                continue
        except Exception:  # e.g. the database is briefly locked; retry after the wait
            log.exception("worker loop error")
        _wake.wait(2)
        _wake.clear()


def start(count: int, stop: threading.Event, live_lane: bool) -> list[threading.Thread]:
    """`count` workers take any job. With `live_lane`, one more takes only live jobs, so a live moment
    never waits behind an hour-long upload."""
    lanes = [0] * count + ([LIVE_PRIORITY] if live_lane else [])
    threads = [threading.Thread(target=_worker, args=(stop, p), name=f"worker-{i}", daemon=True)
               for i, p in enumerate(lanes)]
    for t in threads:
        t.start()
    return threads


def wake() -> None:
    _wake.set()
