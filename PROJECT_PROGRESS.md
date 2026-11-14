# Project progress

Autonomous work session, 14 November 2026. Branch `ai-providers`, starting from `main` at `9f6ef6d`.
A tag `checkpoint-before-ai-providers` marks the starting point, so every change here can be undone with one `git reset`.

Status: **All six phases done.** Work is on branch `ai-providers` in a draft pull request; `main` is unchanged until you merge it.

## Phase 1: audit

### Technology stack

| Part | What it is |
|---|---|
| Backend | Python 3.12, FastAPI 0.162, uvicorn, one process (`backend/app`) |
| Database | SQLite in WAL mode, one file: `DATA_DIR/clipforge.sqlite3`. Tables: projects, channels, sources, clips, jobs, activity |
| File storage | Local disk under `DATA_DIR/media` (sources, clips) and `DATA_DIR/live` (rolling buffers). Docker keeps it in the `clipforge_data` volume |
| Job queue | SQLite `jobs` table, worker threads in the same process (`jobs.py`), one extra lane reserved for live clips |
| Frontend | React 19 + TypeScript 5.9 + Vite 7, react-router 7, served by the backend from `frontend/dist` |
| Speech to text | faster-whisper 1.2 (local, `small` by default; `tiny` for live speech) |
| Video | ffmpeg/ffprobe (Debian package in Docker), libass for burned-in captions, OpenCV 5 YuNet face detector, numpy |
| Video links | yt-dlp with the Deno runtime (YouTube needs it) |
| Live ingestion | streamlink (Twitch, YouTube, Kick) or ffmpeg directly (HLS, RTMP, SRT); one ffmpeg per channel writes 2 s segments and a PCM audio pipe |
| Rolling buffer | `live/recorder.py`: segments listed in `segments.csv`, pruned past `buffer_minutes` |
| Live analysis | `live/signals.py`: audio loudness, Whisper `tiny` hype words, Twitch chat rate, each against its own rolling median/MAD baseline |
| AI text | Local heuristic titles; optional one OpenAI-compatible endpoint (`LLM_BASE_URL`), e.g. Ollama |
| Deployment | `Dockerfile` (Node build stage, Python slim runtime), `docker-compose.yml` (port bound to 127.0.0.1, named volume, optional Ollama profile), `run.sh` for Linux/macOS without Docker |
| Tests | pytest: 66 tests including real-media end-to-end runs (Whisper tiny, ffmpeg, a live HLS stream) |

### Existing features that work (verified by tests and browser runs earlier today)

- Upload a video or paste a YouTube link; transcribe, score windows (hook, loudness, topic, emotion, pace, ending, length fit), pick non-overlapping clips.
- 1080x1920 render: face-tracked crop or blurred-background fit, punch-in zoom on loud moments, karaoke/pop/static captions in 6 templates, Hindi and other Indian scripts.
- Titles, descriptions, hashtags; clip editor with trim, template switch, re-render, MP4 download.
- Live studio: several channels, live/offline status, auto recording, rolling buffer, audio + speech + Twitch chat moment detection, 0-100 scores, clips rendered through the same pipeline, priority lane.
- 24-hour automatic cleanup, white dashboard with activity chart.

### Broken, incomplete or risky areas found

1. **Secrets: `.env` was not in `.gitignore` or `.dockerignore`.** Docker Compose reads keys from `.env`, so a `git add -A` could have committed them. Fixed in Phase 2.
2. Only one AI text endpoint at a time and no fallback; no cloud speech-to-text, so a cloud VM without a GPU transcribes slowly.
3. Highlight words lists are English only, so Hindi videos are scored on loudness, topic and pace alone. AI scoring only re-weighted the clips already chosen.
4. Clips start exactly on the first word's timestamp and end on the last word's. Whisper word times are often 0.1 to 0.3 s late, so first and last syllables can be clipped.
5. ffmpeg helpers (`extract_audio`, `thumbnail`, `concat`) and the stage cache leave a `.tmp` file behind when they fail.
6. The Windows PC's RTX 5070 Ti is not used: the Docker image has no CUDA libraries, so Whisper runs on the CPU.
7. Live speech scoring runs Whisper locally every 10 s per channel; that cannot move to a free cloud API (quota).

## Phase 2: safety

- Branch `ai-providers` and tag `checkpoint-before-ai-providers` created. `main` is untouched until the pull request is merged.
- Added `.env` and `.env.*` (except `.env.example`) to `.gitignore` and `.dockerignore`.
- No credentials exist in this cloud container (checked variable names only, never values). No keys are in the repository (`git grep` for key patterns found none).
- The AI hosts api.groq.com, api.cloudflare.com and openrouter.ai are blocked by this container's network policy, so live API calls cannot be tested from here. Adapters are tested against a local fake server instead.

## Phase 3: AI providers

### Research (sources checked 14 November 2026)

| Provider | Model IDs used | Free tier | Fit |
|---|---|---|---|
| Groq | `whisper-large-v3-turbo` (speech), `openai/gpt-oss-120b` (text) | Whisper: 20 req/min, 2,000 req/day, 7,200 audio s/hour, 28,800 audio s/day, 25 MB/file | Best free cloud speech to text: fast, about 8 h of audio a day |
| Cloudflare Workers AI | `@cf/openai/whisper-large-v3-turbo`, `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 10,000 Neurons/day for all models; Whisper turbo = 46.63 Neurons/audio minute, so about 214 min/day | Second speech fallback; text fallback |
| Google Gemini | `gemini-2.5-flash` (stable; no shutdown date announced on the Gemini API deprecations page at the time of checking) | Per-project limits shown in AI Studio; cut in late 2025, changed again in 2026 | Best text quality for Hindi titles and scoring; also the only free option for real video understanding |
| OpenRouter | `openrouter/free` (router over all free models) | 20 req/min; 50 req/day, or 1,000/day after a one-time $10 credit purchase | Last-resort text fallback |

Groq's `llama-3.3-70b-versatile` was reported retired on 16 August 2026 by a third-party tracker, so it is not used. Gemini 3 Flash exists as `gemini-3-flash-preview` but is a preview model, so it is not the default. Every model can be changed with an environment variable, without code changes.

### Decisions

- **Speech to text:** local faster-whisper stays the default (free, private, unlimited). `TRANSCRIBE_PROVIDERS=groq,cloudflare,local` is the recommended setting for a cloud VM without a GPU.
- **Highlight detection and clip scoring:** the local signals still find candidates (they need word timings and loudness, which an LLM cannot see). With a text provider, a pool of extra candidates goes to the AI in one request; its virality score (40%) decides which make the cut. This makes scoring work for Hindi, where the English word lists do nothing.
- **Titles and descriptions:** same request, so one AI call per video.
- **Video understanding:** not added. Only Gemini offers it free, it means uploading video frames, and free-tier data may be used for training. Documented as a next step.
- **Fallback:** text providers in order Gemini, Groq, Cloudflare, OpenRouter (any `LLM_BASE_URL` such as Ollama goes first, because it is private); then the local heuristic.
- **Live clipping stays local:** a cloud call every 10 s per stream would spend a free quota in hours.

### What changed

- `backend/app/ai.py` (new): OpenAI-compatible chat adapter for all four providers, Groq and Cloudflare Whisper adapters, 90 s timeouts, retries for network errors and 5xx, one wait for a short 429, immediate hand-over on a spent quota or refused key, error messages that name the provider and never the key, and `python -m app.ai` to check every configured provider with one tiny request.
- `backend/app/config.py`: provider settings read from the existing environment settings (`GEMINI_API_KEY`, `GROQ_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `OPENROUTER_API_KEY`, `TEXT_PROVIDERS`, `TRANSCRIBE_PROVIDERS`, `TRANSCRIBE_LANGUAGE`, model overrides). Secrets are excluded from `repr`. `LLM_BASE_URL` still works.
- `backend/app/transcribe.py`: `transcribe_file` sends ten-minute MP3 chunks (cut at the quietest moment near each mark) through the provider order, falls back per chunk, saves finished chunks so a retry never spends quota twice. `TRANSCRIBE_LANGUAGE=hi` also stops local Whisper writing Hindi in Urdu script.
- `backend/app/describe.py`: uses the provider chain, one request per video.
- `backend/app/pipeline.py`: AI reranks a larger candidate pool.
- `backend/app/api.py`, `frontend/src/*`: the dashboard shows the active providers in order (names only).
- `.env.example` (new), `docker-compose.yml` (`env_file: .env`, optional), `README.md` (AI providers section, limits, privacy).

### Tests

- `backend/tests/test_ai.py` (new, 9 tests, all pass; the 9th checks that a key echoed in an error is masked and that a date-style Retry-After does not crash) against a local fake API: fallback past a spent quota (429 with a long Retry-After) and a refused key; retry after 503 and a short 429; keys never in error messages; no request without a provider; exact Groq multipart and Cloudflare JSON request shapes and answer parsing (Hindi text); speech falling back to local Whisper on the second chunk and a retry reusing saved chunks.
- `python -m app.ai` with no keys prints the variables to set; with fake keys it reports each failure without printing them.

### Cloudflare status

No Cloudflare credentials are available in this cloud session, and api.cloudflare.com is blocked by its network policy, so **no real request was made**. The integration is ready. To turn it on, put these in `.env` next to `docker-compose.yml` on your PC:

```
CLOUDFLARE_ACCOUNT_ID=<your account ID>
CLOUDFLARE_API_TOKEN=<a token with the Workers AI permission>
```

Then run `docker compose up -d` and `docker compose exec clipforge python -m app.ai`. It prints `ok cloudflare (...)` or the exact error, such as a refused token.

## Phase 4: video pipeline

Inspected: candidate windows and scoring (`highlights.py`), caption word selection and karaoke timing (`pipeline.render_clip`, `captions.py`), crop and camera path (`camera.py`, `render.py`), every ffmpeg temp-file path (`media.py`, `pipeline._cached`, `render.py`).

Changed:

1. **Clip edges no longer clip words.** Clips now start up to 0.25 s before the first word and end up to 0.45 s after the last, because Whisper's word times run late. The padding only uses room left under the length limit, so a "15 to 30 s" clip never goes past 30 s (`highlights.find`, `LEAD_IN`, `TAIL`).
2. **Captions survive a trim through a word.** A word is captioned when most of it is inside the clip, clipped to the clip's edges. Before, a word cut by a trim lost its caption while it could still be heard (`highlights.words_in`, used by `pipeline.render_clip`).
3. **No partial files after an ffmpeg failure.** `media.writing` writes to a temp name, renames on success and deletes on failure. Used by audio extraction, thumbnails, live concatenation, compressed speech chunks and the analysis cache. Before, a failure halfway (for example a full disk) left a `.tmp` file until the 24-hour cleanup.
4. **AI reranking of a larger pool** (Phase 3) is the scoring improvement for non-English videos.

Checked and left alone: vertical crop (face tracking test passes, 1080x1920 output verified), render failure handling (`render.py` already kills both ffmpeg processes, deletes its temp file and reports the last ffmpeg lines), karaoke timing (each word highlights from its start to the next word's start).

Tests added: padding stays within the length limit and leaves room around words; trimmed words keep captions; ffmpeg failing halfway leaves no file for extract_audio, thumbnail and compress_audio.

## Phase 5: cloud readiness

### What still depends on your PC today

| Part | Needs | Can run on a free cloud VM? |
|---|---|---|
| Web app, API, queue | One always-on process with a disk | Yes, same Docker image |
| SQLite database | A local disk | Yes, on the VM's disk. Not on serverless platforms |
| Video and clip files | Disk for 24 hours of work (`KEEP_HOURS`) | Yes. Oracle Always Free includes 200 GB of block storage |
| Speech to text | GPU or many CPU cores | Yes with `TRANSCRIBE_PROVIDERS=groq,cloudflare,local`; CPU-only Whisper is too slow on 4 cores |
| Rendering (ffmpeg, face tracking) | CPU, about 30-60 s per clip on 4 cores | Yes, slower on a small VM |
| Live recording | Always-on process, steady bandwidth, about 2-3 GB per hour per 1080p stream while live | Yes, and this is the main reason to move: your PC can be off |
| Live hype detection | Whisper `tiny` on CPU every 10 s per channel | Yes; about one core per two to three live channels |

### Recommended free setup (not deployed; needs your approval)

1. **Oracle Cloud Always Free ARM VM** (4 OCPU, 24 GB RAM, 200 GB disk). Run the same `docker compose up -d`. Oracle asks for a card to verify identity but does not charge Always Free resources; keep the account on the Always Free tier and do not upgrade.
2. **Cloudflare Tunnel** (free) in front of it for HTTPS without opening ports, plus `APP_PASSWORD`.
3. **Groq then Cloudflare** for speech to text, local Whisper as the last fallback.

The code needs no changes for this: the image builds for ARM and every setting is an environment variable. Not prepared, on purpose: object storage (Cloudflare R2's free tier is 10 GB). With 24-hour cleanup and downloads served by the app, the VM disk is enough, and R2 would add a second place where videos live.

### Not possible for free, documented instead of built

- Free cloud GPUs that can run 24/7 (Colab and Kaggle sessions stop and their terms forbid hosting a service).
- Serverless hosting (Cloudflare Workers, Vercel, Render free): no ffmpeg-length CPU time, no lasting disk, no long-running live recorder.

## Phase 6: testing

| Check | Result |
|---|---|
| Full backend suite (pytest, includes real-media and live-stream end-to-end tests), on the final code | **79 passed**, 0 failed (3 min 57 s) |
| Frontend typecheck and production build (`tsc --noEmit`, `vite build`) | pass |
| Lint (`ruff check --select F,E9,B006,B008`) | 2 unused imports in tests, fixed; app code clean |
| `docker compose config` with a `.env` file | keys reach the container; `.env` stays untracked |
| `/api/system` with fake keys set | shows provider names and order, never a key |
| `python -m app.ai` with no keys, and with fake keys | prints what to set; reports each failure without printing keys |
| Real Gemini, Groq, Cloudflare and OpenRouter calls | **not tested**: no keys here, and this container's network blocks api.groq.com, api.cloudflare.com and openrouter.ai |

Note for whoever runs the tests in this container: set `HF_HOME=/home/user/.cache/hf HF_HUB_OFFLINE=1`, or Whisper tries to download from a blocked host and one test fails.

## Final report

1. **Stack:** see Phase 1. Python 3.12 + FastAPI + SQLite + in-process job queue; React 19 + TypeScript + Vite; ffmpeg, libass, faster-whisper, OpenCV YuNet, yt-dlp, streamlink; Docker Compose.
2. **Works:** upload or link → transcript → scored clips → 9:16 render with captions and face tracking → edit, re-render, download; live studio with rolling buffer, moment detection and auto-render; 24-hour cleanup; dashboard. All covered by tests that pass.
3. **Needs improvement:** GPU Whisper in Docker (your RTX 5070 Ti is unused; needs CUDA 12.8+ libraries and is untested); scene/visual signals (scoring uses speech, loudness and chat, not the picture); chat analysis only for Twitch; live detection runs Whisper `tiny` on the CPU; English-only hook word lists (AI reranking now covers other languages when a key is set); no user accounts (one shared password).
4. **AI providers integrated:** Google Gemini, Groq, Cloudflare Workers AI, OpenRouter, plus any OpenAI-compatible endpoint such as Ollama. Local faster-whisper and the local title heuristic remain the fallback and the default.
5. **Model IDs:** `gemini-2.5-flash`; `openai/gpt-oss-120b` and `whisper-large-v3-turbo` on Groq; `@cf/meta/llama-3.3-70b-instruct-fp8-fast` and `@cf/openai/whisper-large-v3-turbo` on Cloudflare; `openrouter/free`. All overridable by environment variable.
6. **Cloudflare:** adapter ready and tested against a fake server; no real request made (no credentials here). Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in `.env`, then run `docker compose exec clipforge python -m app.ai`.
7. **Files:** new `backend/app/ai.py`, `backend/tests/test_ai.py`, `.env.example`, `PROJECT_PROGRESS.md`; changed `backend/app/{config,transcribe,describe,pipeline,highlights,media,api}.py`, `backend/tests/{test_units,test_pipeline_e2e}.py`, `frontend/src/{api,format,main}.ts(x)`, `frontend/src/pages/Home.tsx`, `docker-compose.yml`, `.gitignore`, `.dockerignore`, `README.md`, `ROADMAP.md`, `docs/ARCHITECTURE.md`.
8. **Tests:** see Phase 6.
9. **Free-tier limits:** Groq Whisper 8 h of audio a day; Cloudflare 10,000 Neurons a day (about 214 min of Whisper, or about 50 title requests, shared); Gemini per-project limits that Google has cut before; OpenRouter 50 requests a day. All can change without notice; the fallback chain absorbs a spent quota.
10. **Still works locally:** yes. With no keys the behaviour is the same as before, apart from the two pipeline fixes (word padding, captions on trims). The full test suite passes.
11. **For cloud deployment:** an always-on VM (Oracle Always Free ARM recommended), the same Docker Compose file, `APP_PASSWORD`, a Cloudflare Tunnel for HTTPS, and `TRANSCRIBE_PROVIDERS=groq,cloudflare,local`. Needs your approval and your Oracle and Cloudflare accounts; nothing was deployed.
12. **100+ video hours a month:** about 3.3 h a day. Speech: Groq alone covers up to 8 h a day (about 240 h a month), Cloudflare adds about 3.5 h a day, and local Whisper takes any overflow, so 100 h fits if no single day goes far past 11 h. Text: one AI request per video, so 100-300 requests a month, well inside every free tier. Rendering: about 800 clips at 30-60 s each, 7-13 CPU hours a month. Storage: with 24-hour cleanup, about 5-10 GB at any time. Live channels are the costly part: each needs an always-on recorder and 2-3 GB of download per live hour.
13. **Next steps:** add your keys and run `python -m app.ai`; set `TRANSCRIBE_LANGUAGE=hi` for Hindi channels; test GPU Whisper on the RTX 5070 Ti; try the Oracle VM + Cloudflare Tunnel deployment after approval; add Gemini frame sampling for scene-aware scoring.

## Decisions that need your approval

- Deploying to any cloud VM (Oracle Always Free recommended), creating a Cloudflare Tunnel, or using your domain.
- Sending audio to Groq or Cloudflare by default. Today it happens only if you set `TRANSCRIBE_PROVIDERS`. Free tiers may use uploaded data to improve their models.
- Merging pull request for branch `ai-providers` into `main`.
