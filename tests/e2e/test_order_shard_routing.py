"""E2E (mocked Kalshi): shard routing + soft reject does not halt trading."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.config import Settings
from backend.app.kalshi.client import KalshiAPIError, KalshiClient
from backend.app.kalshi.portfolio import funded_exchange_indexes, normalize_balance
from backend.app.state import build_app_state
from backend.app.workers.opportunity_eval import collect_candidate_markets
from backend.app.workers.runtime import AutonomyRuntime


@pytest.fixture
def live_state():
    s = Settings(
        WORLDMAP_REQUIRED=False,
        KALSHI_TRADING_MODE="live",
        KALSHI_ENV="production",
        KALSHI_REQUIRE_APPROVAL_ABOVE_CENTS=10_000_000,
        KALSHI_STRONG_ALLOCATION_PCT=0.9,
        RISK_AUTO_KILL_ON_ERRORS=3,
        KALSHI_CATEGORY_ALLOWLIST="Economics,Crypto",
        KALSHI_CATEGORY_BLOCKLIST="Sports",
        KALSHI_FILTER_MODE="blocklist",
    )
    state = build_app_state(s)
    state.switch_book("production", "live")
    state.worldmap_ready = True
    return state


@pytest.mark.asyncio
async def test_e2e_balance_sync_exposes_funded_shards(live_state):
    rt = AutonomyRuntime(live_state)
    mock = MagicMock()
    mock.get_balance = AsyncMock(
        return_value={
            "balance": 1061,
            "portfolio_value": 1,
            "balance_breakdown": [
                {"exchange_index": 0, "balance": "0.0000"},
                {"exchange_index": 2, "balance": "10.6182"},
            ],
        }
    )
    mock.get_positions = AsyncMock(return_value={"market_positions": []})
    rt._kalshi = mock

    out = await rt.sync_live_portfolio()
    assert out["ok"] is True
    shards = live_state.book().live_shard_balances_cents
    assert funded_exchange_indexes(shards) == {2}
    assert live_state.failsafes.can_place_orders()


@pytest.mark.asyncio
async def test_e2e_scan_only_keeps_funded_shard_markets(live_state):
    live_state.book().live_shard_balances_cents = {0: 0, 2: 1062}
    markets = [
        {
            "ticker": "FED-0",
            "title": "Fed",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "5000",
            "series_ticker": "FED",
            "event_ticker": "FED-E",
            "exchange_index": 0,
        },
        {
            "ticker": "BTC-2",
            "title": "BTC",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.46",
            "volume_fp": "5000",
            "series_ticker": "BTC",
            "event_ticker": "BTC-E",
            "exchange_index": 2,
        },
    ]
    series = {
        "FED": {"ticker": "FED", "category": "Economics", "title": "Fed", "tags": []},
        "BTC": {"ticker": "BTC", "category": "Crypto", "title": "BTC", "tags": []},
    }
    funded = funded_exchange_indexes(live_state.book().live_shard_balances_cents)
    cands = collect_candidate_markets(
        markets,
        series,
        live_state.market_filter,
        max_spread=0.2,
        min_liquidity=0,
        funded_shards=funded,
    )
    assert [c["ticker"] for c in cands] == ["BTC-2"]


@pytest.mark.asyncio
async def test_e2e_order_succeeds_on_funded_shard(live_state):
    live_state.live_cash_cents = 1062
    live_state.live_equity_cents = 1062
    live_state.live_portfolio_value_cents = 0
    live_state.portfolio_source = "live"
    live_state.book().live_shard_balances_cents = {0: 0, 2: 1062}

    rt = AutonomyRuntime(live_state)
    captured: list[dict[str, Any]] = []

    async def _create(body: dict[str, Any]) -> dict[str, Any]:
        captured.append(body)
        return {
            "order_id": "e2e-ord-1",
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "ts_ms": 1,
        }

    mock = MagicMock()
    mock.create_order = AsyncMock(side_effect=_create)
    mock.get_balance = AsyncMock(
        return_value={
            "balance": 500,
            "portfolio_value": 50,
            "balance_breakdown": [{"exchange_index": 2, "balance": "5.0000"}],
        }
    )
    mock.get_positions = AsyncMock(return_value={"market_positions": []})
    rt._kalshi = mock

    ok = await rt._maybe_trade(
        {
            "ticker": "KXBTCD-26SEP09-T87000",
            "side": "yes",
            "market_prob": 0.5,
            "action": "buy_yes",
            "category": "Crypto",
            "title": "BTC",
            "exchange_index": 2,
            "edge": 0.1,
        }
    )
    assert ok is True
    assert len(captured) == 1
    assert captured[0]["exchange_index"] == 2
    assert captured[0]["side"] == "bid"
    assert live_state.failsafes.snapshot()["state"] == "running"
    # Ledger shows submitted
    rows = live_state.ledger.list(limit=5)
    assert any(r.get("status") == "submitted" for r in rows)


@pytest.mark.asyncio
async def test_e2e_five_soft_rejects_leave_desk_running(live_state):
    live_state.live_cash_cents = 1062
    live_state.live_equity_cents = 1062
    live_state.portfolio_source = "live"
    # Pretend shard 0 has cash so order is attempted (then soft-rejected)
    live_state.book().live_shard_balances_cents = {0: 1062}

    rt = AutonomyRuntime(live_state)
    mock = MagicMock()
    mock.create_order = AsyncMock(
        side_effect=KalshiAPIError(
            status_code=400,
            code="insufficient_balance",
            message="insufficient balance",
        )
    )
    rt._kalshi = mock

    for i in range(5):
        await rt._maybe_trade(
            {
                "ticker": f"FED-{i}",
                "side": "yes",
                "market_prob": 0.5,
                "action": "buy_yes",
                "category": "Economics",
                "title": "Fed",
                "exchange_index": 0,
            }
        )

    snap = live_state.failsafes.snapshot()
    assert snap["state"] == "running"
    assert snap["error_streak"] == 0
    assert live_state.failsafes.can_place_orders()
    soft = [r for r in live_state.ledger.list(limit=20) if r.get("status") == "soft_reject"]
    assert len(soft) >= 5


def test_normalize_roundtrip_matches_live_shape():
    norms = normalize_balance(
        {
            "balance": 1061,
            "portfolio_value": 1,
            "balance_breakdown": [
                {"exchange_index": 0, "balance": "0.0000"},
                {"exchange_index": 2, "balance": "10.6182"},
            ],
        }
    )
    body = KalshiClient.build_v2_order(
        ticker="KXBTCD-1",
        side="yes",
        count=1,
        yes_price_cents=50,
        client_order_id="11111111-1111-1111-1111-111111111111",
        exchange_index=2,
    )
    assert norms["shard_balances_cents"][2] >= 1000
    assert body["exchange_index"] == 2
