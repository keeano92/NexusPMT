"""Live position management: stop-loss, take-profit, flip, time-stop."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal


Action = Literal["hold", "stop_loss", "take_profit", "flip", "time_stop"]


@dataclass
class PositionDecision:
    action: Action
    reason: str
    close_side: str | None = None  # position side to flatten
    flip_to: str | None = None
    pnl_prob: float = 0.0  # marked move in probability space (signed for holder)


def _holder_pnl_prob(side: str, entry_yes: float, mark_yes: float) -> float:
    """Positive = favorable for holder, in probability points."""
    if (side or "yes").lower() == "no":
        # NO value ≈ 1 - yes; pnl ≈ entry_yes - mark_yes
        return float(entry_yes) - float(mark_yes)
    return float(mark_yes) - float(entry_yes)


def decide_position_action(
    *,
    side: str,
    entry_yes_prob: float,
    mark_yes_prob: float,
    stop_loss_prob: float,
    take_profit_prob: float,
    flip_side: str | None,
    flip_min_edge: float,
    seconds_to_close: float | None,
    time_stop_sec: float,
    held_sec: float | None = None,
    max_hold_sec: float | None = None,
) -> PositionDecision:
    side_l = (side or "yes").lower()
    if side_l not in {"yes", "no"}:
        side_l = "yes"
    pnl = _holder_pnl_prob(side_l, entry_yes_prob, mark_yes_prob)

    if (
        max_hold_sec is not None
        and held_sec is not None
        and held_sec >= max_hold_sec
    ):
        return PositionDecision(
            action="time_stop",
            reason=f"max_hold {held_sec:.0f}s ≥ {max_hold_sec:.0f}s pnl={pnl:+.3f}",
            close_side=side_l,
            pnl_prob=pnl,
        )

    # Flip when underwater enough AND re-eval prefers the opposite side
    # (mirrors manual SOL: NO entry → YES cover after adverse move).
    if (
        flip_side
        and flip_side.lower() in {"yes", "no"}
        and flip_side.lower() != side_l
        and pnl <= -abs(flip_min_edge)
    ):
        return PositionDecision(
            action="flip",
            reason=f"flip to {flip_side} (pnl_prob={pnl:+.3f})",
            close_side=side_l,
            flip_to=flip_side.lower(),
            pnl_prob=pnl,
        )

    if pnl <= -abs(stop_loss_prob):
        return PositionDecision(
            action="stop_loss",
            reason=f"stop_loss pnl_prob={pnl:+.3f} ≤ -{stop_loss_prob}",
            close_side=side_l,
            pnl_prob=pnl,
        )

    if pnl >= abs(take_profit_prob):
        return PositionDecision(
            action="take_profit",
            reason=f"take_profit pnl_prob={pnl:+.3f} ≥ {take_profit_prob}",
            close_side=side_l,
            pnl_prob=pnl,
        )

    if (
        seconds_to_close is not None
        and seconds_to_close <= time_stop_sec
        and pnl < 0
    ):
        return PositionDecision(
            action="time_stop",
            reason=f"time_stop {seconds_to_close:.0f}s left underwater pnl={pnl:+.3f}",
            close_side=side_l,
            pnl_prob=pnl,
        )

    return PositionDecision(action="hold", reason="inside bands", pnl_prob=pnl)


def build_close_v2_order(
    *,
    ticker: str,
    position_side: str,
    count: int,
    mark_yes_prob: float,
    client_order_id: str | None = None,
    exchange_index: int | None = None,
) -> dict[str, Any]:
    """Flatten a live position via Create Order V2.

    Holding YES → sell YES (ask). Holding NO → buy YES (bid) to cover.
    Price is aggressive vs mark so we get out (slip a few cents).
    """
    side_l = (position_side or "yes").lower()
    mark_cents = int(round(float(mark_yes_prob) * 100))
    mark_cents = max(1, min(99, mark_cents))
    if side_l == "no":
        # Cover NO: bid YES a bit above mark to lift offers
        book_side = "bid"
        px = min(99, mark_cents + 3)
    else:
        # Sell YES: ask a bit below mark to hit bids
        book_side = "ask"
        px = max(1, mark_cents - 3)
    body: dict[str, Any] = {
        "ticker": ticker,
        "side": book_side,
        "count": f"{max(1, int(count))}.00",
        "price": f"{px / 100:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }
    if exchange_index is not None:
        body["exchange_index"] = int(exchange_index)
    return body
