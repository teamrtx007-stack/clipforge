"""ffmpeg and ffprobe calls. Outputs go to a temp name and are renamed when complete,
so a crash never leaves a half-written file that a later run would trust."""
import json
import subprocess
import tempfile
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HOP = 0.5  # seconds per loudness sample
MAX_DURATION = 12 * 3600  # longer files are rejected; it also bounds every array sized by duration
SAMPLE_RATE = 16000


class MediaError(RuntimeError):
    pass


def tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise MediaError(tail(p.stderr) or f"{args[0]} exited with code {p.returncode}")
    return p.stdout


def temp_path(path: Path) -> Path:
    """Sibling path that keeps the extension, because ffmpeg picks the format from it."""
    return path.with_name(f"{path.stem}.tmp{path.suffix}")


@contextmanager
def writing(path: Path) -> Iterator[Path]:
    """Yield a temp sibling to write. It replaces `path` only when complete, and is deleted if writing fails."""
    tmp = temp_path(path)
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _rate(value: str | None) -> float:
    try:
        num, _, den = (value or "").partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> Probe:
    try:
        out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    except MediaError as exc:
        raise MediaError(f"Not a playable video: {exc}") from exc
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise MediaError("The file has no video track")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("Could not read the video length")
    if duration > MAX_DURATION:
        raise MediaError(f"Videos longer than {MAX_DURATION // 3600} hours are not supported. Split it first.")
    width, height = int(video["width"]), int(video["height"])
    rotation = next((int(d.get("rotation", 0)) for d in video.get("side_data_list", []) if "rotation" in d), 0)
    if abs(rotation) % 180 == 90:  # phone video; ffmpeg auto-rotates frames on decode
        width, height = height, width
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    fps = min(fps, 60.0) if fps >= 1 else 30.0
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Probe(duration, width, height, round(fps, 3), has_audio)


def extract_audio(video: Path, wav: Path) -> None:
    with writing(wav) as tmp:
        run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp)])


def compress_audio(wav: Path, start: float, duration: float, out: Path) -> None:
    """A slice of the WAV as 64 kb/s mono MP3: ten minutes is about 4.8 MB, under free-tier upload limits."""
    with writing(out) as tmp:
        run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
             "-i", str(wav), "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(tmp)])


def loudness_curve(wav: Path) -> np.ndarray:
    """RMS loudness in dBFS for every HOP seconds. Reads in 2 minute chunks, so long files never load whole."""
    chunks = []
    with wave.open(str(wav)) as w:
        hop = int(w.getframerate() * HOP)
        while frames := w.readframes(hop * 240):
            x = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
            x = np.pad(x, (0, (-len(x)) % hop))
            chunks.append(np.sqrt(np.mean(x.reshape(-1, hop) ** 2, axis=1)))
    rms = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return 20 * np.log10(np.maximum(rms, 1e-5))


def frames(video: Path, start: float, duration: float, width: int, height: int, fps: float) -> Iterator[np.ndarray]:
    """Decoded BGR frames of the range, resampled to `fps` and scaled to exactly width x height.
    ffmpeg's error output goes to a temp file, so a damaged video cannot fill a pipe and stall both sides."""
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
         "-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL,
    )
    size = width * height * 3
    finished = False
    try:
        while len(buf := proc.stdout.read(size)) == size:
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
        finished = True
    finally:
        proc.stdout.close()
        if not finished:  # the caller stopped early or failed
            proc.kill()
        code = proc.wait()
    if code != 0:
        errors.seek(0)
        raise MediaError(f"Could not decode the video: {tail(errors.read().decode(errors='replace'))}")


def thumbnail(video: Path, at: float, out: Path) -> None:
    with writing(out) as tmp:
        run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{max(at, 0):.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(tmp)])


def concat(parts: list[Path], out: Path) -> None:
    """Join stream segments without re-encoding."""
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    try:
        with writing(out) as tmp:
            run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-movflags", "+faststart", str(tmp)])
    finally:
        listing.unlink(missing_ok=True)
