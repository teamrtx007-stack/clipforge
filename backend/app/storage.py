"""Object storage over the S3 API: Cloudflare R2 (primary) and Backblaze B2 (backup).

Buckets are private. The browser only ever gets short-lived signed URLs; credentials stay on the server
(this process's environment) and never appear in errors, logs or API answers.
Run `python -m app.storage` to check each configured bucket, and the backup and restore between them, with
small throwaway test files that are deleted at the end. It never touches real videos or the database.
"""
import base64
import functools
import hashlib
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
from boto3.exceptions import Boto3Error
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import media
from .config import Bucket, settings

SINGLE_PUT_LIMIT = 100 * 1024 * 1024  # up to here one PUT with Content-MD5; above, multipart
URL_SECONDS = 3600
LINK_TEST_SECONDS = 5  # the check signs a link valid this long, then makes sure the provider refuses it after
LINK_TEST_WAIT = 10  # seconds to wait before trying the expired link: tolerates this computer's clock being a bit off


class StorageError(Exception):
    """A provider could not be reached or refused the request. Safe to show: names the provider, not secrets."""


class StorageFull(StorageError):
    """The upload would pass the provider's free allowance, and STORAGE_STOP_AT_LIMIT is on."""


def md5_of(path: Path) -> str:
    digest = hashlib.md5()  # the checksum S3-compatible APIs verify on upload; not used for security
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ObjectStorage:
    """One private S3-compatible bucket."""

    name = "s3"

    def __init__(self, bucket: Bucket):
        self.config = bucket
        self.bucket = bucket.bucket
        self._client = boto3.client(
            "s3", endpoint_url=bucket.endpoint, region_name=bucket.region,
            aws_access_key_id=bucket.key_id, aws_secret_access_key=bucket.secret,
            config=Config(
                signature_version="s3v4", connect_timeout=10, read_timeout=120,
                retries={"max_attempts": 4, "mode": "standard"},
                # boto3's default extra checksums are not supported by every S3-compatible service.
                request_checksum_calculation="when_required", response_checksum_validation="when_required",
            ),
        )
        self._urls: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _error(self, what: str, exc: Exception) -> StorageError:
        """A readable error with the provider's own explanation, e.g. "(InvalidArgument: Credential access key has
        length 31, should be 32)". The key id and secret are cut out in case a provider ever echoes them."""
        if isinstance(exc, ClientError):
            err = exc.response.get("Error") or {}
            code = err.get("Code") or str((exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode", "error"))
            detail = f"{code}: {err['Message']}" if err.get("Message") and err["Message"] != code else code
        else:
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        for secret in (self.config.secret, self.config.key_id):
            if secret:
                detail = detail.replace(secret, "***")
        return StorageError(f"{self.name}: {what} failed ({detail[:300]})")

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ClientError, BotoCoreError, Boto3Error, OSError) as exc:  # Boto3Error: a failed multipart upload
            raise self._error(what, exc) from exc

    def put(self, key: str, path: Path, content_type: str, md5: str | None = None) -> tuple[int, str]:
        """Upload a whole file and return (size, md5). Files up to 100 MB carry Content-MD5, so the provider
        itself rejects a corrupted upload; bigger ones go up in parts. Either way the object appears whole or not
        at all, so a failed upload never leaves half a file under the key."""
        size, md5 = path.stat().st_size, md5 or md5_of(path)
        # no-cache: browsers check with the bucket before reusing a copy, so a re-rendered clip is never stale.
        meta = {"ContentType": content_type, "CacheControl": "no-cache", "Metadata": {"md5": md5}}
        if size <= SINGLE_PUT_LIMIT:
            with path.open("rb") as body:
                self._call("upload", self._client.put_object, Bucket=self.bucket, Key=key, Body=body,
                           ContentMD5=base64.b64encode(bytes.fromhex(md5)).decode(), **meta)
        else:
            self._call("upload", self._client.upload_file, str(path), self.bucket, key, ExtraArgs=meta,
                       Config=TransferConfig(multipart_chunksize=64 * 1024 * 1024))
        return size, md5

    def stat(self, key: str) -> tuple[int, str | None] | None:
        """(size, md5 recorded at upload) of an object, or None if there is no such object."""
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._error("check", exc) from exc
        except (BotoCoreError, OSError) as exc:
            raise self._error("check", exc) from exc
        return head["ContentLength"], head.get("Metadata", {}).get("md5")

    def verify(self, key: str, size: int, md5: str) -> bool:
        """The object exists with the expected size and checksum."""
        return self.stat(key) == (size, md5)

    def get(self, key: str, dest: Path) -> None:
        """Download to `dest`, which only appears once the download is complete."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with media.writing(dest) as tmp:
            self._call("download", self._client.download_file, self.bucket, key, str(tmp))

    def delete(self, key: str) -> None:
        self._call("delete", self._client.delete_object, Bucket=self.bucket, Key=key)  # no error if already gone

    def old_versions(self, key: str) -> int:
        """How many hidden old versions of a deleted key the bucket still keeps (and bills). 0 where the
        provider has no versioning (R2)."""
        return 0

    def purge(self, key: str) -> None:
        """Delete a key and any old versions of it. Only used for the check's own test files."""
        self.delete(key)

    def url(self, key: str, filename: str | None = None, expires: int = URL_SECONDS) -> str:
        """A signed GET URL valid for an hour. Reused for half an hour, so a page that polls keeps the same
        URL and a playing video does not restart. Signing happens locally: no request is made."""
        cache_key = (key, filename)
        with self._lock:
            cached = self._urls.get(cache_key)
            if expires == URL_SECONDS and cached and time.time() - cached[1] < URL_SECONDS / 2:
                return cached[0]
            params = {"Bucket": self.bucket, "Key": key}
            if filename:
                params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
            url = self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
            if expires != URL_SECONDS:
                return url
            self._urls[cache_key] = (url, time.time())
            if len(self._urls) > 5000:
                self._urls.clear()
            return url


class CloudflareR2Storage(ObjectStorage):
    name = "r2"


class BackblazeB2Storage(ObjectStorage):
    name = "b2"

    def _versions(self, key: str) -> list[dict]:
        found = self._call("version listing", self._client.list_object_versions, Bucket=self.bucket, Prefix=key)
        return [v for v in found.get("Versions", []) + found.get("DeleteMarkers", []) if v["Key"] == key]

    def old_versions(self, key: str) -> int:
        """Deleting a B2 file only hides it, and a hidden file still counts toward the 10 GB: forever with the
        default lifecycle "Keep all versions", about a day with "Keep only the last version"."""
        return sum(1 for v in self._versions(key) if "Size" in v)

    def purge(self, key: str) -> None:
        self.delete(key)
        for v in self._versions(key):
            self._call("delete", self._client.delete_object, Bucket=self.bucket, Key=key, VersionId=v["VersionId"])


PROVIDERS: dict[str, type[ObjectStorage]] = {"r2": CloudflareR2Storage, "b2": BackblazeB2Storage}


@functools.cache
def provider(name: str) -> ObjectStorage:
    return PROVIDERS[name](settings.buckets[name])


def primary() -> ObjectStorage | None:
    return None if settings.storage_primary == "local" else provider(settings.storage_primary)


def backup() -> ObjectStorage | None:
    return provider(settings.storage_backup) if settings.storage_backup else None


def _fetch(url: str) -> tuple[int, bytes]:
    """GET a URL the way a browser would, without credentials: (HTTP status, body)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


class CheckFailed(Exception):
    pass


def _expect(ok: bool, step: str) -> None:
    if not ok:
        raise CheckFailed(step)


def check() -> int:
    """Check every configured bucket with throwaway files under clipforge-check/, deleted at the end:
    upload, verify, signed URL, link expiry, download and delete in each bucket; then, with both buckets,
    a backup from primary to backup storage and a restore back. Never touches real videos or the database."""
    if not settings.buckets:
        print("No bucket is configured: files stay on this computer. Set the R2_* and B2_* variables.")
        return 0
    print(f"Primary: {settings.storage_primary}. Backup: {settings.storage_backup or 'none'}.")
    run = uuid.uuid4().hex
    work = settings.data_dir / "tmp" / f"storage-check-{run}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "test.txt"
    src.write_text(f"ClipForge storage check {run}\n")
    data, failed, notes = src.read_bytes(), 0, []
    stores = {name: provider(name) for name in settings.buckets}
    key = f"clipforge-check/{run}.txt"

    def fail(label: str, exc: Exception) -> None:
        nonlocal failed
        failed += 1
        reason = str(exc)
        if isinstance(exc, CheckFailed) and exc.args[0] == "link expiry":
            reason = "link expiry: an expired link still worked (or this computer's clock is off by over 5 s)"
        print(f"  FAIL  {label}: {reason}")

    try:
        # 1. Each bucket: upload, verify, a signed link that works, and a short one that must stop working.
        short, passed, healthy = {}, [], []
        for name, store in stores.items():
            try:
                size, md5 = store.put(key, src, "text/plain")
                _expect(store.verify(key, size, md5), "verify after upload")
                _expect(_fetch(store.url(key)) == (200, data), "signed URL download")
                short[name] = store.url(key, expires=LINK_TEST_SECONDS)
                _expect(_fetch(short[name])[0] == 200, "fresh short link (is this computer's clock right?)")
                passed.append(name)
            except (StorageError, CheckFailed) as exc:
                fail(f"{name} bucket {store.bucket}", exc)
        if short:
            time.sleep(LINK_TEST_WAIT)
        for name in passed:
            store = stores[name]
            try:
                _expect(_fetch(short[name])[0] in (400, 401, 403), "link expiry")
                store.get(key, work / f"{name}.back")
                _expect((work / f"{name}.back").read_bytes() == data, "download")
                store.delete(key)
                _expect(store.stat(key) is None, "delete")
                if store.old_versions(key):
                    notes.append(f"{name} bucket {store.bucket} keeps deleted files as hidden versions, which count "
                                 "toward the free 10 GB until its lifecycle removes them. Make sure its Lifecycle "
                                 "Settings say 'Keep only the last version' (they cannot be read from here)")
                healthy.append(name)
                print(f"  ok    {name} bucket {store.bucket}: upload, verify, signed URL, link expiry, download, delete")
            except (StorageError, CheckFailed) as exc:
                fail(f"{name} bucket {store.bucket}", exc)

        # 2. Backup and restore: primary -> backup verified, primary copy lost, backup -> primary verified.
        primary_name, backup_name = settings.storage_primary, settings.storage_backup
        if primary_name in healthy and backup_name in healthy:
            first, second, label = stores[primary_name], stores[backup_name], f"backup {primary_name} -> {backup_name}"
            try:
                size, md5 = first.put(key, src, "text/plain")
                first.get(key, work / "for-backup")
                second.put(key, work / "for-backup", "text/plain", md5)
                _expect(second.verify(key, size, md5), "backup verify (size and checksum)")
                first.delete(key)
                _expect(first.stat(key) is None, "primary copy removed")
                label = f"restore {backup_name} -> {primary_name}"
                second.get(key, work / "restored")
                first.put(key, work / "restored", "text/plain", md5)
                _expect(first.verify(key, size, md5), "restore verify (size and checksum)")
                _expect(_fetch(first.url(key)) == (200, data), "download of the restored copy")
                print(f"  ok    backup {primary_name} -> {backup_name} and restore {backup_name} -> {primary_name}: "
                      "size and checksum verified")
            except (StorageError, CheckFailed) as exc:
                fail(label, exc)
    finally:
        for store in stores.values():
            try:
                store.purge(key)
            except StorageError as exc:
                print(f"  note  could not remove the test file {key} from {store.name}: {exc}")
        for path in work.iterdir():
            path.unlink(missing_ok=True)
        work.rmdir()
    for note in notes:
        print(f"  note  {note}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(check())
