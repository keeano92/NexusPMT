from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(request: Request) -> dict:
    state = request.app.state.state
    ready = state.worldmap_ready or not state.settings.worldmap_required
    return {
        "status": "ok" if ready else "blocked",
        "service": "nexus-pmt",
        "trading_mode": state.settings.kalshi_trading_mode,
        "failsafes": state.failsafes.snapshot()["state"],
        "worldmap_ready": state.worldmap_ready,
        "worldmap_required": state.settings.worldmap_required,
        "worldmap_block_reason": state.worldmap_block_reason,
        "worldmap_health_url": state.settings.worldmap_health_url,
    }
