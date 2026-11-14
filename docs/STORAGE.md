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
6. Restart and check both buckets with one tiny round trip each:
   ```bash
   docker compose up -d --build
   ```
   ```bash
   docker compose exec clipforge python -m app.storage
   ```

The storage keys live in the worker's `.env` because the worker uploads and downloads video directly, which the Supabase gateway cannot carry (Edge Functions limit request size and time). They are scoped to one bucket each, never sent to the browser, and never logged: errors name the provider only.
