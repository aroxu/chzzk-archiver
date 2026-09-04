"""FastAPI application assembly.

All behaviour lives in dedicated modules: settings in :mod:`app.config`,
persistence in :mod:`app.models`, domain logic in :mod:`app.services` and HTTP
surfaces in :mod:`app.routers`. This module only wires them together.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .lifecycle import lifespan
from .routers import admin, auth, extension, media, recordings, subscriptions, workers

app = FastAPI(title="CHZZK Archive", lifespan=lifespan)


@app.get("/health/live")
def health():
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(subscriptions.router)
app.include_router(recordings.router)
app.include_router(media.router)
app.include_router(extension.router)
app.include_router(admin.router)
app.include_router(workers.router)

if settings.web_dist.exists():
    app.mount("/assets", StaticFiles(directory=settings.web_dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(settings.web_dist / "index.html")
