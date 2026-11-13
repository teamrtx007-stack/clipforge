#!/usr/bin/env bash
# Run ClipForge without Docker. Needs Python 3.11+, Node 20+ and ffmpeg on PATH.
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "ffmpeg is missing. Install it first (apt install ffmpeg, brew install ffmpeg, or winget install ffmpeg)."; exit 1; }
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
if [ ! -d frontend/dist ]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
cd backend
DATA_DIR="${DATA_DIR:-../data}" exec .venv/bin/python -m app
