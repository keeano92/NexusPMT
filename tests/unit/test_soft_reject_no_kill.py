"""Soft order rejects must not trip error_streak auto-kill."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.config import Settings
from backend.app.kalshi.client import KalshiAPIError, KalshiClient
from backend.app.risk.failsafes import FailSafeState
from backend.app.state import AppState, build_app_state
from backend.app.workers.runtime import AutonomyRuntime


def _state() -> AppState:
    settings = Settings(
        WORLDMAP_REQUIRED=False,
        KALSHI_TRADING_MODE="live",
        KALSHI_ENV="production",
        LIVE_UNLOCK=True,
        XAI_ENABLED=False,
        KALSHI_REQUIRE_APPROVAL_ABOVE_CENTS=10_000_000,
        KALSHI_STRONG_ALLOCATION_PCT=0.9,
        RISK_AUTO_KILL_ON_ERRORS=5,
    )
    state = build_app_state(settings)
    state.switch_book("production", "live")
    state.worldmap_ready = True
    state.live_cash_cents = 1062
    state.live_equity_cents = 1062
    state.live_portfolio_value_cents = 0
    state.portfolio_source = "live"
    state.book().live_shard_balances_cents = {0: 0, 2: 1062}
    return state


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_kill_desk():
    state = _state()
    rt = AutonomyRuntime(state)
    mock = MagicMock()
    mock.create_order = AsyncMock(
        side_effect=KalshiAPIError(
            status_code=400,
            code="insufficient_balance",
            message="insufficient balance",
        )
    )
    rt._kalshi = mock

    edge = {
        "ticker": "FED-SHARD0",
        "side": "yes",
        "market_prob": 0.5,
        "action": "buy_yes",
        "category": "Economics",
        "title": "Fed",
        "exchange_index": 0,
    }
    # Five soft rejects must leave desk running
    for _ in range(5):
        ok = await rt._maybe_trade(edge)
        assert ok is False
        # cooldown would block same ticker; clear for test
        rt._recent_trades.clear()

    snap = state.failsafes.snapshot()
    assert snap["state"] == "running"
    assert snap["error_streak"] == 0
    assert state.failsafes.can_place_orders()


@pytest.mark.asyncio
async def test_hard_error_still_increments_streak():
    state = _state()
    rt = AutonomyRuntime(state)
    mock = MagicMock()
    mock.create_order = AsyncMock(
        side_effect=KalshiAPIError(status_code=500, code="internal", message="boom")
    )
    rt._kalshi = mock

    edge = {
        "ticker": "BTC-2",
        "side": "yes",
        "market_prob": 0.5,
        "action": "buy_yes",
        "category": "Crypto",
        "title": "BTC",
        "exchange_index": 2,
    }
    for _ in range(5):
        await rt._maybe_trade(edge)
        rt._recent_trades.clear()

    assert state.failsafes.state == FailSafeState.KILLED


@pytest.mark.asyncio
async def test_order_uses_market_exchange_index_and_shard_cash():
    state = _state()
    rt = AutonomyRuntime(state)
    captured: dict[str, Any] = {}

    async def _create(body: dict[str, Any]) -> dict[str, Any]:
        captured.update(body)
        return {"order_id": "ord-1", "fill_count": "1.00", "remaining_count": "0.00", "ts_ms": 1}

    mock = MagicMock()
    mock.create_order = AsyncMock(side_effect=_create)
    mock.get_balance = AsyncMock(
        return_value={
            "balance": 500,
            "portfolio_value": 0,
            "balance_breakdown": [{"exchange_index": 2, "balance": "5.0000"}],
        }
    )
    mock.get_positions = AsyncMock(return_value={"market_positions": []})
    rt._kalshi = mock

    edge = {
        "ticker": "KXBTCD-1",
        "side": "yes",
        "market_prob": 0.5,
        "action": "buy_yes",
        "category": "Crypto",
        "title": "BTC",
        "exchange_index": 2,
    }
    ok = await rt._maybe_trade(edge)
    assert ok is True
    assert captured.get("exchange_index") == 2
    assert captured.get("ticker") == "KXBTCD-1"
    # Sized against shard-2 cash (~1062), not zero shard-0
    count = float(captured["count"])
    assert count >= 1


@pytest.mark.asyncio
async def test_skips_unfunded_shard_without_api_call():
    state = _state()
    rt = AutonomyRuntime(state)
    mock = MagicMock()
    mock.create_order = AsyncMock()
    rt._kalshi = mock

    edge = {
        "ticker": "FED-0",
        "side": "yes",
        "market_prob": 0.5,
        "action": "buy_yes",
        "category": "Economics",
        "title": "Fed",
        "exchange_index": 0,  # shard 0 has $0
    }
    ok = await rt._maybe_trade(edge)
    assert ok is False
    mock.create_order.assert_not_called()
    assert state.failsafes.snapshot()["state"] == "running"
