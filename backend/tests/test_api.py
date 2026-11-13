"""HTTP boundary and job queue behavior."""
import base64
import threading

import pytest
from fastapi.testclient import TestClient

from app import db, jobs, main
from app.config import source_dir


@pytest.fixture
def client():
    with TestClient(main.create_app(background=False), base_url="http://localhost:8000") as c:
        yield c


def _project(client, **settings) -> int:
    return client.post("/api/projects", json={"name": "Talks", "settings": settings}).json()["id"]


def test_project_crud(client):
    pid = _project(client, length="short", max_clips=4)
    assert client.get(f"/api/projects/{pid}").json()["settings"]["max_clips"] == 4
    assert client.patch(f"/api/projects/{pid}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert any(p["id"] == pid for p in client.get("/api/projects").json())
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_invalid_settings_rejected(client):
    r = client.post("/api/projects", json={"name": "x", "settings": {"template": "nope"}})
    assert r.status_code == 422
    assert client.post("/api/projects", json={"name": ""}).status_code == 422


def test_upload_streams_to_disk_and_queues_analysis(client, talking_video):
    pid = _project(client)
    r = client.post(f"/api/projects/{pid}/upload", params={"filename": "My Talk.mp4", "length": "short"},
                    content=talking_video.read_bytes())
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["status"] == "queued" and src["name"] == "My Talk" and src["meta"]["length"] == "short"
    assert (source_dir(src["id"]) / "original.mp4").stat().st_size == talking_video.stat().st_size
    assert db.one("SELECT * FROM jobs WHERE kind = 'analyze_source' AND target_id = ?", src["id"])["status"] == "queued"
    db.execute("DELETE FROM jobs")


def test_upload_rejects_bad_type_empty_and_oversize(client, override):
    pid = _project(client)
    assert client.post(f"/api/projects/{pid}/upload", params={"filename": "a.exe"}, content=b"x").status_code == 400
    assert client.post(f"/api/projects/{pid}/upload", params={"filename": "a.mp4"}, content=b"").status_code == 400
    override(max_upload_bytes=10)
    assert client.post(f"/api/projects/{pid}/upload", params={"filename": "a.mp4"}, content=b"x" * 11).status_code == 413
    # Failed uploads leave no rows and no files behind.
    assert client.get(f"/api/projects/{pid}").json()["sources"] == []


def test_clip_edit_marks_draft_and_validates_range(client):
    pid = _project(client)
    sid = db.insert("sources", project_id=pid, kind="upload", name="v", ext=".mp4", status="ready", duration=100,
                    created_at=db.now())
    cid = db.insert("clips", source_id=sid, start=10, end=40, template="viral", status="ready", created_at=db.now(),
                    rendered_at=1.0)
    r = client.patch(f"/api/clips/{cid}", json={"title": "New", "hashtags": ["fun stuff", "#ok"]})
    assert r.json()["status"] == "ready" and r.json()["hashtags"] == ["#funstuff", "#ok"]
    assert client.patch(f"/api/clips/{cid}", json={"end": 95}).json()["status"] == "draft"
    assert client.patch(f"/api/clips/{cid}", json={"end": 300}).status_code == 422
    assert client.patch(f"/api/clips/{cid}", json={"start": 94}).status_code == 422
    assert client.post(f"/api/clips/{cid}/render").json()["status"] == "queued"
    db.execute("DELETE FROM jobs")


def test_templates_and_system(client):
    ids = [t["id"] for t in client.get("/api/templates").json()]
    assert "viral" in ids and len(ids) >= 5
    info = client.get("/api/system").json()
    assert info["whisper_model"] and info["llm"]["enabled"] is False


def test_channel_creates_its_project(client):
    r = client.post("/api/channels", json={"url": "twitch.tv/SomeStreamer"})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["platform"] == "twitch" and ch["name"] == "SomeStreamer"
    assert db.one("SELECT name FROM projects WHERE id = ?", ch["project_id"])["name"] == "Live: SomeStreamer"
    assert client.post("/api/channels", json={"url": "https://www.twitch.tv/somestreamer"}).status_code == 409
    assert client.post("/api/channels", json={"url": "not a url"}).status_code == 422
    listed = client.get("/api/channels").json()
    assert listed[0]["runtime"]["status"] == "off"  # live monitoring is disabled in tests
    assert client.patch(f"/api/channels/{ch['id']}", json={"sensitivity": "high"}).json()["sensitivity"] == "high"
    assert client.delete(f"/api/channels/{ch['id']}").status_code == 204


def test_password_protects_everything(override):
    override(password="s3cret")
    with TestClient(main.create_app(background=False), base_url="http://clips.example.com") as c:
        assert c.get("/api/projects").status_code == 401
        good = base64.b64encode(b"any:s3cret").decode()
        bad = base64.b64encode(b"any:wrong").decode()
        assert c.get("/api/projects", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        assert c.get("/api/projects", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert c.get("/media/x", headers={"Authorization": "Basic !!!"}).status_code == 401


class TestQueue:
    def setup_method(self):
        db.execute("DELETE FROM jobs")
        self.ran: list[int] = []
        self.failed: list[tuple[int, str]] = []
        jobs.register("test", self._run, lambda t, e: self.failed.append((t, e)))

    def _run(self, target: int) -> None:
        if target < 0:
            raise ValueError("boom")
        self.ran.append(target)

    def test_two_workers_never_share_a_job(self):
        for i in range(40):
            jobs.enqueue("test", i)
        threads = [threading.Thread(target=lambda: [None for _ in iter(jobs.run_one, False)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(self.ran) == list(range(40))

    def test_duplicate_requests_merge(self):
        jobs.enqueue("test", 7)
        jobs.enqueue("test", 7)
        assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE target_id = 7")["n"] == 1

    def test_failure_marks_target(self):
        jobs.enqueue("test", -1)
        jobs.run_one()
        assert self.failed == [(-1, "boom")]
        assert db.one("SELECT status FROM jobs WHERE target_id = -1")["status"] == "failed"

    def test_crashed_jobs_resume_then_give_up(self):
        jobs.enqueue("test", 5)
        jobs.claim()  # the process "dies" here
        jobs.recover_stale()
        assert db.one("SELECT status FROM jobs WHERE target_id = 5")["status"] == "queued"
        db.execute("UPDATE jobs SET status = 'running', attempts = 2 WHERE target_id = 5")
        jobs.recover_stale()
        assert self.failed == [(5, "stopped after repeated crashes")]

    def test_priority_goes_first(self):
        jobs.enqueue("test", 1)
        jobs.enqueue("test", 2, priority=jobs.LIVE_PRIORITY)
        jobs.run_one()
        assert self.ran == [2]


def test_browser_boundary(client, override):
    # Without a password, only localhost answers, so a DNS-rebinding page cannot read anything.
    assert client.get("/api/projects", headers={"Host": "attacker.example"}).status_code == 403
    # Another website cannot post to the API, with or without a password.
    pid = _project(client)
    evil = {"Origin": "http://evil.example", "Content-Type": "text/plain"}
    assert client.post(f"/api/projects/{pid}/upload", params={"filename": "a.mp4"}, content=b"x", headers=evil).status_code == 403
    same = {"Origin": "http://localhost:8000"}
    assert client.post("/api/projects", json={"name": "ok"}, headers=same).status_code == 201


def test_find_clips_again_uses_current_settings(client):
    pid = _project(client, length="short", max_clips=3)
    sid = db.insert("sources", project_id=pid, kind="upload", name="v", ext=".mp4", status="ready",
                    meta={"length": "medium", "max_clips": 8, "template": "viral"}, created_at=db.now())
    assert client.post(f"/api/sources/{sid}/reprocess").json()["meta"]["length"] == "short"
    assert db.one("SELECT status FROM jobs WHERE kind = 'analyze_source' AND target_id = ?", sid)["status"] == "queued"
    db.execute("DELETE FROM jobs")


def test_channel_flags_are_booleans(client):
    ch = client.post("/api/channels", json={"url": "kick.com/someone"}).json()
    assert ch["auto_record"] is True and ch["enabled"] is True
    assert client.patch(f"/api/channels/{ch['id']}", json={"chat": False}).json()["chat"] is False
    client.delete(f"/api/channels/{ch['id']}")
