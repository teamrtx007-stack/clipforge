"""Face tracking and the virtual camera that frames a vertical crop.

YuNet (a 230 KB MIT face detector that ships inside OpenCV) finds faces a few times per second.
plan() turns those detections into a calm camera path: it follows one subject, ignores flicker,
and only moves once the subject leaves a dead zone, the way a human operator would.
"""
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import ASSETS
from . import media
from .media import HOP
from .templates import Layout

SAMPLE_FPS = 4.0
DETECT_WIDTH = 640
MODEL = ASSETS / "models" / "face_detection_yunet_2023mar.onnx"


@dataclass(frozen=True)
class Face:
    cx: float  # center, as a share of frame width
    cy: float  # center, as a share of frame height
    size: float  # face height as a share of frame height
    score: float


@dataclass(frozen=True)
class CameraPath:
    times: np.ndarray
    cx: np.ndarray
    cy: np.ndarray
    face_ratio: float  # share of samples that saw a face

    def at(self, t: float) -> tuple[float, float]:
        return float(np.interp(t, self.times, self.cx)), float(np.interp(t, self.times, self.cy))


CENTER = CameraPath(np.array([0.0]), np.array([0.5]), np.array([0.5]), 0.0)


def detect(video: Path, start: float, end: float, src_w: int, src_h: int) -> list[list[Face]]:
    """Faces in frames sampled at SAMPLE_FPS between start and end."""
    h = max(2, round(DETECT_WIDTH * src_h / src_w / 2) * 2)
    detector = cv2.FaceDetectorYN.create(str(MODEL), "", (DETECT_WIDTH, h), 0.6, 0.3, 50)
    samples = []
    for frame in media.frames(video, start, end - start, DETECT_WIDTH, h, SAMPLE_FPS):
        _, found = detector.detect(frame)
        samples.append([
            Face((x + fw / 2) / DETECT_WIDTH, (y + fh / 2) / h, fh / h, float(score))
            for x, y, fw, fh, *_, score in (found if found is not None else [])
        ])
    return samples


# shortcut: follows the biggest face, not the one speaking; add active-speaker detection for two-person podcasts.
def _pick(faces: list[Face], prev: tuple[float, float] | None) -> Face | None:
    """The biggest, most confident face, with a strong preference for whoever we were already following."""
    def weight(f: Face) -> float:
        near = prev is not None and abs(f.cx - prev[0]) < 0.12 and abs(f.cy - prev[1]) < 0.2
        return f.size * f.score * (2.5 if near else 1.0)
    return max(faces, key=weight, default=None)


def plan(samples: list[list[Face]], sample_fps: float = SAMPLE_FPS, dead_zone: float = 0.05,
         follow: float = 0.3) -> CameraPath:
    if not samples:
        return CENTER
    targets: list[tuple[float, float] | None] = []
    prev = None
    for faces in samples:
        f = _pick(faces, prev)
        prev = (f.cx, f.cy) if f else prev
        targets.append(prev)
    seen = sum(bool(f) for f in samples)
    if seen == 0:
        return CENTER

    first = next(t for t in targets if t is not None)
    raw = np.array([t or first for t in targets])  # before the first face, aim where it will appear
    # Median of 5 samples drops one-off false detections.
    pad = np.pad(raw, ((2, 2), (0, 0)), mode="edge")
    target = np.median(np.stack([pad[i:i + len(raw)] for i in range(5)]), axis=0)

    cam = np.empty_like(target)
    cam[0] = target[0]
    moving = False
    for i in range(1, len(target)):
        gap = target[i] - cam[i - 1]
        moving = np.abs(gap).max() > (dead_zone / 3 if moving else dead_zone)
        cam[i] = cam[i - 1] + gap * follow if moving else cam[i - 1]
    return CameraPath(np.arange(len(target)) / sample_fps, cam[:, 0], cam[:, 1], seen / len(samples))


def resolve_layout(layout: Layout, face_ratio: float) -> Layout:
    if layout == "auto":
        return "fill" if face_ratio >= 0.35 else "fit_blur"
    return layout


def zoom_curve(loudness_z: np.ndarray, n_frames: int, fps: float, punch: float,
               threshold: float = 1.8) -> np.ndarray:
    """Per-frame zoom factor. Snaps in on loud moments and eases back out."""
    if punch <= 0 or loudness_z.size == 0:
        return np.ones(n_frames)
    hops = np.minimum((np.arange(n_frames) / fps / HOP).astype(int), loudness_z.size - 1)
    # Stay punched in for a second after the peak, so the zoom reads as a beat, not a flicker.
    hold = int(1.0 / HOP)
    loud = np.convolve((loudness_z >= threshold).astype(float), np.ones(hold + 1), mode="full")[:loudness_z.size] > 0
    target = np.where(loud[hops], 1.0 + punch, 1.0)
    attack, release = 1 - np.exp(-1 / (fps * 0.08)), 1 - np.exp(-1 / (fps * 0.6))
    out = np.empty(n_frames)
    z = 1.0
    for i, t in enumerate(target):
        z += (t - z) * (attack if t > z else release)
        out[i] = z
    return out
