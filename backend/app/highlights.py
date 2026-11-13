"""Find the moments worth clipping. Pure functions over the transcript and the loudness curve.

Each candidate window starts and ends on a sentence boundary. It gets a score from seven signals:
hook, energy, topic, emotion, pace, clean ending and length fit, plus chat activity for livestreams.
The best windows that do not overlap become clips.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .media import HOP


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Sentence:
    words: tuple[Word, ...]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class ClipLength:
    min: float
    max: float
    target: float


LENGTHS = {
    "short": ClipLength(15, 30, 22),
    "medium": ClipLength(30, 60, 45),
    "long": ClipLength(60, 90, 75),
}


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    score: int
    reasons: tuple[str, ...]
    text: str
    keywords: tuple[str, ...]
    peak: float  # loudest moment, used for the thumbnail


WEIGHTS = {"hook": 0.22, "energy": 0.22, "topic": 0.16, "emotion": 0.14, "pace": 0.10,
           "ending": 0.08, "fit": 0.08, "chat": 0.30}

# shortcut: hook, filler and emotion word lists are English; other languages still score on energy, topic and pace.
HOOK = re.compile(
    r"\b(here'?s (why|how|what|the)|the (secret|truth|problem|reason|key) (is|to|about)|nobody|no one|never|"
    r"you won'?t believe|i can'?t believe|the (best|worst|biggest)|mistake|how (to|i|we)|why (you|i|we|most)|"
    r"did you know|what if|imagine|listen|number one|\d+ (ways|things|reasons|tips|steps))\b"
)
FILLER_START = frozenset("and but so um uh like because or yeah okay ok also then well anyway".split())
INTENSE = frozenset(
    "insane crazy amazing incredible unbelievable wow omg holy wild love hate huge shocking seriously "
    "literally absolutely terrifying hilarious ridiculous massive".split()
)
LAUGH = re.compile(r"\b(ha){2,}\b|\blol\b|\blmao\b|\[laugh", re.IGNORECASE)
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between
both but by can cannot could did do does doing down during each few for from further gonna got had has have
having he her here hers herself him himself his how i if in into is it its itself just know like me more most my
myself no nor not now of off on once only or other ought our ours ourselves out over own really right same say
said she should so some such than that the their theirs them themselves then there these they thing things
think this those through to too um uh under until up very was we were what when where which while who whom why
will with would yeah yes you your yours yourself yourselves going want actually okay well kind sort mean gonna
wanna didn doesn isn wasn aren weren don won can't don't it's i'm that's there's they're you're we're
""".split())
_WORD = re.compile(r"[^\w']+")


def words_from(transcript: dict) -> list[Word]:
    return [Word(w["start"], w["end"], w["word"]) for seg in transcript.get("segments", []) for w in seg["words"]]


def sentences(words: list[Word], max_gap: float = 0.7, max_words: int = 30) -> list[Sentence]:
    """Split on end punctuation, long pauses, or run-ons, whichever comes first."""
    out, current = [], []
    for i, w in enumerate(words):
        current.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or w.text.endswith((".", "?", "!")) or nxt.start - w.end > max_gap or len(current) >= max_words:
            out.append(Sentence(tuple(current)))
            current = []
    return out


def robust_z(x: np.ndarray) -> np.ndarray:
    """z-scores from median and MAD, so a few loud spikes do not hide each other."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    return (x - med) / (mad if mad > 1e-6 else (np.std(x) or 1.0))


def loudness_z(db: np.ndarray) -> np.ndarray:
    """Loudness relative to this video's normal speaking level. Pauses are left out of the baseline,
    otherwise silence widens the spread and real shouts stop standing out."""
    db = np.asarray(db, dtype=np.float64)
    if db.size == 0:
        return db
    voiced = db[db >= max(np.percentile(db, 20), -60.0)]
    base = voiced if voiced.size >= 10 else db
    med = np.median(base)
    spread = max(np.median(np.abs(base - med)) * 1.4826, 1.0)  # at least 1 dB, so steady audio never divides by ~0
    return np.maximum((db - med) / spread, -4.0)


def fmt_time(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def content_terms(text: str) -> list[str]:
    terms = (_WORD.sub("", t).strip("'") for t in text.lower().split())
    return [t for t in terms if len(t) >= 4 and t not in STOPWORDS and not t.isdigit()]


@dataclass(frozen=True)
class _Window:
    start: float
    end: float
    sents: tuple[Sentence, ...] = ()
    terms: Counter = field(default_factory=Counter)


def _sentence_windows(sents: list[Sentence], length: ClipLength) -> list[_Window]:
    term_counts = [Counter(content_terms(s.text)) for s in sents]
    out = []
    for i, first in enumerate(sents):
        terms: Counter = Counter()
        for j in range(i, len(sents)):
            terms += term_counts[j]
            dur = sents[j].end - first.start
            if dur > length.max:
                break
            if dur >= length.min:
                out.append(_Window(first.start, sents[j].end, tuple(sents[i:j + 1]), terms.copy()))
    return out


def _even_windows(duration: float, length: ClipLength, step: float = 2.0) -> list[_Window]:
    span = min(length.target, duration)
    return [_Window(t, t + span) for t in np.arange(0, max(duration - span, 0) + 1e-9, step)]


def _quiet_windows(sents: list[Sentence], duration: float, length: ClipLength) -> list[_Window]:
    """Even windows that contain no speech, so a shout in gameplay or music can compete with the talking."""
    speech = np.zeros(int(duration / HOP) + 2, bool)
    for s in sents:
        speech[int(s.start / HOP):int(s.end / HOP) + 1] = True
    covered = np.concatenate([[0], np.cumsum(speech)])  # covered[i] = speech hops before hop i
    return [w for w in _even_windows(duration, length)
            if covered[int(w.end / HOP) + 1] == covered[int(w.start / HOP)]]


def _around(t: float, duration: float, length: ClipLength) -> _Window:
    start = max(0.0, min(t - length.target * 0.65, duration - length.target))
    return _Window(start, min(duration, start + length.target))


def _slice(z: np.ndarray, start: float, end: float) -> np.ndarray:
    return z[int(start / HOP): max(int(math.ceil(end / HOP)), int(start / HOP) + 1)]


def _top_mean(x: np.ndarray, share: float = 0.2) -> float:
    if x.size == 0:
        return 0.0
    k = max(1, int(x.size * share))
    return float(np.mean(np.partition(x, -k)[-k:]))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


def _signals(w: _Window, z: np.ndarray, chat_z: np.ndarray | None, idf: dict[str, float],
             length: ClipLength) -> tuple[dict[str, float], list[tuple[float, str]], float]:
    """Feature values in [0, 1], reasons with their strength, and the loudest time."""
    dur = w.end - w.start
    f: dict[str, float] = {"fit": 1 - _clamp(abs(dur - length.target) / (length.max - length.min))}
    reasons: list[tuple[float, str]] = []

    zs = _slice(z, w.start, w.end)
    peak = w.start + (int(np.argmax(zs)) + 0.5) * HOP if zs.size else w.start
    f["energy"] = _sigmoid(1.2 * (_top_mean(zs) - 1.0))
    if zs.size and zs.max() >= 2.0:
        reasons.append((f["energy"], f"Loud reaction at {fmt_time(peak)}"))

    if chat_z is not None:
        cs = _slice(chat_z, w.start, w.end)
        f["chat"] = _sigmoid(1.2 * (_top_mean(cs) - 1.0))
        if cs.size and cs.max() >= 2.5:
            reasons.append((f["chat"] + 0.2, "Chat exploded"))

    if w.sents:
        first = w.sents[0].text.lower()
        lead = _WORD.sub("", first.split()[0]) if first.split() else ""
        if first.rstrip().endswith("?"):
            f["hook"] = 1.0
            reasons.append((1.0, "Opens with a question"))
        elif m := HOOK.search(first):
            f["hook"] = 0.85
            reasons.append((0.85, f'Strong hook: "{m.group(0)}"'))
        else:
            f["hook"] = 0.15 if lead in FILLER_START else 0.45

        words = [wd for s in w.sents for wd in s.words]
        text = " ".join(s.text for s in w.sents).lower()
        exclaims = sum(s.text.endswith("!") for s in w.sents)
        intense = sum(_WORD.sub("", t) in INTENSE for t in text.split())
        laughs = len(LAUGH.findall(text))
        f["emotion"] = _clamp((exclaims + intense + 2 * laughs) / 4)
        if laughs:
            reasons.append((0.9, "Laughter"))
        elif f["emotion"] >= 0.5:
            reasons.append((f["emotion"], "Emotional moment"))

        wps = len(words) / dur
        coverage = sum(wd.end - wd.start for wd in words) / dur
        f["pace"] = _clamp(1 - abs(wps - 2.8) / 2.2) * _clamp(coverage / 0.6)
        if wps >= 3.2 and coverage >= 0.6:
            reasons.append((0.6, "Fast, energetic delivery"))

        f["ending"] = 1.0 if w.sents[-1].text.endswith((".", "!", "?")) else 0.4
        f["topic"] = sum(sorted((n * idf.get(t, 0.0) for t, n in w.terms.items()), reverse=True)[:5])
    return f, reasons, peak


def find(transcript: dict, loudness_db: np.ndarray, duration: float, length: ClipLength, max_clips: int,
         must_include: float | None = None, chat: np.ndarray | None = None) -> list[Candidate]:
    z = loudness_z(loudness_db)
    chat_z = robust_z(chat) if chat is not None and np.any(chat) else None
    sents = sentences(words_from(transcript))

    if duration <= length.min:
        windows = [_Window(0.0, duration, tuple(sents), Counter(t for s in sents for t in content_terms(s.text)))]
    else:
        windows = _sentence_windows(sents, length) + _quiet_windows(sents, duration, length)
    if must_include is not None:
        windows = [w for w in windows if w.start <= must_include <= w.end] or [_around(must_include, duration, length)]

    df = Counter(t for s in sents for t in set(content_terms(s.text)))
    idf = {t: math.log((len(sents) + 1) / (n + 0.5)) for t, n in df.items()}
    scored = [(w, *_signals(w, z, chat_z, idf, length)) for w in windows]

    # Topic strength only means something relative to this video, so rank it across windows.
    topics = sorted(f["topic"] for _, f, _, _ in scored if "topic" in f)
    for _, f, _, _ in scored:
        if "topic" in f:
            f["topic"] = np.searchsorted(topics, f["topic"], side="right") / len(topics) if len(topics) > 1 else 0.5

    candidates = []
    for w, f, reasons, peak in scored:
        weight = sum(WEIGHTS[k] for k in f)
        score = round(100 * sum(WEIGHTS[k] * v for k, v in f.items()) / weight)
        keywords = tuple(t for t, _ in sorted(w.terms.items(), key=lambda kv: -kv[1] * idf.get(kv[0], 0))[:5])
        if f.get("topic", 0) >= 0.85 and keywords:
            reasons.append((f["topic"] * 0.8, "Focused on " + ", ".join(keywords[:2])))
        top_reasons = tuple(r for _, r in sorted(reasons, key=lambda r: -r[0])[:3])
        candidates.append(Candidate(round(w.start, 2), round(w.end, 2), score, top_reasons,
                                    " ".join(s.text for s in w.sents), keywords, round(peak, 2)))
    return select(candidates, max_clips)


def select(candidates: list[Candidate], max_clips: int) -> list[Candidate]:
    """Highest scores first, never two clips that overlap. On a tie, the clip that centers its peak wins."""
    chosen: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: (-c.score, abs(c.peak - (c.start + c.end) / 2), c.start)):
        if len(chosen) >= max_clips:
            break
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
    return chosen
