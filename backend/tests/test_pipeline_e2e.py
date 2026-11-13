"""Upload-to-MP4 on a real video: transcribe, find highlights, track the face, render."""
import json
import shutil
import subprocess

import numpy as np

from app import camera, db, jobs, pipeline
from app.config import clip_dir, source_dir
from app.media import probe


def _make_source(video, **meta) -> int:
    pid = db.insert("projects", name="e2e", settings={}, created_at=db.now())
    sid = db.insert("sources", project_id=pid, kind="upload", name="talk", ext=".mp4", status="queued",
                    meta={"length": "short", "max_clips": 3, "template": "viral", **meta}, created_at=db.now())
    source_dir(sid).mkdir(parents=True)
    shutil.copy(video, source_dir(sid) / "original.mp4")
    return sid


def _drain():
    while jobs.run_one():
        pass


def test_upload_becomes_rendered_vertical_clips(talking_video):
    sid = _make_source(talking_video)
    jobs.enqueue("analyze_source", sid)
    _drain()

    src = db.one("SELECT * FROM sources WHERE id = ?", sid)
    assert src["status"] == "ready", src["error"]
    transcript = json.loads((source_dir(sid) / "transcript.json").read_text())
    text = " ".join(s["text"] for s in transcript["segments"]).lower()
    assert "country" in text and "fellow americans" in text

    clips = db.rows("SELECT * FROM clips WHERE source_id = ? ORDER BY score DESC", sid)
    assert clips, "no clips found"
    assert all(c["status"] == "ready" for c in clips), [c["error"] for c in clips]
    spans = sorted((c["start"], c["end"]) for c in clips)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), "clips overlap"
    assert all(15 <= c["end"] - c["start"] <= 30.5 for c in clips)
    assert all(c["title"] and c["hashtags"] for c in clips)

    best = clips[0]
    # The loud second take (11 s to 22 s) should be inside the best clip.
    assert best["start"] < 18 and best["end"] > 15, (best["start"], best["end"])
    out = clip_dir(best["id"]) / "clip.mp4"
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert abs(info.duration - (best["end"] - best["start"])) < 0.6
    assert (clip_dir(best["id"]) / "thumb.jpg").stat().st_size > 1000
    assert "Dialogue:" in (clip_dir(best["id"]) / "captions.ass").read_text()


def test_crop_follows_the_moving_face(talking_video):
    info = probe(talking_video)
    path = camera.plan(camera.detect(talking_video, 0, 22, info.width, info.height))
    assert path.face_ratio > 0.9
    # The face drifts from the left edge to the right edge over the first 22 s.
    assert path.at(1)[0] < 0.35 and path.at(21)[0] > 0.65
    assert np.all(np.diff(path.cx) >= -1e-9), "camera should only pan one way while the face does"


def test_reanalysis_replaces_clips(talking_video):
    sid = _make_source(talking_video, auto_render=False)
    jobs.enqueue("analyze_source", sid)
    _drain()
    first = [c["id"] for c in db.rows("SELECT id FROM clips WHERE source_id = ?", sid)]
    jobs.enqueue("analyze_source", sid)
    _drain()
    second = db.rows("SELECT id, status FROM clips WHERE source_id = ?", sid)
    assert first and len(second) == len(first) and not set(first) & {c["id"] for c in second}
    assert all(c["status"] == "draft" for c in second)


def test_trim_during_render_leaves_the_clip_draft(talking_video, monkeypatch):
    sid = _make_source(talking_video, auto_render=False)
    jobs.enqueue("analyze_source", sid)
    _drain()
    clip = db.rows("SELECT * FROM clips WHERE source_id = ?", sid)[0]

    def render_while_user_trims(video, info, start, end, *args):
        db.update("clips", clip["id"], end=start + 5)  # the user saves a shorter cut mid-render
        shutil.copy(talking_video, args[-2])

    monkeypatch.setattr(pipeline, "render", render_while_user_trims)
    jobs.enqueue("render_clip", clip["id"])
    _drain()
    after = db.one("SELECT * FROM clips WHERE id = ?", clip["id"])
    assert after["status"] == "draft", "the MP4 shows the old cut, so it must not be marked ready"


def test_deleting_a_source_mid_job_stops_it_quietly(talking_video, monkeypatch):
    sid = _make_source(talking_video)
    real_probe = pipeline.media.probe

    def delete_then_probe(video):
        db.execute("DELETE FROM sources WHERE id = ?", sid)
        shutil.rmtree(source_dir(sid))
        return real_probe(talking_video)

    monkeypatch.setattr(pipeline.media, "probe", delete_then_probe)
    jobs.enqueue("analyze_source", sid)
    _drain()
    job = db.one("SELECT * FROM jobs WHERE kind = 'analyze_source' AND target_id = ?", sid)
    assert job["status"] == "done" and not source_dir(sid).exists()
