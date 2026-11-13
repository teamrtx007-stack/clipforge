"""Editing templates. Adding a template is adding one entry here."""
from dataclasses import dataclass
from typing import Literal

from .captions import CaptionStyle

Layout = Literal["auto", "fill", "fit_blur"]


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    layout: Layout  # auto: face crop when faces are visible, full frame over blur otherwise
    captions: CaptionStyle | None
    punch_zoom: float  # extra zoom on loud moments, 0 turns it off


TEMPLATES = {t.id: t for t in [
    Template("viral", "Viral Bold", "Face-tracked crop, yellow word-by-word captions, punch-in zoom on loud moments.",
             "auto", CaptionStyle(), 0.12),
    Template("podcast", "Podcast", "Tight face crop, white pop-in captions, gentle zoom.",
             "fill", CaptionStyle(font="Montserrat ExtraBold", size=78, animation="pop", uppercase=False,
                                  max_words=4, max_chars=22), 0.06),
    Template("gaming", "Gaming", "Full frame over a blurred background, big green word-by-word captions.",
             "fit_blur", CaptionStyle(highlight="3DFF6E", size=92, margin_v=380), 0.08),
    Template("clean", "Clean", "Face-tracked crop, small static captions, no zoom.",
             "auto", CaptionStyle(font="Montserrat ExtraBold", size=64, animation="static", uppercase=False,
                                  outline=4, shadow=1, max_words=5, max_chars=28, margin_v=260), 0.0),
    Template("cinematic", "Cinematic", "Full frame over blur, lower-third captions, no zoom.",
             "fit_blur", CaptionStyle(font="Montserrat ExtraBold", size=60, animation="pop", uppercase=False,
                                      outline=3, max_words=5, max_chars=30, margin_v=420), 0.0),
    Template("raw", "No captions", "Face-tracked crop only. Add your own captions later.", "auto", None, 0.08),
]}

DEFAULT_TEMPLATE = "viral"
