# Roadmap

ClipForge is free. It has no plans, no payments and no credits. Every feature below works for every user.

## Build order

| # | Milestone | Status |
|---|---|---|
| 1 | Website dashboard: projects, sources, clips, queue | done |
| 2 | Video upload: streamed to disk, with progress, type and size checks | done |
| 3 | Video processing: probe, audio extraction, loudness curve | done |
| 4 | AI transcription: faster-whisper with word timestamps, runs locally | done |
| 5 | AI highlight detection: hook, energy, pace, topic and emotion scoring, with optional LLM rerank | done |
| 6 | Automatic editing: face tracking, smart crop, punch-in zoom, 9:16, animated captions, templates | done |
| 7 | Shorts generation: MP4 render, thumbnail, title, description, hashtags, download | done |
| 8 | Live AI Clipping Studio: channels, live and offline status, auto recording | done |
| 9 | Rolling buffer: segmented recording with pruning, clips cut from the buffer | done |
| 10 | Audio, speech and chat analysis: loudness spikes, real-time speech, Twitch chat rate | done |
| 11 | Multi-stream monitoring: one supervisor thread per channel, shared queue | done |
| 12 | Testing and deployment: pytest, end-to-end runs, Docker, free hosting guide | done |
| 13 | Paste a YouTube link: yt-dlp downloads it, then the same pipeline runs | done |
| 14 | Automatic cleanup: videos and clips deleted 24 hours after they are added | done |
| 15 | Captions in Hindi, the other main Indian scripts and Urdu; characters no font can draw are left out, never boxes | done |
| 16 | White dashboard: sidebar with creators, link box, stat cards, 14-day activity chart, creators table | done |

## Next

- Split-screen layout (face cam on top, gameplay below).
- Chat analysis for YouTube and Kick.
- Caption editor that fixes single words before rendering.
- GPU encoding (NVENC) when a GPU is present.

## Later, only if monetization is added

These are not being built. The code leaves room for them without a rewrite.

- User accounts. Add a `users` table and an `owner_id` column on `projects`. `api.py` already reads the current user from one auth dependency.
- Usage limits. They would wrap `jobs.enqueue`, the single place where work enters the queue.
- Payments. They would be a new module that never touches the pipeline.
