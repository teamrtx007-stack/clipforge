"""Turn per-second stream signals into moments. Pure: no I/O, no clocks, no threads.

Each signal (audio loudness, chat rate, speech hype) is compared with its own running baseline.
The combined score opens an event when it crosses the threshold; the event closes a few seconds
later and reports its peak. A cooldown stops one long burst from becoming ten clips.
"""
import math
import re
from collections import deque
from dataclasses import dataclass, field

import numpy as np

THRESHOLDS = {"low": 3.8, "medium": 3.0, "high": 2.3}
WEIGHTS = {"audio": 1.0, "chat": 0.9, "speech": 0.7}
LABELS = {"audio": "Audio spike", "chat": "Chat spike", "speech": "Hype in speech"}
FLOORS = {"audio": 1.0, "chat": 0.5, "speech": 0.15}  # dB, weighted messages per second, hype 0..1

HYPE = re.compile(
    r"\b(oh my god|no way|let'?s go+|what the|holy|insane|crazy|unbelievable|clutch|clip (it|that)|"
    r"wow+|omg|lol|lmao|wtf|(ha){2,}|yes+|pog\w*|kekw|lul\w*|omegalul|w+)\b|!",
    re.IGNORECASE,
)


def hype_score(text: str, seconds: float) -> float:
    """0..1 from hype words, exclamations and fast talking in a transcript chunk."""
    words = len(text.split())
    rate_bonus = max(0.0, words / max(seconds, 1.0) - 3.0) / 3.0
    return min(1.0, len(HYPE.findall(text)) / 4 + rate_bonus)


def chat_weight(message: str) -> float:
    """A chat message counts once, twice if it is hype (emote spam, 'clip it')."""
    return 2.0 if HYPE.search(message) else 1.0


def audio_level(samples) -> float:
    """Loudness of one second of 16 kHz audio: the 90th percentile of its 50 ms frames, in dBFS.
    Plain RMS mixes in the pauses between words, so speech would swing more than a shout stands out."""
    frames = samples[: len(samples) // 800 * 800].reshape(-1, 800)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    return float(np.percentile(20 * np.log10(np.maximum(rms, 1e-5)), 90))


class Baseline:
    """How unusual a value is against the last `horizon` seconds, from their median and MAD (as uploads do).

    A median ignores bursts that fill up to half the window, so one loud minute does not hide the next,
    and a lasting change in level becomes the new normal within a minute. Values are averaged over
    `smooth` seconds first, so one noisy second never fires. `floor` is the smallest spread, in the
    signal's own units, so a perfectly steady signal does not turn tiny changes into huge scores.
    """

    def __init__(self, floor: float, horizon: int = 120, smooth: int = 3):
        self.floor = floor
        self.window: deque[float] = deque(maxlen=horizon)
        self.recent: deque[float] = deque(maxlen=smooth)

    def z(self, x: float) -> float:
        self.recent.append(x)
        x = sum(self.recent) / len(self.recent)
        past = np.fromiter(self.window, float)
        self.window.append(x)
        if past.size < 10:
            return 0.0
        med = np.median(past)
        return float((x - med) / max(1.4826 * np.median(np.abs(past - med)), self.floor))


@dataclass(frozen=True)
class Moment:
    t: float  # stream time of the peak, in seconds
    score: float
    reasons: tuple[str, ...]


@dataclass
class _Event:
    start: float
    peak_t: float
    peak: float
    zs: dict[str, float]


@dataclass
class Detector:
    threshold: float
    warmup: float = 60.0
    cooldown: float = 45.0
    max_event: float = 8.0
    baselines: dict[str, Baseline] = field(default_factory=dict)
    _event: _Event | None = None
    _quiet_until: float = 0.0
    _streak: int = 0  # consecutive seconds at or above the threshold

    def push(self, t: float, levels: dict[str, float | None]) -> tuple[float, Moment | None]:
        """Feed one second of signals. Returns the combined score and a moment when one completes."""
        zs = {k: self.baselines.setdefault(k, Baseline(FLOORS[k])).z(v) for k, v in levels.items() if v is not None}
        # Length of the weighted positive z-scores: a strong signal alone counts in full,
        # a flat signal adds nothing, and two moderate signals together add up.
        score = math.sqrt(sum((WEIGHTS[k] * max(z, 0.0)) ** 2 for k, z in zs.items()))
        if t < self.warmup:  # the baselines are still settling; their z-scores mean nothing yet
            return 0.0, None
        self._streak = self._streak + 1 if score >= self.threshold else 0
        ev = self._event
        if ev is None:
            # Two seconds in a row, so one noisy peak never becomes a clip.
            if self._streak >= 2 and t >= self._quiet_until:
                self._event = _Event(t, t, score, zs)
            return score, None
        if score > ev.peak:
            ev.peak_t, ev.peak, ev.zs = t, score, zs
        if score < self.threshold * 0.5 or t - ev.start >= self.max_event:
            self._event = None
            self._quiet_until = t + self.cooldown
            reasons = tuple(LABELS[k] for k, z in sorted(ev.zs.items(), key=lambda kv: -kv[1]) if z >= 1.5 and k in LABELS)
            return score, Moment(ev.peak_t, round(ev.peak, 2), reasons)
        return score, None
