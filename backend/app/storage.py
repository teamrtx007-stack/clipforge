"""Object storage over the S3 API: Cloudflare R2 (primary) and Backblaze B2 (backup).

Buckets are private. The browser only ever gets short-lived signed URLs; credentials stay on the server
(this process's environment) and never appear in errors, logs or API answers.
Run `python -m app.storage` to check each configured bucket with one tiny round trip.
"""
import base64
import functools
import hashlib
import threading
import time
import uuid
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import media
from .config import Bucket, settings

SINGLE_PUT_LIMIT = 100 * 1024 * 1024  # up to here one PUT with Content-MD5; above, multipart
URL_SECONDS = 3600


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

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "error")
            raise StorageError(f"{self.name}: {what} failed ({code})") from exc
        except (BotoCoreError, OSError) as exc:
            raise StorageError(f"{self.name}: {what} failed ({type(exc).__name__})") from exc

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
            raise StorageError(f"{self.name}: check failed ({exc.response.get('Error', {}).get('Code')})") from exc
        except (BotoCoreError, OSError) as exc:
            raise StorageError(f"{self.name}: check failed ({type(exc).__name__})") from exc
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

    def url(self, key: str, filename: str | None = None) -> str:
        """A signed GET URL valid for an hour. Reused for half an hour, so a page that polls keeps the same
        URL and a playing video does not restart. Signing happens locally: no request is made."""
        cache_key = (key, filename)
        with self._lock:
            cached = self._urls.get(cache_key)
            if cached and time.time() - cached[1] < URL_SECONDS / 2:
                return cached[0]
            params = {"Bucket": self.bucket, "Key": key}
            if filename:
                params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
            url = self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=URL_SECONDS)
            self._urls[cache_key] = (url, time.time())
            if len(self._urls) > 5000:
                self._urls.clear()
            return url


class CloudflareR2Storage(ObjectStorage):
    name = "r2"


class BackblazeB2Storage(ObjectStorage):
    name = "b2"


PROVIDERS: dict[str, type[ObjectStorage]] = {"r2": CloudflareR2Storage, "b2": BackblazeB2Storage}


@functools.cache
def provider(name: str) -> ObjectStorage:
    return PROVIDERS[name](settings.buckets[name])


def primary() -> ObjectStorage | None:
    return None if settings.storage_primary == "local" else provider(settings.storage_primary)


def backup() -> ObjectStorage | None:
    return provider(settings.storage_backup) if settings.storage_backup else None


def check() -> int:
    """Upload, verify, sign, download and delete one tiny object in each configured bucket."""
    if not settings.buckets:
        print("No bucket is configured: files stay on this computer. Set the R2_* and B2_* variables.")
        return 0
    print(f"Primary: {settings.storage_primary}. Backup: {settings.storage_backup or 'none'}.")
    failed = 0
    tmp = settings.data_dir / f"storage-check-{uuid.uuid4().hex}.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("ClipForge storage check\n")
    try:
        for name in settings.buckets:
            store, key = provider(name), f"clipforge-check/{tmp.name}"
            try:
                size, md5 = store.put(key, tmp, "text/plain")
                ok = store.verify(key, size, md5)
                store.url(key)
                store.get(key, tmp.with_suffix(".back"))
                ok = ok and md5_of(tmp.with_suffix(".back")) == md5
                store.delete(key)
                print(f"  {'ok  ' if ok else 'FAIL'}  {name} bucket {store.bucket}: upload, verify, signed URL, download, delete")
                failed += not ok
            except StorageError as exc:
                failed += 1
                print(f"  FAIL  {name} bucket {store.bucket}: {exc}")
            finally:
                tmp.with_suffix(".back").unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(check())
