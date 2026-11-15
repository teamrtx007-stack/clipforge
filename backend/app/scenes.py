"""Shot cuts and context changes, so a clip never jumps from one place to an unrelated one.

One cheap pass over the whole video: small frames (160 px wide) at SAMPLE_FPS. A **cut** is a sudden jump
between neighbouring frames, in colour or in the picture itself, that stands out from the motion around it
(a running camera changes a lot every frame, so a fixed threshold alone would see cuts everywhere).
A **context change** is a cut where the few seconds before and after look like different places: a different
colour palette and brightness, such as outdoors to indoors. A cut between two camera angles in the same room
keeps its palette, so it is a cut but not a context change. A fade or dissolve to a different place has no
sudden jump, so it is found separately: the palette two to four seconds before differs from the palette two
to four seconds after.

Run `python -m app.scenes <source id>` to see what this finds in a video, and which of its clips cross a
context change. It reads only; it changes no clip.
"""
from pathlib import Path

import cv2
import numpy as np

from . import media

VERSION = 1
SAMPLE_FPS = 4.0
WIDTH = 160
CUT_MIN = 0.35  # combined frame-to-frame difference (0..1) a cut must reach
CUT_RATIO = 3.0  # ...and how far above the usual difference around it
CONTEXT_MIN = 0.45  # palette difference (0..1) between the seconds before and after a cut
CONTEXT_SECONDS = 3.0
MIN_SHOT = 0.5  # a "shot" shorter than this is a flash or a fade, not a new shot
GRADUAL_GAP, GRADUAL_SPAN = 2.0, 2.0  # a fade is compared from GAP to GAP + SPAN seconds on each side
DARK = 0.08  # mean brightness (0..1) below which a frame is a fade to black, left out of palette comparisons


def _signature(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A colour histogram (hue and saturation, plus brightness) and a tiny grey thumbnail of one frame."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = np.concatenate([
        cv2.calcHist([hsv], [0, 1], None, [16, 4], [0, 180, 0, 256]).ravel(),
        cv2.calcHist([hsv], [2], None, [8], [0, 256]).ravel(),
    ]).astype(np.float32)
    hist /= max(float(hist.sum()), 1.0)
    thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 18), interpolation=cv2.INTER_AREA)
    return hist, thumb.astype(np.float32) / 255.0


def _hist_distance(a: np.ndarray, b: np.ndarray) -> float:
    """0 for the same colours, 1 for nothing in common (Bhattacharyya distance)."""
    return float(cv2.compareHist(a, b, cv2.HISTCMP_BHATTACHARYYA))


def analyse(hists: np.ndarray, thumbs: np.ndarray, fps: float = SAMPLE_FPS) -> dict:
    """Cuts and context changes from per-frame signatures. Pure, so it is tested without video."""
    n = len(hists)
    if n < 2:
        return {"version": VERSION, "fps": fps, "cuts": [], "context_changes": []}
    diff = np.array([0.6 * _hist_distance(hists[i - 1], hists[i]) + 0.4 * min(1.0, 4 * float(
        np.mean(np.abs(thumbs[i] - thumbs[i - 1])))) for i in range(1, n)])
    cuts: list[int] = []  # index of the first frame of each new shot
    half = max(2, int(fps * 2))
    for i, d in enumerate(diff, start=1):
        around = np.delete(diff[max(0, i - 1 - half):i - 1 + half], min(half, i - 1))
        usual = float(np.median(around)) if around.size else 0.0
        if d >= CUT_MIN and d >= CUT_RATIO * usual and d == diff[max(0, i - 2):i + 1].max():
            if cuts and (i - cuts[-1]) / fps < MIN_SHOT:
                if d > diff[cuts[-1] - 1]:
                    cuts[-1] = i  # two cuts in a flash: keep the stronger one
                continue
            cuts.append(i)
    edges = [0, *cuts, n]
    context = []
    span = int(CONTEXT_SECONDS * fps)
    for k, c in enumerate(cuts, start=1):
        before = hists[max(edges[k - 1], c - span):c].mean(axis=0)
        after = hists[c:min(edges[k + 1], c + span)].mean(axis=0)
        if _hist_distance(before.astype(np.float32), after.astype(np.float32)) >= CONTEXT_MIN:
            context.append(c)
    context = sorted(context + _gradual(hists, thumbs.reshape(n, -1).mean(axis=1) >= DARK, fps, context))
    return {"version": VERSION, "fps": fps, "cuts": [round(c / fps, 2) for c in cuts],
            "context_changes": [round(c / fps, 2) for c in context]}


def _gradual(hists: np.ndarray, lit: np.ndarray, fps: float, known: list[int]) -> list[int]:
    """Context changes without a hard cut: fades and dissolves to a different place. Black frames in the
    middle of a fade are left out, or every fade to black would look like a change of place."""
    gap, span, n = int(GRADUAL_GAP * fps), int(GRADUAL_SPAN * fps), len(hists)
    if n < 2 * (gap + span):
        return []
    sums = np.concatenate([np.zeros((1, hists.shape[1])), np.cumsum(hists * lit[:, None], axis=0)])
    counts = np.concatenate([[0], np.cumsum(lit)])

    def mean(a: int, b: int) -> np.ndarray | None:
        k = counts[b] - counts[a]
        return ((sums[b] - sums[a]) / k).astype(np.float32) if k else None

    scores = np.zeros(n)
    for i in range(gap + span, n - gap - span + 1):
        a, b = mean(i - gap - span, i - gap), mean(i + gap, i + gap + span)
        if a is not None and b is not None:
            scores[i] = _hist_distance(a, b)
    found: list[int] = []
    reach = gap + span  # one fade is one change: keep the strongest point, and none next to a known change
    for i in np.argsort(-scores):
        if scores[i] < CONTEXT_MIN:
            break
        if all(abs(i - j) > reach for j in found + known):
            # The middle of the stretch where the change is strong: a fade starts to differ as soon as it darkens.
            lo, hi = int(i), int(i)
            while lo > 0 and scores[lo - 1] >= 0.8 * scores[i]:
                lo -= 1
            while hi < n - 1 and scores[hi + 1] >= 0.8 * scores[i]:
                hi += 1
            mid = (lo + hi) // 2
            if all(abs(mid - j) > reach for j in found + known):
                found.append(mid)
    return found


def detect(video: Path, duration: float, src_w: int, src_h: int) -> dict:
    """Cuts and context changes of a whole video, in seconds. Times are the first sampled frame of the new shot,
    so they are accurate to 1 / SAMPLE_FPS."""
    h = max(2, round(WIDTH * src_h / max(src_w, 1) / 2) * 2)
    hists, thumbs = [], []
    for frame in media.frames(video, 0.0, duration, WIDTH, h, SAMPLE_FPS):
        hist, thumb = _signature(frame)
        hists.append(hist)
        thumbs.append(thumb)
    if not hists:
        return analyse(np.zeros((0, 72), np.float32), np.zeros((0, 18, 32), np.float32))
    return analyse(np.stack(hists), np.stack(thumbs))


def crossing(scenes: dict | None, start: float, end: float, margin: float = 0.3) -> list[float]:
    """Context changes inside a clip, ignoring ones within `margin` of its edges (that is the clip starting or
    ending on the cut, which is fine)."""
    if not scenes:
        return []
    return [t for t in scenes.get("context_changes", []) if start + margin < t < end - margin]


def _report(source_id: int) -> int:
    """Read-only: a source's scenes, which of its current clips cross a context change, and the clips the
    scene-aware choice would pick instead. Nothing is saved and no clip is changed."""
    import json

    from . import db, files, highlights
    from .config import source_dir

    src = db.one("SELECT * FROM sources WHERE id = ?", source_id)
    if src is None:
        print(f"No video with id {source_id}.")
        return 1
    folder = source_dir(source_id)
    video = folder / f"original{src['ext']}"
    if not video.exists() and not files.fetch("source_video", source_id, video):
        print("The original video is gone (deleted after processing). Add it again to check it.")
        return 1
    info = media.probe(video)
    found = detect(video, info.duration, info.width, info.height)
    print(f"Video {source_id} '{src['name']}': {info.duration:.0f} s, {len(found['cuts'])} cuts, "
          f"{len(found['context_changes'])} context changes")
    for t in found["context_changes"]:
        print(f"  context change at {highlights.fmt_time(t)} ({t:.1f} s)")

    print("Current clips:")
    for c in db.rows('SELECT id, start, "end", title FROM clips WHERE source_id = ? ORDER BY start', source_id):
        bad = crossing(found, c["start"], c["end"])
        where = ", ".join(f"{t - c['start']:.1f} s in" for t in bad)
        print(f"  clip {c['id']} {highlights.fmt_time(c['start'])}-{highlights.fmt_time(c['end'])} "
              f"'{c['title']}': {'crosses a context change, ' + where if bad else 'one continuous scene'}")

    transcript, loudness = folder / "transcript.json", folder / "loudness.npy"
    if not (files.fetch("transcript", source_id, transcript) and files.fetch("loudness", source_id, loudness)):
        print("The transcript is gone, so the new choice cannot be shown without analysing again.")
        return 0
    meta = src["meta"]
    picks = highlights.find(json.loads(transcript.read_text()), np.load(loudness), info.duration,
                            highlights.LENGTHS[meta.get("length", "medium")], meta.get("max_clips", 8),
                            must_include=meta.get("peak"), scenes=found)
    print("Scene-aware choice (before AI titles and reranking; not saved):")
    for c in picks:
        bad = crossing(found, c.start, c.end)
        print(f"  {highlights.fmt_time(c.start)}-{highlights.fmt_time(c.end)} ({c.end - c.start:.0f} s, "
              f"score {c.score}): {'crosses a context change' if bad else 'one continuous scene'}")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Usage: python -m app.scenes <video id>   (the number in the video's page address)")
        raise SystemExit(2)
    raise SystemExit(_report(int(sys.argv[1])))
