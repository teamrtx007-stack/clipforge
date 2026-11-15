"""Where each stored file lives, and how long it stays. One `files` row per copy: the primary copy (R2) and its
backup (B2) are separate rows, so every read goes to a provider that recorded the file, and cleanup can always
tell whether a copy is the last one.

Lifecycle, when a primary bucket is configured:
- A source video and its work files (transcript, loudness curve) go to primary storage when processing starts.
  They are temporary and never backed up. SOURCE_KEEP_HOURS after processing ends, all their copies and the
  local working copy are deleted, never while a job is using them.
- A clip video and its thumbnail go to primary storage after each render, then get a verified backup copy.
  The primary copy goes after CLIP_KEEP_DAYS only if a verified backup exists. The backup copy goes after
  BACKUP_KEEP_DAYS only while the primary copy still exists. A last copy goes only when the user deletes the clip.
- The local copy of a clip is dropped once it is safely stored.
"""
import logging
import shutil
import uuid
from pathlib import Path

from . import db, jobs, storage
from .config import clip_dir, settings, source_dir
from .storage import StorageError, StorageFull

log = logging.getLogger("clipforge.files")

# file_type: (owner kind, content type, backed up)
FILES = {
    "source_video": ("source", "video/mp4", False),
    "transcript": ("source", "application/json", False),
    "loudness": ("source", "application/octet-stream", False),
    "clip_video": ("clip", "video/mp4", True),
    "thumbnail": ("clip", "image/jpeg", True),
}
RETRY_AFTER = 900  # seconds before a failed upload or backup is tried again


def enabled() -> bool:
    return storage.primary() is not None


def _key(file_type: str, owner_id: int, name: str) -> str:
    return f"{FILES[file_type][0]}s/{owner_id}/{name}"


def _local(row: dict) -> Path:
    folder = source_dir(row["owner_id"]) if row["owner_kind"] == "source" else clip_dir(row["owner_id"])
    return folder / row["object_key"].rsplit("/", 1)[-1]


def _copies(file_type: str, owner_id: int, statuses: tuple[str, ...] = ("uploaded",)) -> list[dict]:
    """Copies of one file, primary first."""
    rows = db.rows(f"""SELECT * FROM files WHERE owner_kind = ? AND owner_id = ? AND file_type = ?
                       AND upload_status IN ({','.join('?' * len(statuses))})""",
                   FILES[file_type][0], owner_id, file_type, *statuses)
    return sorted(rows, key=lambda r: r["storage_provider"] != settings.storage_primary)


def _store(name: str) -> storage.ObjectStorage | None:
    return storage.provider(name) if name in settings.buckets else None


def _upsert(store: storage.ObjectStorage, file_type: str, owner_id: int, key: str, **values) -> dict:
    now = db.now()
    db.execute(
        """INSERT INTO files (owner_kind, owner_id, file_type, storage_provider, object_key, upload_status,
                              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT (storage_provider, object_key) DO NOTHING""",
        FILES[file_type][0], owner_id, file_type, store.name, key, now, now)
    row = db.one("SELECT * FROM files WHERE storage_provider = ? AND object_key = ?", store.name, key)
    if values:
        db.update("files", row["id"], updated_at=now, **values)
        row = {**row, **values}
    return row


def usage(name: str) -> int:
    """Bytes ClipForge keeps in one provider, by its own records."""
    # CAST: PostgreSQL's SUM of whole numbers is a decimal, which cannot be compared with the float limit.
    return db.one("""SELECT CAST(COALESCE(SUM(file_size), 0) AS BIGINT) AS n FROM files
                     WHERE storage_provider = ? AND upload_status IN ('uploaded', 'deleting')""", name)["n"]


def quota() -> dict[str, dict]:
    out = {}
    for name, bucket in settings.buckets.items():
        used = usage(name)
        out[name] = {"used_bytes": used, "free_bytes": bucket.free_bytes, "percent": round(100 * used / bucket.free_bytes, 1),
                     "warning": used >= 0.8 * bucket.free_bytes, "full": used >= bucket.free_bytes,
                     "role": "primary" if name == settings.storage_primary else "backup" if name == settings.storage_backup else "unused"}
    return out


def _check_room(store: storage.ObjectStorage, extra: int) -> None:
    limit = store.config.free_bytes
    used = usage(store.name)
    if used + extra > 0.8 * limit:
        log.warning("%s is at %.0f%% of its free storage", store.name, 100 * (used + extra) / limit)
    if settings.storage_stop_at_limit and used + extra > limit:
        raise StorageFull(f"{store.name}: this would pass the free {limit / 1000**3:.0f} GB "
                          f"({used / 1000**3:.2f} GB used). Delete clips or raise {store.name.upper()}_FREE_GB")


def save(file_type: str, owner_id: int, path: Path, expires_at: float | None = None) -> dict | None:
    """Put a finished local file in primary storage and record it. Uploading the same content again does
    nothing. If the provider is down or full, the row is marked failed, the local file is kept, and cleanup()
    tries again later. Returns the row, or None when no primary bucket is configured."""
    store = storage.primary()
    if store is None or not path.exists():
        return None
    key, md5 = _key(file_type, owner_id, path.name), storage.md5_of(path)
    size = path.stat().st_size
    row = _upsert(store, file_type, owner_id, key)
    if row["upload_status"] == "uploaded" and row["md5"] == md5:
        return _upsert(store, file_type, owner_id, key, expires_at=expires_at)
    try:
        _check_room(store, size - (row["file_size"] if row["upload_status"] == "uploaded" else 0))
        store.put(key, path, FILES[file_type][1], md5)
        if not store.verify(key, size, md5):
            raise StorageError(f"{store.name}: the upload could not be verified")
    except StorageError as exc:
        log.warning("Could not store %s: %s", key, exc)
        return _upsert(store, file_type, owner_id, key, upload_status="failed", error=str(exc))
    backed = FILES[file_type][2] and storage.backup() is not None
    row = _upsert(store, file_type, owner_id, key, upload_status="uploaded", file_size=size, md5=md5,
                  expires_at=expires_at, error=None, backup_status="pending" if backed else "none")
    if backed:
        jobs.enqueue("backup_file", row["id"])
    return row


def fetch(file_type: str, owner_id: int, dest: Path) -> bool:
    """Make sure a local working copy exists: download it from the first stored copy, primary first."""
    if dest.exists():
        return True
    for row in _copies(file_type, owner_id):
        if (store := _store(row["storage_provider"])) is None:
            continue
        try:
            store.get(row["object_key"], dest)
            return True
        except StorageError as exc:
            log.warning("Could not fetch %s from %s: %s", row["object_key"], store.name, exc)
    return dest.exists()


def url(file_type: str, owner_id: int, filename: str | None = None) -> str | None:
    """A signed URL to the primary copy, the download location. None if there is no primary copy."""
    primary = storage.primary()
    for row in _copies(file_type, owner_id):
        if primary and row["storage_provider"] == primary.name:
            return primary.url(row["object_key"], filename)
    return None


def state(owner_kind: str, owner_id: int) -> dict | None:
    """Where a clip's or source's main file is: primary, backup only, or None if not stored at all."""
    file_type = "clip_video" if owner_kind == "clip" else "source_video"
    copies = _copies(file_type, owner_id)
    if not copies:
        return None
    first = copies[0]
    in_primary = first["storage_provider"] == settings.storage_primary
    return {"location": "primary" if in_primary else "backup", "provider": first["storage_provider"],
            "backup": first["backup_status"] if in_primary else "verified"}


def _temp_copy(row: dict) -> tuple[Path, bool]:
    """The local working copy, or a fresh download of the stored copy. True if it is a temporary file."""
    local = _local(row)
    if local.exists():
        return local, False
    tmp = settings.data_dir / "tmp" / f"{uuid.uuid4().hex}-{local.name}"
    _store(row["storage_provider"]).get(row["object_key"], tmp)
    return tmp, True


def backup_file(file_id: int) -> None:
    """Copy a primary file to the backup bucket, then verify size and checksum before calling it backed up.
    The same key in the same bucket: running this twice never makes a second copy."""
    row = db.one("SELECT * FROM files WHERE id = ?", file_id)
    target = storage.backup()
    if row is None or row["upload_status"] != "uploaded":
        return
    if target is None:
        db.update("files", file_id, backup_status="none", updated_at=db.now())
        return
    try:
        _check_room(target, row["file_size"])
    except StorageFull as exc:
        db.update("files", file_id, backup_status="skipped", error=str(exc), updated_at=db.now())
        return
    copy = _upsert(target, row["file_type"], row["owner_id"], row["object_key"])
    path, temporary = _temp_copy(row)
    try:
        target.put(row["object_key"], path, FILES[row["file_type"]][1], row["md5"])  # Content-MD5: corrupted bytes are refused
        if not target.verify(row["object_key"], row["file_size"], row["md5"]):
            raise StorageError(f"{target.name}: the backup could not be verified")
    except StorageError as exc:
        _upsert(target, row["file_type"], row["owner_id"], row["object_key"], upload_status="failed", error=str(exc))
        raise
    finally:
        if temporary:
            path.unlink(missing_ok=True)
    _upsert(target, row["file_type"], row["owner_id"], row["object_key"], upload_status="uploaded",
            file_size=row["file_size"], md5=row["md5"], error=None)
    db.update("files", file_id, backup_status="verified", error=None, updated_at=db.now())
    log.info("Backed up %s to %s (file %s)", row["object_key"], target.name, copy["id"])


def _backup_failed(file_id: int, error: str) -> None:
    db.update("files", file_id, backup_status="failed", error=error[:300], updated_at=db.now())


def restore_file(file_id: int) -> None:
    """Copy a backup file back into primary storage and verify it. Primary stays the download location."""
    row = db.one("SELECT * FROM files WHERE id = ?", file_id)
    target = storage.primary()
    if row is None or row["upload_status"] != "uploaded" or target is None or row["storage_provider"] == target.name:
        return
    _check_room(target, row["file_size"])
    path, temporary = _temp_copy(row)
    try:
        target.put(row["object_key"], path, FILES[row["file_type"]][1], row["md5"])
        if not target.verify(row["object_key"], row["file_size"], row["md5"]):
            raise StorageError(f"{target.name}: the restored copy could not be verified")
    finally:
        if temporary:
            path.unlink(missing_ok=True)
    keep = settings.clip_keep_days * 86400 if row["owner_kind"] == "clip" and settings.clip_keep_days > 0 else None
    _upsert(target, row["file_type"], row["owner_id"], row["object_key"], upload_status="uploaded",
            file_size=row["file_size"], md5=row["md5"], backup_status="verified", error=None,
            expires_at=db.now() + keep if keep else None)


def restore(owner_kind: str, owner_id: int) -> int:
    """Queue a restore for each of a clip's files that only exists as a backup. Returns how many."""
    queued = 0
    for file_type, (kind, _, _) in FILES.items():
        copies = _copies(file_type, owner_id)
        if kind == owner_kind and copies and copies[0]["storage_provider"] != settings.storage_primary:
            jobs.enqueue("restore_file", copies[0]["id"])
            queued += 1
    return queued


def _delete_copy(row: dict) -> bool:
    """Delete one copy and its row. If the provider is unreachable, the row stays 'deleting' for cleanup()."""
    db.update("files", row["id"], upload_status="deleting", updated_at=db.now())
    store = _store(row["storage_provider"])
    try:
        if store is None:
            raise StorageError(f"{row['storage_provider']} is no longer configured")
        store.delete(row["object_key"])
    except StorageError as exc:
        log.warning("Could not delete %s: %s; will retry", row["object_key"], exc)
        return False
    db.execute("DELETE FROM files WHERE id = ?", row["id"])
    return True


def delete_all(owner_kind: str, owner_id: int) -> None:
    """The user deleted a clip or video: every stored copy goes, last copies included."""
    for row in db.rows("SELECT * FROM files WHERE owner_kind = ? AND owner_id = ?", owner_kind, owner_id):
        _delete_copy(row)


def _source_busy(source_id: int) -> bool:
    return db.one("""SELECT 1 FROM sources s WHERE s.id = ? AND (s.status IN ('uploading', 'queued', 'processing')
                       OR EXISTS (SELECT 1 FROM clips c WHERE c.source_id = s.id AND c.status IN ('queued', 'rendering')))""",
                  source_id) is not None


def _clip_busy(clip_id: int) -> bool:
    return db.one("SELECT 1 FROM clips WHERE id = ? AND status IN ('queued', 'rendering')", clip_id) is not None


def cleanup() -> None:
    """One pass of retries and retention. Runs every few minutes; every step is safe to repeat."""
    now, primary, backup = db.now(), settings.storage_primary, settings.storage_backup
    stale = now - RETRY_AFTER
    for row in db.rows("SELECT * FROM files WHERE upload_status = 'deleting' AND updated_at < ?", stale):
        _delete_copy(row)

    # Uploads that failed while the provider was down or full: try again from the local working copy.
    for row in db.rows("""SELECT * FROM files WHERE storage_provider = ? AND upload_status IN ('pending', 'failed')
                          AND updated_at < ?""", primary, stale):
        if _local(row).exists():
            save(row["file_type"], row["owner_id"], _local(row), row["expires_at"])

    # Backups that failed, were skipped for space, or never ran.
    for row in db.rows("""SELECT * FROM files WHERE storage_provider = ? AND upload_status = 'uploaded'
                          AND backup_status IN ('pending', 'failed', 'skipped') AND updated_at < ?""", primary, stale):
        if backup:
            jobs.enqueue("backup_file", row["id"])

    # Temporary source files: the clock starts when processing ends, and stops again while a job uses them.
    for sid in {r["owner_id"] for r in db.rows("SELECT owner_id FROM files WHERE owner_kind = 'source'")}:
        if _source_busy(sid):
            continue
        rows = db.rows("SELECT * FROM files WHERE owner_kind = 'source' AND owner_id = ?", sid)
        if any(r["expires_at"] is None for r in rows) and settings.source_keep_hours > 0:
            db.execute("UPDATE files SET expires_at = ? WHERE owner_kind = 'source' AND owner_id = ? AND expires_at IS NULL",
                       now + settings.source_keep_hours * 3600, sid)
        elif rows and all(r["expires_at"] and r["expires_at"] < now for r in rows):
            if all([_delete_copy(r) for r in rows]):
                shutil.rmtree(source_dir(sid), ignore_errors=True)

    # Clips in primary storage past their retention: only when a verified backup copy exists.
    for row in db.rows("""SELECT * FROM files WHERE owner_kind = 'clip' AND storage_provider = ?
                          AND upload_status = 'uploaded' AND expires_at < ?""", primary, now):
        backup_copy = db.one("""SELECT 1 FROM files WHERE storage_provider = ? AND object_key = ?
                                AND upload_status = 'uploaded' AND md5 = ?""", backup, row["object_key"], row["md5"])
        if row["backup_status"] == "verified" and backup_copy and not _clip_busy(row["owner_id"]):
            _delete_copy(row)

    # Redundant backup copies past their retention: only while the primary copy still exists.
    if backup and settings.backup_keep_days > 0:
        for row in db.rows("""SELECT * FROM files WHERE storage_provider = ? AND upload_status = 'uploaded'
                              AND created_at < ?""", backup, now - settings.backup_keep_days * 86400):
            original = db.one("""SELECT id FROM files WHERE storage_provider = ? AND object_key = ?
                                 AND upload_status = 'uploaded'""", primary, row["object_key"])
            if original and _delete_copy(row):
                db.update("files", original["id"], backup_status="none", updated_at=now)

    # Local copies of clips that are safely stored: this computer only keeps what is still being worked on.
    for row in db.rows("""SELECT * FROM files WHERE owner_kind = 'clip' AND storage_provider = ?
                          AND upload_status = 'uploaded' AND backup_status IN ('verified', 'none')""", primary):
        if not _clip_busy(row["owner_id"]):
            _local(row).unlink(missing_ok=True)


jobs.register("backup_file", backup_file, _backup_failed)
jobs.register("restore_file", restore_file, lambda file_id, error: log.warning("Restore of file %s failed: %s", file_id, error))
