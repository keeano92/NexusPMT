from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(request: Request) -> dict:
    state = request.app.state.state
    return {
        "status": "ok",
        "service": "nexus-pmt",
        "trading_mode": state.settings.kalshi_trading_mode,
        "failsafes": state.failsafes.snapshot()["state"],
        "worldmap_health_url": state.settings.worldmap_health_url,
    }
