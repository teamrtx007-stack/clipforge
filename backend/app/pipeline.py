"""Job handlers. analyze_source turns a video into clip candidates; render_clip makes one MP4.

Expensive stages cache their output next to the video (audio.wav, loudness.npy, transcript.json),
so a retry after a crash picks up where the last run stopped.
"""
import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import camera, captions, db, describe, fetch, highlights, jobs, media, transcribe
from .config import clip_dir, settings, source_dir
from .media import HOP
from .render import render
from .templates import DEFAULT_TEMPLATE, TEMPLATES

def _cached(path: Path, compute: Callable[[], object], save: Callable[[Path, object], None],
            load: Callable[[Path], object]):
    if path.exists():
        return load(path)
    value = compute()
    with media.writing(path) as tmp:
        save(tmp, value)
    return value


def _save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value))


def _save_npy(path: Path, value) -> None:
    with path.open("wb") as f:
        np.save(f, value)


def source_video(src: dict) -> Path:
    return source_dir(src["id"]) / f"original{src['ext']}"


def _priority(src: dict) -> int:
    return jobs.LIVE_PRIORITY if src["kind"] == "live" else 0


def queue_analysis(src: dict) -> None:
    """Mark a source as waiting and queue its analysis, together."""
    with db.transaction():
        db.update("sources", src["id"], status="queued", stage="Waiting in queue", progress=0, error=None)
        jobs.enqueue("analyze_source", src["id"], _priority(src))


def queue_render(clip_id: int, src: dict) -> None:
    """Mark a clip as waiting and queue its render, together."""
    with db.transaction():
        db.update("clips", clip_id, status="queued", progress=0, error=None)
        jobs.enqueue("render_clip", clip_id, _priority(src))


def discard_interrupted_uploads() -> None:
    """At startup, a source still marked 'uploading' was cut off by a restart. Drop it and its partial file."""
    for row in db.rows("SELECT id FROM sources WHERE status = 'uploading'"):
        db.execute("DELETE FROM sources WHERE id = ?", row["id"])
        shutil.rmtree(source_dir(row["id"]), ignore_errors=True)


def remove_files(source_ids: list[int], clip_ids: list[int]) -> None:
    for sid in source_ids:
        shutil.rmtree(source_dir(sid), ignore_errors=True)
    for cid in clip_ids:
        shutil.rmtree(clip_dir(cid), ignore_errors=True)


def expire(hours: float) -> None:
    """Delete videos and their clips once they are `hours` old, unless a job is still working on them,
    then projects left empty. ClipForge is a workbench, not an archive: people keep the clips they download."""
    db.execute("DELETE FROM activity WHERE at < ?", db.now() - 31 * 86400)  # the chart shows at most 30 days
    if hours <= 0:
        return
    cutoff = db.now() - hours * 3600
    with db.transaction():
        old = [r["id"] for r in db.rows(
            """SELECT id FROM sources s WHERE created_at < ? AND status NOT IN ('uploading', 'queued', 'processing')
                 AND NOT EXISTS (SELECT 1 FROM clips WHERE source_id = s.id AND status IN ('queued', 'rendering'))""",
            cutoff)]
        clips = [r["id"] for sid in old for r in db.rows("SELECT id FROM clips WHERE source_id = ?", sid)]
        for sid in old:
            db.execute("DELETE FROM sources WHERE id = ?", sid)  # cascades to its clips
        db.execute("""DELETE FROM projects WHERE created_at < ?
                        AND NOT EXISTS (SELECT 1 FROM sources WHERE project_id = projects.id)
                        AND NOT EXISTS (SELECT 1 FROM channels WHERE project_id = projects.id)""", cutoff)
    remove_files(old, clips)


@contextmanager
def _owned(folder: Path) -> Iterator[None]:
    """If the job's row is deleted mid-job, delete whatever the job wrote after the API cleaned up."""
    try:
        yield
    except jobs.Gone:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _download(src: dict, url: str, stage: Callable[[str, float], None]) -> Path:
    """Fetch a pasted link. The video, and a project still named after the link, take the video's title."""
    folder = source_dir(src["id"])
    folder.mkdir(parents=True, exist_ok=True)
    stage("Downloading video", 0.0)

    def titled(title: str) -> None:
        if title:
            db.update("sources", src["id"], name=title[:200])
            db.execute("UPDATE projects SET name = ? WHERE id = ? AND name = ?", title[:120], src["project_id"], url[:120])

    video = fetch.download(url, folder, settings.max_upload_bytes, titled, lambda p: stage("Downloading video", p))
    db.update("sources", src["id"], ext=video.suffix)
    return video


def analyze_source(source_id: int) -> None:
    src = db.one("SELECT * FROM sources WHERE id = ?", source_id)
    if src is None:  # deleted while it waited in the queue
        return
    folder, video = source_dir(source_id), source_video(src)

    def stage(name: str, progress: float) -> None:
        if not db.update("sources", source_id, status="processing", stage=name, progress=round(progress, 3),
                         error=None):
            raise jobs.Gone()

    with _owned(folder):
        if not video.exists() and (url := src["meta"].get("url")):
            video = _download(src, url, stage)
        stage("Reading video", 0.01)
        info = media.probe(video)
        db.update("sources", source_id, duration=info.duration, width=info.width, height=info.height, fps=info.fps)

        wav = folder / "audio.wav"
        if info.has_audio and not wav.exists():
            stage("Extracting audio", 0.03)
            media.extract_audio(video, wav)
        loudness = _cached(
            folder / "loudness.npy",
            lambda: media.loudness_curve(wav) if info.has_audio else np.full(int(info.duration / HOP) + 1, -100.0),
            _save_npy, np.load,
        )
        transcript = _cached(
            folder / "transcript.json",
            lambda: transcribe.transcribe_file(wav, info.duration, folder, loudness,
                                               lambda p: stage("Transcribing speech", 0.05 + 0.8 * p))
            if info.has_audio else {"language": None, "segments": []},
            _save_json, lambda p: json.loads(p.read_text()),
        )

        stage("Finding highlights", 0.88)
        meta = src["meta"]
        chat_path = folder / "chat.npy"
        max_clips = meta.get("max_clips", 8)
        # With AI on, a few extra candidates go in and its virality score decides which make the cut.
        pool = max_clips + min(max_clips, 8) if settings.text_providers else max_clips
        found = highlights.find(
            transcript, loudness, info.duration, highlights.LENGTHS[meta.get("length", "medium")],
            pool, must_include=meta.get("peak"),
            chat=np.load(chat_path) if chat_path.exists() else None,
        )
        stage("Writing titles", 0.94)
        described = describe.describe(found)[:max_clips]

        with db.transaction():
            if db.one("SELECT id FROM sources WHERE id = ?", source_id) is None:
                raise jobs.Gone()
            on_stream = [f"On stream: {r}" for r in meta.get("moment", {}).get("reasons", [])]
            old = [r["id"] for r in db.rows("SELECT id FROM clips WHERE source_id = ?", source_id)]
            db.execute("DELETE FROM clips WHERE source_id = ?", source_id)
            new = [
                db.insert("clips", source_id=source_id, start=d.candidate.start, end=d.candidate.end,
                          peak=d.candidate.peak, score=d.candidate.score, title=d.title, description=d.description,
                          hashtags=list(d.hashtags), reasons=on_stream + list(d.candidate.reasons),
                          template=meta.get("template", DEFAULT_TEMPLATE), status="draft", created_at=db.now())
                for d in described
            ]
            db.update("sources", source_id, status="ready", stage="Done", progress=1.0)
            db.record("video")
    for clip_id in old:
        shutil.rmtree(clip_dir(clip_id), ignore_errors=True)
    if meta.get("auto_render", True):
        for clip_id in new:
            queue_render(clip_id, src)


def render_clip(clip_id: int) -> None:
    clip = db.one("SELECT * FROM clips WHERE id = ?", clip_id)
    if clip is None:
        return
    src = db.one("SELECT * FROM sources WHERE id = ?", clip["source_id"])
    template = TEMPLATES.get(clip["template"], TEMPLATES[DEFAULT_TEMPLATE])
    folder = clip_dir(clip_id)
    folder.mkdir(parents=True, exist_ok=True)

    def progress(p: float) -> None:
        if not db.update("clips", clip_id, status="rendering", progress=round(p, 3), error=None):
            raise jobs.Gone()

    with _owned(folder):
        progress(0.0)
        video, start, end = source_video(src), clip["start"], clip["end"]
        info = media.probe(video)
        sfolder = source_dir(src["id"])

        path = camera.CENTER
        if template.layout != "fit_blur":
            path = camera.plan(camera.detect(video, start, end, info.width, info.height))
        layout = camera.resolve_layout(template.layout, path.face_ratio)
        progress(0.1)

        z = highlights.loudness_z(np.load(sfolder / "loudness.npy"))
        n_frames = max(1, round((end - start) * info.fps))
        zoom = camera.zoom_curve(z[int(start / HOP):int(end / HOP) + 1], n_frames, info.fps, template.punch_zoom)

        ass = None
        if template.captions:
            transcript = json.loads((sfolder / "transcript.json").read_text())
            words = highlights.words_in(highlights.words_from(transcript), start, end)
            ass = folder / "captions.ass"
            ass.write_text(captions.build_ass(words, template.captions))

        out = folder / "clip.mp4"
        render(video, info, start, end, path, zoom, layout, ass, out, lambda p: progress(0.1 + 0.88 * p))
        peak = clip["peak"] if clip["peak"] is not None and start <= clip["peak"] < end else (start + end) / 2
        media.thumbnail(out, peak - start, folder / "thumb.jpg")
        # Ready only if nobody trimmed the clip or switched its template while it rendered.
        if not db.execute(
            """UPDATE clips SET progress = 1, rendered_at = ?,
                 status = CASE WHEN start = ? AND end = ? AND template = ? THEN 'ready' ELSE 'draft' END
               WHERE id = ?""", db.now(), start, end, clip["template"], clip_id,
        ).rowcount:
            raise jobs.Gone()
        db.record("clip")


def _mark(table: str) -> Callable[[int, str], None]:
    return lambda row_id, error: db.update(table, row_id, status="failed", error=error)


jobs.register("analyze_source", analyze_source, _mark("sources"))
jobs.register("render_clip", render_clip, _mark("clips"))
