"""A real livestream, end to end: an ffmpeg-served HLS stream with a shout in it becomes a rendered clip."""
import functools
import http.server
import shutil
import subprocess
import threading
import time

import pytest

from app import db, jobs, pipeline  # noqa: F401  (pipeline registers the job handlers)
from app.config import LIVE_DIR, clip_dir
from app.live import supervisor
from app.media import probe
from conftest import ASSETS


@pytest.fixture
def live_stream(tmp_path):
    """HLS stream played in real time: five normal takes of a speech, then one shouted 18 dB louder, looping.
    Shouts are rare on a real stream, so the test stream keeps them rare too."""
    clip = tmp_path / "loop.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x1d2733:s=640x360:r=30:d=66",
         "-loop", "1", "-i", str(ASSETS / "astronaut.png"), "-i", str(ASSETS / "jfk.flac"),
         "-filter_complex", "[1:v]scale=200:-1[f];[0:v][f]overlay=x=220:y=80:shortest=1[v];"
                            "[2:a]asplit=6[a][b][c][d][e][f];[f]volume=18dB[fl];"
                            "[a][b][c][d][e][fl]concat=n=6:v=0:a=1[au]",
         "-map", "[v]", "-map", "[au]", "-t", "66", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
    hls = tmp_path / "hls"
    hls.mkdir()
    streamer = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-re", "-stream_loop", "-1", "-i", str(clip), "-c", "copy",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "10", "-hls_flags", "delete_segments+omit_endlist",
         str(hls / "live.m3u8")])
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(hls))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + 20
    while not (hls / "live.m3u8").exists() and time.time() < deadline:
        time.sleep(0.2)
    yield f"http://127.0.0.1:{server.server_address[1]}/live.m3u8"
    server.shutdown()
    streamer.kill()
    streamer.wait()


def test_shout_on_stream_becomes_a_rendered_clip(live_stream, monkeypatch):
    for name, value in {"WARMUP": 8.0, "COOLDOWN": 30.0, "PRE_ROLL": 12.0, "POST_ROLL": 4.0,
                        "ANALYSIS_DELAY": 3}.items():
        monkeypatch.setattr(supervisor, name, value)
    pid = db.insert("projects", name="Live: test", settings={}, created_at=db.now())
    cid = db.insert("channels", project_id=pid, platform="direct", url=live_stream, name="test", chat=0, speech=1,
                    sensitivity="high", template="viral", created_at=db.now())
    worker = supervisor.ChannelWorker(cid)
    worker.start()
    try:
        deadline = time.time() + 180
        source = None
        while time.time() < deadline and source is None:
            time.sleep(1)
            source = db.one("SELECT * FROM sources WHERE channel_id = ? AND kind = 'live'", cid)
        assert source, f"no moment detected; worker state: {worker.state.as_dict()}"
        assert worker.state.status == "recording"
        assert "Audio spike" in source["meta"]["moment"]["reasons"]
    finally:
        worker.stop()
        worker.join(timeout=30)
    assert not worker.is_alive()
    assert not any((LIVE_DIR / str(cid)).glob("*/seg_*.ts")), "the rolling buffer should be deleted when recording stops"

    while jobs.run_one():
        pass
    clip = db.one("SELECT * FROM clips c JOIN sources s ON s.id = c.source_id WHERE s.channel_id = ?", cid)
    assert clip and clip["status"] == "ready", clip and clip["error"]
    assert "On stream: Audio spike" in clip["reasons"]
    info = probe(clip_dir(clip["id"]) / "clip.mp4")
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    shutil.rmtree(LIVE_DIR / str(cid), ignore_errors=True)
