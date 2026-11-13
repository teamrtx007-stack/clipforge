"""Record a livestream into a rolling buffer of 2 second segments, and stream its audio for analysis.

One ffmpeg process does both: it copies the stream into segment files without re-encoding, and
decodes the audio to 16 kHz mono PCM on stdout. Both outputs share one timeline that starts at 0.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..media import tail
from . import platforms

SEGMENT_SECONDS = 2
BYTES_PER_SECOND = 16000 * 2  # 16 kHz, mono, 16-bit


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float


class Recorder:
    def __init__(self, target: platforms.Target, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        cmd = platforms.stream_command(target)
        self._feeder = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL) if cmd else None
        self._log = (folder / "ffmpeg.log").open("wb")
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-fflags", "+genpts",
             *([] if self._feeder else ["-rw_timeout", "15000000"]),  # a stalled stream ends the recording after 15 s
             "-i", "pipe:0" if self._feeder else target.url,
             "-map", "0:v:0?", "-map", "0:a:0", "-c", "copy",
             "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
             "-segment_list", "segments.csv", "-segment_list_type", "csv", "seg_%06d.ts",
             "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            cwd=folder, stdin=self._feeder.stdout if self._feeder else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=self._log,
        )
        if self._feeder:
            self._feeder.stdout.close()  # ffmpeg owns the pipe now
        self._csv_pos = 0
        self.segments: list[Segment] = []

    def read_second(self) -> bytes | None:
        """The next second of audio, or None once the stream has ended."""
        data = self._ffmpeg.stdout.read(BYTES_PER_SECOND)
        return data if len(data) == BYTES_PER_SECOND else None

    def poll_segments(self) -> list[Segment]:
        """Pick up segments ffmpeg has finished since the last call."""
        listing = self.folder / "segments.csv"
        if listing.exists():
            with listing.open("rb") as f:
                f.seek(self._csv_pos)
                chunk = f.read()
            complete = chunk[:chunk.rfind(b"\n") + 1]  # ignore a line ffmpeg is still writing
            self._csv_pos += len(complete)
            for line in complete.decode().splitlines():
                name, start, end = line.rsplit(",", 2)
                self.segments.append(Segment(self.folder / name, float(start), float(end)))
        return self.segments

    def window(self, start: float, end: float) -> list[Segment]:
        return [s for s in self.segments if s.end > start and s.start < end]

    def prune(self, keep_from: float) -> None:
        """Delete segments that ended before `keep_from` seconds of stream time."""
        old = [s for s in self.segments if s.end < keep_from]
        for s in old:
            s.path.unlink(missing_ok=True)
        self.segments = self.segments[len(old):]

    def stop(self) -> str:
        """Stop recording (safe to call twice or from another thread). Returns ffmpeg's last error lines."""
        for proc in (self._feeder, self._ffmpeg):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (self._feeder, self._ffmpeg):
            if proc:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if not self._log.closed:
            self._log.close()
        log = self.folder / "ffmpeg.log"
        return tail(log.read_text(errors="replace")) if log.exists() else ""
