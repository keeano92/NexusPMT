from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def live_bus(websocket: WebSocket) -> None:
    await websocket.accept()
    state = websocket.app.state.state
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    state.bus_subscribers.add(queue)
    try:
        await websocket.send_text(json.dumps({"type": "snapshot", "data": state.dashboard_snapshot()}))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                await websocket.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        state.bus_subscribers.discard(queue)
