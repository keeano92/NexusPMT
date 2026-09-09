"""Multi-horizon equity / PnL time series."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque, Literal

Horizon = Literal["rt", "5m", "15m", "30m", "1h", "daily"]

HORIZON_SECONDS: dict[Horizon, int] = {
    "rt": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "daily": 24 * 60 * 60,
}


@dataclass
class EquityMark:
    ts: float
    equity_cents: int
    realized_pnl_cents: int = 0
    unrealized_pnl_cents: int = 0


class PnLSeriesStore:
    def __init__(self, max_points: int = 10_000) -> None:
        self._lock = Lock()
        self._marks: Deque[EquityMark] = deque(maxlen=max_points)
        self._day_anchor_cents: int | None = None
        self._live_baseline_set: bool = False

    @staticmethod
    def _now() -> float:
        return datetime.now(timezone.utc).timestamp()

    def reset_anchor(self, equity_cents: int, *, clear_history: bool = True) -> None:
        """Re-base session PnL at equity (used when first live Kalshi sync arrives)."""
        with self._lock:
            if clear_history:
                self._marks.clear()
            self._day_anchor_cents = equity_cents
            self._live_baseline_set = True
            self._marks.append(
                EquityMark(ts=self._now(), equity_cents=equity_cents)
            )

    def has_anchor(self) -> bool:
        with self._lock:
            return self._day_anchor_cents is not None

    def record(
        self,
        equity_cents: int,
        *,
        realized_pnl_cents: int = 0,
        unrealized_pnl_cents: int = 0,
        ts: float | None = None,
        establish_anchor: bool = True,
    ) -> EquityMark:
        mark = EquityMark(
            ts=ts if ts is not None else self._now(),
            equity_cents=equity_cents,
            realized_pnl_cents=realized_pnl_cents,
            unrealized_pnl_cents=unrealized_pnl_cents,
        )
        with self._lock:
            if self._day_anchor_cents is None and establish_anchor:
                self._day_anchor_cents = equity_cents
                self._live_baseline_set = True
            self._marks.append(mark)
        return mark

    def _window(self, horizon: Horizon, now: float | None = None) -> list[EquityMark]:
        seconds = HORIZON_SECONDS[horizon]
        now_ts = now if now is not None else self._now()
        cutoff = now_ts - seconds
        with self._lock:
            return [m for m in self._marks if m.ts >= cutoff]

    def series(self, horizon: Horizon, *, max_points: int = 120) -> dict[str, Any]:
        points = self._window(horizon)
        if not points:
            with self._lock:
                day = self._day_anchor_cents
            return {
                "horizon": horizon,
                "points": [],
                "change_cents": 0,
                "change_pct": 0.0,
                "high_cents": None,
                "low_cents": None,
                "last_cents": None,
                "daily_pnl_cents": 0 if day is None else 0,
            }

        # Downsample for charts
        if len(points) > max_points:
            step = max(1, len(points) // max_points)
            points = points[::step]

        first = points[0].equity_cents
        last = points[-1].equity_cents
        change = last - first
        change_pct = (change / first * 100.0) if first else 0.0
        high = max(p.equity_cents for p in points)
        low = min(p.equity_cents for p in points)
        with self._lock:
            day_anchor = self._day_anchor_cents
        daily_pnl = (last - day_anchor) if day_anchor is not None else 0

        return {
            "horizon": horizon,
            "points": [
                {
                    "ts": p.ts,
                    "equity_cents": p.equity_cents,
                    "realized_pnl_cents": p.realized_pnl_cents,
                    "unrealized_pnl_cents": p.unrealized_pnl_cents,
                }
                for p in points
            ],
            "change_cents": change,
            "change_pct": round(change_pct, 4),
            "high_cents": high,
            "low_cents": low,
            "last_cents": last,
            "daily_pnl_cents": daily_pnl,
        }

    def all_horizons(self) -> dict[str, Any]:
        return {h: self.series(h) for h in HORIZON_SECONDS}

    def daily_pnl_cents(self) -> int:
        with self._lock:
            if not self._marks or self._day_anchor_cents is None:
                return 0
            return self._marks[-1].equity_cents - self._day_anchor_cents
