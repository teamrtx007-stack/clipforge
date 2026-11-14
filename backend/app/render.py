"""Render a vertical 1080x1920 MP4.

ffmpeg decodes the source into raw frames. Python crops, zooms and composes each frame with OpenCV.
A second ffmpeg encodes H.264, burns in the captions with libass and copies the audio.
"""
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from .camera import CameraPath
from .config import FONTS
from .media import MediaError, Probe, frames, tail, temp_path
from .templates import Layout

OUT_W, OUT_H = 1080, 1920
MAX_DECODE_H = 1920


def crop_box(cx: float, cy: float, zoom: float, src_w: int, src_h: int) -> tuple[float, float, float]:
    """Top-left corner and width of the 9:16 crop centered on (cx, cy), kept inside the frame."""
    w = min(src_h * OUT_W / OUT_H, src_w) / zoom
    h = w * OUT_H / OUT_W
    x0 = min(max(cx * src_w - w / 2, 0.0), src_w - w)
    y0 = min(max(cy * src_h - h / 2, 0.0), max(src_h - h, 0.0))
    return x0, y0, w


def _fill(frame: np.ndarray, cx: float, cy: float, zoom: float) -> np.ndarray:
    src_h, src_w = frame.shape[:2]
    x0, y0, w = crop_box(cx, cy, zoom, src_w, src_h)
    s = OUT_W / w
    m = np.float32([[s, 0, -x0 * s], [0, s, -y0 * s]])
    return cv2.warpAffine(frame, m, (OUT_W, OUT_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _fit_blur(frame: np.ndarray, zoom: float) -> np.ndarray:
    src_h, src_w = frame.shape[:2]
    # Background: the center of the frame, blurred at low resolution (cheap), then scaled up and darkened.
    bw = min(src_w, src_h * OUT_W / OUT_H)
    bx = (src_w - bw) / 2
    bh = bw * OUT_H / OUT_W
    by = max((src_h - bh) / 2, 0)
    small = cv2.resize(frame[int(by):int(by + bh), int(bx):int(bx + bw)], (OUT_W // 8, OUT_H // 8),
                       interpolation=cv2.INTER_AREA)
    bg = cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
    bg = cv2.convertScaleAbs(bg, alpha=0.55)
    s = OUT_W / src_w * zoom
    m = np.float32([[s, 0, OUT_W / 2 - src_w * s / 2], [0, s, OUT_H / 2 - src_h * s / 2]])
    return cv2.warpAffine(frame, m, (OUT_W, OUT_H), dst=bg, flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_TRANSPARENT)


def _filter_value(path: Path, cwd: Path) -> str:
    # A relative path avoids drive letters and most characters ffmpeg's filter syntax treats as special.
    return "'" + Path(os.path.relpath(path, cwd)).as_posix().replace("'", r"'\''") + "'"


def decode_size(src_w: int, src_h: int) -> tuple[int, int]:
    """Frame size the decoder outputs. Computed here and passed to ffmpeg, so both sides agree exactly."""
    if src_h <= MAX_DECODE_H:
        return src_w, src_h
    return max(2, round(src_w * MAX_DECODE_H / src_h / 2) * 2), MAX_DECODE_H


def render(video: Path, probe: Probe, start: float, end: float, camera: CameraPath, zoom: np.ndarray,
           layout: Layout, captions: Path | None, out: Path, on_progress: Callable[[float], None]) -> None:
    dur, fps = end - start, probe.fps
    vf = "format=yuv420p"
    if captions is not None:
        vf = f"ass={_filter_value(captions, out.parent)}:fontsdir={_filter_value(FONTS, out.parent)}," + vf
    tmp = temp_path(out)
    enc_err = tempfile.TemporaryFile()
    encoder = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(fps), "-i", "-",
         "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
         "-map", "0:v", "-map", "1:a?", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", tmp.name],
        stdin=subprocess.PIPE, stderr=enc_err, cwd=out.parent,
    )
    source = frames(video, start, dur, *decode_size(probe.width, probe.height), fps)
    total = max(1, round(dur * fps))
    i = 0
    try:
        for i, frame in enumerate(source, start=1):
            z = float(zoom[min(i - 1, len(zoom) - 1)])
            composed = _fill(frame, *camera.at((i - 1) / fps), z) if layout == "fill" else _fit_blur(frame, z)
            encoder.stdin.write(composed.tobytes())
            if i % 15 == 0:
                on_progress(min(i / total, 0.99))
        encoder.stdin.close()
    except BrokenPipeError:
        pass  # the encoder died; its exit code and stderr below explain why
    except BaseException:
        encoder.kill()
        encoder.wait()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    if encoder.wait() != 0 or i == 0:
        tmp.unlink(missing_ok=True)
        detail = _read(enc_err) if encoder.returncode != 0 else "the clip range has no video frames"
        raise MediaError(f"Rendering failed: {detail or 'ffmpeg exited with an error'}")
    tmp.replace(out)


def _read(f) -> str:
    f.seek(0)
    return tail(f.read().decode(errors="replace"))
