"""NexusPMT FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api import controls, dashboard, health, ws
from backend.app.config import get_settings
from backend.app.state import build_app_state
from backend.app.workers.runtime import AutonomyRuntime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nexuspmt")

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"
FRONTEND_DIST = FRONTEND_ROOT / "dist"
FRONTEND_STATIC = FRONTEND_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    state = build_app_state(settings)
    runtime = AutonomyRuntime(state)
    app.state = SimpleNamespace(settings=settings, state=state, runtime=runtime)
    state.ledger.append(kind="system", status="boot", message="NexusPMT starting")
    await runtime.start()
    logger.info(
        "NexusPMT up mode=%s env=%s worldmap=%s",
        settings.kalshi_trading_mode,
        settings.kalshi_env,
        settings.worldmap_health_url,
    )
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="NexusPMT", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(controls.router)
app.include_router(ws.router)

if FRONTEND_STATIC.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_STATIC), name="static")
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/")
async def index():
    index_path = FRONTEND_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    fallback = FRONTEND_ROOT / "index.html"
    if fallback.exists():
        return FileResponse(fallback)
    return {"service": "nexus-pmt", "ui": "frontend not built"}
