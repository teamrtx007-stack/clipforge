"""Titles, descriptions and hashtags for clips.

A local heuristic always works. If LLM_BASE_URL is set, any OpenAI-compatible endpoint
(Ollama, LM Studio, Groq, Gemini, OpenRouter) rewrites them and adds a virality score.
On any LLM error the heuristic result stands.
"""
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, replace

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
Write in the transcript's language.

"""


def _ask_llm(items: list[Described]) -> list[dict]:
    body = PROMPT + "\n\n".join(f"Clip {i}:\n{d.candidate.text[:1500]}" for i, d in enumerate(items))
    req = urllib.request.Request(
        f"{settings.llm_base_url}/chat/completions",
        data=json.dumps({
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": body}],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {})},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.DOTALL)  # some models wrap the JSON in prose or fences
    return json.loads(match.group(0) if match else content)["clips"]


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
    if not (settings.llm_base_url and settings.llm_model) or not out:
        return out
    try:
        by_id = {int(a["id"]): a for a in _ask_llm(out) if isinstance(a, dict) and "id" in a}
        merged = [_merge(d, by_id[i]) if i in by_id else d for i, d in enumerate(out)]
    except Exception as exc:  # the model's JSON is untrusted input; any surprise keeps the local titles
        log.warning("LLM titles failed, keeping the local ones: %s", exc)
        return out
    return sorted(merged, key=lambda d: -d.candidate.score)
