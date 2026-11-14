"""Titles, descriptions and hashtags for clips.

A local heuristic always works. When an AI text provider is configured (see ai.py), it rewrites them,
in the clip's own language, and adds a virality score that reranks the clips. On any AI error the
heuristic result stands.
"""
import logging
import re
from dataclasses import dataclass, replace

from . import ai
from .config import settings
from .highlights import FILLER_START, Candidate, fmt_time

log = logging.getLogger("clipforge.describe")


@dataclass(frozen=True)
class Described:
    candidate: Candidate
    title: str
    description: str
    hashtags: tuple[str, ...]


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "..."


def _strip_filler(sentence: str) -> str:
    words = sentence.split()
    while words and re.sub(r"[^\w']", "", words[0].lower()) in FILLER_START:
        words.pop(0)
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 3]


def heuristic(c: Candidate) -> Described:
    sents = _sentences(c.text)
    if not sents:
        title = f"Highlight at {fmt_time(c.start)}"
        return Described(c, title, title, ("#shorts",))
    opening = sents[:3]
    pick = (next((s for s in opening if s.endswith("?")), None)
            or next((s for s in opening if s.endswith("!")), None)
            or max(opening, key=lambda s: sum(k in s.lower() for k in c.keywords)))
    title = _shorten(_strip_filler(pick).rstrip("."), 70)
    description = _shorten(" ".join(_strip_filler(s) for s in sents[:2]), 220)
    tags = tuple(dict.fromkeys(f"#{re.sub(r'[^0-9a-z]', '', k)}" for k in c.keywords[:3])) + ("#shorts",)
    return Described(c, title, description, tuple(t for t in tags if len(t) > 2))


PROMPT = """You write titles for short vertical videos (TikTok, Reels, YouTube Shorts).
For each clip transcript below, return JSON: {"clips": [{"id": <id>, "title": "<max 70 chars, punchy, no quotes>",
"description": "<1-2 sentences>", "hashtags": ["#tag", ...3-5 tags], "virality": <0-100>, "reason": "<max 8 words>"}]}.
Judge virality by hook strength, emotion, surprise and whether the clip makes sense on its own.
Write in the transcript's language and script. Return only the JSON object.

"""


def _ask_ai(items: list[Described]) -> list[dict]:
    """One request for all clips, to spend as little free quota as possible."""
    body = PROMPT + "\n\n".join(f"Clip {i}:\n{d.candidate.text[:1000]}" for i, d in enumerate(items))
    return ai.chat_json(body)[0]["clips"]


def _merge(d: Described, ai: dict) -> Described:
    virality = max(0, min(100, int(ai.get("virality", d.candidate.score))))
    reason = str(ai.get("reason", "")).strip()
    candidate = replace(
        d.candidate,
        score=round(0.6 * d.candidate.score + 0.4 * virality),
        reasons=d.candidate.reasons + ((f"AI: {reason}",) if reason else ()),
    )
    tags = tuple(("#" + str(t).lstrip("#").replace(" ", ""))[:30] for t in ai.get("hashtags", []) if str(t).strip("#"))
    return Described(
        candidate,
        _shorten(str(ai.get("title") or d.title).strip().strip('"'), 90),
        _shorten(str(ai.get("description") or d.description).strip(), 300),
        tags[:5] or d.hashtags,
    )


def describe(candidates: list[Candidate]) -> list[Described]:
    out = [heuristic(c) for c in candidates]
    if not settings.text_providers or not out:
        return out
    try:
        by_id = {int(a["id"]): a for a in _ask_ai(out) if isinstance(a, dict) and "id" in a}
        merged = [_merge(d, by_id[i]) if i in by_id else d for i, d in enumerate(out)]
    except Exception as exc:  # the model's JSON is untrusted input; any surprise keeps the local titles
        log.warning("AI titles failed, keeping the local ones: %s", exc)
        return out
    return sorted(merged, key=lambda d: -d.candidate.score)
