"""Autonomous ingest → wheel → edge → trade loop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.app.kalshi.client import KalshiClient
from backend.app.kalshi.models import OrderIntent
from backend.app.kalshi.portfolio import normalize_balance, normalize_positions
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
        self._recent_trades: dict[str, float] = {}  # ticker -> unix ts

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
        self.state.portfolio_source = "paper"
        self._tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._opportunity_loop(), name="opportunities"),
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
                code = 0
                compact: dict[str, Any] = {}
                try:
                    code, _ = await self._wm.sidecar_health()
                    try:
                        compact = await self._wm.health_compact()
                    except Exception:
                        compact = {"status": "UNKNOWN", "sidecar": code}
                except Exception as exc:
                    logger.warning("WorldMap liveness unreachable: %s", exc)
                    compact = {"status": "UNREACHABLE", "error": str(exc)}
                self.state.worldmap_health = {
                    "sidecar_status": code,
                    **(compact if isinstance(compact, dict) else {}),
                }

                if self.state.settings.worldmap_required and code != 200:
                    was_ready = self.state.worldmap_ready
                    self.state.set_worldmap_ready(
                        False,
                        "SK AI WorldMap is down or unreachable. NexusPMT cannot trade or build "
                        "a Futures Wheel without WorldMap telemetry. Use FORCE START WORLDMAP.",
                    )
                    self.state.wheel_nodes = []
                    self.state.edges = []
                    self.state.intel_snippets = []
                    if was_ready:
                        self.state.ledger.append(
                            kind="system",
                            status="blocked",
                            message="WorldMap required — autonomy blocked until sidecar-health returns 200",
                        )
                    await self.state.publish(
                        {
                            "type": "worldmap",
                            "worldmap_ready": False,
                            "worldmap_block_reason": self.state.worldmap_block_reason,
                            "worldmap_health": self.state.worldmap_health,
                        }
                    )
                    continue

                snippets = await self._wm.collect_intel_snippets()
                if not snippets:
                    if self.state.settings.worldmap_required:
                        self.state.set_worldmap_ready(
                            False,
                            "WorldMap is up but returned no usable intel payloads yet.",
                        )
                        self.state.wheel_nodes = []
                        self.state.edges = []
                        await self.state.publish(
                            {
                                "type": "worldmap",
                                "worldmap_ready": False,
                                "worldmap_block_reason": self.state.worldmap_block_reason,
                            }
                        )
                        continue

                # WorldMap sidecar healthy + intel available
                was_down = not self.state.worldmap_ready
                self.state.set_worldmap_ready(True)
                if was_down and self.state.worldmap_paused_autonomy:
                    snap = self.state.failsafes.snapshot()
                    if snap["state"] == "paused":
                        self.state.failsafes.resume(actor="worldmap_gate")
                        self.state.ledger.append(
                            kind="system",
                            status="resumed",
                            message="WorldMap restored — autonomy resumed",
                        )
                    self.state.worldmap_paused_autonomy = False

                self.state.intel_snippets = snippets[-100:]
                query = self._pick_query(snippets)
                wheel = await self._wheel.build(query, snippets)
                self.state.wheel_nodes = [n.model_dump() for n in wheel.nodes]
                self.state.last_ingest_ts = _iso()
                await self.state.publish({"type": "wheel", "nodes": self.state.wheel_nodes})
                await self.state.publish(
                    {
                        "type": "worldmap",
                        "worldmap_ready": True,
                        "worldmap_health": self.state.worldmap_health,
                    }
                )

                await self._refresh_edges(wheel.nodes)
                self.state.failsafes.record_api_success()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ingest loop error")
                self.state.failsafes.record_api_error()
                if self.state.settings.worldmap_required:
                    self.state.set_worldmap_ready(False, "Ingest loop error while WorldMap required")
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

    async def _opportunity_loop(self) -> None:
        """Continuously re-score Kalshi markets against the latest Futures Wheel."""
        while not self._stop.is_set():
            try:
                if self.state.worldmap_ready and self.state.wheel_nodes:
                    from backend.app.workers.futures_wheel import WheelNode

                    nodes = []
                    for raw in self.state.wheel_nodes:
                        try:
                            nodes.append(WheelNode.model_validate(raw))
                        except Exception:
                            continue
                    if nodes:
                        await self._refresh_edges(nodes)
                        await self.state.publish(
                            {
                                "type": "edges",
                                "edges": self.state.edges,
                                "scanned_at": _iso(),
                            }
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("opportunity scan error")
            interval = max(15.0, float(self.state.settings.kalshi_opportunity_scan_sec))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _trade_loop(self) -> None:
        """Auto-place trades from top Futures-Wheel-ranked Kalshi opportunities."""
        while not self._stop.is_set():
            try:
                if (
                    self.state.worldmap_ready
                    and self.state.failsafes.can_place_orders()
                    and self.state.edges
                ):
                    max_n = max(1, int(self.state.settings.kalshi_max_trades_per_cycle))
                    open_count = len(self.state.positions)
                    room = max(0, int(self.state.settings.kalshi_max_open_positions) - open_count)
                    placed = 0
                    for edge in self.state.edges:
                        if placed >= max_n or placed >= room:
                            break
                        did = await self._maybe_trade(edge)
                        if did:
                            placed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trade loop error")
            interval = max(5.0, float(self.state.settings.kalshi_trade_interval_sec))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _trade_cooldown_ok(self, ticker: str, cooldown_sec: float = 300.0) -> bool:
        import time

        last = self._recent_trades.get(ticker)
        now = time.time()
        if last is not None and (now - last) < cooldown_sec:
            return False
        return True

    async def _maybe_trade(self, edge: dict[str, Any]) -> bool:
        s = self.state.settings
        if s.worldmap_required and not self.state.worldmap_ready:
            return False
        if edge.get("action") == "skip":
            return False
        ticker = str(edge.get("ticker") or "")
        if not ticker:
            return False
        if not self._trade_cooldown_ok(ticker):
            return False
        if any(str(p.get("ticker")) == ticker for p in self.state.positions):
            return False
        if not self.state.market_filter.allow_market(
            {"ticker": ticker, "title": edge.get("title"), "category": edge.get("category")}
        ):
            self.state.ledger.append(
                kind="filter",
                ticker=ticker,
                status="blocked",
                message="Sports/non-fundamental market blocked",
            )
            return False

        count = max(1, int(s.kalshi_contract_count))
        yes_price = int(round(float(edge.get("market_prob", 0.5)) * 100))
        yes_price = min(99, max(1, yes_price))
        notional = yes_price * count
        if notional > s.kalshi_max_notional_cents:
            count = max(1, s.kalshi_max_notional_cents // max(1, yes_price))
            notional = yes_price * count

        intent = OrderIntent(
            ticker=ticker,
            side=edge["side"],
            count=count,
            yes_price=yes_price,
            client_order_id=f"nx-{uuid.uuid4().hex[:12]}",
            mode=s.kalshi_trading_mode,
            edge=edge.get("edge"),
            reason="futures_wheel_edge",
            meta={"wheel_driven": True, "category": edge.get("category")},
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
                message="Awaiting operator approval (above notional gate)",
                meta=intent.model_dump(),
            )
            await self.state.publish({"type": "ledger"})
            return False

        if not self.state.failsafes.can_place_orders():
            return False

        import time

        if s.kalshi_trading_mode == "paper" or not self._kalshi:
            await self._paper_fill(intent, edge)
            self._recent_trades[ticker] = time.time()
            await self.state.publish({"type": "ledger"})
            return True

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
                message="Live order submitted (wheel-driven)",
                meta={"response": resp, "edge": edge.get("edge")},
            )
            self.state.failsafes.record_api_success()
            self._recent_trades[ticker] = time.time()
            await self.sync_live_portfolio()
            await self.state.publish({"type": "ledger"})
            return True
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
            return False

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
        self.state.portfolio_source = "paper"
        self.state.positions = list(self.state.paper_positions.values())
        self.state.portfolio_updated_ts = _iso()
        equity = self._paper_equity_cents()
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
            message=f"Paper fill edge={edge.get('edge')} (wheel-driven)",
            meta=intent.model_dump(),
        )
        await self.state.publish({"type": "portfolio", "portfolio_source": "paper", "positions": self.state.positions})

    async def sync_live_portfolio(self) -> dict[str, Any]:
        """Pull authoritative cash/positions from Kalshi when live (or on demand)."""
        if not self._kalshi:
            return {"ok": False, "error": "Kalshi client not initialized"}
        try:
            bal = await self._kalshi.get_balance()
            pos_payload = await self._kalshi.get_positions()
            norms = normalize_balance(bal if isinstance(bal, dict) else {})
            positions, realized = normalize_positions(pos_payload if isinstance(pos_payload, dict) else {})

            self.state.live_cash_cents = norms["cash_cents"]
            self.state.live_portfolio_value_cents = norms["portfolio_value_cents"]
            self.state.live_equity_cents = norms["equity_cents"]
            self.state.live_realized_pnl_cents = realized
            self.state.positions = positions
            self.state.portfolio_source = "live"
            self.state.portfolio_updated_ts = _iso()

            self.state.pnl.record(
                norms["equity_cents"],
                realized_pnl_cents=realized,
            )
            self.state.failsafes.update_equity(norms["equity_cents"])
            self.state.failsafes.record_api_success()
            await self.state.publish(
                {
                    "type": "portfolio",
                    "portfolio_source": "live",
                    "cash_cents": norms["cash_cents"],
                    "live_equity_cents": norms["equity_cents"],
                    "positions": positions,
                }
            )
            return {"ok": True, **norms, "positions": len(positions), "realized_pnl_cents": realized}
        except Exception as exc:
            logger.exception("live portfolio sync failed")
            self.state.failsafes.record_api_error()
            return {"ok": False, "error": str(exc)}

    def _paper_equity_cents(self) -> int:
        return self.state.paper_cash_cents + sum(
            int(p.get("qty", 0)) * int(p.get("avg_price_cents", 0))
            for p in self.state.paper_positions.values()
        )

    async def _pnl_heartbeat(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            s = self.state.settings
            if s.kalshi_trading_mode == "live" and self._kalshi is not None:
                # Sync from Kalshi every tick while live so UI matches the exchange
                await self.sync_live_portfolio()
            else:
                self.state.portfolio_source = "paper"
                self.state.positions = list(self.state.paper_positions.values())
                equity = self._paper_equity_cents()
                self.state.pnl.record(equity)
                self.state.failsafes.update_equity(equity)
                await self.state.publish({"type": "pnl", "pnl": self.state.pnl.all_horizons()})
            ticks += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def set_trading_config(
        self,
        *,
        trading_mode: str | None = None,
        kalshi_env: str | None = None,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Switch paper/live and demo/production; rebuild Kalshi client when env changes."""
        s = self.state.settings
        prev_mode = s.kalshi_trading_mode
        prev_env = s.kalshi_env

        if trading_mode is not None:
            mode = trading_mode.strip().lower()
            if mode not in {"paper", "live"}:
                return {"ok": False, "error": "trading_mode must be paper|live"}
            s.kalshi_trading_mode = mode  # type: ignore[assignment]

        if kalshi_env is not None:
            env = kalshi_env.strip().lower()
            if env not in {"demo", "production"}:
                return {"ok": False, "error": "kalshi_env must be demo|production"}
            s.kalshi_env = env  # type: ignore[assignment]

        # Rebuild Kalshi client if env changed or client missing
        if s.kalshi_env != prev_env or self._kalshi is None:
            if self._kalshi is not None:
                await self._kalshi.aclose()
                self._kalshi = None
            if s.kalshi_key_id:
                try:
                    pem = s.private_key_bytes()
                    self._kalshi = KalshiClient(
                        api_key_id=s.kalshi_key_id,
                        private_key_pem=pem,
                        base_url=s.kalshi_base_url,
                    )
                except Exception as exc:
                    logger.exception("Kalshi client rebuild failed")
                    self.state.ledger.append(
                        kind="control",
                        status="error",
                        message=f"Kalshi client rebuild failed: {exc}",
                    )
                    return {
                        "ok": False,
                        "error": f"Kalshi client rebuild failed: {exc}",
                        "trading_mode": s.kalshi_trading_mode,
                        "kalshi_env": s.kalshi_env,
                    }

        self.state.ledger.append(
            kind="control",
            status="mode_change",
            message=(
                f"Trading config by {actor}: mode {prev_mode}->{s.kalshi_trading_mode}, "
                f"env {prev_env}->{s.kalshi_env}"
            ),
        )

        portfolio_sync: dict[str, Any] | None = None
        if s.kalshi_trading_mode == "live":
            portfolio_sync = await self.sync_live_portfolio()
        else:
            self.state.portfolio_source = "paper"
            self.state.positions = list(self.state.paper_positions.values())
            equity = self._paper_equity_cents()
            self.state.pnl.record(equity)
            await self.state.publish({"type": "portfolio", "portfolio_source": "paper"})

        await self.state.publish(
            {
                "type": "trading_config",
                "trading_mode": s.kalshi_trading_mode,
                "kalshi_env": s.kalshi_env,
            }
        )
        return {
            "ok": True,
            "trading_mode": s.kalshi_trading_mode,
            "kalshi_env": s.kalshi_env,
            "kalshi_base_url": s.kalshi_base_url,
            "previous": {"trading_mode": prev_mode, "kalshi_env": prev_env},
            "portfolio_sync": portfolio_sync,
        }
    async def force_wheel_refresh(self) -> dict[str, Any]:
        """Operator-triggered Futures Wheel rebuild from latest intel."""
        if not self._wheel:
            return {"ok": False, "error": "wheel engine not started"}
        if self.state.settings.worldmap_required and not self.state.worldmap_ready:
            return {"ok": False, "error": "WorldMap not ready"}
        snippets = list(self.state.intel_snippets)
        if not snippets and self._wm:
            snippets = await self._wm.collect_intel_snippets()
            self.state.intel_snippets = snippets[-100:]
        query = self._pick_query(snippets)
        wheel = await self._wheel.build(query, snippets)
        self.state.wheel_nodes = [n.model_dump() for n in wheel.nodes]
        self.state.last_ingest_ts = _iso()
        await self.state.publish({"type": "wheel", "nodes": self.state.wheel_nodes})
        await self._refresh_edges(wheel.nodes)
        return {
            "ok": True,
            "query": query,
            "nodes": len(self.state.wheel_nodes),
            "edges": len(self.state.edges),
            "model": wheel.raw_model,
        }

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
