"""Per-environment trading books — demo never shares PnL/ledger with production."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.app.store.pnl_series import PnLSeriesStore
from backend.app.store.trade_ledger import TradeLedger


def book_key(kalshi_env: str, trading_mode: str) -> str:
    return f"{kalshi_env.strip().lower()}:{trading_mode.strip().lower()}"


@dataclass
class EnvBook:
    """Isolated cash, positions, PnL, and ledger for one env+mode pair."""

    key: str
    kalshi_env: str
    trading_mode: str
    pnl: PnLSeriesStore = field(default_factory=PnLSeriesStore)
    ledger: TradeLedger = field(default_factory=TradeLedger)
    paper_cash_cents: int = 100_000
    paper_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: list[dict[str, Any]] = field(default_factory=list)
    live_cash_cents: int | None = None
    live_portfolio_value_cents: int | None = None
    live_equity_cents: int | None = None
    live_realized_pnl_cents: int = 0
    portfolio_source: str = "paper"
    portfolio_updated_ts: str | None = None

    @classmethod
    def create(cls, kalshi_env: str, trading_mode: str, paper_start_cents: int = 100_000) -> EnvBook:
        key = book_key(kalshi_env, trading_mode)
        book = cls(
            key=key,
            kalshi_env=kalshi_env,
            trading_mode=trading_mode,
            paper_cash_cents=paper_start_cents,
        )
        book.pnl.record(paper_start_cents)
        return book

    def cash_cents(self) -> int:
        if self.portfolio_source == "live" and self.live_cash_cents is not None:
            return self.live_cash_cents
        return self.paper_cash_cents

    def equity_cents(self) -> int:
        if self.portfolio_source == "live" and self.live_equity_cents is not None:
            return self.live_equity_cents
        return self.paper_cash_cents + sum(
            int(p.get("qty", 0)) * int(p.get("avg_price_cents", 0))
            for p in self.paper_positions.values()
        )
