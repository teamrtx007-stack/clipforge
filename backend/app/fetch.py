"""Download a video from a pasted link with yt-dlp (open source; YouTube and about a thousand other sites).
YouTube only serves its videos to a JavaScript runtime, so the Deno runtime is installed with yt-dlp."""
import re
from collections.abc import Callable
from pathlib import Path

import deno
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .media import MAX_DURATION, MediaError


def download(url: str, folder: Path, max_bytes: int, on_title: Callable[[str], None],
             on_progress: Callable[[float], None]) -> Path:
    """Save the video as folder/original.<ext> and return its path. 1080p is all a 1080x1920 clip can use,
    and H.264 decodes fastest, so both are preferred. Raises MediaError with a message for the user."""
    reported = -1

    def hook(d: dict) -> None:
        nonlocal reported
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d["status"] == "downloading" and total and int(100 * d["downloaded_bytes"] / total) != reported:
            reported = int(100 * d["downloaded_bytes"] / total)
            on_progress(min(reported / 100, 1.0))

    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "extract_flat": "in_playlist",  # a pasted channel is refused at once, not after reading every video
        "outtmpl": str(folder / "original.%(ext)s"),
        "format": "bv*+ba/b", "format_sort": ["res:1080", "vcodec:h264"], "merge_output_format": "mp4",
        "js_runtimes": {"deno": {"path": deno.find_deno_bin()}},
        "progress_hooks": [hook],
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") in ("playlist", "multi_video"):
                raise MediaError("Paste a link to one video, not a playlist or channel")
            if info.get("is_live"):
                raise MediaError("This video is live right now. Add the channel in Live studio instead")
            if (info.get("duration") or 0) > MAX_DURATION:
                raise MediaError(f"Videos longer than {MAX_DURATION // 3600} hours are not supported")
            if (info.get("filesize") or info.get("filesize_approx") or 0) > max_bytes:
                raise MediaError(f"This video is larger than the {max_bytes // 2**20} MB limit")
            on_title(info.get("title") or "")
            info = ydl.process_ie_result(info, download=True)
    except DownloadError as exc:
        raise MediaError(re.sub(r"\x1b\[[0-9;]*m|^ERROR: ", "", str(exc))) from exc
    return Path(info["requested_downloads"][0]["filepath"])
