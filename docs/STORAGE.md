# Cloud storage: Cloudflare R2 + Backblaze B2

ClipForge can keep its files in two private, S3-compatible buckets instead of only on this computer:

| Bucket | Role | Holds |
|---|---|---|
| Cloudflare R2 | Primary, and where every download comes from | Source videos, their analysis files (transcript, loudness), finished clips and thumbnails |
| Backblaze B2 | Backup | Verified copies of finished clips and thumbnails only. Source videos are temporary and never backed up |

Without the `R2_*` variables, nothing changes: files stay on this computer and `KEEP_HOURS` applies, as before.

## How a file moves

1. A video is uploaded (or downloaded from a link) to this computer, then copied to R2 when processing starts.
2. Its transcript and loudness curve go to R2 too, so another worker can render without analyzing again.
3. Each rendered clip and thumbnail go to R2, then are copied to B2. The backup only counts once B2 confirms the size and MD5 checksum (B2 also rejects a corrupted upload outright).
4. The browser plays and downloads from R2 through signed URLs that expire after an hour. The buckets stay private.
5. This computer drops its copy of a clip once it is safely stored. A worker without a local copy downloads it from R2, or from B2 if R2 does not have it.

Every copy is a row in the `files` table: `id` (file_id), `user_id`, `object_key`, `storage_provider`, `file_type`, `file_size`, `md5`, `upload_status`, `backup_status`, `created_at`, `expires_at`. A file is always read from a provider that recorded it.

## Retention

| Setting | Default | Meaning |
|---|---|---|
| `SOURCE_KEEP_HOURS` | `24` | Source videos and their analysis files are deleted from R2 and this computer this long after processing ends. The clock stops while any job uses them |
| `CLIP_KEEP_DAYS` | `7` | Finished clips leave R2 after this many days, **only if a verified B2 backup exists**. `0` keeps them |
| `BACKUP_KEEP_DAYS` | `30` | A B2 backup is deleted after this many days **only while R2 still has the clip** |

A last copy is never deleted by retention: a clip that is only in B2 stays there until you delete it. Deleting a clip, video or project in ClipForge deletes every copy. If a provider is unreachable at that moment, the deletion is retried until it succeeds.

A clip that is only in the B2 archive shows **Restore** in ClipForge. That copies it back to R2 and verifies it.

## Failures

- **R2 down or full during an upload:** the file stays on this computer, its row is marked `failed`, and ClipForge retries every 15 minutes. Nothing is overwritten: an upload either lands whole or not at all.
- **Backup fails or does not verify:** marked `failed` and retried every 15 minutes, to the same key, so there is never a second copy.
- **Original deleted after 24 hours:** "Find clips again" and re-rendering need it, so ClipForge says so and asks you to add the video again.

## Free allowance

Both providers give 10 GB free. ClipForge adds up what it has stored in each (in decimal GB, the stricter count):

- At 80% it warns in the log and on the Home page.
- At 100% it **refuses new uploads** instead of paying for more (`STORAGE_STOP_AT_LIMIT=1`). Clips then stay on this computer until space is freed. Your R2 account has a payment method on file, so this stop is what keeps it at ₹0.
- `R2_FREE_GB` and `B2_FREE_GB` change the limits, for example if you knowingly want a paid plan.

R2 also has free monthly limits on requests (1 million writes, 10 million reads). ClipForge makes a few requests per file, far below them.

## Billing risks

ClipForge's 10 GB stop is a safety check inside the app, **not a billing cap**. It only counts the files ClipForge itself recorded. The providers bill for what is actually in the bucket and for requests, so these can still cost money:

- **Cloudflare R2 has no hard spending cap.** With a payment method on file, anything past the free tier is charged: files put in the bucket by other tools, more than the free monthly requests, and unfinished multipart uploads (large uploads cut off part way still take space until R2 cleans them up). Turn on **billing notifications** in the Cloudflare dashboard; they warn, they do not stop.
- **Backblaze B2 keeps deleted files as hidden versions** by default ("Keep all versions"). A hidden file still counts toward the 10 GB, and ClipForge's count does not see it. Set the bucket's lifecycle as below.
- **B2 has small free daily allowances** for downloads and for checks/listings. Normal use stays far below them. B2's **Caps & Alerts** page can stop usage at $0 instead of charging; set it as below.
- **Keys on a synced folder:** if the ClipForge folder is on a OneDrive Desktop, OneDrive uploads `.env` (and these keys) to Microsoft's cloud.

## B2 settings to change once

1. **Buckets → your bucket → Lifecycle Settings → Keep only the last version of the file.** Deleted backups then disappear after about a day instead of counting forever. (This cannot be read through the S3 API, so `python -m app.storage` can only remind you.)
2. **Caps & Alerts:** set every daily cap (storage, download bandwidth, class B and class C transactions) to **$0**. B2 then refuses extra usage instead of billing it.

## Setup

1. **R2 bucket:** in the Cloudflare dashboard, open **R2 → Create bucket**, name it (for example `clipforge`) and keep it private.
2. **R2 key:** **R2 → Manage API tokens → Create API token**. Permission **Object Read & Write**, and **apply to the one bucket only**. Copy the Access Key ID, the Secret Access Key, and the S3 endpoint (`https://<account-id>.r2.cloudflarestorage.com`).
3. **B2 bucket:** in Backblaze, **Buckets → Create a Bucket**, set **Private**. Note its endpoint, for example `s3.us-east-005.backblazeb2.com`.
4. **B2 key:** **Application Keys → Add a New Application Key**. Allow access to **that bucket only**, type **Read and Write**. Copy the keyID and applicationKey (shown once).
5. Add to ClipForge's `.env` (git-ignored, never committed):
   ```
   R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
   R2_ACCESS_KEY_ID=<r2 access key id>
   R2_SECRET_ACCESS_KEY=<r2 secret>
   R2_BUCKET_NAME=clipforge
   R2_REGION=auto
   B2_ENDPOINT_URL=https://s3.us-east-005.backblazeb2.com
   B2_ACCESS_KEY_ID=<b2 keyID>
   B2_SECRET_ACCESS_KEY=<b2 applicationKey>
   B2_BUCKET_NAME=<your b2 bucket>
   B2_REGION=us-east-005
   ```
6. Restart and check both buckets (see **Checking the buckets** below):
   ```bash
   docker compose up -d --build
   ```
   ```bash
   docker compose exec clipforge python -m app.storage
   ```

The storage keys live in the worker's `.env` because the worker uploads and downloads video directly, which the Supabase gateway cannot carry (Edge Functions limit request size and time). They are scoped to one bucket each, never sent to the browser, and never logged: errors name the provider only.

## Checking the buckets

`python -m app.storage` uses only throwaway test files under `clipforge-check/` and deletes them at the end, including hidden B2 versions. It never touches real videos or the database. It checks:

| Step | Each bucket | Both buckets |
|---|---|---|
| Upload, then verify size and checksum | yes | |
| Download through a signed link, as the browser does | yes | |
| A 5-second signed link is refused once expired (takes about 10 s) | yes | |
| Download with the keys, then delete | yes | |
| Backup R2 → B2, verified by size and checksum | | yes |
| R2 copy removed, restore B2 → R2, verified, then downloaded through a signed link | | yes |

It prints `ok` or `FAIL` per line, with the provider's own explanation on failure, never the keys. A `note` line reminds you about B2's hidden versions. Quota protection, failed-backup retries and cleanup timing are covered by the offline tests (`backend/tests/test_storage.py`), since testing them live would need 10 GB of uploads, a forced outage or a day of waiting.

## Troubleshooting

| Message | Cause |
|---|---|
| `No bucket is configured` | ClipForge did not get the `R2_*` / `B2_*` values: fill them in `.env`, then `docker compose up -d --force-recreate` |
| `InvalidArgument: Credential access key has length 31, should be 32` | The R2 key ID is wrong, often still a placeholder or a Cloudflare API token instead of the R2 **Access Key ID** |
| B2 `403` or `ConnectionClosedError` on upload | The B2 keyID or applicationKey is wrong, or it is the master key (use an application key for that one bucket) |
| `link expiry: an expired link still worked` | This computer's clock is off by more than a few seconds: turn on **Set time automatically** in Windows |

Usual key lengths: R2 Access Key ID 32, R2 Secret Access Key 64, B2 keyID 25 (starts with `00`), B2 applicationKey 31. To see what ClipForge received without showing values:

```bash
docker compose exec clipforge python -c "import os; [print(k, len(os.environ.get(k, ''))) for k in ['R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','B2_ACCESS_KEY_ID','B2_SECRET_ACCESS_KEY']]"
```
