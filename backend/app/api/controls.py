from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.app.api.deps import require_operator

router = APIRouter(tags=["controls"], prefix="/api/controls")


class FlattenBody(BaseModel):
    confirm: str


class CloseBody(BaseModel):
    ticker: str


class ApprovalBody(BaseModel):
    client_order_id: str


@router.get("/status")
async def status(request: Request, _actor: str = Depends(require_operator)) -> dict:
    return request.app.state.state.failsafes.snapshot()


@router.post("/kill")
async def kill(request: Request, actor: str = Depends(require_operator)) -> dict:
    state = request.app.state.state
    state.failsafes.kill(actor=actor, reason="manual")
    state.ledger.append(kind="control", status="killed", message=f"KILL by {actor}")
    await state.publish({"type": "failsafes", "failsafes": state.failsafes.snapshot()})
    return state.failsafes.snapshot()


@router.post("/pause")
async def pause(request: Request, actor: str = Depends(require_operator)) -> dict:
    state = request.app.state.state
    state.failsafes.pause(actor=actor)
    state.ledger.append(kind="control", status="paused", message=f"Pause by {actor}")
    await state.publish({"type": "failsafes", "failsafes": state.failsafes.snapshot()})
    return state.failsafes.snapshot()


@router.post("/resume")
async def resume(request: Request, actor: str = Depends(require_operator)) -> dict:
    state = request.app.state.state
    state.failsafes.resume(actor=actor)
    state.ledger.append(kind="control", status="resumed", message=f"Resume by {actor}")
    await state.publish({"type": "failsafes", "failsafes": state.failsafes.snapshot()})
    return state.failsafes.snapshot()


@router.post("/clear-kill")
async def clear_kill(request: Request, actor: str = Depends(require_operator)) -> dict:
    state = request.app.state.state
    state.failsafes.clear_kill(actor=actor)
    state.ledger.append(kind="control", status="clear_kill", message=f"Clear kill by {actor}")
    await state.publish({"type": "failsafes", "failsafes": state.failsafes.snapshot()})
    return state.failsafes.snapshot()


@router.post("/flatten")
async def flatten(
    body: FlattenBody,
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    if body.confirm.strip().upper() != "FLATTEN":
        return {"ok": False, "error": "confirm must be FLATTEN"}
    runtime = request.app.state.runtime
    await runtime.flatten_all(actor=actor)
    return {"ok": True, "positions": request.app.state.state.positions}


@router.post("/close")
async def close_position(
    body: CloseBody,
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    ok = await request.app.state.runtime.close_position(body.ticker, actor=actor)
    return {"ok": ok}


@router.post("/approve")
async def approve(
    body: ApprovalBody,
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    order = request.app.state.state.failsafes.approve(body.client_order_id, actor=actor)
    return {"ok": order is not None, "order": order}


@router.post("/reject")
async def reject(
    body: ApprovalBody,
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    order = request.app.state.state.failsafes.reject(body.client_order_id, actor=actor)
    return {"ok": order is not None, "order": order}
