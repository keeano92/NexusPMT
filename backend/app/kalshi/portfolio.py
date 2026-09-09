"""Normalize Kalshi portfolio balance/positions into NexusPMT shapes."""

from __future__ import annotations

from typing import Any


def _dollars_to_cents(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        # Heuristic: ints that look like cents stay; small floats are dollars
        if isinstance(value, int) and abs(value) >= 100:
            return int(value)
        return int(round(float(value) * 100))
    s = str(value).strip()
    if not s:
        return 0
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return 0


def _fp_count(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_balance(payload: dict[str, Any]) -> dict[str, int]:
    """Kalshi GET /portfolio/balance → cash + portfolio_value + equity (cents)."""
    cash = int(payload.get("balance") or 0)
    # Prefer explicit cents field; fall back to dollars string
    if "balance" not in payload and payload.get("balance_dollars") is not None:
        cash = _dollars_to_cents(payload.get("balance_dollars"))

    port = payload.get("portfolio_value")
    if port is None and payload.get("portfolio_value_dollars") is not None:
        port_cents = _dollars_to_cents(payload.get("portfolio_value_dollars"))
    else:
        port_cents = int(port or 0)

    # Total account equity ≈ available cash + mark of open positions
    equity = cash + port_cents
    return {
        "cash_cents": cash,
        "portfolio_value_cents": port_cents,
        "equity_cents": equity,
    }


def normalize_positions(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Return (positions, sum_realized_pnl_cents)."""
    raw = payload.get("market_positions") or payload.get("positions") or []
    out: list[dict[str, Any]] = []
    realized_total = 0
    for p in raw:
        if not isinstance(p, dict):
            continue
        qty_fp = _fp_count(p.get("position_fp", p.get("position", 0)))
        if qty_fp == 0:
            continue
        side = "yes" if qty_fp > 0 else "no"
        qty = int(abs(round(qty_fp)))
        exposure = p.get("market_exposure")
        if exposure is None:
            exposure_cents = _dollars_to_cents(p.get("market_exposure_dollars"))
        else:
            exposure_cents = int(exposure) if isinstance(exposure, int) else _dollars_to_cents(exposure)

        realized = p.get("realized_pnl")
        if realized is None:
            realized_cents = _dollars_to_cents(p.get("realized_pnl_dollars"))
        else:
            realized_cents = int(realized) if isinstance(realized, int) else _dollars_to_cents(realized)
        realized_total += realized_cents

        avg_price = int(round(exposure_cents / qty)) if qty else 0
        out.append(
            {
                "ticker": p.get("ticker") or p.get("market_ticker"),
                "qty": qty,
                "side": side,
                "avg_price_cents": abs(avg_price),
                "exposure_cents": exposure_cents,
                "realized_pnl_cents": realized_cents,
                "source": "live",
                "raw_position_fp": qty_fp,
            }
        )
    return out, realized_total
