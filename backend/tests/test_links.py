"""Pasted video links (downloaded with yt-dlp from a local web server) and automatic cleanup."""
import functools
import http.server
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from app import db, jobs, main, pipeline
from app.config import clip_dir, source_dir


@pytest.fixture
def client():
    with TestClient(main.create_app(background=False), base_url="http://localhost:8000") as c:
        yield c


@pytest.fixture
def site(tmp_path, talking_video):
    """A web server with one video on it, standing in for YouTube."""
    shutil.copy(talking_video, tmp_path / "talk.mp4")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _drain():
    while jobs.run_one():
        pass


def test_link_is_checked_before_it_is_queued(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    for bad in ("youtube.com/watch?v=abc", "ftp://example.com/a.mp4", "http://169.254.169.254/latest/meta-data"):
        assert client.post(f"/api/projects/{pid}/link", json={"url": bad}).status_code == 422, bad
    assert client.get(f"/api/projects/{pid}").json()["sources"] == []


def test_pasted_link_is_downloaded_and_clipped(client, site, monkeypatch):
    url = f"{site}/talk.mp4"
    pid = client.post("/api/projects", json={"name": url, "settings": {"max_clips": 1}}).json()["id"]
    r = client.post(f"/api/projects/{pid}/link", json={"url": url, "length": "short"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "queued" and r.json()["meta"]["url"] == url

    _drain()
    project = client.get(f"/api/projects/{pid}").json()
    src = project["sources"][0]
    assert src["status"] == "ready", src["error"]
    assert src["name"] == "talk" and project["name"] == "talk", "the video and project take the video's title"
    assert (source_dir(src["id"]) / "original.mp4").exists()
    assert project["clips"] and project["clips"][0]["status"] == "ready"

    # Finding clips again reuses the download.
    monkeypatch.setattr(pipeline.fetch, "download", lambda *args: pytest.fail("downloaded twice"))
    client.post(f"/api/sources/{src['id']}/reprocess")
    _drain()
    assert client.get(f"/api/projects/{pid}").json()["sources"][0]["status"] == "ready"


def test_a_bad_link_fails_with_a_readable_reason(client, site):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/link", json={"url": f"{site}/missing.mp4"}).json()["id"]
    _drain()
    src = db.one("SELECT * FROM sources WHERE id = ?", sid)
    assert src["status"] == "failed" and "404" in src["error"] and not src["error"].startswith("ERROR")


def test_old_videos_and_empty_projects_are_deleted():
    day_ago = db.now() - 25 * 3600

    def source(pid, created_at, status="ready", clip_status="ready"):
        sid = db.insert("sources", project_id=pid, kind="upload", name="v", ext=".mp4", status=status, meta={},
                        created_at=created_at)
        cid = db.insert("clips", source_id=sid, start=0, end=10, score=50, title="t", template="viral",
                        status=clip_status, created_at=created_at)
        for folder in (source_dir(sid), clip_dir(cid)):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "file").write_bytes(b"x")
        return sid, cid

    old_project = db.insert("projects", name="old", settings={}, created_at=day_ago)
    old, old_clip = source(old_project, day_ago)
    busy_project = db.insert("projects", name="busy", settings={}, created_at=day_ago)
    analyzing, _ = source(busy_project, day_ago, status="processing")
    rendering, _ = source(busy_project, day_ago, clip_status="rendering")
    new_project = db.insert("projects", name="new", settings={}, created_at=db.now())
    recent, _ = source(new_project, db.now())
    channel_project = db.insert("projects", name="Live: x", settings={}, created_at=day_ago)
    db.insert("channels", project_id=channel_project, platform="direct", url="http://127.0.0.1/x.m3u8", name="x", template="viral",
              created_at=day_ago)
    empty_new = db.insert("projects", name="just made", settings={}, created_at=db.now())

    pipeline.expire(24)

    exists = lambda table, row_id: db.one(f"SELECT id FROM {table} WHERE id = ?", row_id) is not None  # noqa: E731
    assert not exists("sources", old) and not source_dir(old).exists() and not clip_dir(old_clip).exists()
    assert not exists("projects", old_project), "a project left empty goes too"
    assert all(exists("sources", s) for s in (analyzing, rendering, recent)), "never delete what a job is using"
    assert all(exists("projects", p) for p in (busy_project, new_project, channel_project, empty_new))

    pipeline.expire(0)  # KEEP_HOURS=0 keeps everything
    assert exists("sources", recent)
