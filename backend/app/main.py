"""App factory: API, media files, the built frontend, optional password, and the background threads."""
import base64
import binascii
import hmac
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api, db, jobs, pipeline  # noqa: F401  (importing pipeline registers the job handlers)
from .config import MEDIA, settings
from .live import supervisor

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "dist"


# shortcut: one shared password for everyone; add a users table and projects.owner_id when accounts are needed.
def authorized(header: str | None) -> bool:
    if not settings.password:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(password.encode(), settings.password.encode())


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def refusal(method: str, host: str, origin: str | None) -> str | None:
    """Why a browser request must be refused, or None. Without a password only localhost is served, so a
    DNS-rebinding page cannot read clips. Writes from another site are refused even with a password,
    because browsers resend saved Basic credentials on cross-site requests."""
    if not settings.password and urlsplit(f"//{host}").hostname not in LOCAL_HOSTS:
        return "Without APP_PASSWORD, ClipForge only answers on localhost"
    if method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != host:
        return "Requests from other websites are not allowed"
    return None


def create_app(background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()
        stop = threading.Event()
        if background:
            pipeline.discard_interrupted_uploads()
            jobs.recover_stale()
            jobs.start(settings.workers, stop, live_lane=settings.live_enabled)
            supervisor.start(stop)
        yield
        stop.set()
        jobs.wake()
        supervisor.wake()

    app = FastAPI(title="ClipForge", lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not authorized(request.headers.get("authorization")):
            return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="ClipForge"'})
        if reason := refusal(request.method, request.headers.get("host", ""), request.headers.get("origin")):
            return Response(reason, status_code=403)
        return await call_next(request)

    app.include_router(api.router)
    MEDIA.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=MEDIA), name="media")

    if FRONTEND.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            file = (FRONTEND / path).resolve()
            if path and file.is_file() and FRONTEND in file.parents:
                return FileResponse(file)
            return FileResponse(FRONTEND / "index.html")  # client-side routes like /projects/3

    return app


app = create_app()
