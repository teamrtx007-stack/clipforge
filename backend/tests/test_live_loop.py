"""The live recording loop, fed at full speed by a fake recorder: no network, no ffmpeg, under a second."""
import numpy as np

from app import db
from app.live import platforms, supervisor
from app.live.recorder import Recorder, Segment

SECONDS, SHOUT = 200, range(120, 126)


class FakeRecorder:
    """Noise with one shout. Segments appear two seconds at a time, as ffmpeg's would."""

    def __init__(self, target, folder):
        self.folder, self.t, self.segments = folder, 0, []
        folder.mkdir(parents=True, exist_ok=True)
        self.rng = np.random.default_rng(0)

    def read_second(self):
        if self.t >= SECONDS:
            return None
        amp = 0.6 if self.t in SHOUT else 0.05
        self.t += 1
        return (self.rng.normal(0, amp, 16000).clip(-1, 1) * 32767).astype(np.int16).tobytes()

    def poll_segments(self):
        while (len(self.segments) + 1) * 2 <= self.t:
            i = len(self.segments)
            path = self.folder / f"seg_{i:06d}.ts"
            path.write_bytes(b"")
            self.segments.append(Segment(path, i * 2.0, i * 2.0 + 2))
        return self.segments

    window = Recorder.window
    prune = Recorder.prune

    def stop(self):
        return ""


def test_a_shout_is_cut_while_the_stream_is_still_recording(monkeypatch):
    monkeypatch.setattr(supervisor, "Recorder", FakeRecorder)
    monkeypatch.setattr(supervisor.media, "concat", lambda parts, out: out.write_bytes(b"x"))
    cut_at: list[int] = []
    pid = db.insert("projects", name="Live: loop", settings={}, created_at=db.now())
    cid = db.insert("channels", project_id=pid, platform="direct", url="http://127.0.0.1:1/loop.m3u8", name="loop",
                    chat=0, speech=0, sensitivity="medium", template="viral", created_at=db.now())
    worker = supervisor.ChannelWorker(cid)
    monkeypatch.setattr(supervisor.pipeline, "queue_analysis", lambda src: cut_at.append(worker._recorder.t))

    ch = db.one("SELECT * FROM channels WHERE id = ?", cid)
    worker._record(ch, platforms.Target("direct", ch["url"], ch["name"]))

    assert worker.state.moments == 1, "one shout, one moment; noise alone must not fire"
    assert cut_at and cut_at[0] < SECONDS, "the moment must be cut during the stream, not when it ends"
    source = db.one("SELECT * FROM sources WHERE channel_id = ?", cid)
    assert source["kind"] == "live" and "Audio spike" in source["meta"]["moment"]["reasons"]
    assert 0 < source["meta"]["peak"] < supervisor.PRE_ROLL + supervisor.POST_ROLL
