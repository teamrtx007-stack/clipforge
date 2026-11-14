"""Behavior of the pure modules: scoring, captions, camera, live signals, parsing."""
from pathlib import Path

import numpy as np
import pytest

from app import camera, captions, describe, highlights, render
from app.config import Provider
from app.highlights import LENGTHS, Candidate, Word
from app.live import chat, platforms, signals


def _transcript(sentences: list[tuple[float, str]], pace: float = 0.35) -> dict:
    segs = []
    for start, text in sentences:
        words, t = [], start
        for w in text.split():
            words.append({"start": t, "end": t + pace * 0.9, "word": w})
            t += pace
        segs.append({"start": start, "end": t, "text": text, "words": words})
    return {"segments": segs}


def _filler_talk(duration: float, every: float = 5.0) -> list[tuple[float, str]]:
    line = "we went to the shop and we bought some bread and milk today."
    return [(t, line) for t in np.arange(0, duration - every, every)]


class TestHighlights:
    def test_loud_hooky_window_wins(self):
        talk = _filler_talk(300)
        talk[24] = (120.0, "Why do most people fail at this? Here's why it matters!")
        loud = np.full(int(300 / 0.5) + 1, -20.0)
        loud[240:260] = -6.0  # a shout from 120 s to 130 s
        found = highlights.find(_transcript(talk), loud, 300, LENGTHS["medium"], 3)
        best = found[0]
        assert best.start <= 120 <= best.end
        assert "Opens with a question" in best.reasons
        assert any(r.startswith("Loud reaction") for r in best.reasons)

    def test_clips_never_overlap_and_respect_length(self):
        found = highlights.find(_transcript(_filler_talk(600)), np.full(1201, -20.0), 600, LENGTHS["short"], 10)
        assert len(found) == 10
        spans = sorted((c.start, c.end) for c in found)
        assert all(a[1] - b[0] <= highlights.LEAD_IN + highlights.TAIL + 1e-6 for a, b in zip(spans, spans[1:])), \
            "clips share at most their padding"
        assert all(15 <= c.end - c.start <= 30 for c in found)

    def test_clip_edges_leave_room_around_the_words(self):
        tr = _transcript([(t, line) for t, line in _filler_talk(120) if 10 <= t < 30])  # 20 s of talk in 2 min
        words = highlights.words_from(tr)
        found = highlights.find(tr, np.full(241, -20.0), 120, LENGTHS["short"], 1)
        assert found and found[0].end - found[0].start <= LENGTHS["short"].max
        for c in found:
            inside = [w for w in words if c.start <= w.start and w.end <= c.end]
            assert inside[0].start - c.start >= 0.2 or c.start == 0, "the first syllable is not clipped"
            assert c.end - inside[-1].end >= 0.4, "the last syllable rings out"

    def test_no_speech_falls_back_to_loudness(self):
        loud = np.full(241, -30.0)
        loud[150:160] = -5.0  # 75 s to 80 s
        found = highlights.find({"segments": []}, loud, 120, LENGTHS["short"], 1)
        assert len(found) == 1 and found[0].start <= 75 and found[0].end >= 80

    def test_shout_between_talking_still_competes(self):
        talk = [(t, line) for t, line in _filler_talk(1200) if t < 40]  # 40 s of talk, then 19 min of gameplay
        loud = np.full(2401, -25.0)
        loud[1800:1812] = -4.0  # a shout at 900 s
        found = highlights.find(_transcript(talk), loud, 1200, LENGTHS["short"], 5)
        assert any(c.start <= 900 <= c.end for c in found[:2])

    def test_single_window_score_stays_in_range(self):
        found = highlights.find(_transcript([(0, "rare zebra quantum gizmo talk here today.")] * 1),
                                np.full(30, -20.0), 14.5, LENGTHS["medium"], 1)
        assert 0 <= found[0].score <= 100

    def test_must_include_keeps_the_live_peak(self):
        found = highlights.find(_transcript(_filler_talk(50)), np.full(101, -20.0), 50, LENGTHS["medium"], 1,
                                must_include=40.0)
        assert len(found) == 1 and found[0].start <= 40 <= found[0].end

    def test_video_shorter_than_min_length_is_one_clip(self):
        found = highlights.find(_transcript([(0, "short and sweet.")]), np.full(20, -20.0), 9, LENGTHS["short"], 3)
        assert [(c.start, c.end) for c in found] == [(0.0, 9.0)]

    def test_loudness_z_ignores_silence(self):
        db = np.concatenate([np.full(50, -90.0), np.full(150, -20.0), np.full(10, -8.0)])
        z = highlights.loudness_z(db)
        assert z[-1] > 5 and abs(z[100]) < 0.5


class TestDescribe:
    def _cand(self, text: str, keywords=("country", "americans")) -> Candidate:
        return Candidate(0, 30, 70, (), text, keywords, 5)

    def test_heuristic_prefers_question_and_drops_filler(self):
        d = describe.heuristic(self._cand("So this is the start. Um why does nobody talk about this? Fine then."))
        assert d.title == "Why does nobody talk about this?"
        assert d.hashtags[-1] == "#shorts" and "#country" in d.hashtags

    def test_no_text_still_gets_a_title(self):
        assert describe.heuristic(self._cand("", ())).title.startswith("Highlight at")

    def test_unreachable_llm_keeps_local_titles(self, override):
        override(text_providers=(Provider("custom", "http://127.0.0.1:9", "x"),))
        out = describe.describe([self._cand("Is this the best idea ever? It really is.")])
        assert out[0].title == "Is this the best idea ever?"

    def test_malformed_llm_answer_keeps_local_titles(self, override, monkeypatch):
        override(text_providers=(Provider("custom", "http://x", "x"),))
        monkeypatch.setattr(describe, "_ask_ai", lambda items: [{"id": 0, "virality": "very high"}])
        assert describe.describe([self._cand("Is this the best idea ever? It really is.")])[0].title.startswith("Is")

    def test_llm_answer_is_merged(self, override, monkeypatch):
        override(text_providers=(Provider("custom", "http://x", "x"),))
        monkeypatch.setattr(describe, "_ask_ai", lambda items: [
            {"id": 0, "title": "AI title", "description": "AI desc", "hashtags": ["#a", "b"], "virality": 100,
             "reason": "great hook"}])
        out = describe.describe([self._cand("Some words here. More words there.")])
        assert (out[0].title, out[0].hashtags) == ("AI title", ("#a", "#b"))
        assert out[0].candidate.score == round(0.6 * 70 + 0.4 * 100)
        assert "AI: great hook" in out[0].candidate.reasons


class TestCaptions:
    words = [Word(0.0, 0.3, "ask"), Word(0.35, 0.6, "not"), Word(0.65, 0.9, "what."), Word(2.0, 2.3, "{bad}\\x")]

    def test_karaoke_highlights_each_word_once(self):
        ass = captions.build_ass(self.words, captions.CaptionStyle())
        lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        assert len(lines) == 4  # three words in the first line, one in the second
        assert all(line.count("\\c&H") == 1 for line in lines)
        assert "{bad}" not in ass and "BADX" in ass

    def test_lines_break_at_sentence_end_and_pauses(self):
        groups = captions.chunk(self.words, max_words=5, max_chars=40)
        assert [len(g) for g in groups] == [3, 1]

    def test_static_style_has_no_overrides(self):
        ass = captions.build_ass(self.words[:2], captions.CaptionStyle(animation="static", uppercase=False))
        assert ass.splitlines()[-1].endswith(",,ask not")


class TestCamera:
    def test_plan_ignores_jitter_and_follows_moves(self):
        jitter = [[camera.Face(0.30 + 0.01 * (i % 2), 0.4, 0.3, 0.9)] for i in range(20)]
        moved = [[camera.Face(0.75, 0.4, 0.3, 0.9)] for _ in range(20)]
        path = camera.plan(jitter + moved)
        assert np.ptp(path.cx[:20]) < 1e-9  # dead zone: no wobble
        assert path.cx[-1] == pytest.approx(0.75, abs=0.02)

    def test_prefers_the_face_already_followed(self):
        a, b = camera.Face(0.2, 0.4, 0.30, 0.9), camera.Face(0.8, 0.4, 0.34, 0.9)
        path = camera.plan([[a]] * 4 + [[a, b]] * 8)
        assert path.cx[-1] == pytest.approx(0.2, abs=0.02)

    def test_no_faces_means_centered_and_blur_layout(self):
        path = camera.plan([[] for _ in range(10)])
        assert path.face_ratio == 0 and path.at(1.0) == (0.5, 0.5)
        assert camera.resolve_layout("auto", path.face_ratio) == "fit_blur"

    def test_zoom_punches_in_and_eases_out(self):
        z = np.zeros(20)
        z[4] = 3.0  # loud at 2 s
        curve = camera.zoom_curve(z, n_frames=300, fps=30, punch=0.1)
        assert curve[0] == 1.0 and curve[75] > 1.09 and curve[-1] < 1.01

    def test_crop_box_stays_inside_frame(self):
        for cx in (0.0, 0.5, 1.0):
            x0, y0, w = render.crop_box(cx, 0.5, 1.2, 1920, 1080)
            assert 0 <= x0 and x0 + w <= 1920 and w == pytest.approx(1080 * 9 / 16 / 1.2)


class TestLiveSignals:
    def test_spike_after_warmup_becomes_one_moment(self):
        d = signals.Detector(threshold=2.8, warmup=30, cooldown=40)
        rng = np.random.default_rng(1)
        moments = []
        for t in range(200):
            audio = -20 + rng.normal(0, 2) + (15 if 100 <= t < 104 or 110 <= t < 113 else 0)
            _, m = d.push(t, {"audio": audio, "chat": None})
            if m:
                moments.append(m)
        assert len(moments) == 1  # the second burst falls inside the cooldown
        assert 100 <= moments[0].t <= 104 and moments[0].reasons == ("Audio spike",)

    def test_nothing_during_warmup(self):
        d = signals.Detector(threshold=2.8, warmup=30)
        assert all(d.push(t, {"audio": -20.0 + (30 if t == 10 else 0)})[1] is None for t in range(40))

    def test_chat_alone_can_trigger(self):
        d = signals.Detector(threshold=2.1, warmup=20)
        found = [d.push(t, {"audio": -20.0, "chat": 2.0 + (40 if 60 <= t < 64 else t % 3)})[1] for t in range(90)]
        assert any(m and "Chat spike" in m.reasons for m in found)

    def test_flat_signals_do_not_dilute_a_spike(self):
        def peak(extra: dict) -> float:
            d = signals.Detector(threshold=99, warmup=30)
            rng = np.random.default_rng(3)
            return max(d.push(t, {"audio": -20 + rng.normal(0, 1) + (10 if 80 <= t < 85 else 0), **extra})[0]
                       for t in range(100))
        assert peak({"speech": 0.0, "chat": 0.0}) == pytest.approx(peak({}))

    def test_long_bursts_do_not_hide_each_other(self):
        # A loud take that fills a quarter of the stream must still fire every time it comes back.
        d = signals.Detector(threshold=2.8, warmup=30, cooldown=20)
        rng = np.random.default_rng(2)
        peaks = []
        for t in range(400):
            loud = (t % 44) in range(11, 22)
            _, m = d.push(t, {"audio": -20 + rng.normal(0, 1.5) + (12 if loud else 0)})
            if m:
                peaks.append(m.t % 44)
        assert len(peaks) >= 6 and all(9 <= p <= 24 for p in peaks)

    def test_audio_level_ignores_pauses_between_words(self):
        speech = np.concatenate([np.full(4000, 0.1), np.zeros(4000)] * 2).astype(np.float32)  # 250 ms words and pauses
        assert signals.audio_level(speech) == pytest.approx(signals.audio_level(np.full(16000, 0.1, np.float32)), abs=0.5)

    def test_speech_scorer_stops_even_when_backlogged(self, monkeypatch):
        import threading
        import time
        from app.live import supervisor
        monkeypatch.setattr(supervisor.transcribe, "transcribe", lambda *a, **k: time.sleep(0.3) or {"segments": []})
        scorer = supervisor.SpeechScorer()
        for t in range(200):  # far more chunks than the queue holds
            scorer.feed(t, np.zeros(16000, np.float32))
        scorer.close()
        time.sleep(1.5)
        assert not [th for th in threading.enumerate() if th.name == "live-speech" and th.is_alive()]

    def test_hype_text_scores_higher(self):
        assert signals.hype_score("oh my god no way LET'S GO!", 10) > signals.hype_score("the weather is fine", 10)
        assert signals.chat_weight("KEKW KEKW") == 2.0 and signals.chat_weight("hello") == 1.0


class TestParsing:
    @pytest.mark.parametrize("url,platform,canonical", [
        ("twitch.tv/SomeStreamer", "twitch", "https://www.twitch.tv/somestreamer"),
        ("https://www.youtube.com/@creator/live", "youtube", "https://www.youtube.com/@creator/live"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://kick.com/Someone", "kick", "https://kick.com/someone"),
        ("http://127.0.0.1:8080/live.m3u8", "direct", "http://127.0.0.1:8080/live.m3u8"),
    ])
    def test_platform_urls(self, url, platform, canonical):
        target = platforms.parse(url)
        assert (target.platform, target.url) == (platform, canonical)

    def test_cloud_metadata_address_is_refused(self):
        with pytest.raises(ValueError, match="not allowed"):
            platforms.parse("http://169.254.169.254/latest/meta-data/iam/security-credentials/")

    def test_rejects_non_stream_text(self):
        with pytest.raises(ValueError):
            platforms.parse("just some words")

    def test_twitch_irc_lines(self):
        line = "@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #streamer :KEKW that was :D"
        assert chat.parse_message(line) == "KEKW that was :D"
        assert chat.parse_message(":tmi.twitch.tv 001 justinfan123 :Welcome") is None


@pytest.mark.skipif(captions.drawable() is None, reason="needs fontconfig's fc-scan")
def test_captions_never_burn_in_empty_boxes():
    style = captions.CaptionStyle(animation="static", uppercase=False)
    ass = captions.build_ass([Word(0, 1, "你好"), Word(1, 2, "देखो"), Word(2, 3, "سلام"), Word(3, 4, "ok")], style)
    assert "देखो" in ass and "سلام" in ass and "ok" in ass, "Hindi, Urdu and Latin have bundled fonts"
    assert "你" not in ass, "a script with no bundled font is left out rather than drawn as boxes"


def test_a_trim_through_a_word_keeps_its_caption():
    words = [Word(9.8, 10.4, "hello"), Word(10.5, 11.0, "there"), Word(11.6, 12.4, "friend"), Word(12.5, 13.0, "gone")]
    got = highlights.words_in(words, 10.0, 12.2)
    assert [w.text for w in got] == ["hello", "there", "friend"]
    assert got[0].start == 0 and abs(got[-1].end - 2.2) < 1e-9, "times are clipped to the clip"


def test_ffmpeg_failing_halfway_leaves_no_partial_file(tmp_path, monkeypatch):
    from app import media

    def dies_halfway(cmd):  # e.g. the disk fills up mid-write
        Path(cmd[-1]).write_bytes(b"half a file")
        raise media.MediaError("No space left on device")

    monkeypatch.setattr(media, "run", dies_halfway)
    for make in (lambda: media.extract_audio(tmp_path / "v.mp4", tmp_path / "audio.wav"),
                 lambda: media.thumbnail(tmp_path / "v.mp4", 1.0, tmp_path / "thumb.jpg"),
                 lambda: media.compress_audio(tmp_path / "a.wav", 0, 10, tmp_path / "chunk.mp3")):
        with pytest.raises(media.MediaError):
            make()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(captions.drawable() is None, reason="needs fontconfig")
def test_hindi_captions_render_without_any_system_font(tmp_path, monkeypatch):
    """The Docker image has no fonts of its own. The bundled Noto fonts must reach libass through
    fontconfig, or Hindi burns in as empty boxes (it did, before this test)."""
    import subprocess

    from app import media

    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=0x335577:s=640x360:r=10:d=1",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    ass = tmp_path / "c.ass"
    ass.write_text(captions.build_ass([Word(0.0, 1.0, "देखो")], captions.CaptionStyle(animation="static")))

    def frame(conf_text: str) -> bytes:
        conf = tmp_path / "fonts.conf"
        conf.write_text(conf_text)
        monkeypatch.setattr(render, "fontconfig_file", lambda: conf)
        out = tmp_path / "out.mp4"
        render.render(video, media.probe(video), 0.0, 1.0, camera.CENTER, np.ones(10), "fit_blur", ass, out, lambda p: None)
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1",
                               "-f", "image2pipe", "-vcodec", "png", "-"], check=True, capture_output=True).stdout

    # As in Docker: the system's font configuration is missing, so only what fontconfig_file adds exists.
    monkeypatch.setattr(captions, "SYSTEM_FONTS_CONF", str(tmp_path / "no-system-fonts.conf"))
    captions.fontconfig_file.cache_clear()
    real = captions.fontconfig_file()
    captions.fontconfig_file.cache_clear()
    with_fonts = frame(real.read_text())
    no_fonts = frame(f'<?xml version="1.0"?><fontconfig><cachedir>{tmp_path / "cache"}</cachedir></fontconfig>')
    assert with_fonts != no_fonts, "the Hindi word was drawn as boxes: the bundled fonts did not reach libass"
