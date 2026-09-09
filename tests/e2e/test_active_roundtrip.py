"""E2E mock: odds ENTER → adverse mark → stop-loss close; desk stays running."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.config import Settings
from backend.app.state import build_app_state
from backend.app.workers.runtime import AutonomyRuntime


@pytest.fixture
def live_state():
    s = Settings(
        WORLDMAP_REQUIRED=False,
        KALSHI_TRADING_MODE="live",
        KALSHI_ENV="production",
        LIVE_UNLOCK=True,
        XAI_ENABLED=False,
        KALSHI_REQUIRE_APPROVAL_ABOVE_CENTS=10_000_000,
        KALSHI_STRONG_ALLOCATION_PCT=0.9,
        KALSHI_MAX_ENTRY_PCT=0.5,
        KALSHI_ENTER_VOI_THRESHOLD=0.28,
        KALSHI_REQUIRE_STRONG_ENTER=False,
        KALSHI_PREFER_15M=True,
        KALSHI_STOP_LOSS_PROB=0.10,
        KALSHI_MIN_EDGE=0.03,
        RISK_AUTO_KILL_ON_ERRORS=5,
        XAI_API_KEY="missing",
    )
    state = build_app_state(s)
    state.switch_book("production", "live")
    state.worldmap_ready = True
    state.live_cash_cents = 800
    state.live_equity_cents = 800
    state.live_portfolio_value_cents = 0
    state.portfolio_source = "live"
    state.book().live_shard_balances_cents = {0: 0, 2: 800}
    return state


@pytest.mark.asyncio
async def test_e2e_odds_enter_then_stop_loss(live_state):
    rt = AutonomyRuntime(live_state)
    from backend.app.workers.opportunity_eval import OpportunityEvaluator

    rt._evaluator = OpportunityEvaluator(
        api_key="missing",
        base_url="https://api.x.ai/v1",
        model="grok-4.6",
        enter_voi_threshold=0.28,
        require_strong_enter=False,
        min_edge=0.03,
    )

    orders: list[dict[str, Any]] = []

    async def _create(body: dict[str, Any]) -> dict[str, Any]:
        orders.append(body)
        return {
            "order_id": f"ord-{len(orders)}",
            "fill_count": body["count"],
            "remaining_count": "0.00",
            "ts_ms": 1,
        }

    mock = MagicMock()
    mock.create_order = AsyncMock(side_effect=_create)
    mock.get_balance = AsyncMock(
        return_value={
            "balance": 400,
            "portfolio_value": 400,
            "balance_breakdown": [{"exchange_index": 2, "balance": "4.0000"}],
        }
    )
    mock.get_positions = AsyncMock(
        return_value={
            "market_positions": [
                {
                    "ticker": "KXSOL15M-TEST",
                    "position_fp": "-5.00",
                    "market_exposure": 200,
                    "realized_pnl": 0,
                    "exchange_index": 2,
                }
            ]
        }
    )
    mock.list_markets = AsyncMock(
        return_value={
            "markets": [
                {
                    "ticker": "KXSOL15M-TEST",
                    "yes_bid_dollars": "0.66",
                    "yes_ask_dollars": "0.70",
                    "exchange_index": 2,
                    "close_time": "2099-01-01T00:00:00Z",
                }
            ]
        }
    )
    rt._kalshi = mock

    # ENTER via odds heuristic (fade 0.57 → NO)
    ok = await rt._maybe_trade(
        {
            "ticker": "KXSOL15M-TEST",
            "side": "no",
            "market_prob": 0.57,
            "action": "buy_no",
            "category": "Crypto",
            "title": "SOL up",
            "exchange_index": 2,
            "edge": 0.07,
            "micro_horizon": True,
        }
    )
    assert ok is True
    assert orders
    assert orders[0]["exchange_index"] == 2
    assert orders[0]["side"] == "ask"  # buy NO

    # Simulate open NO position + adverse mark (YES rose to 0.68)
    rt._entry_marks["KXSOL15M-TEST"] = {
        "side": "no",
        "entry_yes_prob": 0.57,
        "exchange_index": 2,
        "opened_ts": 1.0,
        "count": 5,
    }
    mock.list_markets = AsyncMock(
        return_value={
            "markets": [
                {
                    "ticker": "KXSOL15M-TEST",
                    "yes_bid_dollars": "0.66",
                    "yes_ask_dollars": "0.70",
                    "exchange_index": 2,
                    "close_time": "2099-01-01T00:00:00Z",
                }
            ]
        }
    )
    # Keep position visible through sync until close fills
    mock.get_positions = AsyncMock(
        return_value={
            "market_positions": [
                {
                    "ticker": "KXSOL15M-TEST",
                    "position_fp": "-5.00",
                    "market_exposure": 200,
                    "realized_pnl": 0,
                    "exchange_index": 2,
                }
            ]
        }
    )
    mock.get_balance = AsyncMock(
        return_value={
            "balance": 400,
            "portfolio_value": 400,
            "balance_breakdown": [{"exchange_index": 2, "balance": "4.0000"}],
        }
    )

    await rt._manage_open_positions()
    assert live_state.failsafes.snapshot()["state"] == "running"
    # Close or flip order should have been sent
    assert len(orders) >= 2
    statuses = [r.get("status") for r in live_state.ledger.list(limit=20)]
    assert any(s in {"stop_loss", "flip", "time_stop", "take_profit", "submitted"} for s in statuses)
