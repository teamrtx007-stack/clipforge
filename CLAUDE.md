# ClipForge: notes for Claude

ClipForge is a free AI video clipping platform: long videos and livestreams in, captioned vertical 9:16 shorts out.

## The owner's rules

- **Free only.** Never add subscriptions, paid plans, payments, credits, billing or any monetization. Never switch to a paid API silently. Say plainly when a free tier has limits.
- **Secrets.** Never ask for API keys in chat and never write keys to files, logs, commits or PRs. AI provider keys live only in Supabase Edge Function secrets (`docs/AI_GATEWAY.md`). The worker's git-ignored `.env` holds `GATEWAY_URL`, `GATEWAY_TOKEN`, and the bucket-scoped R2/B2 storage keys (`docs/STORAGE.md`), because video uploads cannot go through an Edge Function.
- **Preserve what works.** Do not rebuild from scratch or delete features. Work on a branch and open a draft PR; the owner merges on GitHub.
- **Language.** The owner may write in Hindi or English. Always answer in English.
- **Their machine.** Windows, RTX 5070 Ti, i9-14900K, 32 GB RAM, Docker Desktop. They run ClipForge with `docker compose up -d --build` from `%USERPROFILE%\OneDrive\Desktop\clipforge` (start commands with `cd /d "%USERPROFILE%\OneDrive\Desktop\clipforge"`). Give Command Prompt commands, one per code block.
- **OneDrive warning.** That folder is synced by OneDrive, so its `.env` (gateway token, R2 and B2 keys) is uploaded to Microsoft's cloud. Remind the owner when keys are involved; moving ClipForge outside OneDrive (e.g. `%USERPROFILE%\clipforge`) avoids it. Never put other secret files, such as the Supabase secrets file, in that folder: use `%USERPROFILE%\clipforge-secrets.env` and delete it after `secrets set`.
- **Style.** Smallest complete change; a test for new logic; plain-English docs. The owner asked that these skills guide the work: ponytail, gstack, pstack, thermo-nuclear code review.

## Layout

| Path | What |
|---|---|
| `backend/app` | Python 3.12, FastAPI, SQLite (WAL), in-process job queue, faster-whisper, ffmpeg + libass, OpenCV YuNet, yt-dlp + Deno, streamlink |
| `backend/app/ai.py` | AI providers for the worker: the Supabase gateway, or direct keys, with local fallbacks |
| `backend/app/live` | Live studio: recorder, rolling buffer, moment detection, supervisor |
| `frontend/src` | React 19, TypeScript, Vite; white dashboard with sidebar |
| `supabase/functions/ai-gateway` | Deno Edge Function: the only holder of provider keys. Key pools, cooldowns, routes per provider |
| `backend/app/storage.py`, `backend/app/files.py` | R2 (primary) and B2 (backup) over the S3 API; `files` table, verified backups, restore, retention, free-tier stop |
| `supabase/migrations` | Gateway tables (`ai_key_state`, `ai_usage`), RLS with no policies |
| `docs/ARCHITECTURE.md`, `docs/AI_GATEWAY.md`, `ROADMAP.md`, `PROJECT_PROGRESS.md` | Design, gateway setup, roadmap, last audit |

## Checks

- Backend: `cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest -q`. The first run downloads Whisper `tiny`; once cached, `HF_HUB_OFFLINE=1` avoids the network.
- Gateway: `cd supabase/functions/ai-gateway && deno test && deno check index.ts && deno lint` (the `deno` PyPI package installs a binary at `backend/.venv/bin/deno`).
- Frontend: `cd frontend && npm ci && npm run build`.

## Open decision

Whether to move the rest of the backend to Supabase: Auth instead of the shared password, Postgres instead of SQLite, Storage for finished clips (1 GB free). Video processing and live recording cannot run in Edge Functions (no ffmpeg, 150 s limit), so they stay on a worker: the owner's PC or a free VM.
