"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from backend.app.state import AppState
from backend.app.workers.runtime import AutonomyRuntime


def get_state(request: Request) -> AppState:
    return request.app.state.state  # type: ignore[attr-defined]


def get_runtime(request: Request) -> AutonomyRuntime:
    return request.app.state.runtime  # type: ignore[attr-defined]


def require_operator(
    request: Request,
    x_nexus_token: Annotated[str | None, Header()] = None,
) -> str:
    expected = request.app.state.settings.nexuspmt_operator_token  # type: ignore[attr-defined]
    token = x_nexus_token or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not expected or token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid operator token")
    return "operator"
