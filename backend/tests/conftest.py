import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Settings are read at import time, so point them at a throwaway data dir before importing the app.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ.setdefault("WHISPER_MODEL", "tiny")
os.environ["LIVE_ENABLED"] = "0"
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("LLM_BASE_URL", None)

ASSETS = Path(__file__).parent / "assets"


@pytest.fixture(scope="session", autouse=True)
def database():
    from app import db

    db.init()


@pytest.fixture(scope="session")
def talking_video(tmp_path_factory) -> Path:
    """44 s, 1280x720. A public-domain astronaut portrait drifts left to right and back while JFK
    speaks four times. The second take is 12 dB louder, so it should read as the highlight."""
    out = tmp_path_factory.mktemp("media") / "talk.mp4"
    graph = (
        "[1:v]scale=400:-1[face];"
        "[0:v][face]overlay=x='60+(W-w-120)*(0.5-0.5*cos(t*PI/22))':y=(H-h)/2:shortest=1[v];"
        "[2:a]asplit=4[a1][a2][a3][a4];[a2]volume=12dB[a2l];"
        "[a1][a2l][a3][a4]concat=n=4:v=0:a=1[a]"
    )
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x1d2733:s=1280x720:r=30:d=44",
         "-loop", "1", "-i", str(ASSETS / "astronaut.png"), "-i", str(ASSETS / "jfk.flac"),
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", "44",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
        check=True,
    )
    return out


@pytest.fixture
def override():
    """Temporarily change frozen settings: override(llm_model="x")."""
    from app.config import settings

    saved: dict = {}

    def apply(**values):
        for key, value in values.items():
            saved.setdefault(key, getattr(settings, key))
            object.__setattr__(settings, key, value)

    yield apply
    for key, value in saved.items():
        object.__setattr__(settings, key, value)
