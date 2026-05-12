"""FastAPI application — HTML UI + API routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from openclose.server.routes import router
from openclose.storage.db import close_db
from openclose.log import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = _TEMPLATES_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle for the FastAPI app."""
    # Startup: clean up empty sessions left over from previous runs
    from openclose.storage.db import get_db
    from openclose.session.session import SessionManager
    try:
        mgr = SessionManager(get_db())
        mgr.cleanup_empty_sessions()
    except Exception:
        log.warning("Failed to clean up empty sessions on startup", exc_info=True)

    # Startup: start the job scheduler
    from openclose.jobs.scheduler import get_scheduler
    scheduler = get_scheduler()
    try:
        await scheduler.start()
    except Exception:
        log.warning("Failed to start job scheduler", exc_info=True)

    yield

    # Shutdown: stop scheduler then checkpoint WAL and close the database.
    try:
        await scheduler.stop()
    except Exception:
        log.warning("Failed to stop job scheduler cleanly", exc_info=True)
    log.info("Shutting down — closing database")
    close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="OpenClose", version="0.1.0", lifespan=_lifespan)

    # Mount static files
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Include API routes
    app.include_router(router)

    return app


# Global templates instance
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
