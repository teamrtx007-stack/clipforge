"""Which livestreams we can watch, and how to read them.

Twitch, YouTube and Kick go through streamlink (open source; it tracks site changes, so keep it
updated). Direct HLS, RTMP and SRT URLs go straight to ffmpeg.
"""
import ipaddress
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Target:
    platform: str  # twitch | youtube | kick | direct
    url: str
    name: str


_SITES = [
    ("twitch", re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$"),
     lambda m: (f"https://www.twitch.tv/{m[1].lower()}", m[1])),
    ("youtube", re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.-]{3,40}|channel/[\w-]{10,40})(?:/live)?/?$"),
     lambda m: (f"https://www.youtube.com/{m[1]}/live", m[1].removeprefix("channel/"))),
    ("youtube", re.compile(r"^(?:https?://)?(?:(?:www\.|m\.)?youtube\.com/(?:watch\?v=|live/)|youtu\.be/)([\w-]{11})\b.*$"),
     lambda m: (f"https://www.youtube.com/watch?v={m[1]}", f"YouTube {m[1]}")),
    ("kick", re.compile(r"^(?:https?://)?(?:www\.)?kick\.com/([\w-]{3,25})/?$"),
     lambda m: (f"https://kick.com/{m[1].lower()}", m[1])),
    ("direct", re.compile(r"^((?:https?|rtmps?|srt)://[^\s]+)$"),
     lambda m: (m[1], re.sub(r"^\w+://([^/:]+).*$", r"\1", m[1]))),
]


def check_direct_host(url: str) -> None:
    """Refuse addresses no stream lives on: link-local, which includes the cloud metadata service at
    169.254.169.254. A ClipForge on a cloud VM must never fetch that. LAN and localhost stay allowed,
    because OBS servers and IP cameras live there; any other probing needs the app password."""
    host = urlsplit(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ValueError(f"Could not find the host {host!r}") from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("That address is not allowed as a stream source")


def parse(url: str) -> Target:
    url = url.strip()
    for platform, pattern, build in _SITES:
        if m := pattern.match(url):
            canonical, name = build(m)
            if platform == "direct":
                check_direct_host(canonical)
            return Target(platform, canonical, name)
    raise ValueError("Paste a Twitch, YouTube or Kick channel link, or a direct .m3u8, RTMP or SRT stream URL")


def stream_command(target: Target) -> list[str] | None:
    """Command that writes the live stream to stdout, or None when ffmpeg can open the URL itself."""
    if target.platform == "direct":
        return None
    return [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error", "--stream-timeout", "30",
            "--twitch-disable-ads", target.url, "best"]


def is_live(target: Target) -> bool:
    """True if the channel is broadcasting now. Raises with a readable message on lookup errors."""
    if target.platform == "direct":
        check_direct_host(target.url)  # again, in case the name now resolves somewhere else
        probe = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "10000000", "-show_entries",
                                "stream=codec_type", "-of", "csv=p=0", target.url],
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return probe.returncode == 0 and "audio" in probe.stdout
    from streamlink import Streamlink
    from streamlink.exceptions import NoPluginError, PluginError

    try:
        return bool(Streamlink().streams(target.url))
    except NoPluginError as exc:
        raise RuntimeError("streamlink does not support this site") from exc
    except PluginError as exc:
        raise RuntimeError(f"Could not check the stream: {exc}") from exc


# shortcut: Twitch only; YouTube and Kick chat need scraping libraries, add them when someone asks.
def chat_channel(target: Target) -> str | None:
    """Twitch chat is readable anonymously. Other sites need keys or scraping, so they have no chat signal."""
    return target.url.rsplit("/", 1)[-1] if target.platform == "twitch" else None
