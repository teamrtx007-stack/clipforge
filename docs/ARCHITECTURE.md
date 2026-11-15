# ClipForge architecture

## Problem

ClipForge turns long videos and livestreams into vertical short clips with captions, face tracking and AI titles. It must cost nothing to run. Every model runs locally and is open source. Paid APIs are never called unless the user configures one on purpose.

Video work is heavy. Transcription, face detection and encoding use a lot of CPU. The design keeps all heavy work in one background worker so the web UI stays responsive, and so a crash or restart never loses a job.

## Usage (the user's view)

1. Run `docker compose up` (or `./run.sh`) and open `http://localhost:8000`.
2. Drop in a long video or paste a YouTube link, and pick a clip length and template.
3. The project page shows progress. It runs through upload or download, probe, transcription, scene changes, highlight detection and rendering.
4. Each clip card shows a 9:16 preview, a score from 0 to 100, the reasons it was picked, a title, a description and hashtags.
5. On a clip you can trim it, edit the text, switch the template, re-render, and download the MP4.
6. In Live Studio, add a Twitch, YouTube, Kick or direct HLS URL. ClipForge watches it. It records into a rolling buffer when the channel goes live. It cuts a clip when the audio, chat and speech signals spike, and the clip flows through the same pipeline.

## Shape

### Data (SQLite, one file; PostgreSQL when `DATABASE_URL` is set, see docs/DATABASE.md)

```
Project  id, name, settings{clip_length, max_clips, template}
Source   id, project_id, kind(upload|live), status(queued|processing|ready|failed),
         meta{length, max_clips, template, url?: a pasted link, downloaded by the analyze job},
         stage, progress, duration, width, height, fps, channel_id?, error
Clip     id, source_id, start, end, score, title, description, hashtags[], reasons[],
         template, status(queued|rendering|ready|failed), progress, error
Job      id, kind(analyze_source|render_clip), target_id, status(queued|running|done|failed),
         priority, attempts, error
Channel  id, platform, url, name, enabled, auto_record, auto_render, chat, speech,
         sensitivity, template, buffer_minutes
Activity kind(video|clip|moment), at   one row per finished piece of work, for the dashboard chart;
         it outlives the videos it counts and is kept 31 days
```

Files live under `data/`. Paths are derived from ids and never stored. `data/sources/<id>/` holds the original video, `audio.wav`, `transcript.json` and `analysis.json`. `data/clips/<id>/` holds `clip.mp4`, `thumb.jpg` and `captions.ass`. A cleanup thread deletes sources and their clips `KEEP_HOURS` (24) after they were added, never while a job uses them, then projects left empty. The user keeps what they download.

### Load-bearing decisions

- **One pipeline for uploads and live.** A live moment becomes a short `Source` of kind `live` cut from the rolling buffer. It then goes through the same analyze and render jobs as an upload. Live Studio only detects and cuts. It never renders. Per the "two ways to do one task" red flag, there is one way to make a clip.
- **A job queue in the database.** A worker claims a job with one `UPDATE ... RETURNING` statement, which is atomic; on PostgreSQL it also takes a row lock (`FOR UPDATE SKIP LOCKED`), so workers on different machines never take the same job. On startup, jobs left `running` by a crash go back to `queued`. Every pipeline stage checks for its output file first and writes through a temp file plus rename, so a re-run skips finished work (per make-operations-idempotent).
- **Each piece of state has one owner.** The queue owns `Job` rows. The pipeline owns `Source` and `Clip` progress and status. The live supervisor owns channel runtime state (offline, live, recording) in memory, and the API reads snapshots of it.
- **Pure scoring code.** Highlight scoring, caption building, camera path planning and spike detection are pure functions over arrays and lists. They have no ffmpeg, database or HTTP calls, so tests cover them directly (per boundary-discipline).
- **Registries, not branches.** Templates, caption styles, layouts and platforms are lookup tables. A new template is one entry (per model-the-domain).
- **One LLM path.** Optional AI titles use any OpenAI-compatible endpoint: Ollama (local, free), LM Studio, Groq, Gemini or OpenRouter. With none configured, a local heuristic writes the titles. There is no vendor SDK.

### Live moment detection

Each second of a recording gives up to three signals:

- **Audio level.** The 90th percentile of the second's 50 ms frames, in dBFS. Plain RMS mixes in the pauses between words, so normal speech swings by about as much as a shout stands out.
- **Chat rate.** Twitch messages per second, with hype messages (emote spam, "clip it") counted twice.
- **Speech hype.** Whisper `tiny` transcribes 10 second chunks on its own thread and scores hype words, laughter and fast talking.

Each signal is averaged over 3 seconds. It is then compared with the median and MAD of its last 120 seconds, with a minimum spread in the signal's own units. A median ignores bursts that fill up to half the window, so one loud minute does not hide the next one. A lasting change in level, like a louder mic, becomes the new normal within a minute.

The combined score is the length of the weighted positive z-scores. A strong signal alone counts in full, a flat one adds nothing, and two moderate ones add up. An earlier normalized sum let a quiet chat or calm speech dilute every audio spike, and the live test caught it. A moment needs two seconds in a row above the threshold. It ends after 8 seconds or when the score drops, it reports its peak, and a 45 second cooldown follows. Signals are judged 15 seconds late, so the slower speech score has arrived. The cut runs from 35 seconds before the peak to 15 seconds after.

These choices were measured, not guessed. On 2000 seconds of pure noise, the exponential-average baseline we started with fired 3 to 10 false moments. The median baseline fires 0 or 1. With a test stream that shouts once every 66 seconds, the "high" sensitivity catches every shout with no false moments.

### Modules

```
backend/app/
  config.py      settings from environment variables
  db.py          SQLite (default) or PostgreSQL (DATABASE_URL): connections, schema, row helpers, backup
  schema_postgres.sql  the same tables for PostgreSQL / Supabase (clipforge schema)
  jobs.py        queue (enqueue, claim, finish) and worker threads
  pipeline.py    analyze_source and render_clip job handlers
  media.py       ffprobe, audio extraction, loudness curve, segment concat
  transcribe.py  faster-whisper with word timestamps; optional cloud Whisper chain, per 10-minute chunk
  scenes.py      shot cuts and context changes (outdoors to indoors), so a clip stays in one place
  highlights.py  candidate windows, scoring, selection (pure); never across a context change
  describe.py    titles, descriptions, hashtags (heuristic + optional AI, which also reranks the clips)
  ai.py          free-tier AI adapters (Gemini, Groq, Cloudflare, OpenRouter): retries, rate limits, fallback
  supabase/functions/ai-gateway  the only holder of provider keys (Supabase secrets); see docs/AI_GATEWAY.md
  camera.py      YuNet face detection and the smoothed crop and zoom path
  captions.py    animated ASS subtitles (pure)
  templates.py   template registry
  render.py      frame pipeline: decode, crop and zoom, encode with burned captions
  live/          platforms, recorder, chat, signals (pure), supervisor
  api.py         REST routes
  main.py        app factory, auth, static frontend, worker and live startup
frontend/        React + TypeScript + Vite single-page app
```

## Synthesis decision

Two structurally different shapes were compared.

| | A. Python monolith, SQLite queue | B. Services: Postgres, Redis, Celery, MinIO, Node API |
|---|---|---|
| Runs on a laptop for ₹0 | Yes, one process | Needs 5 services and about 2 GB more RAM |
| Moving parts | 1 process, 1 DB file | 5 containers to keep healthy |
| Scales past one machine | No, it needs a migration | Yes |
| Crash safety | Atomic claim plus idempotent stages | Celery acks plus retries |

A is the base. B's only win is multi-machine scale, which a free, single-user tool does not need yet. From B we kept the idea of a separate worker boundary. `jobs.py` is the only module that knows how jobs are claimed, so moving to Redis or Postgres later touches one file.

## Tradeoffs accepted

- We accept one machine's CPU as the ceiling in exchange for zero cost and one command to start.
- We accept a single shared password (`APP_PASSWORD`) instead of user accounts. Accounts belong to the future monetization work. Projects already have their own table, so adding an `owner_id` later is a column, not a rewrite.
- We accept rendering frames in Python (decode pipe, OpenCV warp, encode pipe), which is slower than a pure ffmpeg filter graph. In exchange, face tracking, smooth camera motion and punch-in zoom are plain, testable code.
- We accept that chat analysis covers Twitch only. Twitch has a free anonymous chat API. YouTube and Kick need scraping or keys. Their audio and speech signals still work.

## Alternatives considered

- **Pure ffmpeg filter graph for cropping.** ffmpeg's crop filter cannot change the crop size per frame, so zoom would need `zoompan` hacks. It lost on clarity.
- **MediaPipe for faces.** Its wheels lag new Python versions. OpenCV's YuNet is a 230 KB MIT model and runs inside OpenCV, which we already need.
- **Separate live clip type.** It would duplicate rendering, captions and titles. It lost to "a live moment is a short source".

## Open questions and risks

- Should finished renders auto-delete after N days to save disk? Today they stay until you delete the project.
- Twitch, YouTube and Kick change their sites often. streamlink tracks those changes, so keep it updated (`pip install -U streamlink`).
- CPU-only Whisper `small` runs at about 1x to 3x real time on 4 cores. A two-hour video can take an hour to transcribe. `WHISPER_MODEL=base` is faster and less accurate.

## Eng review (gstack plan-eng-review, applied to this plan)

### Scope challenge

The plan builds exactly the feature list in the brief. It adds nothing for billing, plans or payments. Two cuts keep it honest. Chat analysis covers Twitch only. It is listed below so it is visible, not forgotten.

### Not in scope

- Subscriptions, payments, credits, billing dashboards, revenue tracking. These are excluded on purpose.
- User accounts and teams. One shared password protects a public deployment.
- Auto-posting to TikTok, YouTube or Instagram.
- B-roll, music beds, emoji overlays, caption translation.
- Chat analysis for YouTube and Kick.

### Failure modes

| Path | Realistic failure | Handling | User sees |
|---|---|---|---|
| Upload | Connection drops mid-file | Body streams to `upload.part`, renamed only when complete | Upload error, nothing half-saved |
| Link | YouTube blocks the download or changes its site | yt-dlp's message goes to `Source.error`; the job tries once more | The reason on the video row, with a retry button |
| Link | Playlist, live video, over 12 hours or over the size limit | Checked from the page info before downloading | The reason on the video row |
| Storage | Videos pile up on the disk | Deleted `KEEP_HOURS` after they were added, checked every 10 minutes | A note above the clips says so |
| Probe | File is not a video | ffprobe error goes to `Source.error` | "Not a playable video" on the card |
| Probe | File claims an absurd length (forged metadata) | Over 12 hours is rejected, which bounds every array sized by duration | "Videos longer than 12 hours are not supported" |
| Audio | Video has no audio track | Empty transcript and flat loudness, windows come from the even split | Clips with no captions, no crash |
| Transcribe | Model download fails offline | Error names the model and the fix | Failed card with the message |
| Highlights | No speech (music, gameplay) | Loudness-only windows, plus windows that open on a shot cut | Clips cut at the loudest moments, inside one scene |
| Scenes | The video changes place (outdoors to indoors) | Windows across the change are dropped; low-speech windows end at it. If every window crosses one, the clip is kept with a lower score | Clips that stay in one place |
| Scenes | Detection fails, or `SCENE_DETECTION=0` | Clips are chosen from speech and sound only, as before | Nothing new |
| Worker | Process killed mid-render | Running jobs reset to queued at startup, once; a second crash fails the job | Job resumes after restart; a file that crashes twice is skipped |
| Upload | Restart during an upload | Rows still `uploading` and their partial files are deleted at startup | The upload disappears; upload it again |
| Render | Disk full, ffmpeg exits non-zero | Last lines of ffmpeg stderr go to `Clip.error` | Failed clip with a retry button |
| Delete | Project deleted while its job runs | The next progress write finds no row and raises `Gone`; the job ends and deletes what it wrote | Nothing, the project is gone |
| Edit | Clip trimmed while it renders | The render marks it `ready` only if start, end and template still match | The clip shows "Edited. Render again" |
| AI | Provider down, slow, out of free quota, or key refused | 90 s timeout, retries for 5xx, one wait for a short 429, then the next provider; the heuristic and local Whisper are the last step | Titles and transcripts still appear |
| Live | Recorder process dies | Supervisor sees the exit and goes back to polling | Status flips to offline or error |
| Live | Stream stalls with the connection open | 15 s read timeout in ffmpeg, 30 s in streamlink | Status flips to offline, then polling resumes |
| Live | Busy CPU while an upload transcribes | A second worker takes only live jobs | Live clips still appear in minutes |
| Security | A channel URL points at cloud metadata (169.254.169.254) | Link-local addresses are refused when added and on every poll | "That address is not allowed" |
| Security | Another website posts to the API, or DNS-rebinds to it | Writes need a matching Origin; without a password only localhost Host headers are served | 403 |
| Live | Many spikes in a row | Cooldown between moments | One clip per burst |
| Live | Buffer fills the disk | Segments older than `buffer_minutes` are deleted | Nothing |
| Auth | Password guessing | Constant-time compare | 401 |

No failure is silent and untested and unhandled at once, so there are no critical gaps.

### Test plan

- Pure units: window building, scoring order, overlap selection, `must_include`, caption timing and escaping, camera smoothing and clamping, auto layout fallback, spike detection with warmup and cooldown, Twitch IRC parsing, heuristic titles.
- Queue: two threads never claim the same job, stale `running` jobs reset, failures mark the target failed.
- Links and cleanup: a video served by a local web server downloads, takes its title and is clipped; a 404 fails with a readable reason; cleanup never deletes what a job uses (`tests/test_links.py`).
- API: project CRUD, upload type and size checks, partial upload cleanup, clip edit and re-render, auth on and off.
- End to end on a real generated video (a moving face photo and a real speech recording): analyze, then render. Check a 1080x1920 MP4 with audio, that the crop follows the face, and that the caption events exist (`tests/test_pipeline_e2e.py`).
- The live recording loop at full speed with a fake recorder: one shout gives one moment, cut while the stream is still recording (`tests/test_live_loop.py`, under a second).
- Live end to end against a local HLS stream played in real time: recording starts, a shout becomes a moment, the moment becomes a rendered clip, and the buffer is deleted when recording stops (`tests/test_live_e2e.py`).
- Browser QA of every page with Playwright.

### Performance

Transcription dominates. faster-whisper `small` with int8 on 4 CPU cores runs at about 1x to 3x real time. Face detection samples 4 frames per second at 640 px, so it costs little. Rendering a 45 s clip takes about 30 to 60 s on 4 cores. The loudness curve reads the WAV through a memory map, so a 3 hour file never loads into RAM.

### Parallelization

Sequential implementation. The backend modules share the database schema and the job contract, and the frontend depends on the API shape.
