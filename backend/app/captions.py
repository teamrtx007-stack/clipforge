"""Animated captions as ASS subtitles, burned in by ffmpeg's libass."""
import functools
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from .config import FONTS, settings
from .highlights import Word

Animation = Literal["karaoke", "pop", "static"]


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Montserrat Black"
    size: int = 86
    color: str = "FFFFFF"  # RGB hex
    highlight: str = "FFE81F"
    outline: int = 7
    shadow: int = 3
    margin_v: int = 560  # pixels above the bottom edge of the 1920 px frame
    uppercase: bool = True
    animation: Animation = "karaoke"
    max_words: int = 3
    max_chars: int = 18


def _ass_color(rgb: str, alpha: int = 0) -> str:
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def _ts(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


SYSTEM_FONTS_CONF = "/etc/fonts/fonts.conf"


@functools.cache
def fontconfig_file() -> Path:
    """Fontconfig settings that add the bundled fonts to the system's. libass draws a letter missing from the
    caption font with a fallback font found through fontconfig only; fonts given to ffmpeg with fontsdir are
    not part of that search. So on a machine with no Hindi font of its own, such as the Docker image, Hindi
    came out as boxes. The encoder runs with FONTCONFIG_FILE pointing here."""
    folder = settings.data_dir / "fontconfig"
    folder.mkdir(parents=True, exist_ok=True)
    conf = folder / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        f'  <include ignore_missing="yes">{escape(SYSTEM_FONTS_CONF)}</include>\n'
        f"  <dir>{escape(str(FONTS))}</dir>\n  <cachedir>{escape(str(folder / 'cache'))}</cachedir>\n</fontconfig>\n"
    )
    return conf


@functools.cache
def drawable() -> frozenset[int] | None:
    """Code points the bundled fonts can draw: Latin and Cyrillic (Montserrat), the main Indian scripts and
    Arabic (Noto). None when fontconfig's fc-scan is missing; then nothing is dropped."""
    try:
        out = subprocess.run(["fc-scan", "--format", "%{charset}\n", str(FONTS)],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    points: set[int] = set()
    for span in out.split():
        lo, _, hi = span.partition("-")
        points.update(range(int(lo, 16), int(hi or lo, 16) + 1))
    return frozenset(points)


def _clean(text: str, style: CaptionStyle) -> str:
    text = text.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ").strip().rstrip(".,;:")
    if (fonts := drawable()) is not None:  # a character no font has would burn in as an empty box
        text = "".join(ch for ch in text if ord(ch) in fonts).strip()
    return text.upper() if style.uppercase else text


def chunk(words: list[Word], max_words: int, max_chars: int, max_gap: float = 0.6) -> list[list[Word]]:
    """Group words into short caption lines, breaking at pauses and sentence ends."""
    chunks: list[list[Word]] = []
    for w in words:
        cur = chunks[-1] if chunks else None
        if (cur is None or len(cur) >= max_words
                or len(" ".join(x.text for x in cur + [w])) > max_chars
                or w.start - cur[-1].end > max_gap
                or cur[-1].text.endswith((".", "?", "!"))):
            chunks.append([w])
        else:
            cur.append(w)
    return chunks


def build_ass(words: list[Word], style: CaptionStyle, width: int = 1080, height: int = 1920) -> str:
    """`words` use times relative to the clip start."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.size},{_ass_color(style.color)},{_ass_color(style.color)},&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,60,60,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    groups = chunk([w for w in words if _clean(w.text, style)], style.max_words, style.max_chars)
    for i, group in enumerate(groups):
        start = group[0].start
        nxt = groups[i + 1][0].start if i + 1 < len(groups) else None
        # Hold the line until the next one if the gap is short, so captions do not flicker.
        end = nxt if nxt is not None and nxt - group[-1].end < 0.5 else group[-1].end + 0.15
        texts = [_clean(w.text, style) for w in group]
        if style.animation == "karaoke":
            hl = _ass_color(style.highlight)
            for k, w in enumerate(group):
                w_start = start if k == 0 else w.start
                w_end = group[k + 1].start if k + 1 < len(group) else end
                line = " ".join(
                    f"{{\\c{hl}\\fscx112\\fscy112}}{t}{{\\r}}" if j == k else t for j, t in enumerate(texts)
                )
                events.append((w_start, w_end, line))
        elif style.animation == "pop":
            pop = "{\\fad(40,0)\\fscx60\\fscy60\\t(0,110,\\fscx106\\fscy106)\\t(110,180,\\fscx100\\fscy100)}"
            events.append((start, end, pop + " ".join(texts)))
        else:
            events.append((start, end, " ".join(texts)))
    body = "".join(f"Dialogue: 0,{_ts(s)},{_ts(e)},Default,,0,0,0,,{text}\n" for s, e, text in events if e > s)
    return header + body
