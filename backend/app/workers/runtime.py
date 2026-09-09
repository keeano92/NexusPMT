"""Autonomous ingest → wheel → edge → trade loop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.app.kalshi.client import KalshiClient
from backend.app.kalshi.models import OrderIntent
from backend.app.state import AppState
from backend.app.workers.edge_engine import score_edges
from backend.app.workers.futures_wheel import FuturesWheelEngine
from backend.app.worldmap.client import WorldMapClient

logger = logging.getLogger(__name__)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutonomyRuntime:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._tasks: list[asyncio.Task] = []
        self._wm: WorldMapClient | None = None
        self._kalshi: KalshiClient | None = None
        self._wheel: FuturesWheelEngine | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        s = self.state.settings
        self._wm = WorldMapClient(s.worldmap_base_url)
        self._wheel = FuturesWheelEngine(
            api_key=s.xai_api_key,
            base_url=s.xai_base_url,
            model=s.xai_model,
            market_filter=self.state.market_filter,
        )
        if s.kalshi_key_id:
            try:
                pem = s.private_key_bytes()
                self._kalshi = KalshiClient(
                    api_key_id=s.kalshi_key_id,
                    private_key_pem=pem,
                    base_url=s.kalshi_base_url,
                )
            except Exception:
                logger.exception("Kalshi client init failed; paper-only without live API")
                self.state.ledger.append(
                    kind="system",
                    status="warn",
                    message="Kalshi credentials missing or invalid; running paper simulation only",
                )

        # Seed paper equity
        self.state.pnl.record(self.state.paper_cash_cents)
        self._tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._trade_loop(), name="trade"),
            asyncio.create_task(self._pnl_heartbeat(), name="pnl"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._wm:
            await self._wm.aclose()
        if self._kalshi:
            await self._kalshi.aclose()

    async def _ingest_loop(self) -> None:
        assert self._wm and self._wheel
        interval = self.state.settings.worldmap_poll_interval_sec
        while not self._stop.is_set():
            try:
                code, _ = await self._wm.sidecar_health()
                compact = {}
                try:
                    compact = await self._wm.health_compact()
                except Exception:
                    compact = {"status": "UNKNOWN", "sidecar": code}
                self.state.worldmap_health = {"sidecar_status": code, **(compact if isinstance(compact, dict) else {})}

                snippets: list[str] = []
                try:
                    boot = await self._wm.bootstrap()
                    snippets.extend(self._snippets_from_bootstrap(boot))
                except Exception as exc:
                    logger.warning("bootstrap failed: %s", exc)
                    snippets.append(f"WorldMap bootstrap unavailable: {exc}")

                self.state.intel_snippets = snippets[-100:]
                query = self._pick_query(snippets)
                wheel = await self._wheel.build(query, snippets)
                self.state.wheel_nodes = [n.model_dump() for n in wheel.nodes]
                self.state.last_ingest_ts = _iso()
                await self.state.publish({"type": "wheel", "nodes": self.state.wheel_nodes})

                await self._refresh_edges(wheel.nodes)
                self.state.failsafes.record_api_success()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ingest loop error")
                self.state.failsafes.record_api_error()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _snippets_from_bootstrap(self, boot: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(boot, dict):
            return [str(boot)[:200]]
        for key, val in list(boot.items())[:40]:
            if isinstance(val, (str, int, float)):
                out.append(f"{key}: {val}")
            elif isinstance(val, dict):
                out.append(f"{key}: {str(val)[:180]}")
            elif isinstance(val, list) and val:
                out.append(f"{key}: {str(val[0])[:180]}")
        return out

    def _pick_query(self, snippets: list[str]) -> str:
        if not snippets:
            return "Near-term macro and geopolitical risk outlook for prediction markets"
        return f"Futures wheel for: {snippets[0][:200]}"

    async def _refresh_edges(self, nodes: list[Any]) -> None:
        markets: list[dict[str, Any]] = []
        series_by: dict[str, dict[str, Any]] = {}
        if self._kalshi:
            try:
                for cat in list(self.state.settings.allowlist)[:6]:
                    series_resp = await self._kalshi.list_series(category=cat.title() if cat.islower() else cat)
                    for s in series_resp.get("series") or []:
                        if self.state.market_filter.allow_series(s):
                            series_by[str(s.get("ticker"))] = s
                # Pull open markets (public)
                mresp = await self._kalshi.list_markets(status="open", limit=200)
                markets = list(mresp.get("markets") or [])
            except Exception:
                logger.exception("Kalshi market pull failed")
                self.state.failsafes.record_api_error()

        if not markets:
            # Synthetic fixture for UI/dev without Kalshi
            markets = [
                {
                    "ticker": "DEMO-FED-CUT",
                    "event_ticker": "DEMO-FED",
                    "series_ticker": "DEMO-FED",
                    "title": "Fed cuts rates this quarter?",
                    "category": "Economics",
                    "yes_bid_dollars": "0.42",
                    "yes_ask_dollars": "0.46",
                    "volume_fp": "5000",
                }
            ]
            series_by["DEMO-FED"] = {"ticker": "DEMO-FED", "category": "Economics", "title": "Fed", "tags": []}

        edges = score_edges(
            nodes,
            markets,
            series_by,
            self.state.market_filter,
            min_edge=self.state.settings.kalshi_min_edge,
            max_spread=self.state.settings.kalshi_max_spread,
            min_liquidity=self.state.settings.kalshi_min_liquidity,
        )
        self.state.edges = [e.model_dump() for e in edges]
        await self.state.publish({"type": "edges", "edges": self.state.edges})

    async def _trade_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.state.failsafes.can_place_orders() and self.state.edges:
                    await self._maybe_trade(self.state.edges[0])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trade loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _maybe_trade(self, edge: dict[str, Any]) -> None:
        s = self.state.settings
        if edge.get("action") == "skip":
            return
        ticker = edge["ticker"]
        if not self.state.market_filter.allow_market({"ticker": ticker, "title": edge.get("title"), "category": edge.get("category")}):
            self.state.ledger.append(
                kind="filter",
                ticker=ticker,
                status="blocked",
                message="Sports/non-fundamental market blocked",
            )
            return

        notional = min(100, s.kalshi_max_notional_cents)
        intent = OrderIntent(
            ticker=ticker,
            side=edge["side"],
            count=1,
            yes_price=int(round(float(edge.get("market_prob", 0.5)) * 100)),
            client_order_id=f"nx-{uuid.uuid4().hex[:12]}",
            mode=s.kalshi_trading_mode,
            edge=edge.get("edge"),
            reason="autonomous_edge",
        )

        if (
            s.kalshi_trading_mode == "live"
            and notional >= s.kalshi_require_approval_above_cents
        ):
            self.state.failsafes.enqueue_approval(intent.model_dump())
            self.state.ledger.append(
                kind="order",
                ticker=ticker,
                side=intent.side,
                qty=intent.count,
                price_cents=intent.yes_price,
                mode="live",
                status="pending_approval",
                message="Awaiting operator approval",
                meta=intent.model_dump(),
            )
            await self.state.publish({"type": "ledger"})
            return

        if not self.state.failsafes.can_place_orders():
            return

        if s.kalshi_trading_mode == "paper" or not self._kalshi:
            await self._paper_fill(intent, edge)
        else:
            try:
                body = {
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "action": "buy",
                    "count": intent.count,
                    "type": "limit",
                    "yes_price": intent.yes_price,
                    "client_order_id": intent.client_order_id,
                }
                resp = await self._kalshi.create_order(body)
                self.state.ledger.append(
                    kind="order",
                    ticker=ticker,
                    side=intent.side,
                    qty=intent.count,
                    price_cents=intent.yes_price,
                    mode="live",
                    status="submitted",
                    message="Live order submitted",
                    meta={"response": resp},
                )
                self.state.failsafes.record_api_success()
            except Exception as exc:
                self.state.failsafes.record_api_error()
                self.state.ledger.append(
                    kind="order",
                    ticker=ticker,
                    mode="live",
                    status="error",
                    message=str(exc),
                )
        await self.state.publish({"type": "ledger"})

    async def _paper_fill(self, intent: OrderIntent, edge: dict[str, Any]) -> None:
        price = intent.yes_price or 50
        cost = price * intent.count
        self.state.paper_cash_cents -= cost
        pos = self.state.paper_positions.get(intent.ticker) or {
            "ticker": intent.ticker,
            "qty": 0,
            "avg_price_cents": 0,
            "side": intent.side,
        }
        pos["qty"] = int(pos["qty"]) + intent.count
        pos["avg_price_cents"] = price
        pos["side"] = intent.side
        self.state.paper_positions[intent.ticker] = pos
        self.state.positions = list(self.state.paper_positions.values())
        equity = self.state.paper_cash_cents + sum(
            int(p["qty"]) * int(p["avg_price_cents"]) for p in self.state.paper_positions.values()
        )
        self.state.pnl.record(equity, realized_pnl_cents=self.state.pnl.daily_pnl_cents())
        self.state.failsafes.update_equity(equity)
        self.state.ledger.append(
            kind="fill",
            ticker=intent.ticker,
            side=intent.side,
            qty=intent.count,
            price_cents=price,
            mode="paper",
            status="filled",
            message=f"Paper fill edge={edge.get('edge')}",
            meta=intent.model_dump(),
        )

    async def _pnl_heartbeat(self) -> None:
        while not self._stop.is_set():
            equity = self.state.paper_cash_cents + sum(
                int(p.get("qty", 0)) * int(p.get("avg_price_cents", 0))
                for p in self.state.paper_positions.values()
            )
            self.state.pnl.record(equity)
            self.state.failsafes.update_equity(equity)
            await self.state.publish({"type": "pnl", "pnl": self.state.pnl.all_horizons()})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def flatten_all(self, actor: str = "operator") -> None:
        """Manual fail-safe: flatten paper positions (live cancel/offset best-effort)."""
        for ticker, pos in list(self.state.paper_positions.items()):
            qty = int(pos.get("qty") or 0)
            if qty <= 0:
                continue
            proceeds = qty * int(pos.get("avg_price_cents") or 0)
            self.state.paper_cash_cents += proceeds
            self.state.ledger.append(
                kind="control",
                ticker=ticker,
                qty=qty,
                price_cents=int(pos.get("avg_price_cents") or 0),
                mode=self.state.settings.kalshi_trading_mode,
                status="flattened",
                message=f"Flatten by {actor}",
            )
        self.state.paper_positions.clear()
        self.state.positions = []
        equity = self.state.paper_cash_cents
        self.state.pnl.record(equity)
        await self.state.publish({"type": "ledger"})
        await self.state.publish({"type": "pnl", "pnl": self.state.pnl.all_horizons()})

    async def close_position(self, ticker: str, actor: str = "operator") -> bool:
        pos = self.state.paper_positions.pop(ticker, None)
        if not pos:
            return False
        qty = int(pos.get("qty") or 0)
        proceeds = qty * int(pos.get("avg_price_cents") or 0)
        self.state.paper_cash_cents += proceeds
        self.state.positions = list(self.state.paper_positions.values())
        self.state.ledger.append(
            kind="control",
            ticker=ticker,
            qty=qty,
            status="closed",
            message=f"Close by {actor}",
        )
        await self.state.publish({"type": "ledger"})
        return True
