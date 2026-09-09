from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["dashboard"])


@router.get("/api/dashboard")
async def dashboard(request: Request) -> dict:
    snap = request.app.state.state.dashboard_snapshot()
    runtime = getattr(request.app.state, "runtime", None)
    paper = getattr(runtime, "_paper", None) if runtime else None
    if paper is not None:
        snap["paper"] = paper.snapshot()
    return snap


@router.get("/api/pnl")
async def pnl(request: Request) -> dict:
    return request.app.state.state.pnl.all_horizons()


@router.get("/api/ledger")
async def ledger(request: Request, limit: int = 200, kind: str | None = None) -> list:
    return request.app.state.state.ledger.list(limit=limit, kind=kind)


@router.get("/api/edges")
async def edges(request: Request) -> list:
    return request.app.state.state.edges


@router.get("/api/wheel")
async def wheel(request: Request) -> list:
    return request.app.state.state.wheel_nodes
