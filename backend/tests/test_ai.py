"""AI provider adapters against a local fake server: request shapes, retries, rate limits and fallback.
No real API is called and no key is needed."""
import functools
import http.server
import json
import threading

import pytest

from app import ai, media, transcribe
from app.config import Provider


class FakeAPI(http.server.BaseHTTPRequestHandler):
    """Answers each path from a script of (status, body, headers), one entry per request."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, dict(self.headers), body))
        status, answer, headers = self.server.script[self.path].pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def api(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    server.script, server.seen = {}, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    yield server
    server.shutdown()


def _url(server, name: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/{name}"


def _reply(text: str) -> tuple:
    return 200, {"choices": [{"message": {"content": text}}]}, {}


def test_text_falls_back_past_quota_and_bad_keys(api, override):
    override(text_providers=(Provider("gemini", _url(api, "a"), "m1", "key-a"),
                             Provider("groq", _url(api, "b"), "m2", "secret-b"),
                             Provider("openrouter", _url(api, "c"), "m3", "key-c")))
    api.script = {
        "/a/chat/completions": [(429, {"error": "quota"}, {"Retry-After": "3600"})],  # daily quota: move on
        "/b/chat/completions": [(401, {"error": "bad key"}, {})],
        "/c/chat/completions": [_reply('Sure! ```json\n{"clips": [{"id": 0}]}\n```')],
    }
    result, provider = ai.chat_json("hi")
    assert result == {"clips": [{"id": 0}]} and provider.name == "openrouter"
    assert [path for path, _, _ in api.seen] == ["/a/chat/completions", "/b/chat/completions", "/c/chat/completions"]
    sent = json.loads(api.seen[2][2])
    assert sent["model"] == "m3" and api.seen[2][1]["Authorization"] == "Bearer key-c"


def test_errors_name_the_provider_and_never_the_key(api, override):
    override(text_providers=(Provider("groq", _url(api, "b"), "m", "secret-b"),))
    api.script = {"/b/chat/completions": [(401, {"error": "secret-b is invalid"}, {})]}
    with pytest.raises(ai.AIError) as err:
        ai.chat_json("hi")
    assert "groq" in str(err.value) and "refused" in str(err.value) and "secret-b" not in str(err.value)


def test_server_errors_and_short_rate_limits_are_retried(api, override):
    override(text_providers=(Provider("custom", _url(api, "a"), "m"),))
    api.script = {"/a/chat/completions": [(503, b"busy", {}), (429, b"slow down", {"Retry-After": "2"}),
                                          _reply('{"ok": true}')]}
    assert ai.chat_json("hi")[0] == {"ok": True}
    assert "Authorization" not in api.seen[0][1], "a keyless local endpoint gets no auth header"


def test_no_provider_means_no_request(override):
    override(text_providers=())
    with pytest.raises(ai.AIError, match="no AI text provider"):
        ai.chat_json("hi")


def test_groq_whisper_request_and_answer(api, override, monkeypatch, tmp_path):
    override(groq_key="gk", transcribe_language="hi")
    monkeypatch.setattr(ai, "GROQ_URL", _url(api, "groq"))
    api.script = {"/groq/audio/transcriptions": [(200, {
        "language": "hindi", "segments": [{"start": 0.0, "end": 2.0, "text": " नमस्ते दोस्तों"}],
        "words": [{"word": "नमस्ते", "start": 0.1, "end": 0.6}, {"word": "दोस्तों", "start": 0.7, "end": 1.4}]}, {})]}
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"ID3fake")
    out = ai.transcribe_chunk("groq", audio, offset=600)
    assert out["segments"] == [{"start": 600.0, "end": 602.0, "text": "नमस्ते दोस्तों",
                                "words": [{"start": 600.1, "end": 600.6, "word": "नमस्ते"},
                                          {"start": 600.7, "end": 601.4, "word": "दोस्तों"}]}]
    path, headers, body = api.seen[0]
    assert headers["Authorization"] == "Bearer gk" and headers["Content-Type"].startswith("multipart/form-data")
    for part in (b'name="model"\r\n\r\nwhisper-large-v3-turbo', b'name="language"\r\n\r\nhi',
                 b'name="timestamp_granularities[]"\r\n\r\nword', b"ID3fake"):
        assert part in body


def test_cloudflare_whisper_request_and_answer(api, override, monkeypatch, tmp_path):
    override(cloudflare_account_id="acct", cloudflare_token="ct")
    monkeypatch.setattr(ai, "CLOUDFLARE_URL", _url(api, "cf"))
    path = "/cf/accounts/acct/ai/run/@cf/openai/whisper-large-v3-turbo"
    api.script = {path: [(200, {"success": True, "result": {
        "transcription_info": {"language": "en"},
        "segments": [{"start": 1.0, "end": 2.0, "text": "Hello there", "words": [
            {"word": "Hello", "start": 1.0, "end": 1.4}, {"word": "there", "start": 1.5, "end": 1.9}]}]}}, {}),
        (200, {"success": False, "errors": [{"message": "daily neuron limit"}]}, {})]}
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"abc")
    out = ai.transcribe_chunk("cloudflare", audio, offset=10)
    assert out["language"] == "en" and out["segments"][0]["words"][1] == {"start": 11.5, "end": 11.9, "word": "there"}
    assert json.loads(api.seen[0][2])["audio"] == "YWJj" and api.seen[0][1]["Authorization"] == "Bearer ct"
    with pytest.raises(ai.AIError, match="daily neuron limit"):
        ai.transcribe_chunk("cloudflare", audio, offset=0)


def test_missing_keys_fail_fast_without_a_request(override, tmp_path):
    override(groq_key=None, cloudflare_token=None)
    for name in ("groq", "cloudflare"):
        with pytest.raises(ai.AIError, match="not set|must both be set"):
            ai.transcribe_chunk(name, tmp_path / "x.mp3", 0)


def test_speech_falls_back_per_chunk_and_never_pays_twice(override, monkeypatch, tmp_path, talking_video):
    """Groq runs out of quota on the second chunk; local Whisper takes over; a retry reuses saved chunks."""
    wav = tmp_path / "audio.wav"
    media.extract_audio(talking_video, wav)
    override(transcribe_providers=("groq", "local"))
    monkeypatch.setattr(transcribe, "CHUNK", 20)
    calls = []

    def groq(name, mp3, offset):
        calls.append(offset)
        assert mp3.exists() and mp3.stat().st_size > 1000, "a real compressed slice is sent"
        if len(calls) > 1:
            raise ai.AIError("groq: free quota or rate limit reached (429)")
        return {"language": "en", "segments": [{"start": offset + 1, "end": offset + 2, "text": "cloud", "words": []}]}

    monkeypatch.setattr(ai, "transcribe_chunk", groq)
    out = transcribe.transcribe_file(wav, 44.0, tmp_path)
    assert out["segments"][0]["text"] == "cloud" and len(out["segments"]) > 1
    later = [s for s in out["segments"] if s["start"] >= 20]
    assert later and all(s["text"] != "cloud" for s in later), "local Whisper transcribed the rest"
    assert all(w["start"] >= 19 for s in later for w in s["words"]), "local times are shifted to the chunk's place"
    assert not list(tmp_path.glob("*.mp3")), "compressed chunks are deleted"

    monkeypatch.setattr(ai, "transcribe_chunk", lambda *a: pytest.fail("a saved chunk was sent again"))
    monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: pytest.fail("a saved chunk was transcribed again"))
    assert transcribe.transcribe_file(wav, 44.0, tmp_path) == out
