from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.app.api.deps import require_operator
from backend.app.worldmap.lifecycle import container_status, start_worldmap

router = APIRouter(tags=["controls"], prefix="/api/controls")


class FlattenBody(BaseModel):
    confirm: str


class CloseBody(BaseModel):
    ticker: str


class ApprovalBody(BaseModel):
    client_order_id: str


class TradingConfigBody(BaseModel):
    trading_mode: str | None = None  # paper | live
    kalshi_env: str | None = None  # demo | production
    confirm: str = ""


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


@router.get("/worldmap")
async def worldmap_status(request: Request) -> dict:
    state = request.app.state.state
    settings = state.settings
    docker = await container_status(settings.worldmap_container_name)
    return {
        "worldmap_ready": state.worldmap_ready,
        "worldmap_required": settings.worldmap_required,
        "worldmap_block_reason": state.worldmap_block_reason,
        "worldmap_health": state.worldmap_health,
        "health_url": settings.worldmap_health_url,
        "container": docker,
    }


@router.post("/start-worldmap")
async def force_start_worldmap(
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    state = request.app.state.state
    settings = state.settings
    result = await start_worldmap(
        settings.worldmap_container_name,
        settings.worldmap_compose_project,
    )
    state.ledger.append(
        kind="control",
        status="start_worldmap",
        message=f"Force start WorldMap by {actor}: {result.get('action')} ok={result.get('ok')}",
        meta=result,
    )
    await state.publish({"type": "worldmap", "start_result": result})
    return result


@router.post("/trading-config")
async def set_trading_config(
    body: TradingConfigBody,
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    """Switch paper/live and demo/production with typed confirmations."""
    mode = (body.trading_mode or "").strip().lower() or None
    env = (body.kalshi_env or "").strip().lower() or None
    confirm = (body.confirm or "").strip().upper()

    if mode == "live" and confirm not in {"LIVE", "LIVE DEMO", "LIVE PRODUCTION"}:
        return {
            "ok": False,
            "error": "Switching to LIVE requires confirm=LIVE (or LIVE DEMO / LIVE PRODUCTION).",
        }
    if env == "production" and confirm not in {"PRODUCTION", "LIVE PRODUCTION"}:
        return {
            "ok": False,
            "error": "Switching to PRODUCTION requires confirm=PRODUCTION (or LIVE PRODUCTION).",
        }
    if mode == "live" and env == "production" and confirm != "LIVE PRODUCTION":
        return {
            "ok": False,
            "error": "Live + production together requires confirm=LIVE PRODUCTION.",
        }

    result = await request.app.state.runtime.set_trading_config(
        trading_mode=mode,
        kalshi_env=env,
        actor=actor,
    )
    return result


@router.post("/refresh-wheel")
async def refresh_wheel(
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    result = await request.app.state.runtime.force_wheel_refresh()
    request.app.state.state.ledger.append(
        kind="control",
        status="refresh_wheel",
        message=f"Wheel refresh by {actor}: ok={result.get('ok')} nodes={result.get('nodes')}",
        meta=result,
    )
    return result


@router.post("/refresh-portfolio")
async def refresh_portfolio(
    request: Request,
    actor: str = Depends(require_operator),
) -> dict:
    runtime = request.app.state.runtime
    state = request.app.state.state
    if state.settings.kalshi_trading_mode == "live":
        result = await runtime.sync_live_portfolio()
    else:
        state.portfolio_source = "paper"
        state.positions = list(state.paper_positions.values())
        result = {
            "ok": True,
            "portfolio_source": "paper",
            "cash_cents": state.paper_cash_cents,
            "positions": len(state.positions),
        }
        await state.publish({"type": "portfolio", "portfolio_source": "paper"})
    state.ledger.append(
        kind="control",
        status="refresh_portfolio",
        message=f"Portfolio refresh by {actor}: ok={result.get('ok')}",
        meta=result,
    )
    return result
