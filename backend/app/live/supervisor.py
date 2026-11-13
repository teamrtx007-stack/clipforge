"""Watch every enabled channel at once, with one worker thread per channel.

A worker polls whether its channel is live. When it is, the worker records into a rolling buffer,
scores each second of audio, chat and speech, and cuts a short source video around every moment.
That source goes through the normal pipeline, so live clips get the same transcription,
captions, face tracking and titles as uploads.
"""
import logging
import queue
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .. import db, media, pipeline, transcribe
from ..config import LIVE_DIR, settings, source_dir
from . import chat, platforms, signals
from .recorder import Recorder

log = logging.getLogger("clipforge.live")

POLL_SECONDS = {"direct": 15.0}
DEFAULT_POLL = 60.0
PRE_ROLL, POST_ROLL = 35.0, 15.0
ANALYSIS_DELAY = 15  # judge each second this late, so slower signals (speech) have arrived
SPEECH_CHUNK = 10
WARMUP, COOLDOWN = 60.0, 45.0


@dataclass
class State:
    status: str = "starting"  # starting | offline | live | recording | error | disabled
    since: float = field(default_factory=time.time)
    error: str | None = None
    moments: int = 0
    last_moment_at: float | None = None
    recorded_seconds: int = 0
    levels: deque = field(default_factory=lambda: deque(maxlen=90))  # recent per-second scores for the UI

    def as_dict(self) -> dict:
        return {**{k: v for k, v in vars(self).items() if k != "levels"}, "levels": list(self.levels)}


class SpeechScorer:
    """Transcribes the stream in short chunks on its own thread and scores the hype in each chunk."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(maxsize=3)
        self._stop = threading.Event()  # an event, not a queued sentinel: a full queue must not block shutdown
        self._lock = threading.Lock()
        self._scores: dict[int, float] = {}
        self._pending: list[np.ndarray] = []
        self._pending_start = 0
        threading.Thread(target=self._loop, daemon=True, name="live-speech").start()

    def feed(self, t: int, samples: np.ndarray) -> None:
        if not self._pending:
            self._pending_start = t
        self._pending.append(samples)
        if len(self._pending) >= SPEECH_CHUNK:
            try:
                self._queue.put_nowait((self._pending_start, np.concatenate(self._pending)))
            except queue.Full:
                pass  # the CPU is behind; skip this chunk rather than fall further behind
            self._pending = []
            with self._lock:  # only recent scores are ever asked for
                for old in [k for k in self._scores if k < t - 120]:
                    del self._scores[old]

    def score_at(self, t: int) -> float | None:
        with self._lock:
            return self._scores.get(t - t % SPEECH_CHUNK)  # chunks start at 0, 10, 20...

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                t0, audio = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = transcribe.transcribe(audio, model_name=settings.live_whisper_model, fast=True)
                score = signals.hype_score(" ".join(s["text"] for s in result["segments"]), len(audio) / 16000)
                with self._lock:
                    self._scores[t0] = score
            except Exception:
                log.exception("live speech analysis failed")


class _ChatCounter:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0.0

    def add(self, message: str) -> None:
        with self._lock:
            self._value += signals.chat_weight(message)

    def take(self) -> float:
        with self._lock:
            value, self._value = self._value, 0.0
        return value


class ChannelWorker(threading.Thread):
    def __init__(self, channel_id: int):
        super().__init__(daemon=True, name=f"live-{channel_id}")
        self.channel_id = channel_id
        self.state = State()
        self._halt = threading.Event()  # not `_stop`: that name is threading.Thread's own method
        self._poke = threading.Event()
        self._recorder: Recorder | None = None

    def stop(self) -> None:
        self._halt.set()
        self._poke.set()
        if self._recorder:
            self._recorder.stop()  # unblocks a read that waits on a stalled stream

    def poke(self) -> None:
        self._poke.set()

    def _set(self, status: str, error: str | None = None) -> None:
        if status != self.state.status:
            self.state.since = time.time()
        self.state.status, self.state.error = status, error

    def _channel(self) -> dict | None:
        ch = db.one("SELECT * FROM channels WHERE id = ?", self.channel_id)
        return ch if ch and ch["enabled"] else None

    def _wait(self, seconds: float) -> None:
        self._poke.wait(seconds)
        self._poke.clear()

    def run(self) -> None:
        while not self._halt.is_set():
            ch = self._channel()
            if ch is None:
                self._set("disabled")
                return
            target = platforms.Target(ch["platform"], ch["url"], ch["name"])
            poll = POLL_SECONDS.get(ch["platform"], DEFAULT_POLL)
            try:
                live = platforms.is_live(target)
            except Exception as exc:
                self._set("error", str(exc))
                self._wait(poll)
                continue
            if live and ch["auto_record"]:
                self._record(ch, target)
                if self.state.status == "error":  # the recorder could not start; do not retry in a tight loop
                    self._wait(poll)
                continue
            self._set("live" if live else "offline")
            self._wait(poll)

    def _record(self, ch: dict, target: platforms.Target) -> None:
        rec = self._recorder = Recorder(target, LIVE_DIR / str(ch["id"]) / time.strftime("%Y%m%d-%H%M%S"))
        self._set("recording")
        self.state.recorded_seconds = 0
        detector = signals.Detector(signals.THRESHOLDS[ch["sensitivity"]], warmup=WARMUP, cooldown=COOLDOWN)
        chat_stop = threading.Event()
        counter = _ChatCounter()
        chat_name = platforms.chat_channel(target) if ch["chat"] else None
        if chat_name:
            threading.Thread(target=chat.run, args=(chat_name, counter.add, chat_stop), daemon=True,
                             name=f"chat-{chat_name}").start()
        speech = SpeechScorer() if ch["speech"] else None
        waiting: deque[tuple[int, dict]] = deque()  # seconds not yet judged, oldest first
        chat_history: dict[int, float] = {}
        pending: list[signals.Moment] = []
        t = 0
        try:
            while not self._halt.is_set() and (pcm := rec.read_second()) is not None:
                samples = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
                chat_rate = counter.take() if chat_name else None
                if chat_rate is not None:
                    chat_history[t] = chat_rate
                if speech:
                    speech.feed(t, samples)
                waiting.append((t, {"audio": signals.audio_level(samples), "chat": chat_rate}))
                if len(waiting) > ANALYSIS_DELAY:
                    judged, levels = waiting.popleft()
                    levels["speech"] = speech.score_at(judged) if speech else None
                    score, moment = detector.push(judged, levels)
                    self.state.levels.append({"t": judged, "score": round(score, 2)})
                    if moment:
                        pending.append(moment)
                t += 1
                self.state.recorded_seconds = t

                segments = rec.poll_segments()
                recorded_until = segments[-1].end if segments else 0.0
                for m in [m for m in pending if m.t + POST_ROLL <= recorded_until]:
                    pending.remove(m)
                    self._cut(ch, rec, m, chat_history)
                keep_from = min([t - ch["buffer_minutes"] * 60.0] + [m.t - PRE_ROLL for m in pending])
                rec.prune(keep_from)
                for old in [k for k in chat_history if k < keep_from - 1]:
                    del chat_history[old]

                if t % 10 == 0:  # pick up settings changes; stop if the channel was disabled or deleted
                    fresh = self._channel()
                    if not fresh or not fresh["auto_record"]:
                        break
                    ch = fresh
                    detector.threshold = signals.THRESHOLDS[ch["sensitivity"]]
        finally:
            chat_stop.set()
            if speech:
                speech.close()
            rec.poll_segments()
            for m in pending:  # recording stopped before the post-roll; clip what we have
                self._cut(ch, rec, m, chat_history)
            error = rec.stop()
            self._recorder = None
            shutil.rmtree(rec.folder, ignore_errors=True)
        if not self._halt.is_set():
            self._set("offline" if t > 0 or not error else "error", None if t > 0 else error or None)

    def _cut(self, ch: dict, rec: Recorder, moment: signals.Moment, chat_history: dict[int, float]) -> None:
        segments = rec.window(moment.t - PRE_ROLL, moment.t + POST_ROLL)
        if not segments:
            return
        start = segments[0].start
        sid = None
        try:
            sid = db.insert(
                "sources", project_id=ch["project_id"], channel_id=ch["id"], kind="live", ext=".mp4",
                name=f"{ch['name']} at {time.strftime('%H:%M')}", status="queued", created_at=db.now(),
                meta={"length": "medium", "max_clips": 1, "template": ch["template"],
                      "auto_render": bool(ch["auto_render"]), "peak": round(moment.t - start, 2),
                      "moment": {"score": moment.score, "reasons": list(moment.reasons)}},
            )
            folder = source_dir(sid)
            folder.mkdir(parents=True, exist_ok=True)
            media.concat([s.path for s in segments], folder / "original.mp4")
            counts = [chat_history.get(s, 0.0) for s in range(int(start), int(segments[-1].end) + 1)]
            if any(counts):  # chat per second, stretched to the pipeline's half-second hops
                np.save(folder / "chat.npy", np.repeat(np.array(counts, np.float32), int(1 / media.HOP)))
            pipeline.queue_analysis(db.one("SELECT * FROM sources WHERE id = ?", sid))
        except Exception:  # e.g. the project was deleted, or the disk is full
            log.exception("could not save a live moment for channel %s", ch["id"])
            if sid is not None:
                db.execute("DELETE FROM sources WHERE id = ?", sid)
                shutil.rmtree(source_dir(sid), ignore_errors=True)
            return
        self.state.moments += 1
        self.state.last_moment_at = time.time()


_workers: dict[int, ChannelWorker] = {}
_lock = threading.Lock()
_wake = threading.Event()


def _reconcile() -> None:
    enabled = {r["id"] for r in db.rows("SELECT id FROM channels WHERE enabled = 1")}
    with _lock:
        for cid, worker in list(_workers.items()):
            if cid not in enabled or not worker.is_alive():
                worker.stop()
                del _workers[cid]
        for cid in enabled - _workers.keys():
            _workers[cid] = ChannelWorker(cid)
            _workers[cid].start()
        for worker in _workers.values():
            worker.poke()


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            _reconcile()
        except Exception:
            log.exception("live supervisor error")
        _wake.wait(30)
        _wake.clear()
    with _lock:
        for worker in _workers.values():
            worker.stop()
        _workers.clear()


def start(stop: threading.Event) -> threading.Thread | None:
    if not settings.live_enabled:
        return None
    thread = threading.Thread(target=_loop, args=(stop,), daemon=True, name="live-supervisor")
    thread.start()
    return thread


def wake() -> None:
    _wake.set()


def snapshot() -> dict[int, dict]:
    with _lock:
        return {cid: w.state.as_dict() for cid, w in _workers.items()}


def idle_state(ch: dict) -> dict:
    status = "disabled" if not ch["enabled"] else "starting" if settings.live_enabled else "off"
    return State(status=status).as_dict()
