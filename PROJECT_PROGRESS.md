# Project progress

Autonomous work session, 14 November 2026. Branch `ai-providers`, starting from `main` at `9f6ef6d`.
A tag `checkpoint-before-ai-providers` marks the starting point, so every change here can be undone with one `git reset`.

Status: **Phases 1-3 done. Phase 4 in progress.**

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

- `backend/tests/test_ai.py` (new, 8 tests, all pass) against a local fake API: fallback past a spent quota (429 with a long Retry-After) and a refused key; retry after 503 and a short 429; keys never in error messages; no request without a provider; exact Groq multipart and Cloudflare JSON request shapes and answer parsing (Hindi text); speech falling back to local Whisper on the second chunk and a retry reusing saved chunks.
- `python -m app.ai` with no keys prints the variables to set; with fake keys it reports each failure without printing them.

### Cloudflare status

No Cloudflare credentials are available in this cloud session, and api.cloudflare.com is blocked by its network policy, so **no real request was made**. The integration is ready. To turn it on, put these in `.env` next to `docker-compose.yml` on your PC:

```
CLOUDFLARE_ACCOUNT_ID=<your account ID>
CLOUDFLARE_API_TOKEN=<a token with the Workers AI permission>
```

Then run `docker compose up -d` and `docker compose exec clipforge python -m app.ai`. It prints `ok cloudflare (...)` or the exact error, such as a refused token.
