"""Settings, read once from environment variables, and the on-disk layout derived from them."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
FONTS = ASSETS / "fonts"  # caption fonts; libass falls back across all of them, glyph by glyph


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible chat endpoint. The key never appears in repr, logs or API responses."""
    name: str
    base_url: str
    model: str
    key: str | None = field(default=None, repr=False)


# Text providers, best first. Each is used only when its key is set. "gateway" is the ClipForge AI gateway on
# Supabase (supabase/functions/ai-gateway): it holds the real provider keys, so the worker holds only one revocable
# token. The others call a provider directly with a key in this machine's .env.
# Model IDs checked November 2026; override them with the *_MODEL variables.
TEXT_PRESETS = {
    "gateway": ("{gateway}/v1", "GATEWAY_TOKEN", "GATEWAY_MODEL", "auto"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY", "GEMINI_MODEL",
               "gemini-3.8-flash"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1", "CLOUDFLARE_API_TOKEN",
                   "CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "openrouter/free"),
}
SPEECH_PROVIDERS = ("local", "gateway", "groq", "cloudflare")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password: str | None = field(repr=False)
    whisper_model: str
    whisper_device: str
    live_whisper_model: str
    text_providers: tuple[Provider, ...]
    transcribe_providers: tuple[str, ...]
    transcribe_language: str | None
    gateway_url: str | None
    gateway_token: str | None = field(repr=False)
    groq_key: str | None = field(repr=False)
    groq_whisper_model: str
    cloudflare_account_id: str | None
    cloudflare_token: str | None = field(repr=False)
    cloudflare_whisper_model: str
    max_upload_bytes: int
    workers: int
    live_enabled: bool
    keep_hours: float


def _names(value: str | None, allowed) -> tuple[str, ...]:
    names = tuple(n.strip().lower() for n in (value or "").split(",") if n.strip())
    if unknown := [n for n in names if n not in allowed]:
        raise ValueError(f"Unknown AI provider {', '.join(unknown)}. Use: {', '.join(allowed)}")
    return names


def _text_providers(env) -> tuple[Provider, ...]:
    """TEXT_PROVIDERS picks and orders them; by default every preset with a key, plus LLM_BASE_URL
    (for example a local Ollama) first, because it is the most private."""
    found: dict[str, Provider] = {}
    if (base := (env("LLM_BASE_URL") or "").rstrip("/")) and env("LLM_MODEL"):
        found["custom"] = Provider("custom", base, env("LLM_MODEL"), env("LLM_API_KEY") or None)
    account, gateway = env("CLOUDFLARE_ACCOUNT_ID"), (env("GATEWAY_URL") or "").rstrip("/")
    for name, (base, key_var, model_var, model) in TEXT_PRESETS.items():
        needs = {"cloudflare": account, "gateway": gateway}.get(name, True)
        if (key := env(key_var)) and needs:
            found[name] = Provider(name, base.format(account=account, gateway=gateway), env(model_var) or model, key)
    chosen = _names(env("TEXT_PROVIDERS"), ("custom", *TEXT_PRESETS)) or tuple(found)
    return tuple(found[n] for n in chosen if n in found)


def _load() -> Settings:
    env = os.environ.get
    return Settings(
        data_dir=Path(env("DATA_DIR", "data")).resolve(),
        password=env("APP_PASSWORD") or None,
        whisper_model=env("WHISPER_MODEL", "small"),
        whisper_device=env("WHISPER_DEVICE", "auto"),
        live_whisper_model=env("LIVE_WHISPER_MODEL", "tiny"),
        text_providers=_text_providers(env),
        transcribe_providers=_names(env("TRANSCRIBE_PROVIDERS"), SPEECH_PROVIDERS) or ("local",),
        transcribe_language=env("TRANSCRIBE_LANGUAGE") or None,
        gateway_url=(env("GATEWAY_URL") or "").rstrip("/") or None,
        gateway_token=env("GATEWAY_TOKEN") or None,
        groq_key=env("GROQ_API_KEY") or None,
        groq_whisper_model=env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
        cloudflare_account_id=env("CLOUDFLARE_ACCOUNT_ID") or None,
        cloudflare_token=env("CLOUDFLARE_API_TOKEN") or None,
        cloudflare_whisper_model=env("CLOUDFLARE_WHISPER_MODEL", "@cf/openai/whisper-large-v3-turbo"),
        max_upload_bytes=int(env("MAX_UPLOAD_MB", "8192")) * 1024 * 1024,
        workers=max(1, int(env("WORKERS", "1"))),
        live_enabled=env("LIVE_ENABLED", "1") != "0",
        keep_hours=float(env("KEEP_HOURS", "24")),
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
