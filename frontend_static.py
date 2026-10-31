"""
Serves the built frontend (frontend/dist) from the FastAPI process.

Mounted at "/" after every API route, so /api/* always reaches the API. The
directory is checked per request rather than at import, so building the
frontend after the server started works without a restart, and a missing build
gives a short explanation instead of a crash or a bare 404.
"""

import os
from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

DEFAULT_DIST_DIR = Path(__file__).resolve().parent / "frontend" / "dist"

MISSING_BUILD_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>CodeRev Bot</title></head>"
    "<body style='font-family:sans-serif;padding:2rem'>"
    "<h1>Frontend not built</h1>"
    "<p>The API is running, but <code>frontend/dist</code> was not found. "
    "Run <code>npm install</code> and <code>npm run build</code> in <code>frontend/</code>, "
    "then reload this page.</p></body></html>"
)


class FrontendApp:
    def __init__(self, dist_dir: Path):
        self.dist_dir = Path(dist_dir)
        self._static = None
        self._static_dir = None

    def _static_app(self) -> StaticFiles:
        if self._static is None or self._static_dir != self.dist_dir:
            self._static = StaticFiles(directory=self.dist_dir, html=True)
            self._static_dir = self.dist_dir
        return self._static

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not (self.dist_dir / "index.html").is_file():
            response = HTMLResponse(MISSING_BUILD_HTML, status_code=503)
            await response(scope, receive, send)
            return
        await self._static_app()(scope, receive, send)


def frontend_dist_dir() -> Path:
    return Path(os.getenv("FRONTEND_DIST_DIR", str(DEFAULT_DIST_DIR)))
