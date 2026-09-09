"""Shared runtime state for workers and API."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from backend.app.config import Settings, get_settings
from backend.app.kalshi.market_filter import MarketFilter
from backend.app.risk.failsafes import FailSafeController
from backend.app.store.env_book import EnvBook, book_key
from backend.app.store.pnl_series import PnLSeriesStore
from backend.app.store.trade_ledger import TradeLedger


@dataclass
class AppState:
    settings: Settings
    failsafes: FailSafeController
    market_filter: MarketFilter
    books: dict[str, EnvBook] = field(default_factory=dict)
    active_book_key: str = "demo:paper"
    intel_snippets: list[str] = field(default_factory=list)
    wheel_nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    terminal_lines: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    worldmap_health: dict[str, Any] = field(default_factory=dict)
    worldmap_ready: bool = False
    worldmap_block_reason: str = "Waiting for SK AI WorldMap liveness"
    worldmap_paused_autonomy: bool = False
    last_ingest_ts: str | None = None
    bus_subscribers: set[asyncio.Queue] = field(default_factory=set)

    def book(self) -> EnvBook:
        if self.active_book_key not in self.books:
            env, mode = self.active_book_key.split(":", 1)
            self.books[self.active_book_key] = EnvBook.create(env, mode)
        return self.books[self.active_book_key]

    def switch_book(self, kalshi_env: str, trading_mode: str) -> EnvBook:
        key = book_key(kalshi_env, trading_mode)
        if key not in self.books:
            self.books[key] = EnvBook.create(kalshi_env, trading_mode)
        self.active_book_key = key
        book = self.books[key]
        # Never carry demo equity anchors into production (or vice versa).
        # Live books wait for Kalshi sync before arming risk/PnL anchors.
        if book.trading_mode == "live" and book.live_equity_cents is None:
            self.failsafes.reset_equity_anchors(None)
        else:
            self.failsafes.reset_equity_anchors(book.equity_cents())
        self.terminal(
            f"BOOK SWITCH → {key} | cash={book.cash_cents()}¢ equity={book.equity_cents()}¢ source={book.portfolio_source}"
        )
        return book

    # --- compatibility accessors (delegate to active env book) ---
    @property
    def pnl(self) -> PnLSeriesStore:
        return self.book().pnl

    @property
    def ledger(self) -> TradeLedger:
        return self.book().ledger

    @property
    def positions(self) -> list[dict[str, Any]]:
        return self.book().positions

    @positions.setter
    def positions(self, value: list[dict[str, Any]]) -> None:
        self.book().positions = value

    @property
    def paper_cash_cents(self) -> int:
        return self.book().paper_cash_cents

    @paper_cash_cents.setter
    def paper_cash_cents(self, value: int) -> None:
        self.book().paper_cash_cents = value

    @property
    def paper_positions(self) -> dict[str, dict[str, Any]]:
        return self.book().paper_positions

    @property
    def live_cash_cents(self) -> int | None:
        return self.book().live_cash_cents

    @live_cash_cents.setter
    def live_cash_cents(self, value: int | None) -> None:
        self.book().live_cash_cents = value

    @property
    def live_portfolio_value_cents(self) -> int | None:
        return self.book().live_portfolio_value_cents

    @live_portfolio_value_cents.setter
    def live_portfolio_value_cents(self, value: int | None) -> None:
        self.book().live_portfolio_value_cents = value

    @property
    def live_equity_cents(self) -> int | None:
        return self.book().live_equity_cents

    @live_equity_cents.setter
    def live_equity_cents(self, value: int | None) -> None:
        self.book().live_equity_cents = value

    @property
    def live_realized_pnl_cents(self) -> int:
        return self.book().live_realized_pnl_cents

    @live_realized_pnl_cents.setter
    def live_realized_pnl_cents(self, value: int) -> None:
        self.book().live_realized_pnl_cents = value

    @property
    def portfolio_source(self) -> str:
        return self.book().portfolio_source

    @portfolio_source.setter
    def portfolio_source(self, value: str) -> None:
        self.book().portfolio_source = value

    @property
    def portfolio_updated_ts(self) -> str | None:
        return self.book().portfolio_updated_ts

    @portfolio_updated_ts.setter
    def portfolio_updated_ts(self, value: str | None) -> None:
        self.book().portfolio_updated_ts = value

    def terminal(self, line: str) -> None:
        from backend.app.timeutil import local_clock

        msg = f"[{local_clock()}] {line}"
        self.terminal_lines.append(msg)

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
            self.failsafes.pause(actor="worldmap_gate")
            self.worldmap_paused_autonomy = True
            self.ledger.append(
                kind="system",
                status="blocked",
                message="WorldMap lost — autonomy paused",
            )
            self.terminal("WARN WorldMap lost — autonomy paused")
        if not ready and self.settings.worldmap_required and not was_ready:
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
        book = self.book()
        return {
            "trading_mode": self.settings.kalshi_trading_mode,
            "kalshi_env": self.settings.kalshi_env,
            "book_key": book.key,
            "fundamentals_filter": self.settings.kalshi_sports_filter,
            "failsafes": self.failsafes.snapshot(),
            "pnl": book.pnl.all_horizons(),
            "daily_pnl_cents": book.pnl.daily_pnl_cents(),
            "session_pnl_cents": book.pnl.daily_pnl_cents(),
            "equity_cents": book.equity_cents(),
            "wheel": self.wheel_nodes[-50:],
            "edges": self.edges[:50],
            "evaluations": self.evaluations[-40:],
            "terminal": list(self.terminal_lines)[-120:],
            "ledger": book.ledger.list(limit=100),
            "positions": book.positions,
            "worldmap_health": self.worldmap_health,
            "worldmap_ready": self.worldmap_ready,
            "worldmap_required": self.settings.worldmap_required,
            "worldmap_block_reason": self.worldmap_block_reason,
            "last_ingest_ts": self.last_ingest_ts,
            "paper_cash_cents": book.paper_cash_cents,
            "portfolio_source": book.portfolio_source,
            "cash_cents": book.cash_cents(),
            "live_cash_cents": book.live_cash_cents,
            "live_portfolio_value_cents": book.live_portfolio_value_cents,
            "live_equity_cents": book.live_equity_cents,
            "live_realized_pnl_cents": book.live_realized_pnl_cents,
            "portfolio_updated_ts": book.portfolio_updated_ts,
            "trading_halted": self.failsafes.snapshot()["state"] == "killed",
            "trading_halt_reason": next(
                (
                    (e.get("detail") or {}).get("reason")
                    for e in reversed(self.failsafes.snapshot().get("audit_tail") or [])
                    if e.get("action") == "auto_kill"
                ),
                None,
            ),
        }


def build_app_state(settings: Settings | None = None) -> AppState:
    s = settings or get_settings()
    key = book_key(s.kalshi_env, s.kalshi_trading_mode)
    books = {key: EnvBook.create(s.kalshi_env, s.kalshi_trading_mode)}
    return AppState(
        settings=s,
        failsafes=FailSafeController(
            max_daily_loss_cents=s.risk_max_daily_loss_cents,
            max_drawdown_pct=s.risk_max_drawdown_pct,
            auto_kill_on_errors=s.risk_auto_kill_on_errors,
        ),
        market_filter=MarketFilter.from_settings(
            s.allowlist,
            s.blocklist,
            strict=s.kalshi_sports_filter == "strict",
            mode=s.kalshi_filter_mode,
        ),
        books=books,
        active_book_key=key,
    )
