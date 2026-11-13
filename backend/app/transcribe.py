"""Speech to text with faster-whisper (open source, runs locally). Models download once from Hugging Face."""
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .config import settings

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
