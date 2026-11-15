"""Scene detection, and clips that stay inside one scene (PR A: clip continuity)."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app import db, highlights, media, pipeline, scenes
from app.config import source_dir
from app.highlights import LENGTHS

from test_units import _filler_talk, _transcript

OUTDOOR = "colorchannelmixer=rr=0.3:gg=1:bb=0.6,eq=brightness=0.05"
INDOOR = "colorchannelmixer=rr=1:gg=0.55:bb=0.2,eq=brightness=-0.25"


def _video(path: Path, graph: str) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_complex", graph, str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def three_shots(tmp_path_factory) -> Path:
    """Outdoors, the same place from another angle (a cut), then indoors (a context change), 4 s each."""
    return _video(tmp_path_factory.mktemp("scenes") / "three.mp4",
                  f"testsrc2=s=640x360:r=25:d=4,{OUTDOOR}[a];testsrc2=s=640x360:r=25:d=4,hflip,vflip,{OUTDOOR}[b];"
                  f"testsrc2=s=640x360:r=25:d=4,{INDOOR}[c];[a][b][c]concat=n=3:v=1:a=0")


class TestDetection:
    def test_a_cut_between_angles_is_not_a_change_of_place(self, three_shots):
        assert scenes.detect(three_shots, 12, 640, 360) == {
            "version": scenes.VERSION, "fps": scenes.SAMPLE_FPS, "cuts": [4.0, 8.0], "context_changes": [8.0]}

    def test_fast_camera_motion_is_not_a_cut_and_a_fade_to_another_place_is_a_change(self, tmp_path):
        video = _video(tmp_path / "motion.mp4",
                       f"testsrc2=s=1920x360:r=25:d=8,crop=640:360:'mod(t*900,1280)':0,{OUTDOOR}[a];"
                       f"testsrc2=s=640x360:r=25:d=4,{OUTDOOR},fade=out:st=1:d=2[b];"
                       f"testsrc2=s=640x360:r=25:d=4,{INDOOR},fade=in:st=0:d=2[c];[a][b][c]concat=n=3:v=1:a=0")
        found = scenes.detect(video, 16, 640, 360)
        assert found["cuts"] == [], "a camera whipping around every frame is still one shot"
        assert len(found["context_changes"]) == 1 and 8.0 <= found["context_changes"][0] <= 12.5

    def test_steady_and_tiny_inputs(self):
        hist = np.full((40, 72), 1 / 72, np.float32)
        thumbs = np.random.default_rng(1).random((40, 18, 32)).astype(np.float32) * 0.02  # sensor noise
        assert scenes.analyse(hist, thumbs)["cuts"] == []
        assert scenes.analyse(hist[:1], thumbs[:1])["cuts"] == []

    def test_crossing_ignores_changes_at_the_clip_edges(self):
        found = {"context_changes": [10.0, 20.0]}
        assert scenes.crossing(found, 10.1, 19.8) == []
        assert scenes.crossing(found, 5.0, 25.0) == [10.0, 20.0]
        assert scenes.crossing(None, 0, 100) == []


def _loud_at(duration: float, start: float, end: float) -> np.ndarray:
    loud = np.full(int(duration / 0.5) + 1, -20.0)
    loud[int(start / 0.5):int(end / 0.5)] = -6.0
    return loud


class TestContinuity:
    def test_the_reviewed_case_a_clip_no_longer_jumps_to_another_place(self):
        """A strong moment at 20-40 s, and the video changes place at 37 s: the clip must stop before it."""
        talk = _filler_talk(120)
        talk[4] = (20.0, "Why do most people fail at this? Here's why it matters!")
        tr, loud = _transcript(talk), _loud_at(120, 20, 45)
        old = highlights.find(tr, loud, 120, LENGTHS["medium"], 1)[0]
        assert old.start < 37 < old.end, "without scene information the clip runs across the change"
        found = highlights.find(tr, loud, 120, LENGTHS["medium"], 3, scenes={"cuts": [37.0], "context_changes": [37.0]})
        assert found and all(not scenes.crossing({"context_changes": [37.0]}, c.start, c.end) for c in found)

    def test_without_scenes_nothing_changes(self):
        talk = _filler_talk(300)
        tr, loud = _transcript(talk), _loud_at(300, 100, 110)
        empty = {"cuts": [], "context_changes": []}
        assert highlights.find(tr, loud, 300, LENGTHS["short"], 5) == highlights.find(
            tr, loud, 300, LENGTHS["short"], 5, scenes=empty)

    def test_low_speech_video_clips_start_on_a_cut_and_end_at_the_change(self):
        loud = _loud_at(120, 44, 52)  # a big moment in a video with no speech, like a sports clip with music
        found_scenes = {"cuts": [30.0, 60.0], "context_changes": [60.0]}
        found = highlights.find({"segments": []}, loud, 120, LENGTHS["short"], 2, scenes=found_scenes)
        assert found[0].start <= 44 and 52 <= found[0].end
        assert all(not scenes.crossing(found_scenes, c.start, c.end) for c in found)
        shot = highlights._shot_windows([], 120, LENGTHS["short"], found_scenes)
        assert [(w.start, w.end) for w in shot] == [(30.0, 52.0), (60.0, 82.0)], "windows that open on a shot"
        assert highlights._end_at_change(shot[0], [40.0], LENGTHS["short"]) is None, "too short once cut"
        assert highlights._end_at_change(shot[0], [50.0], LENGTHS["short"]).end == 50.0

    def test_when_every_window_crosses_a_change_the_clip_is_still_made_but_ranked_lower(self):
        tr, loud = _transcript(_filler_talk(60)), _loud_at(60, 20, 30)
        every_10s = {"cuts": [10.0, 20.0, 30.0, 40.0, 50.0], "context_changes": [10.0, 20.0, 30.0, 40.0, 50.0]}
        plain = highlights.find(tr, loud, 60, LENGTHS["short"], 1)
        crossed = highlights.find(tr, loud, 60, LENGTHS["short"], 1, scenes=every_10s)
        assert plain and crossed, "a video that changes place every 10 s still gets a 15-30 s clip"
        assert crossed[0].score < plain[0].score

    def test_clip_edges_never_flash_the_neighbouring_shot(self):
        start, end = highlights._away_from_cuts(9.75, 40.45, 10.0, 40.0, [9.9, 40.2])
        assert (start, end) == (9.9, 40.2)
        # A cut inside a word is left alone: cutting the word would be worse than a flash.
        assert highlights._away_from_cuts(9.75, 40.45, 9.8, 40.3, [9.9, 40.2]) == (9.75, 40.45)


class TestPipeline:
    def test_scenes_are_cached_recomputed_when_outdated_and_optional(self, three_shots, tmp_path, override):
        info = media.probe(three_shots)
        first = pipeline.find_scenes(three_shots, info, tmp_path)
        assert first["context_changes"] == [8.0] and (tmp_path / "scenes.json").exists()
        (tmp_path / "scenes.json").write_text(json.dumps({"version": 0, "cuts": [], "context_changes": []}))
        assert pipeline.find_scenes(three_shots, info, tmp_path) == first, "an old detector's file is redone"
        override(scene_detection=False)
        assert pipeline.find_scenes(three_shots, info, tmp_path) is None, "SCENE_DETECTION=0: the old behaviour"

    def test_a_detection_failure_falls_back_to_choosing_without_scenes(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a video")
        info = media.Probe(duration=10, width=640, height=360, fps=25, has_audio=False)
        assert pipeline.find_scenes(broken, info, tmp_path) is None
        assert pipeline.find_scenes(broken, media.Probe(10, 0, 0, 0, True), tmp_path) is None  # audio only

    def test_report_reads_only(self, three_shots, capsys):
        import shutil

        pid = db.insert("projects", name="p", settings={}, created_at=db.now())
        sid = db.insert("sources", project_id=pid, kind="upload", name="walk", ext=".mp4", status="ready",
                        meta={"length": "short"}, created_at=db.now())
        source_dir(sid).mkdir(parents=True)
        shutil.copy(three_shots, source_dir(sid) / "original.mp4")
        cid = db.insert("clips", source_id=sid, start=2, end=11, score=50, title="Run", template="viral",
                        status="ready", created_at=db.now())
        before = db.rows("SELECT * FROM clips WHERE source_id = ?", sid)
        assert scenes._report(sid) == 0
        out = capsys.readouterr().out
        assert "1 context changes" in out and f"clip {cid}" in out and "crosses a context change, 6.0 s in" in out
        assert "transcript is gone" in out
        assert db.rows("SELECT * FROM clips WHERE source_id = ?", sid) == before, "nothing is changed"
        assert scenes._report(999999) == 1


def test_analysis_of_a_real_video_keeps_every_clip_inside_one_place(tmp_path, monkeypatch):
    """End to end, no speech model needed: a silent 60 s video, 30 s outdoors then 30 s indoors, with a loud
    burst of sound right before the change. Analysis writes scenes.json and no clip crosses the change."""
    import shutil

    from app import jobs, transcribe

    # The sound is a tone, not speech: an empty transcript, without loading a speech model.
    monkeypatch.setattr(transcribe, "transcribe_file", lambda *a, **k: {"language": None, "segments": []})
    video = _video(tmp_path / "walk.mp4",
                   f"testsrc2=s=640x360:r=25:d=30,{OUTDOOR}[a];testsrc2=s=640x360:r=25:d=30,{INDOOR}[b];"
                   "[a][b]concat=n=2:v=1:a=0;sine=f=440:d=60,volume='if(between(t,22,34),1,0.05)':eval=frame")
    pid = db.insert("projects", name="p", settings={}, created_at=db.now())
    sid = db.insert("sources", project_id=pid, kind="upload", name="walk", ext=".mp4", status="queued",
                    meta={"length": "short", "max_clips": 3, "auto_render": False}, created_at=db.now())
    source_dir(sid).mkdir(parents=True)
    shutil.copy(video, source_dir(sid) / "original.mp4")
    jobs.enqueue("analyze_source", sid)
    while jobs.run_one():
        pass
    assert db.one("SELECT status FROM sources WHERE id = ?", sid)["status"] == "ready"
    saved = json.loads((source_dir(sid) / "scenes.json").read_text())
    assert saved["context_changes"] == [30.0]
    clips = db.rows('SELECT start, "end" FROM clips WHERE source_id = ?', sid)
    assert clips, "clips are still found"
    assert all(not scenes.crossing(saved, c["start"], c["end"]) for c in clips), clips
