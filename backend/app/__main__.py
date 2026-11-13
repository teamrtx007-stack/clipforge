import logging
import os

import uvicorn

from .config import settings
from .main import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
host = os.environ.get("HOST", "127.0.0.1")
if host not in ("127.0.0.1", "localhost", "::1") and not settings.password and os.environ.get("ALLOW_NO_PASSWORD") != "1":
    raise SystemExit(
        f"Refusing to listen on {host} without APP_PASSWORD, because anyone who can reach it could use it. "
        "Set APP_PASSWORD, or set ALLOW_NO_PASSWORD=1 if something else already restricts access."
    )
uvicorn.run(app, host=host, port=int(os.environ.get("PORT", "8000")), log_level="info")
