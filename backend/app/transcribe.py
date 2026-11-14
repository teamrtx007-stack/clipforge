"""Speech to text. faster-whisper runs locally (open source; models download once from Hugging Face).
TRANSCRIBE_PROVIDERS can put free-tier cloud Whisper (Groq, Cloudflare) in front of it, chunk by chunk."""
import json
import logging
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import ai, media
from .config import settings

log = logging.getLogger("clipforge.transcribe")

_models: dict[str, object] = {}
_lock = threading.Lock()


def model(name: str):
    with _lock:
        if name not in _models:
            from faster_whisper import WhisperModel  # heavy import, only when transcription runs

            try:
                _models[name] = WhisperModel(name, device=settings.whisper_device, compute_type="auto")
            except Exception as exc:
                raise RuntimeError(
                    f"Could not load the Whisper model '{name}'. The first run downloads it from "
                    f"huggingface.co, so check the internet connection. ({exc})"
                ) from exc
        return _models[name]


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono 16-bit WAV (what media.extract_audio writes) as float32 samples."""
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def transcribe(audio: Path | np.ndarray, on_progress: Callable[[float], None] = lambda p: None,
               model_name: str | None = None, fast: bool = False) -> dict:
    """Returns {"language", "segments": [{"start", "end", "text", "words": [{"start", "end", "word"}]}]}.

    `audio` is a 16 kHz mono WAV path or float32 samples. Samples skip faster-whisper's own decoder.
    `fast` skips word timing and beam search, for live hype scoring that only needs the text.
    """
    segments, info = model(model_name or settings.whisper_model).transcribe(
        read_wav(audio) if isinstance(audio, Path) else audio,
        word_timestamps=not fast,
        vad_filter=True,
        beam_size=1 if fast else 5,
        language=settings.transcribe_language,  # e.g. "hi": stops Hindi coming out in Urdu script
    )
    out = []
    for seg in segments:
        out.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                      for w in seg.words or [] if w.word.strip()],
        })
        if info.duration:
            on_progress(min(seg.end / info.duration, 1.0))
    return {"language": info.language, "segments": out}


CHUNK = 600  # seconds of audio per cloud request


def _cuts(duration: float, loudness: np.ndarray | None) -> list[float]:
    """Chunk boundaries about every ten minutes, each moved to the quietest half second within 20 s of
    its target so no word is cut in two."""
    cuts = [0.0]
    while duration - cuts[-1] > CHUNK * 1.05:  # no tiny last chunk
        target = cuts[-1] + CHUNK
        lo = int((target - 20) / media.HOP)
        if loudness is not None and len(window := loudness[lo:lo + int(40 / media.HOP)]):
            target = (lo + int(np.argmin(window))) * media.HOP
        cuts.append(target)
    return cuts + [duration]


def _read_range(wav: Path, start: float, end: float) -> np.ndarray:
    with wave.open(str(wav)) as w:
        w.setpos(min(int(start * w.getframerate()), w.getnframes()))
        frames = w.readframes(int((end - start) * w.getframerate()))
    return np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0


def _shift(result: dict, offset: float) -> dict:
    move = lambda d: {**d, "start": round(d["start"] + offset, 3), "end": round(d["end"] + offset, 3)}  # noqa: E731
    return {**result, "segments": [{**move(s), "words": [move(w) for w in s["words"]]} for s in result["segments"]]}


def _chunk(wav: Path, start: float, end: float, folder: Path) -> dict:
    """One chunk through TRANSCRIBE_PROVIDERS in order; the first that answers wins."""
    failures, mp3 = [], folder / f"chunk.{int(start)}.mp3"
    try:
        for name in settings.transcribe_providers:
            if name == "local":
                return _shift(transcribe(_read_range(wav, start, end)), start)
            try:
                if not mp3.exists():
                    media.compress_audio(wav, start, end - start, mp3)
                return ai.transcribe_chunk(name, mp3, start)
            except ai.AIError as exc:
                failures.append(str(exc))
                log.warning("Speech to text failed, trying the next provider: %s", exc)
    finally:
        mp3.unlink(missing_ok=True)
    raise RuntimeError("Speech to text failed with every provider: " + "; ".join(failures))


def transcribe_file(wav: Path, duration: float, folder: Path, loudness: np.ndarray | None = None,
                    on_progress: Callable[[float], None] = lambda p: None) -> dict:
    """A whole video's transcript. With only the local model this is `transcribe`. With cloud providers it
    goes ten minutes at a time, so one that runs out of free quota halfway hands the rest to the next,
    and finished chunks are saved in `folder` so a retry never spends quota on them twice."""
    if settings.transcribe_providers == ("local",):
        return transcribe(wav, on_progress)
    cuts, segments, language = _cuts(duration, loudness), [], None
    for i, (start, end) in enumerate(zip(cuts, cuts[1:])):
        part = folder / f"transcript.part{i:03d}.json"
        if part.exists():
            result = json.loads(part.read_text())
        else:
            result = _chunk(wav, start, end, folder)
            with media.writing(part) as tmp:
                tmp.write_text(json.dumps(result))
        segments += result["segments"]
        language = language or result.get("language")
        on_progress(end / duration)
    return {"language": language, "segments": segments}
