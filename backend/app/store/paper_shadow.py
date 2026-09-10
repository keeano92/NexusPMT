"""Paper shadow book: virtual $10 bankroll filled against live Kalshi marks."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PaperFill:
    ts: float
    ticker: str
    side: str
    qty: int
    price_cents: int
    kind: str  # open | close
    status: str  # filled | stop_loss | take_profit | time_stop
    pnl_cents: int = 0
    reason: str = ""


@dataclass
class PaperPosition:
    ticker: str
    side: str
    qty: int
    entry_yes_prob: float
    entry_price_cents: int  # what we paid per contract for our side
    exchange_index: int | None = None
    opened_ts: float = 0.0
    mark_yes_prob: float = 0.5


@dataclass
class PaperShadowBook:
    """Conservative paper fills: buys at ask, sells/covers at bid."""

    start_cents: int = 1000
    target_cents: int = 10000
    cash_cents: int = 1000
    peak_cents: int = 1000
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)
    closed_count: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl_cents: int = 0
    series_cooldown_until: dict[str, float] = field(default_factory=dict)
    # Equity curve for UI charts: [{ts, equity_cents}, ...]
    equity_points: list[dict[str, float]] = field(default_factory=list)

    @classmethod
    def create(cls, start_cents: int = 1000, target_cents: int = 10000) -> PaperShadowBook:
        book = cls(
            start_cents=start_cents,
            target_cents=target_cents,
            cash_cents=start_cents,
            peak_cents=start_cents,
        )
        book._record_equity(force=True)
        return book

    def _record_equity(self, force: bool = False) -> None:
        now = time.time()
        eq = float(self.equity_cents())
        if not force and self.equity_points:
            last = self.equity_points[-1]
            # Throttle: ≥5s or ≥1¢ change
            if now - float(last["ts"]) < 5 and abs(eq - float(last["equity_cents"])) < 1:
                return
        self.equity_points.append({"ts": now, "equity_cents": eq})
        if len(self.equity_points) > 2000:
            self.equity_points = self.equity_points[-1500:]

    def equity_cents(self) -> int:
        eq = self.cash_cents
        for pos in self.positions.values():
            eq += self._mark_value_cents(pos)
        return eq

    def _mark_value_cents(self, pos: PaperPosition) -> int:
        """Mark-to-market value of position in cents."""
        mark_yes = float(pos.mark_yes_prob)
        if pos.side == "yes":
            # Long YES worth mark_yes * 100 per contract
            return int(round(mark_yes * 100 * pos.qty))
        # Long NO worth (1-mark_yes)*100
        return int(round((1.0 - mark_yes) * 100 * pos.qty))

    def session_pnl_cents(self) -> int:
        return self.equity_cents() - self.start_cents

    def drawdown_pct(self) -> float:
        peak = max(self.peak_cents, 1)
        eq = self.equity_cents()
        if eq > self.peak_cents:
            self.peak_cents = eq
            return 0.0
        return ((peak - eq) / peak) * 100.0

    def expectancy_cents(self) -> float:
        if self.closed_count <= 0:
            return 0.0
        return self.realized_pnl_cents / float(self.closed_count)

    def unlocked_for_live(self, min_trades: int = 30) -> bool:
        return (
            self.equity_cents() >= self.target_cents
            and self.closed_count >= min_trades
            and self.expectancy_cents() >= 0
        )

    def can_enter_series(self, series: str, now: float | None = None) -> bool:
        now = now or time.time()
        until = self.series_cooldown_until.get(series) or 0.0
        return now >= until

    def set_series_cooldown(self, series: str, sec: float = 300.0) -> None:
        self.series_cooldown_until[series] = time.time() + sec

    def open_position(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        yes_bid: float,
        yes_ask: float,
        exchange_index: int | None = None,
    ) -> PaperFill | None:
        if qty <= 0 or ticker in self.positions:
            return None
        side_l = "no" if side.lower() == "no" else "yes"
        # Conservative: buy YES at ask; buy NO ≈ pay (1-bid) using bid as YES bid
        if side_l == "yes":
            pay = max(1, min(99, int(round(float(yes_ask) * 100))))
            entry_yes = float(yes_ask)
        else:
            pay = max(1, min(99, int(round((1.0 - float(yes_bid)) * 100))))
            entry_yes = float(yes_bid)
        cost = pay * qty
        if cost > self.cash_cents:
            qty = self.cash_cents // pay
            if qty <= 0:
                return None
            cost = pay * qty
        self.cash_cents -= cost
        pos = PaperPosition(
            ticker=ticker,
            side=side_l,
            qty=qty,
            entry_yes_prob=entry_yes,
            entry_price_cents=pay,
            exchange_index=exchange_index,
            opened_ts=time.time(),
            mark_yes_prob=entry_yes,
        )
        self.positions[ticker] = pos
        fill = PaperFill(
            ts=time.time(),
            ticker=ticker,
            side=side_l,
            qty=qty,
            price_cents=pay,
            kind="open",
            status="filled",
            reason="paper open vs live book",
        )
        self.fills.append(fill)
        self._record_equity(force=True)
        return fill

    def update_mark(self, ticker: str, yes_mid: float) -> None:
        pos = self.positions.get(ticker)
        if not pos:
            return
        pos.mark_yes_prob = float(yes_mid)
        eq = self.equity_cents()
        if eq > self.peak_cents:
            self.peak_cents = eq
        self._record_equity()

    def close_position(
        self,
        *,
        ticker: str,
        yes_bid: float,
        yes_ask: float,
        status: str,
        reason: str = "",
    ) -> PaperFill | None:
        pos = self.positions.pop(ticker, None)
        if not pos:
            return None
        # Exit conservatively: sell YES at bid; cover NO by buying YES at ask
        if pos.side == "yes":
            exit_px = max(1, min(99, int(round(float(yes_bid) * 100))))
            proceeds = exit_px * pos.qty
        else:
            cover = max(1, min(99, int(round(float(yes_ask) * 100))))
            # We paid entry_price for NO; covering costs cover*qty of YES
            # Proceeds conceptually: we get back (100 - cover) per NO... simpler:
            # PnL = (entry_pay - cover_cost_of_yes_for_no) wait
            # Entry: paid `entry_price_cents` per NO (= 100 - yes_bid_at_entry roughly)
            # Exit cover: buy YES at ask → spend ask*100 cents; NO settles as 100-yes
            # Mark exit value of NO = (100 - ask_cents) if we liquidate by buying YES? 
            # Buying YES to flatten NO: spend ask; net cash change = -ask*qty; 
            # we already spent entry; economic PnL ≈ entry_no_value_change
            # Simpler accounting: position marked value at exit mid, cash += mark_value
            exit_px = max(1, min(99, 100 - int(round(float(yes_ask) * 100))))
            proceeds = exit_px * pos.qty
        cost_basis = pos.entry_price_cents * pos.qty
        pnl = proceeds - cost_basis
        self.cash_cents += proceeds
        self.realized_pnl_cents += pnl
        self.closed_count += 1
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
            series = ticker.rsplit("-", 1)[0] if "-" in ticker else ticker
            self.set_series_cooldown(series, 300.0)
        fill = PaperFill(
            ts=time.time(),
            ticker=ticker,
            side=pos.side,
            qty=pos.qty,
            price_cents=exit_px if pos.side == "yes" else (100 - exit_px),
            kind="close",
            status=status,
            pnl_cents=pnl,
            reason=reason,
        )
        self.fills.append(fill)
        eq = self.equity_cents()
        if eq > self.peak_cents:
            self.peak_cents = eq
        self._record_equity(force=True)
        return fill

    def pnl_series(self) -> dict[str, Any]:
        """Shape compatible with frontend chart (same as PnLSeriesStore horizon)."""
        pts = list(self.equity_points) or [
            {"ts": time.time(), "equity_cents": float(self.equity_cents())}
        ]
        equities = [float(p["equity_cents"]) for p in pts]
        first = equities[0]
        last = equities[-1]
        change = last - first
        change_pct = (change / first * 100.0) if first else 0.0
        return {
            "horizon": "paper",
            "points": [
                {
                    "ts": float(p["ts"]),
                    "equity_cents": int(p["equity_cents"]),
                    "realized_pnl_cents": 0,
                    "unrealized_pnl_cents": 0,
                }
                for p in pts
            ],
            "change_cents": int(change),
            "change_pct": round(change_pct, 2),
            "high_cents": int(max(equities)),
            "low_cents": int(min(equities)),
            "last_cents": int(last),
            "daily_pnl_cents": self.session_pnl_cents(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "start_cents": self.start_cents,
            "target_cents": self.target_cents,
            "cash_cents": self.cash_cents,
            "equity_cents": self.equity_cents(),
            "session_pnl_cents": self.session_pnl_cents(),
            "peak_cents": self.peak_cents,
            "drawdown_pct": round(self.drawdown_pct(), 2),
            "closed_count": self.closed_count,
            "wins": self.wins,
            "losses": self.losses,
            "expectancy_cents": round(self.expectancy_cents(), 2),
            "realized_pnl_cents": self.realized_pnl_cents,
            "positions": [asdict(p) for p in self.positions.values()],
            "fills_tail": [asdict(f) for f in self.fills[-40:]],
            "equity_points": self.equity_points[-500:],
            "pnl": self.pnl_series(),
            "gate_ready": self.unlocked_for_live(),
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "start_cents": self.start_cents,
            "target_cents": self.target_cents,
            "cash_cents": self.cash_cents,
            "peak_cents": self.peak_cents,
            "closed_count": self.closed_count,
            "wins": self.wins,
            "losses": self.losses,
            "realized_pnl_cents": self.realized_pnl_cents,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "fills": [asdict(f) for f in self.fills[-500:]],
            "equity_points": self.equity_points[-2000:],
            "series_cooldown_until": self.series_cooldown_until,
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, start_cents: int = 1000, target_cents: int = 10000) -> PaperShadowBook:
        p = Path(path)
        if not p.exists():
            return cls.create(start_cents=start_cents, target_cents=target_cents)
        raw = json.loads(p.read_text(encoding="utf-8"))
        book = cls(
            start_cents=int(raw.get("start_cents") or start_cents),
            target_cents=int(raw.get("target_cents") or target_cents),
            cash_cents=int(raw.get("cash_cents") or start_cents),
            peak_cents=int(raw.get("peak_cents") or start_cents),
            closed_count=int(raw.get("closed_count") or 0),
            wins=int(raw.get("wins") or 0),
            losses=int(raw.get("losses") or 0),
            realized_pnl_cents=int(raw.get("realized_pnl_cents") or 0),
            series_cooldown_until={str(k): float(v) for k, v in (raw.get("series_cooldown_until") or {}).items()},
        )
        for t, pdata in (raw.get("positions") or {}).items():
            book.positions[str(t)] = PaperPosition(**pdata)
        for f in raw.get("fills") or []:
            book.fills.append(PaperFill(**f))
        for pt in raw.get("equity_points") or []:
            book.equity_points.append(
                {"ts": float(pt["ts"]), "equity_cents": float(pt["equity_cents"])}
            )
        if not book.equity_points:
            # Rebuild a coarse curve from fills so charts aren't empty after upgrade
            book.equity_points.append(
                {"ts": time.time() - 3600, "equity_cents": float(book.start_cents)}
            )
            running = float(book.start_cents)
            for f in book.fills:
                if f.kind == "close":
                    running += float(f.pnl_cents)
                    book.equity_points.append({"ts": float(f.ts), "equity_cents": running})
            book._record_equity(force=True)
        return book
