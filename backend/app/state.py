"""Shared runtime state for workers and API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from backend.app.config import Settings, get_settings
from backend.app.kalshi.market_filter import MarketFilter
from backend.app.risk.failsafes import FailSafeController
from backend.app.store.pnl_series import PnLSeriesStore
from backend.app.store.trade_ledger import TradeLedger


@dataclass
class AppState:
    settings: Settings
    failsafes: FailSafeController
    pnl: PnLSeriesStore
    ledger: TradeLedger
    market_filter: MarketFilter
    intel_snippets: list[str] = field(default_factory=list)
    wheel_nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    worldmap_health: dict[str, Any] = field(default_factory=dict)
    worldmap_ready: bool = False
    worldmap_block_reason: str = "Waiting for SK AI WorldMap liveness"
    worldmap_paused_autonomy: bool = False
    last_ingest_ts: str | None = None
    paper_cash_cents: int = 100_000  # $1000 paper starting cash
    paper_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    bus_subscribers: set[asyncio.Queue] = field(default_factory=set)

    def set_worldmap_ready(self, ready: bool, reason: str = "") -> None:
        was_ready = self.worldmap_ready
        self.worldmap_ready = ready
        self.worldmap_block_reason = "" if ready else (reason or "SK AI WorldMap required but unreachable")
        if (
            not ready
            and self.settings.worldmap_required
            and was_ready
            and self.failsafes.can_place_orders()
        ):
            # Hard couple: autonomy cannot place orders without WorldMap intel
            self.failsafes.pause(actor="worldmap_gate")
            self.worldmap_paused_autonomy = True
            self.ledger.append(
                kind="system",
                status="blocked",
                message="WorldMap lost — autonomy paused",
            )
        if not ready and self.settings.worldmap_required and not was_ready:
            # Initial boot / still down: ensure we do not trade
            self.worldmap_paused_autonomy = True

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self.bus_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.bus_subscribers.discard(q)

    def dashboard_snapshot(self) -> dict[str, Any]:
        return {
            "trading_mode": self.settings.kalshi_trading_mode,
            "kalshi_env": self.settings.kalshi_env,
            "fundamentals_filter": self.settings.kalshi_sports_filter,
            "failsafes": self.failsafes.snapshot(),
            "pnl": self.pnl.all_horizons(),
            "daily_pnl_cents": self.pnl.daily_pnl_cents(),
            "wheel": self.wheel_nodes[-50:],
            "edges": self.edges[:50],
            "ledger": self.ledger.list(limit=100),
            "positions": self.positions,
            "worldmap_health": self.worldmap_health,
            "worldmap_ready": self.worldmap_ready,
            "worldmap_required": self.settings.worldmap_required,
            "worldmap_block_reason": self.worldmap_block_reason,
            "last_ingest_ts": self.last_ingest_ts,
            "paper_cash_cents": self.paper_cash_cents,
        }


def build_app_state(settings: Settings | None = None) -> AppState:
    s = settings or get_settings()
    return AppState(
        settings=s,
        failsafes=FailSafeController(
            max_daily_loss_cents=s.risk_max_daily_loss_cents,
            max_drawdown_pct=s.risk_max_drawdown_pct,
            auto_kill_on_errors=s.risk_auto_kill_on_errors,
        ),
        pnl=PnLSeriesStore(),
        ledger=TradeLedger(),
        market_filter=MarketFilter.from_settings(
            s.allowlist,
            s.blocklist,
            strict=s.kalshi_sports_filter == "strict",
        ),
    )
