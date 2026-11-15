"""R2 + B2 storage against a real S3-compatible server (moto), two buckets standing in for the two providers:
uploads, signed downloads, verified backups, restore, retention, outages and quota. No real cloud is called."""
import json
import socket
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer

from app import db, files, jobs, main, storage
from app.config import Bucket, clip_dir, source_dir


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def s3(override):
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    url = f"http://127.0.0.1:{port}"
    urllib.request.urlopen(urllib.request.Request(f"{url}/moto-api/reset", method="POST")).close()  # moto state is per process
    client = boto3.client("s3", endpoint_url=url, region_name="us-east-1",
                          aws_access_key_id="test", aws_secret_access_key="test")
    for name in ("r2-bucket", "b2-bucket"):
        client.create_bucket(Bucket=name)
    buckets = {"r2": Bucket(url, "test", "r2-bucket", "us-east-1", "r2-secret-value"),
               "b2": Bucket(url, "test", "b2-bucket", "us-east-1", "b2-secret-value")}
    override(buckets=buckets, storage_primary="r2", storage_backup="b2",
             source_keep_hours=24, clip_keep_days=7, backup_keep_days=30, storage_stop_at_limit=True)
    storage.provider.cache_clear()
    db.execute("DELETE FROM files")
    db.execute("DELETE FROM jobs")
    yield client, buckets
    storage.provider.cache_clear()
    server.stop()


def _keys(client, bucket: str) -> list[str]:
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket=bucket).get("Contents", []))


def _clip_file(cid: int, data: bytes = b"\x00\x00\x00 ftypisom fake mp4 " * 50):
    path = clip_dir(cid) / "clip.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _drain():
    while jobs.run_one():
        pass


def _age(row_id: int, **fields):
    db.update("files", row_id, **fields)


def test_both_providers_round_trip(s3, tmp_path):
    client, _ = s3
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello storage" * 1000)
    for name in ("r2", "b2"):
        store = storage.provider(name)
        assert isinstance(store, {"r2": storage.CloudflareR2Storage, "b2": storage.BackblazeB2Storage}[name])
        size, md5 = store.put("t/a.bin", src, "application/octet-stream")
        assert store.verify("t/a.bin", size, md5) and not store.verify("t/a.bin", size, "0" * 32)
        with urllib.request.urlopen(store.url("t/a.bin")) as resp:  # the signed URL works without credentials
            assert resp.read() == src.read_bytes()
        store.get("t/a.bin", tmp_path / name / "back.bin")
        assert (tmp_path / name / "back.bin").read_bytes() == src.read_bytes()
        store.delete("t/a.bin")
        store.delete("t/a.bin")  # deleting twice is fine
        assert store.stat("t/a.bin") is None


def test_clip_is_stored_backed_up_and_verified_once(s3, monkeypatch):
    client, _ = s3
    path = _clip_file(9001)
    row = files.save("clip_video", 9001, path, expires_at=db.now() + 7 * 86400)
    assert row["upload_status"] == "uploaded" and row["backup_status"] == "pending"
    assert _keys(client, "r2-bucket") == ["clips/9001/clip.mp4"] and _keys(client, "b2-bucket") == []
    _drain()
    assert db.one("SELECT backup_status FROM files WHERE id = ?", row["id"])["backup_status"] == "verified"
    assert _keys(client, "b2-bucket") == ["clips/9001/clip.mp4"]

    # Running the backup again, or saving the same content again, makes no second copy and no new upload.
    files.backup_file(row["id"])
    monkeypatch.setattr(storage.ObjectStorage, "put", lambda *a, **k: pytest.fail("uploaded unchanged content again"))
    files.save("clip_video", 9001, path)
    assert db.one("SELECT COUNT(*) AS n FROM files")["n"] == 2  # one primary row, one backup row
    assert files.state("clip", 9001) == {"location": "primary", "provider": "r2", "backup": "verified"}


def test_a_backup_that_fails_verification_is_retried_without_duplicates(s3, monkeypatch):
    client, _ = s3
    row = files.save("clip_video", 9002, _clip_file(9002))
    b2 = storage.provider("b2")
    monkeypatch.setattr(b2, "verify", lambda *a: False)
    _drain()
    assert db.one("SELECT backup_status FROM files WHERE id = ?", row["id"])["backup_status"] == "failed"
    monkeypatch.undo()
    _age(row["id"], updated_at=db.now() - 3600)
    files.cleanup()  # queues the backup again
    _drain()
    assert db.one("SELECT backup_status FROM files WHERE id = ?", row["id"])["backup_status"] == "verified"
    assert _keys(client, "b2-bucket") == ["clips/9002/clip.mp4"]
    assert db.one("SELECT COUNT(*) AS n FROM files WHERE storage_provider = 'b2'")["n"] == 1


def test_r2_outage_keeps_the_local_file_and_uploads_later(s3, override):
    client, buckets = s3
    override(buckets={**buckets, "r2": replace(buckets["r2"], endpoint=f"http://127.0.0.1:{_free_port()}")})
    storage.provider.cache_clear()
    path = _clip_file(9003)
    row = files.save("clip_video", 9003, path)
    assert row["upload_status"] == "failed" and "r2" in row["error"] and path.exists()
    assert "secret" not in row["error"]

    override(buckets=buckets)  # R2 is back
    storage.provider.cache_clear()
    _age(row["id"], updated_at=db.now() - 3600)
    files.cleanup()
    assert db.one("SELECT upload_status FROM files WHERE id = ?", row["id"])["upload_status"] == "uploaded"
    assert _keys(client, "r2-bucket") == ["clips/9003/clip.mp4"]


def test_restore_from_backup_when_the_primary_copy_is_gone(s3):
    client, _ = s3
    path = _clip_file(9004)
    original = path.read_bytes()
    row = files.save("clip_video", 9004, path)
    _drain()
    client.delete_object(Bucket="r2-bucket", Key="clips/9004/clip.mp4")  # lost or expired from R2
    db.execute("DELETE FROM files WHERE id = ?", row["id"])
    path.unlink()
    assert files.state("clip", 9004)["location"] == "backup" and files.url("clip_video", 9004) is None

    assert files.restore("clip", 9004) == 1
    _drain()
    assert files.state("clip", 9004) == {"location": "primary", "provider": "r2", "backup": "verified"}
    assert client.get_object(Bucket="r2-bucket", Key="clips/9004/clip.mp4")["Body"].read() == original


def test_retention_never_deletes_active_files_or_a_last_copy(s3):
    client, _ = s3
    now = db.now()
    pid = db.insert("projects", name="p", settings={}, created_at=now)
    sid = db.insert("sources", project_id=pid, kind="upload", name="v", ext=".mp4", status="processing",
                    meta={}, created_at=now)
    src_path = source_dir(sid) / "original.mp4"
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_bytes(b"source video")
    files.save("source_video", sid, src_path)

    files.cleanup()
    assert db.one("SELECT expires_at FROM files WHERE owner_kind = 'source'")["expires_at"] is None, \
        "the 24-hour clock starts only when processing ends"
    db.update("sources", sid, status="ready")
    files.cleanup()
    assert db.one("SELECT expires_at FROM files WHERE owner_kind = 'source'")["expires_at"] > now + 23 * 3600
    db.execute("UPDATE files SET expires_at = ? WHERE owner_kind = 'source'", now - 1)
    db.update("sources", sid, status="processing")  # someone pressed "Find clips again"
    files.cleanup()
    assert _keys(client, "r2-bucket") == [f"sources/{sid}/original.mp4"], "never delete an active job's files"
    db.update("sources", sid, status="ready")
    files.cleanup()
    assert _keys(client, "r2-bucket") == [] and not source_dir(sid).exists()

    # A clip past its R2 retention with a verified backup: the R2 copy goes, the backup stays.
    files.save("clip_video", 9005, _clip_file(9005), expires_at=now - 1)
    _drain()
    # A clip past its R2 retention without a verified backup: it is the last copy, so it stays.
    lonely = files.save("clip_video", 9006, _clip_file(9006), expires_at=now - 1)
    db.execute("DELETE FROM jobs")
    _age(lonely["id"], backup_status="failed")
    files.cleanup()
    assert files.state("clip", 9005)["location"] == "backup"
    assert "clips/9006/clip.mp4" in _keys(client, "r2-bucket")

    # Old backups go only while R2 still has the clip; 9005's backup is now its last copy.
    db.execute("UPDATE files SET created_at = ? WHERE storage_provider = 'b2'", now - 40 * 86400)
    files.cleanup()
    assert _keys(client, "b2-bucket") == ["clips/9005/clip.mp4"]

    # The user deleting a clip removes every copy, last ones included.
    files.delete_all("clip", 9005)
    files.delete_all("clip", 9006)
    assert _keys(client, "b2-bucket") == [] and "clips/9006/clip.mp4" not in _keys(client, "r2-bucket")


def test_free_allowance_warns_at_80_percent_and_stops_at_100(s3, override):
    client, buckets = s3
    override(buckets={**buckets, "r2": replace(buckets["r2"], free_bytes=4000)})
    storage.provider.cache_clear()
    files.save("clip_video", 9007, _clip_file(9007, b"x" * 3300))
    q = files.quota()["r2"]
    assert q["warning"] and not q["full"] and q["role"] == "primary"
    row = files.save("clip_video", 9008, _clip_file(9008, b"y" * 1000))
    assert row["upload_status"] == "failed" and "free" in row["error"]
    assert _keys(client, "r2-bucket") == ["clips/9007/clip.mp4"], "nothing past the free allowance"


def test_api_serves_signed_urls_and_keeps_credentials_server_side(s3):
    client, _ = s3
    pid = db.insert("projects", name="p", settings={}, created_at=db.now())
    sid = db.insert("sources", project_id=pid, kind="upload", name="v", ext=".mp4", status="ready", meta={},
                    created_at=db.now())
    cid = db.insert("clips", source_id=sid, start=0, end=10, score=50, title="My Clip", template="viral",
                    status="ready", created_at=db.now(), rendered_at=db.now())
    files.save("clip_video", cid, _clip_file(cid))
    with TestClient(main.create_app(background=False), base_url="http://localhost:8000") as api:
        r = api.get(f"/api/files/clips/{cid}/clip.mp4", follow_redirects=False)
        assert r.status_code == 307 and "r2-bucket" in r.headers["location"] and "Signature" in r.headers["location"]
        d = api.get(f"/api/clips/{cid}/download", follow_redirects=False)
        assert "response-content-disposition" in d.headers["location"] and "my-clip" in d.headers["location"]
        assert api.get(f"/api/files/clips/{cid}/../../config.py").status_code == 404
        system = api.get("/api/system").text
        assert "r2-secret-value" not in system and "b2-secret-value" not in system
        assert json.loads(system)["storage"]["providers"]["r2"]["role"] == "primary"
        clip = api.get(f"/api/clips/{cid}").json()
        assert clip["storage"]["location"] == "primary" and "secret" not in json.dumps(clip)

        # Only in the backup archive: no URL until restored.
        _drain()
        client.delete_object(Bucket="r2-bucket", Key=f"clips/{cid}/clip.mp4")
        db.execute("DELETE FROM files WHERE storage_provider = 'r2'")
        (clip_dir(cid) / "clip.mp4").unlink()
        assert api.get(f"/api/files/clips/{cid}/clip.mp4").status_code == 409
        assert api.get(f"/api/clips/{cid}").json()["video_url"] is None
        assert api.post(f"/api/clips/{cid}/restore").status_code == 200
        _drain()
        assert api.get(f"/api/files/clips/{cid}/clip.mp4", follow_redirects=False).status_code == 307


def test_real_pipeline_with_cloud_storage_and_a_fresh_machine(s3, talking_video):
    """Analyze and render a real video with R2 + B2, then render again on a machine with no local copy."""
    import shutil

    from app import pipeline

    client, _ = s3
    pid = db.insert("projects", name="cloud", settings={}, created_at=db.now())
    sid = db.insert("sources", project_id=pid, kind="upload", name="talk", ext=".mp4", status="queued",
                    meta={"length": "short", "max_clips": 1, "template": "cinematic"}, created_at=db.now())
    source_dir(sid).mkdir(parents=True)
    shutil.copy(talking_video, source_dir(sid) / "original.mp4")
    jobs.enqueue("analyze_source", sid)
    _drain()
    clip = db.one("SELECT * FROM clips WHERE source_id = ?", sid)
    assert clip and clip["status"] == "ready", clip and clip["error"]
    cid = clip["id"]
    assert _keys(client, "r2-bucket") == sorted([
        f"clips/{cid}/clip.mp4", f"clips/{cid}/thumb.jpg",
        f"sources/{sid}/loudness.npy", f"sources/{sid}/original.mp4", f"sources/{sid}/transcript.json"])
    assert _keys(client, "b2-bucket") == [f"clips/{cid}/clip.mp4", f"clips/{cid}/thumb.jpg"], "only finished clips are backed up"
    files.cleanup()
    assert not (clip_dir(cid) / "clip.mp4").exists(), "this computer keeps no copy once the clip is safely stored"

    shutil.rmtree(source_dir(sid))  # a different worker, or this one after its cache was cleared
    pipeline.queue_render(cid, db.one("SELECT * FROM sources WHERE id = ?", sid))
    _drain()
    assert db.one("SELECT status, error FROM clips WHERE id = ?", cid)["status"] == "ready"
    assert (source_dir(sid) / "original.mp4").exists(), "the source came back from R2"


def _provider_honours_expiry(monkeypatch):
    """moto serves expired signed links; R2 and B2 refuse them. Make the check's fetches behave like R2 and B2,
    judging expiry from the link's own X-Amz-Date and X-Amz-Expires, and keep the waits short."""
    real = storage._fetch

    def fetch(url):
        q = parse_qs(urlparse(url).query)
        signed = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        return (403, b"") if time.time() > signed + int(q["X-Amz-Expires"][0]) else real(url)

    monkeypatch.setattr(storage, "_fetch", fetch)
    monkeypatch.setattr(storage, "LINK_TEST_SECONDS", 2)
    monkeypatch.setattr(storage, "LINK_TEST_WAIT", 3)


def test_check_command_round_trips_both_buckets(s3, capsys, monkeypatch):
    """The live check: each bucket, link expiry, then backup R2 -> B2 and restore B2 -> R2, leaving nothing behind."""
    client, _ = s3
    _provider_honours_expiry(monkeypatch)
    assert storage.check() == 0
    out = capsys.readouterr().out
    assert "ok    r2 bucket r2-bucket: upload, verify, signed URL, link expiry, download, delete" in out
    assert "ok    b2 bucket b2-bucket" in out
    assert "ok    backup r2 -> b2 and restore b2 -> r2: size and checksum verified" in out
    assert "secret" not in out and "FAIL" not in out and "note" not in out
    assert _keys(client, "r2-bucket") == [] and _keys(client, "b2-bucket") == [], "test files are cleaned up"
    assert db.one("SELECT COUNT(*) AS n FROM files")["n"] == 0, "the check never writes to the database"
    assert not list((files.settings.data_dir / "tmp").glob("storage-check-*"))


def test_check_fails_when_an_expired_link_still_works(s3, capsys, monkeypatch):
    client, _ = s3
    monkeypatch.setattr(storage, "LINK_TEST_SECONDS", 1)
    monkeypatch.setattr(storage, "LINK_TEST_WAIT", 0)
    assert storage.check() == 1  # moto as is: the link keeps working, as a misbehaving provider would
    out = capsys.readouterr().out
    assert "FAIL  r2 bucket r2-bucket: link expiry: an expired link still worked" in out
    assert "backup r2 -> b2" not in out, "no backup test on buckets that failed"
    assert _keys(client, "r2-bucket") == [] and _keys(client, "b2-bucket") == []


def test_check_notes_b2_hidden_versions_and_removes_its_own(s3, capsys, monkeypatch):
    """B2 keeps deleted files as hidden versions that still count toward the free 10 GB."""
    client, _ = s3
    client.put_bucket_versioning(Bucket="b2-bucket", VersioningConfiguration={"Status": "Enabled"})
    _provider_honours_expiry(monkeypatch)
    assert storage.check() == 0
    out = capsys.readouterr().out
    assert "note  b2 bucket b2-bucket keeps deleted files as hidden versions" in out
    versions = client.list_object_versions(Bucket="b2-bucket")
    assert not versions.get("Versions") and not versions.get("DeleteMarkers"), "the check's own test file is fully gone"


def test_check_reports_a_failed_restore(s3, capsys, monkeypatch):
    _provider_honours_expiry(monkeypatch)
    r2, calls = storage.provider("r2"), []
    real_verify = r2.verify

    def verify(*args):
        calls.append(args)
        return real_verify(*args) and len(calls) < 2  # the bucket check passes, the restore's verify fails
    monkeypatch.setattr(r2, "verify", verify)
    assert storage.check() == 1
    assert "FAIL  restore b2 -> r2: restore verify (size and checksum)" in capsys.readouterr().out


def test_signed_links_carry_their_expiry_and_short_ones_are_not_reused(s3):
    store = storage.provider("r2")
    hour = parse_qs(urlparse(store.url("k")).query)
    assert hour["X-Amz-Expires"] == ["3600"]
    short = store.url("k", expires=5)
    assert parse_qs(urlparse(short).query)["X-Amz-Expires"] == ["5"]
    assert store.url("k") == store.url("k"), "hour-long links are reused while polling"
    assert store.url("k", expires=5) not in (store.url("k"),), "a short link never replaces the cached hour-long one"


def test_errors_carry_the_providers_explanation_but_never_the_keys(s3, override):
    from botocore.exceptions import ClientError
    client, buckets = s3
    override(buckets={**buckets, "r2": replace(buckets["r2"], bucket="no-such-bucket")})
    storage.provider.cache_clear()
    store = storage.provider("r2")
    with pytest.raises(storage.StorageError, match=r"r2: upload failed \(NoSuchBucket: The specified bucket does not exist\)"):
        store.put("k", _clip_file(9011), "video/mp4")
    leaked = ClientError({"Error": {"Code": "InvalidArgument",
                                    "Message": "Credential access key has length 31, should be 32 (test r2-secret-value)"}},
                         "PutObject")
    message = str(store._error("upload", leaked))
    assert "Credential access key has length 31, should be 32" in message
    assert "r2-secret-value" not in message and "***" in message
    assert str(store._error("upload", ConnectionResetError())) == "r2: upload failed (ConnectionResetError)"


def test_a_failed_large_upload_is_a_storage_error(s3, override, monkeypatch):
    """Files over 100 MB go up in parts, and boto3 reports a failed multipart upload with its own error type."""
    _, buckets = s3
    override(buckets={**buckets, "r2": replace(buckets["r2"], bucket="no-such-bucket")})
    storage.provider.cache_clear()
    monkeypatch.setattr(storage, "SINGLE_PUT_LIMIT", 0)
    with pytest.raises(storage.StorageError, match=r"r2: upload failed \(S3UploadFailedError: .*NoSuchBucket"):
        storage.provider("r2").put("k", _clip_file(9012), "video/mp4")


def test_a_re_render_is_never_served_stale(s3):
    client, _ = s3
    path = _clip_file(9010, b"first render" * 100)
    files.save("clip_video", 9010, path)
    first = files.url("clip_video", 9010)
    assert files.url("clip_video", 9010) == first, "a polling page keeps the same URL, so playback does not restart"
    path.write_bytes(b"second render" * 100)
    files.save("clip_video", 9010, path)
    head = client.head_object(Bucket="r2-bucket", Key="clips/9010/clip.mp4")
    assert head["CacheControl"] == "no-cache", "browsers revalidate, so the new render replaces a cached old one"
    with urllib.request.urlopen(files.url("clip_video", 9010)) as resp:
        assert resp.read() == path.read_bytes()
