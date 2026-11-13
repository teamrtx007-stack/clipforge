"""Settings, read once from environment variables, and the on-disk layout derived from them."""
import os
from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password: str | None
    whisper_model: str
    whisper_device: str
    live_whisper_model: str
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    max_upload_bytes: int
    workers: int
    live_enabled: bool


def _load() -> Settings:
    env = os.environ.get
    return Settings(
        data_dir=Path(env("DATA_DIR", "data")).resolve(),
        password=env("APP_PASSWORD") or None,
        whisper_model=env("WHISPER_MODEL", "small"),
        whisper_device=env("WHISPER_DEVICE", "auto"),
        live_whisper_model=env("LIVE_WHISPER_MODEL", "tiny"),
        llm_base_url=(env("LLM_BASE_URL") or "").rstrip("/") or None,
        llm_api_key=env("LLM_API_KEY") or None,
        llm_model=env("LLM_MODEL") or None,
        max_upload_bytes=int(env("MAX_UPLOAD_MB", "8192")) * 1024 * 1024,
        workers=max(1, int(env("WORKERS", "1"))),
        live_enabled=env("LIVE_ENABLED", "1") != "0",
    )


settings = _load()

# Everything under media/ is served to the browser; the database and live buffers are not.
MEDIA = settings.data_dir / "media"
DB_PATH = settings.data_dir / "clipforge.sqlite3"
LIVE_DIR = settings.data_dir / "live"


def source_dir(source_id: int) -> Path:
    return MEDIA / "sources" / str(source_id)


def clip_dir(clip_id: int) -> Path:
    return MEDIA / "clips" / str(clip_id)
