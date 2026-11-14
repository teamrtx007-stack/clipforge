"""Free-tier cloud AI behind two calls, both optional: `chat_json` for text and `transcribe_chunk` for speech.

Text goes to OpenAI-compatible chat endpoints (Gemini, Groq, Cloudflare Workers AI, OpenRouter, or LLM_BASE_URL),
tried in order: a provider that fails, times out or runs out of free quota hands over to the next.
Speech goes to Groq or Cloudflare Workers AI Whisper. Callers keep a local fallback (heuristic titles,
faster-whisper), so a missing key or a dead provider never stops a video.

Keys come from the environment through `config.settings` and are never logged or returned.
Run `python -m app.ai` to check every configured provider with one tiny request each.
"""
import base64
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .config import Provider, settings

log = logging.getLogger("clipforge.ai")

GROQ_URL = "https://api.groq.com/openai/v1"
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4"
TIMEOUT = 90  # seconds per request; Whisper on a 10-minute chunk takes a few seconds on Groq
MAX_WAIT = 20  # longest Retry-After worth waiting for; a longer one means the free quota is spent for now


class AIError(Exception):
    """A provider could not answer. The message is safe to show: it never contains a key."""


def _post(url: str, body: bytes, headers: dict[str, str], name: str) -> dict:
    """POST and parse JSON. Retries network errors and 5xx twice, waits out one short 429, and fails at once
    on 4xx such as a bad key, so the caller can move on to the next provider."""
    waited = False
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "ClipForge", **headers})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode(errors="replace")
            if secret := headers.get("Authorization", "").removeprefix("Bearer "):
                detail = detail.replace(secret, "[key]")  # some APIs echo the key they refused
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") or "0"
                wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0  # may be a date
                if not waited and 0 < wait <= MAX_WAIT:
                    waited = True
                    time.sleep(wait)
                    continue
                raise AIError(f"{name}: free quota or rate limit reached (429)") from exc
            if exc.code in (401, 403):
                raise AIError(f"{name}: the API key or token was refused ({exc.code}); check it in .env") from exc
            if exc.code < 500 or attempt == 2:
                raise AIError(f"{name}: HTTP {exc.code}: {_clean(detail)}") from exc
        except (OSError, http.client.HTTPException) as exc:  # refused, reset, timed out, cut off midway
            if attempt == 2:
                raise AIError(f"{name}: could not connect ({getattr(exc, 'reason', exc)})") from exc
        except json.JSONDecodeError as exc:
            raise AIError(f"{name}: the answer was not JSON") from exc
        time.sleep(2 ** attempt)
    raise AIError(f"{name}: no answer")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text)[:200]


# Text

def _chat(p: Provider, prompt: str, max_tokens: int) -> str:
    body = {"model": p.model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4, "max_tokens": max_tokens}
    if p.name in ("gemini", "groq"):  # thinking models: their thinking counts against max_tokens
        body["reasoning_effort"] = "low"
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {p.key}"} if p.key else {})}
    answer = _post(f"{p.base_url}/chat/completions", json.dumps(body).encode(), headers, p.name)
    try:
        content = answer["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"{p.name}: unexpected answer shape") from exc
    if not isinstance(content, str) or not content.strip():
        raise AIError(f"{p.name}: empty answer (the model may have refused or run out of tokens)")
    return content


def chat_json(prompt: str, max_tokens: int = 8000) -> tuple[dict, Provider]:
    """Ask each configured text provider in turn for a JSON object. Raises AIError when none answers."""
    failures = []
    for p in settings.text_providers:
        try:
            content = _chat(p, prompt, max_tokens)
            match = re.search(r"\{.*\}", content, re.DOTALL)  # models wrap JSON in prose or code fences
            result = json.loads(match.group(0) if match else content)
            if isinstance(result, dict):
                return result, p
            failures.append(f"{p.name}: the answer was not a JSON object")
        except AIError as exc:
            failures.append(str(exc))
        except json.JSONDecodeError:
            failures.append(f"{p.name}: the answer was not valid JSON")
        log.warning("AI provider failed, trying the next one: %s", failures[-1])
    raise AIError("; ".join(failures) or "no AI text provider is configured")


# Speech

def _multipart(fields: dict[str, str | list[str]], file: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
                 "Content-Type: audio/mpeg\r\n\r\n".encode() + file.read_bytes() + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _words(items, offset: float) -> list[dict]:
    return [{"start": round(offset + float(w["start"]), 3), "end": round(offset + float(w["end"]), 3),
             "word": str(w["word"]).strip()} for w in items or [] if str(w.get("word", "")).strip()]


def _segments(segments, words: list[dict] | None, offset: float) -> list[dict]:
    """Shift to the chunk's place in the video and attach words to their segment, as faster-whisper does."""
    out = []
    for s in segments:
        start, end = round(offset + float(s["start"]), 3), round(offset + float(s["end"]), 3)
        own = _words(s.get("words"), offset) if words is None else [w for w in words if start <= w["start"] < end]
        if text := str(s.get("text", "")).strip():
            out.append({"start": start, "end": end, "text": text, "words": own})
    return out


def _groq(audio: Path, offset: float) -> dict:
    if not settings.groq_key:
        raise AIError("groq: GROQ_API_KEY is not set")
    fields = {"model": settings.groq_whisper_model, "response_format": "verbose_json", "temperature": "0",
              "timestamp_granularities[]": ["segment", "word"]}
    if settings.transcribe_language:
        fields["language"] = settings.transcribe_language
    body, content_type = _multipart(fields, audio)
    r = _post(f"{GROQ_URL}/audio/transcriptions", body,
              {"Content-Type": content_type, "Authorization": f"Bearer {settings.groq_key}"}, "groq")
    if not isinstance(r.get("segments"), list):
        raise AIError("groq: the answer had no segments")
    return {"language": r.get("language"), "segments": _segments(r["segments"], _words(r.get("words"), offset), offset)}


def _cloudflare(audio: Path, offset: float) -> dict:
    if not (settings.cloudflare_token and settings.cloudflare_account_id):
        raise AIError("cloudflare: CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must both be set")
    body = {"audio": base64.b64encode(audio.read_bytes()).decode(), "vad_filter": True}
    if settings.transcribe_language:
        body["language"] = settings.transcribe_language
    r = _post(f"{CLOUDFLARE_URL}/accounts/{settings.cloudflare_account_id}/ai/run/"
              f"{settings.cloudflare_whisper_model}", json.dumps(body).encode(),
              {"Content-Type": "application/json", "Authorization": f"Bearer {settings.cloudflare_token}"},
              "cloudflare")
    result = r.get("result") if r.get("success", True) else None
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        raise AIError(f"cloudflare: unexpected answer {_clean(json.dumps(r.get('errors') or r))}")
    return {"language": (result.get("transcription_info") or {}).get("language"),
            "segments": _segments(result["segments"], None, offset)}


SPEECH = {"groq": _groq, "cloudflare": _cloudflare}


def transcribe_chunk(name: str, audio: Path, offset: float) -> dict:
    """Transcribe one compressed audio chunk that starts `offset` seconds into the video."""
    return SPEECH[name](audio, offset)


# Connectivity check: python -m app.ai

def check() -> int:
    """One tiny request per configured provider. Prints names, models and outcomes, never keys."""
    print(f"Speech to text order: {', '.join(settings.transcribe_providers)}")
    if not settings.text_providers:
        print("No AI text provider is configured. Set GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, "
              "CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN, or LLM_BASE_URL + LLM_MODEL.")
    failed = 0
    for p in settings.text_providers:
        started = time.monotonic()
        try:
            reply = _chat(p, 'Reply with the JSON object {"ok": true} and nothing else.', 300)
            print(f"  ok    {p.name} ({p.model}) answered in {time.monotonic() - started:.1f} s: {_clean(reply)[:40]}")
        except AIError as exc:
            failed += 1
            print(f"  FAIL  {p.name} ({p.model}): {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(check())
