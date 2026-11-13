"""Read-only Twitch chat over IRC. Twitch allows anonymous 'justinfan' logins, so no key or account."""
import logging
import random
import socket
import ssl
import threading
from collections.abc import Callable

log = logging.getLogger("clipforge.chat")

HOST, PORT = "irc.chat.twitch.tv", 6697


def parse_message(line: str) -> str | None:
    """The text of a PRIVMSG line, or None for anything else."""
    # Form: [@tags ]:nick!user@host PRIVMSG #channel :message text
    head, sep, text = line.partition(" PRIVMSG #")
    if not sep:
        return None
    _, sep, text = text.partition(" :")
    return text.strip() if sep else None


def run(channel: str, on_message: Callable[[str], None], stop: threading.Event) -> None:
    """Blocks until `stop` is set. Reconnects with backoff when the connection drops."""
    backoff = 2.0
    while not stop.is_set():
        try:
            with socket.create_connection((HOST, PORT), timeout=15) as raw, \
                    ssl.create_default_context().wrap_socket(raw, server_hostname=HOST) as sock:
                sock.settimeout(1.0)
                nick = f"justinfan{random.randint(10000, 99999)}"
                sock.sendall(f"NICK {nick}\r\nJOIN #{channel.lower()}\r\n".encode())
                backoff = 2.0
                buf = b""
                while not stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw_line in lines:
                        line = raw_line.decode(errors="replace")
                        if line.startswith("PING"):
                            sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        elif (text := parse_message(line)) is not None:
                            on_message(text)
        except OSError as exc:
            log.info("chat #%s disconnected: %s", channel, exc)
        stop.wait(backoff)
        backoff = min(backoff * 2, 60.0)
