# ClipForge

ClipForge turns long videos and livestreams into vertical short clips. It finds the moments worth sharing, then cuts each one to 9:16. Each clip gets animated captions, face tracking, smart cropping, a punch-in zoom on loud moments, and a title, description and hashtags. You download the result as an MP4.

It is free. Every AI model runs on your own computer, and ClipForge never calls a paid API.

ClipForge is a workbench, not an archive. It deletes videos and clips 24 hours after you add them, so download the clips you want to keep.

## Quick start

With Docker:

```bash
docker compose up --build
```

Then open http://localhost:8000. On Windows or macOS, install [Docker Desktop](https://docs.docker.com/desktop/) first and start it. It is free for personal use, education and companies with fewer than 250 employees and under $10 million in yearly revenue. Larger companies need a paid Docker plan. The first build downloads about 1 GB.

Docker keeps videos, clips and models in a volume named `clipforge_data`. Run `docker compose down -v` only if you want to delete them.

Without Docker you need Python 3.11 or newer, Node 20 or newer, and ffmpeg:

```bash
./run.sh
```

The first video you process downloads the Whisper speech model from Hugging Face. The default `small` model is about 480 MB. Later runs reuse it.

## What you can do

**Clip a long video.** Drop a video on the dashboard, or paste a YouTube link, and pick a clip length (15 to 30 s, 30 to 60 s, or 60 to 90 s) and a template. ClipForge runs these steps:

1. For a link, it downloads the video, up to 1080p. Links from about a thousand other video sites work too.
2. It transcribes the speech with word-level timing.
3. It scores every sentence-aligned window for hook, loudness, topic focus, emotion, pace, clean ending and length fit.
4. It keeps the best windows that do not overlap.
5. It renders each one as a 1080x1920 MP4.

Each clip shows its score and the reasons it was picked, for example "Opens with a question" or "Loud reaction at 12:03". A score of 70 or more gets a red circle.

**Edit a clip.** Change the title, description or hashtags, switch the template, or trim the start and end against the source video. Then render again and download.

**Clip livestreams.** In Live studio, add Twitch, YouTube or Kick channels, or any HLS, RTMP or SRT stream URL. You can watch several channels at once. When a channel goes live, ClipForge records it into a rolling buffer (the last 5 minutes by default) and scores every second:

- **Audio.** Loudness spikes against the stream's own baseline.
- **Speech.** A small Whisper model listens for hype words, laughter and fast talking.
- **Chat.** Twitch chat message rate, counting emote spam double. It needs no account.

When the combined score crosses the sensitivity threshold, ClipForge cuts about 50 seconds around the peak. That cut goes through the same pipeline as an upload, so a finished clip appears a minute or two after the moment happens.

## Templates

| Template | Layout | Captions | Zoom |
|---|---|---|---|
| Viral Bold | Face-tracked crop, or full frame over blur when there are no faces | Yellow word-by-word highlight | Punch-in on loud moments |
| Podcast | Tight face crop | White pop-in | Gentle |
| Gaming | Full frame over a blurred background | Big green word-by-word | Light |
| Clean | Face-tracked crop | Small static captions | None |
| Cinematic | Full frame over blur | Lower-third pop-in | None |
| No captions | Face-tracked crop | None | Light |

To add a template, add one entry to `backend/app/templates.py`.

## What is free, and the limits

| Part | Uses | Cost | Limits |
|---|---|---|---|
| Transcription | faster-whisper (MIT), models from Hugging Face | Free | One download per model. On a 4-core CPU, `small` runs at about 1x to 3x real time, so a 2-hour video can take an hour. `WHISPER_MODEL=base` is faster but less accurate. For Hindi and other non-English speech, `WHISPER_MODEL=medium` is much more accurate but about 2x to 3x slower. A CUDA GPU is used automatically if present. |
| Face tracking | OpenCV YuNet (MIT), bundled | Free | Follows the largest face. It does not detect who is speaking when two people are on screen. |
| Editing and captions | ffmpeg, libass, Montserrat and Noto fonts (OFL), bundled | Free | Rendering a 45 s clip takes about 30 to 60 s on 4 cores. Captions draw Latin, Cyrillic, Hindi and the other main Indian scripts, and Urdu/Arabic. Characters in other scripts, such as Chinese, are left out of captions instead of showing as empty boxes. |
| Highlight detection | Local scoring code | Free | Hook and emotion word lists are English. Other languages still score on loudness, topic and pace. |
| Titles and hashtags | Local heuristic | Free | Plainer than an LLM's titles. |
| AI titles and scoring (optional) | Gemini, Groq, Cloudflare Workers AI, OpenRouter, or Ollama | Free tiers | Each has daily limits (see [AI providers](#ai-providers)). ClipForge calls none of them unless you add its key. |
| Cloud speech to text (optional) | Groq or Cloudflare Workers AI Whisper | Free tiers | Off unless `TRANSCRIBE_PROVIDERS` names them. They receive the audio. |
| Video links | yt-dlp (Unlicense) with the Deno runtime (MIT) | Free | YouTube changes its site often. When links stop working, run `docker compose build --no-cache` to get the newest yt-dlp. Download only videos you own or have permission to use: YouTube's terms forbid downloading other videos. |
| Live recording | streamlink (BSD-2) and ffmpeg | Free | Uses your bandwidth while a channel is live. Sites change often, so keep streamlink updated. |
| Live chat | Twitch anonymous IRC | Free | Twitch only. YouTube and Kick clips use audio and speech. |
| Storage | Your disk | Free | Videos and clips are deleted 24 hours after you add them (`KEEP_HOURS`). Whisper models stay, so they download once. |

### Free hosting, honestly

There is no free cloud tier that processes unlimited video. Your options:

- **Your own computer.** This is the best fit and costs nothing. ClipForge listens on `127.0.0.1` by default, so only you can reach it.
- **Oracle Cloud Always Free.** It offers ARM virtual machines with enough CPU and RAM for ClipForge. The Docker image builds on ARM. Oracle asks for a card to verify identity, and free capacity is often unavailable in busy regions. Check their current limits before you rely on it.
- **Hugging Face Spaces (free CPU).** Fine for a demo. The Space sleeps when idle and its disk resets on restart, so clips do not persist.
- **Render, Railway, Vercel free tiers.** Not suitable. They give too little CPU and RAM for video work and no lasting disk.

ClipForge refuses to listen on a public address without `APP_PASSWORD`. Docker Compose publishes the port on `127.0.0.1` only. To reach it from other machines, change the port line to `"8000:8000"` and set a password.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | Where videos, clips, the database and live buffers go |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Address to listen on. Docker sets `HOST=0.0.0.0` |
| `APP_PASSWORD` | unset | Password for the whole app (browser login prompt, any username). Required when `HOST` is not a loopback address |
| `ALLOW_NO_PASSWORD` | unset | Set to `1` to run on a non-loopback `HOST` without a password, only when something else restricts access (Compose sets it because its port is bound to `127.0.0.1`) |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium` or `large-v3`. Bigger is more accurate and slower |
| `WHISPER_DEVICE` | `auto` | `cpu`, `cuda` or `auto` |
| `LIVE_WHISPER_MODEL` | `tiny` | Model for real-time speech analysis on livestreams |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` | unset | Turn on that AI text provider |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | unset | Turn on Cloudflare Workers AI (both are needed) |
| `TEXT_PROVIDERS` | every one with a key | Order to try text providers, for example `groq,gemini` |
| `GEMINI_MODEL`, `GROQ_MODEL`, `CLOUDFLARE_MODEL`, `OPENROUTER_MODEL` | see below | Model override per provider |
| `TRANSCRIBE_PROVIDERS` | `local` | Order to try speech to text: `local`, `groq`, `cloudflare` |
| `TRANSCRIBE_LANGUAGE` | detect | Force the spoken language, for example `hi` |
| `GROQ_WHISPER_MODEL`, `CLOUDFLARE_WHISPER_MODEL` | see below | Speech model override |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | unset | Any other OpenAI-compatible endpoint, for example Ollama at `http://localhost:11434/v1`. Tried first |
| `MAX_UPLOAD_MB` | `8192` | Largest upload accepted. Videos longer than 12 hours are rejected |
| `WORKERS` | `1` | Jobs processed at the same time. Each one uses all CPU cores, so raise it only on big machines |
| `LIVE_ENABLED` | `1` | Set to `0` to turn off live monitoring |
| `KEEP_HOURS` | `24` | Hours after which videos and their clips are deleted. `0` keeps everything |

## AI providers

**Recommended: keep every key in Supabase.** The [AI gateway](docs/AI_GATEWAY.md) is a Supabase Edge Function that holds the keys as secrets, rotates between several keys per provider, cools down rate-limited keys and falls back across providers. ClipForge then needs only `GATEWAY_URL` and `GATEWAY_TOKEN`.

Everything works without a key. Keys add better titles, AI clip scoring in any language, and faster speech to text on computers without a GPU. Put them in `.env` (copy `.env.example`); `.env` is ignored by Git. Then check them with one tiny request each:

```bash
docker compose exec clipforge python -m app.ai
```

| Task | Default | Free cloud options, in the order tried |
|---|---|---|
| Speech to text | Local faster-whisper | Groq `whisper-large-v3-turbo`, Cloudflare `@cf/openai/whisper-large-v3-turbo` |
| Titles, descriptions, hashtags | Local heuristic | Gemini `gemini-3.8-flash` (native API, so new `AQ.` keys work), Z.ai `glm-4.5-flash` (gateway only), Groq `openai/gpt-oss-120b`, Cloudflare `@cf/meta/llama-3.3-70b-instruct-fp8-fast`, OpenRouter `openrouter/free` |
| Clip scoring | Local signals | The same text provider rates each candidate; its score is 40% of the final score and picks which clips make the cut |
| Live moment detection | Local (audio, Whisper `tiny`, chat) | None: a cloud call every few seconds per stream would spend a free quota in hours |

If a provider fails, times out or runs out of its free quota, the next one takes over, and the local model is the last step. Speech goes ten minutes at a time, and finished parts are saved, so a retry never spends quota twice.

Free limits as published in November 2026. They change often, so check each console:

| Provider | Free limit | About |
|---|---|---|
| Groq Whisper | 28,800 audio seconds a day, 2,000 requests a day, 25 MB per file | 8 hours of audio a day |
| Cloudflare Workers AI | 10,000 Neurons a day, shared by all models | About 214 minutes of Whisper a day, or about 50 title requests |
| Gemini API | Set per project, shown in AI Studio; lowered several times since 2025 | Enough for titles on a few dozen videos a day |
| OpenRouter `:free` models | 20 requests a minute, 50 a day (1,000 after a one-time $10 credit purchase) | Fallback only |

Privacy: a cloud provider receives the audio or transcript you send it, and free tiers may use it to improve their models. Local processing sends nothing.

## How it works

The backend is one Python process: FastAPI, SQLite, and background worker threads. The frontend is a React single-page app served by the same process. Jobs wait in a queue in SQLite. A crash or restart resumes them, and every expensive step caches its output. Live channels each get their own thread. A live moment becomes a short video that goes through the same pipeline as an upload.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the data model, the design decisions and the failure modes. [ROADMAP.md](ROADMAP.md) has what is built and what comes next.

## Development

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests need ffmpeg and download Whisper `tiny` (about 75 MB) once. They cover scoring, captions, camera motion, live signals, the job queue and the HTTP API. An end-to-end test transcribes a real speech recording and renders vertical clips from it.

For the frontend with live reload, keep the backend running on port 8000, then run:

```bash
cd frontend
npm install
npm run dev
```

## Bundled third-party files

- `backend/app/assets/models/face_detection_yunet_2023mar.onnx`. YuNet by Shiqi Yu, MIT license (`LICENSE-yunet`).
- `backend/app/assets/fonts/Montserrat-*.ttf`. SIL Open Font License (`OFL-Montserrat.txt`).
- `backend/app/assets/fonts/NotoSans*-Bold.ttf`. Noto Sans for Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu, Kannada, Malayalam and Arabic, by the Noto Project Authors. SIL Open Font License (`OFL-Noto.txt`).
- `backend/tests/assets/jfk.flac`. John F. Kennedy's 1961 inaugural address, public domain.
- `backend/tests/assets/astronaut.png`. NASA portrait of Eileen Collins, public domain, via scikit-image.
