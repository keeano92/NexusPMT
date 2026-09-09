"""Secondary wheel edges must not overwrite opportunity ENTER in production."""

import pytest

from backend.app.config import Settings
from backend.app.state import build_app_state
from backend.app.workers.runtime import AutonomyRuntime


@pytest.mark.asyncio
async def test_refresh_edges_noop_when_secondary_disabled():
    state = build_app_state(
        Settings(
            WORLDMAP_REQUIRED=False,
            KALSHI_ENV="production",
            KALSHI_TRADING_MODE="live",
            KALSHI_ALLOW_SECONDARY_EDGES=False,
        )
    )
    state.edges = [
        {
            "ticker": "KEEP-ME",
            "side": "yes",
            "action": "buy_yes",
            "exchange_index": 2,
            "market_prob": 0.5,
        }
    ]
    rt = AutonomyRuntime(state)
    await rt._refresh_edges([])
    assert len(state.edges) == 1
    assert state.edges[0]["ticker"] == "KEEP-ME"


def test_allowlist_includes_crypto_beyond_first_six():
    """Regression: cats[:6] previously dropped Crypto (shard 2 cash)."""
    s = Settings()
    cats = [c.strip() for c in s.kalshi_category_allowlist.split(",") if c.strip()]
    assert "Crypto" in cats
    assert cats.index("Crypto") >= 6  # must not be truncated away
