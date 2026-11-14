"""REST API. Input is validated here; everything behind it trusts its arguments."""
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import AfterValidator, BaseModel, Field
from starlette.requests import ClientDisconnect

from . import db, files, pipeline
from .config import clip_dir, settings, source_dir
from .highlights import LENGTHS
from .live import platforms, signals, supervisor
from .templates import DEFAULT_TEMPLATE, TEMPLATES

router = APIRouter(prefix="/api")

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".ts", ".mts", ".wmv", ".mpg"})
BROWSER_PLAYABLE = frozenset({".mp4", ".m4v", ".mov", ".webm"})


def _known_template(value: str) -> str:
    if value not in TEMPLATES:
        raise ValueError(f"unknown template {value!r}")
    return value


# Each option list lives in one registry; these types are derived from it.
TemplateId = Annotated[str, AfterValidator(_known_template)]
Length = Literal[tuple(LENGTHS)]  # type: ignore[valid-type]
Sensitivity = Literal[tuple(signals.THRESHOLDS)]  # type: ignore[valid-type]
CHANNEL_FLAGS = ("enabled", "auto_record", "auto_render", "chat", "speech")


class ProjectSettings(BaseModel):
    length: Length = "medium"
    max_clips: int = Field(8, ge=1, le=30)
    template: TemplateId = DEFAULT_TEMPLATE


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    settings: ProjectSettings = ProjectSettings()


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    settings: ProjectSettings | None = None


class ClipPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)
    hashtags: list[str] | None = Field(None, max_length=15)
    start: float | None = Field(None, ge=0)
    end: float | None = Field(None, gt=0)
    template: TemplateId | None = None


class ChannelIn(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    name: str | None = Field(None, max_length=80)
    template: TemplateId = DEFAULT_TEMPLATE
    sensitivity: Sensitivity = "medium"


class ChannelPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    enabled: bool | None = None
    auto_record: bool | None = None
    auto_render: bool | None = None
    chat: bool | None = None
    speech: bool | None = None
    sensitivity: Sensitivity | None = None
    template: TemplateId | None = None
    buffer_minutes: int | None = Field(None, ge=2, le=60)


def _get(table: str, row_id: int) -> dict:
    row = db.one(f"SELECT * FROM {table} WHERE id = ?", row_id)
    if row is None:
        raise HTTPException(404, f"{table[:-1]} {row_id} not found")
    return row


def _clip_out(c: dict) -> dict:
    version = int(c["rendered_at"] or 0)
    ready = c["status"] == "ready" or (c["rendered_at"] is not None and c["status"] != "failed")
    base = f"/api/files/clips/{c['id']}"
    stored = files.state("clip", c["id"])
    ready = ready and not (stored and stored["location"] == "backup")  # archived: restore before viewing
    return {**c,
            "video_url": f"{base}/clip.mp4?v={version}" if ready else None,
            "thumb_url": f"{base}/thumb.jpg?v={version}" if ready else None,
            "download_url": f"/api/clips/{c['id']}/download" if ready else None,
            "storage": stored}


def _channel_out(ch: dict) -> dict:
    return {**ch, **{k: bool(ch[k]) for k in CHANNEL_FLAGS}}


def _source_out(s: dict) -> dict:
    playable = s["ext"] in BROWSER_PLAYABLE and s["status"] != "uploading"
    stored = files.state("source", s["id"])
    expired = (files.enabled() and s["status"] in ("ready", "failed") and stored is None
               and not (source_dir(s["id"]) / f"original{s['ext']}").exists())
    return {**s, "video_url": f"/api/files/sources/{s['id']}/original{s['ext']}" if playable and not expired else None,
            "storage": stored, "expired": expired}


# Projects

@router.get("/projects")
def list_projects() -> list[dict]:
    return db.rows("""
        SELECT p.*,
          (SELECT COUNT(*) FROM sources s WHERE s.project_id = p.id) AS source_count,
          (SELECT COUNT(*) FROM clips c JOIN sources s ON s.id = c.source_id WHERE s.project_id = p.id) AS clip_count,
          (SELECT c.id FROM clips c JOIN sources s ON s.id = c.source_id
             WHERE s.project_id = p.id AND c.rendered_at IS NOT NULL ORDER BY c.score DESC LIMIT 1) AS cover_clip_id,
          (SELECT ch.id FROM channels ch WHERE ch.project_id = p.id) AS channel_id
        FROM projects p ORDER BY p.created_at DESC""")


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn) -> dict:
    pid = db.insert("projects", name=body.name.strip(), settings=body.settings.model_dump(), created_at=db.now())
    return _get("projects", pid)


@router.get("/projects/{pid}")
def get_project(pid: int, limit: int = 100) -> dict:
    """The newest `limit` videos and their clips. A live channel adds a video per moment, for months."""
    project = _get("projects", pid)
    limit = min(max(limit, 1), 1000)
    sources = db.rows("SELECT * FROM sources WHERE project_id = ? ORDER BY created_at DESC LIMIT ?", pid, limit)
    ids = [s["id"] for s in sources]
    clips = db.rows(f"""SELECT * FROM clips WHERE source_id IN ({",".join("?" * len(ids))})
                        ORDER BY score DESC, id""", *ids) if ids else []
    total = db.one("SELECT COUNT(*) AS n FROM sources WHERE project_id = ?", pid)["n"]
    channel = db.one("SELECT * FROM channels WHERE project_id = ?", pid)
    return {**project, "sources": [_source_out(s) for s in sources], "clips": [_clip_out(c) for c in clips],
            "source_total": total, "channel": _channel_out(channel) if channel else None}


@router.patch("/projects/{pid}")
def update_project(pid: int, body: ProjectPatch) -> dict:
    _get("projects", pid)
    changes = body.model_dump(exclude_none=True)
    if changes:
        db.update("projects", pid, **changes)
    return _get("projects", pid)


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: int) -> None:
    _get("projects", pid)
    sources = [r["id"] for r in db.rows("SELECT id FROM sources WHERE project_id = ?", pid)]
    clips = [r["id"] for r in db.rows(
        "SELECT c.id FROM clips c JOIN sources s ON s.id = c.source_id WHERE s.project_id = ?", pid)]
    db.execute("DELETE FROM projects WHERE id = ?", pid)  # cascades to channels, sources and clips
    pipeline.remove_files(sources, clips)
    supervisor.wake()


# Uploads, links and sources

def _options(project: dict, **overrides) -> ProjectSettings:
    """The project's clip settings, with any the request names instead."""
    try:
        return ProjectSettings(**{**project["settings"], **{k: v for k, v in overrides.items() if v is not None}})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/projects/{pid}/upload", status_code=201)
async def upload(pid: int, request: Request, filename: str,
                 length: Length | None = None,
                 max_clips: int | None = None, template: str | None = None) -> dict:
    project = _get("projects", pid)
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}. Use one of: {', '.join(sorted(VIDEO_EXTS))}")
    options = _options(project, length=length, max_clips=max_clips, template=template)
    limit = settings.max_upload_bytes
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, f"File is larger than the {limit // 2**20} MB limit")

    sid = db.insert("sources", project_id=pid, kind="upload", name=Path(filename).stem[:200] or "video", ext=ext,
                    status="uploading", meta=options.model_dump(), created_at=db.now())
    folder = source_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    part, size = folder / "upload.part", 0
    try:
        with part.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File is larger than the {limit // 2**20} MB limit")
                f.write(chunk)  # shortcut: blocks the event loop briefly per chunk; fine for one user, use a thread for many
        if size == 0:
            raise HTTPException(400, "The upload was empty")
        part.replace(folder / f"original{ext}")
    except (HTTPException, ClientDisconnect, OSError):
        db.execute("DELETE FROM sources WHERE id = ?", sid)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    pipeline.queue_analysis(_get("sources", sid))
    return _source_out(_get("sources", sid))


class LinkIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    length: Length | None = None
    max_clips: int | None = None
    template: str | None = None


@router.post("/projects/{pid}/link", status_code=201)
def add_link(pid: int, body: LinkIn) -> dict:
    """Queue a video from a YouTube (or other site's) link. The analysis job downloads it first."""
    project = _get("projects", pid)
    url = body.url.strip()
    if urlsplit(url).scheme not in ("http", "https"):
        raise HTTPException(422, "Paste a full video link, starting with https://")
    try:
        platforms.check_direct_host(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    options = _options(project, length=body.length, max_clips=body.max_clips, template=body.template)
    sid = db.insert("sources", project_id=pid, kind="upload", name=url[:200], ext=".mp4", status="queued",
                    meta={**options.model_dump(), "url": url}, created_at=db.now())
    pipeline.queue_analysis(_get("sources", sid))
    return _source_out(_get("sources", sid))


@router.post("/sources/{sid}/reprocess")
def reprocess(sid: int) -> dict:
    src = _get("sources", sid)
    if src["status"] in ("uploading", "processing"):
        raise HTTPException(409, "This video is still being processed")
    if src["kind"] == "upload":  # use the project's current length, clip count and template
        db.update("sources", sid, meta={**src["meta"], **_get("projects", src["project_id"])["settings"]})
    pipeline.queue_analysis(src)
    return _source_out(_get("sources", sid))


@router.delete("/sources/{sid}", status_code=204)
def delete_source(sid: int) -> None:
    _get("sources", sid)
    clips = [r["id"] for r in db.rows("SELECT id FROM clips WHERE source_id = ?", sid)]
    db.execute("DELETE FROM sources WHERE id = ?", sid)
    pipeline.remove_files([sid], clips)


# Clips

@router.get("/clips")
def recent_clips(limit: int = 12) -> list[dict]:
    rows = db.rows("""SELECT c.*, s.project_id, s.kind AS source_kind, p.name AS project_name
                      FROM clips c JOIN sources s ON s.id = c.source_id JOIN projects p ON p.id = s.project_id
                      ORDER BY c.created_at DESC, c.score DESC LIMIT ?""", min(max(limit, 1), 100))
    return [_clip_out(c) for c in rows]


@router.get("/clips/{cid}")
def get_clip(cid: int) -> dict:
    clip = _get("clips", cid)
    src = _get("sources", clip["source_id"])
    return {**_clip_out(clip), "source": _source_out(src)}


@router.patch("/clips/{cid}")
def update_clip(cid: int, body: ClipPatch) -> dict:
    clip = _get("clips", cid)
    changes = body.model_dump(exclude_none=True)
    start, end = changes.get("start", clip["start"]), changes.get("end", clip["end"])
    duration = _get("sources", clip["source_id"])["duration"] or end
    if not 3 <= end - start <= 180 or end > duration + 0.05:
        raise HTTPException(422, "A clip must be 3 to 180 seconds long and inside the video")
    if "hashtags" in changes:
        changes["hashtags"] = ["#" + re.sub(r"[^\w]", "", t.lstrip("#"))[:40] for t in changes["hashtags"]
                               if re.sub(r"[^\w]", "", t)]
    if any(k in changes and changes[k] != clip[k] for k in ("start", "end", "template")):
        changes["status"] = "draft"  # the MP4 no longer matches; the user re-renders when ready
    if changes:
        db.update("clips", cid, **changes)
    return get_clip(cid)


@router.post("/clips/{cid}/render")
def render_clip(cid: int) -> dict:
    clip = _get("clips", cid)
    src = _get("sources", clip["source_id"])
    if src["status"] != "ready":
        raise HTTPException(409, "The video is still being analyzed")
    pipeline.queue_render(cid, src)
    return get_clip(cid)


FILE_NAMES = {"clip.mp4": "clip_video", "thumb.jpg": "thumbnail"}


def _serve(file_type: str, owner_id: int, path: Path, filename: str | None = None) -> Response:
    """Primary storage through a short-lived signed URL; otherwise the local working copy. A file that only exists
    as a backup has to be restored first, because primary storage is the download location."""
    if url := files.url(file_type, owner_id, filename):
        return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})
    if path.exists():
        return FileResponse(path, filename=filename)
    state = files.state("clip" if file_type in FILE_NAMES.values() else "source", owner_id)
    if state and state["location"] == "backup":
        raise HTTPException(409, "This file is only in the backup archive. Restore it first.")
    raise HTTPException(404, "File not found")


@router.get("/files/{kind}/{owner_id}/{name}")
def serve_file(kind: Literal["clips", "sources"], owner_id: int, name: str) -> Response:
    if kind == "clips":
        if name not in FILE_NAMES:
            raise HTTPException(404, "File not found")
        return _serve(FILE_NAMES[name], owner_id, clip_dir(owner_id) / name)
    src = _get("sources", owner_id)
    if name != f"original{src['ext']}":
        raise HTTPException(404, "File not found")
    return _serve("source_video", owner_id, source_dir(owner_id) / name)


@router.get("/clips/{cid}/download")
def download_clip(cid: int) -> Response:
    clip = _get("clips", cid)
    slug = re.sub(r"[^a-z0-9]+", "-", clip["title"].lower()).strip("-")[:60] or "clip"
    return _serve("clip_video", cid, clip_dir(cid) / "clip.mp4", f"{slug}-{cid}.mp4")


@router.post("/clips/{cid}/restore")
def restore_clip(cid: int) -> dict:
    """Copy a clip that only exists in the backup archive back into primary storage."""
    _get("clips", cid)
    if not files.restore("clip", cid):
        raise HTTPException(409, "Nothing to restore: the clip is not in the backup archive only")
    return get_clip(cid)


@router.delete("/clips/{cid}", status_code=204)
def delete_clip(cid: int) -> None:
    _get("clips", cid)
    db.execute("DELETE FROM clips WHERE id = ?", cid)
    pipeline.remove_files([], [cid])


# Templates, queue and system

@router.get("/templates")
def list_templates() -> list[dict]:
    return [{"id": t.id, "name": t.name, "description": t.description, "layout": t.layout,
             "captions": t.captions is not None, "punch_zoom": t.punch_zoom} for t in TEMPLATES.values()]


@router.get("/jobs")
def list_jobs(limit: int = 60) -> list[dict]:
    return db.rows("""
        SELECT j.*,
          COALESCE(s.name, c.title) AS label,
          COALESCE(s.project_id, cs.project_id) AS project_id,
          COALESCE(s.stage, CASE c.status WHEN 'rendering' THEN 'Rendering' ELSE '' END) AS stage,
          COALESCE(s.progress, c.progress) AS progress
        FROM jobs j
        LEFT JOIN sources s ON j.kind = 'analyze_source' AND s.id = j.target_id
        LEFT JOIN clips c ON j.kind = 'render_clip' AND c.id = j.target_id
        LEFT JOIN sources cs ON cs.id = c.source_id
        ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                 CASE WHEN j.status IN ('running', 'queued') THEN -j.priority ELSE 0 END, j.updated_at DESC
        LIMIT ?""", min(max(limit, 1), 300))


@router.get("/system")
def system() -> dict:
    disk = shutil.disk_usage(settings.data_dir)
    ai = settings.text_providers[0] if settings.text_providers else None
    return {
        "whisper_model": settings.whisper_model,
        "llm": {"enabled": bool(ai), "model": f"{ai.name}: {ai.model}" if ai else None, "host": ai.name if ai else None,
                "fallbacks": [p.name for p in settings.text_providers[1:]]},
        "transcription": list(settings.transcribe_providers),
        "live_enabled": settings.live_enabled,
        "workers": settings.workers,
        "keep_hours": settings.keep_hours,
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "storage": {"primary": settings.storage_primary, "backup": settings.storage_backup, "providers": files.quota(),
                    "source_keep_hours": settings.source_keep_hours, "clip_keep_days": settings.clip_keep_days,
                    "backup_keep_days": settings.backup_keep_days},
        "templates": len(TEMPLATES),
    }


@router.get("/stats")
def stats(tz_offset: int = 0, days: int = 14) -> dict:
    """Work done per day in the viewer's time zone, for the dashboard. `tz_offset` is JavaScript's
    Date.getTimezoneOffset(): minutes behind UTC, so India is -330."""
    days, shift = min(max(days, 1), 30), -min(max(tz_offset, -900), 900) * 60
    today = datetime.fromtimestamp(db.now() + shift, timezone.utc).date()
    counts = {(r["day"], r["kind"]): r["n"] for r in db.rows(
        """SELECT date(at + ?, 'unixepoch') AS day, kind, COUNT(*) AS n FROM activity
           WHERE at >= ? GROUP BY day, kind""", shift, db.now() - (days + 1) * 86400)}
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    return {
        "days": [{"day": d, **{k + "s": counts.get((d, k), 0) for k in ("video", "clip", "moment")}} for d in dates],
        "clips_ready": db.one("SELECT COUNT(*) AS n FROM clips WHERE status = 'ready'")["n"],
        "jobs_active": db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')")["n"],
    }


# Live channels

@router.get("/channels")
def list_channels() -> list[dict]:
    states = supervisor.snapshot()
    return [{**_channel_out(ch), "threshold": signals.THRESHOLDS[ch["sensitivity"]],
             "runtime": states.get(ch["id"], supervisor.idle_state(ch))}
            for ch in db.rows("SELECT * FROM channels ORDER BY created_at")]


@router.post("/channels", status_code=201)
def create_channel(body: ChannelIn) -> dict:
    try:
        target = platforms.parse(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if db.one("SELECT id FROM channels WHERE url = ?", target.url):
        raise HTTPException(409, "This channel is already being monitored")
    name = (body.name or target.name).strip()
    with db.transaction():
        pid = db.insert("projects", name=f"Live: {name}", created_at=db.now(),
                        settings=ProjectSettings(template=body.template).model_dump())
        cid = db.insert("channels", project_id=pid, platform=target.platform, url=target.url, name=name,
                        template=body.template, sensitivity=body.sensitivity, created_at=db.now())
    supervisor.wake()
    return _channel_out(_get("channels", cid))


@router.patch("/channels/{cid}")
def update_channel(cid: int, body: ChannelPatch) -> dict:
    _get("channels", cid)
    changes = body.model_dump(exclude_none=True)
    if changes:
        db.update("channels", cid, **changes)
    supervisor.wake()
    return _channel_out(_get("channels", cid))


@router.delete("/channels/{cid}", status_code=204)
def delete_channel(cid: int) -> None:
    _get("channels", cid)
    db.execute("DELETE FROM channels WHERE id = ?", cid)  # its project and clips stay
    supervisor.wake()
