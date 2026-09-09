"""Autonomous ingest → wheel → edge → trade loop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from backend.app.kalshi.client import (
    KalshiAPIError,
    KalshiClient,
    classify_kalshi_error,
    is_soft_order_reject,
)
from backend.app.kalshi.models import EdgeCandidate, OrderIntent
from backend.app.kalshi.portfolio import (
    funded_exchange_indexes,
    normalize_balance,
    normalize_positions,
    shard_cash_cents,
)
from backend.app.state import AppState
from backend.app.timeutil import local_iso
from backend.app.workers.edge_engine import score_edges, _mid_prob
from backend.app.workers.futures_wheel import FuturesWheelEngine
from backend.app.workers.opportunity_eval import (
    OpportunityEvaluator,
    collect_candidate_markets,
)
from backend.app.workers.position_manager import (
    build_close_v2_order,
    decide_position_action,
)
from backend.app.workers.ranker import rank_candidates
from backend.app.store.paper_shadow import PaperShadowBook
from backend.app.intel.search_brief import fetch_intel_brief
from backend.app.worldmap.client import WorldMapClient

logger = logging.getLogger(__name__)


def _iso() -> str:
    return local_iso()


def _friendly_kalshi_error(exc: Exception) -> str:
    if isinstance(exc, KalshiAPIError):
        return exc.friendly()
    text = str(exc)
    if "410" in text and "Gone" in text:
        return (
            "Kalshi order API 410 Gone — legacy /portfolio/orders retired; "
            "use Create Order V2 /portfolio/events/orders"
        )
    if "401" in text:
        return "Kalshi auth failed (401) — check key/env match (demo keys ≠ production)"
    if "429" in text:
        return "Kalshi rate limited (429) — backing off"
    if "insufficient" in text.lower() and "balance" in text.lower():
        return "Kalshi insufficient_balance — no cash on that exchange shard"
    if "400" in text:
        return f"Kalshi rejected order (400): {text[:180]}"
    # Keep short for ledger readability
    return text.split("For more information")[0].strip()[:220]


class AutonomyRuntime:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._tasks: list[asyncio.Task] = []
        self._wm: WorldMapClient | None = None
        self._kalshi: KalshiClient | None = None
        self._wheel: FuturesWheelEngine | None = None
        self._evaluator: OpportunityEvaluator | None = None
        self._stop = asyncio.Event()
        self._recent_trades: dict[str, float] = {}  # ticker -> unix ts
        self._recent_evals: dict[str, float] = {}
        self._last_block_log_ts: float = 0.0
        self._soft_reject_cycle: bool = False
        # ticker -> {side, entry_yes_prob, exchange_index, opened_ts}
        self._entry_marks: dict[str, dict[str, Any]] = {}
        self._paper: PaperShadowBook | None = None
        self._last_wheel_ts: float = 0.0
        self._intel_by_series: dict[str, dict[str, Any]] = {}
        self._last_paper_mark_log_ts: float = 0.0

    async def start(self) -> None:
        s = self.state.settings
        self.state.switch_book(s.kalshi_env, s.kalshi_trading_mode)
        self._wm = WorldMapClient(s.worldmap_base_url)
        self._wheel = FuturesWheelEngine(
            api_key=s.xai_api_key,
            base_url=s.xai_base_url,
            model=s.xai_model,
            market_filter=self.state.market_filter,
        )
        self._evaluator = OpportunityEvaluator(
            api_key=s.xai_api_key,
            base_url=s.xai_base_url,
            model=s.xai_model,
            enter_voi_threshold=s.kalshi_enter_voi_threshold,
            require_strong_enter=bool(s.kalshi_require_strong_enter),
            min_edge=float(s.kalshi_min_edge),
            xai_enabled=bool(s.xai_enabled),
            max_calls_per_hour=int(s.xai_max_calls_per_hour),
        )
        self._paper = PaperShadowBook.load(
            s.paper_state_path,
            start_cents=int(s.paper_start_cents),
            target_cents=int(s.paper_target_cents),
        )
        # Paper-first lock: never auto-live without unlock
        if s.kalshi_trading_mode == "live" and not s.live_unlock:
            s.kalshi_trading_mode = "paper"  # type: ignore[assignment]
            self.state.terminal(
                "LIVE LOCKED — paper shadow vs live marks until LIVE_UNLOCK + $100 gate"
            )
            self.state.failsafes.kill(actor="paper_first_lock", reason="live_unlock_required")
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
                self.state.terminal("WARN Kalshi client init failed — paper sim only")

        self.state.portfolio_source = "paper" if s.kalshi_trading_mode == "paper" else "live"
        paper_eq = self._paper.equity_cents() if self._paper else 0
        self.state.terminal(
            f"BOOT book={self.state.active_book_key} mode={s.kalshi_trading_mode} env={s.kalshi_env} "
            f"xai={s.xai_enabled} paper=${paper_eq/100:.2f} unlock={s.live_unlock}"
        )
        self._tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._opportunity_loop(), name="opportunities"),
            asyncio.create_task(self._trade_loop(), name="trade"),
            asyncio.create_task(self._position_manage_loop(), name="position_mgr"),
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
                import time as _t

                now = _t.time()
                wheel_interval = float(self.state.settings.xai_wheel_min_interval_sec)
                if (
                    self.state.settings.xai_enabled
                    and (now - self._last_wheel_ts) >= wheel_interval
                ):
                    wheel = await self._wheel.build(query, snippets)
                    self.state.wheel_nodes = [n.model_dump() for n in wheel.nodes]
                    self._last_wheel_ts = now
                elif not self.state.wheel_nodes:
                    self.state.wheel_nodes = [
                        {
                            "label": "macro",
                            "domain": "economics",
                            "confidence": 0.4,
                            "direction": "flat",
                            "kalshi_keywords": ["fed", "rates", "inflation"],
                        }
                    ]
                self.state.last_ingest_ts = _iso()
                await self.state.publish({"type": "wheel", "nodes": self.state.wheel_nodes})
                await self.state.publish(
                    {
                        "type": "worldmap",
                        "worldmap_ready": True,
                        "worldmap_health": self.state.worldmap_health,
                    }
                )

                await self._refresh_edges(self.state.wheel_nodes)
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
        """Wheel heuristic edges — only feed the trade loop when secondary is enabled.

        Production defaults keep secondary off; opportunity ENTER owns ``state.edges``.
        Writing unscoped score_edges here previously overwrote shard-filtered ENTERs
        and re-armed unfunded shard-0 markets for auto-trade.
        """
        s = self.state.settings
        allow_secondary = bool(s.kalshi_allow_secondary_edges) and s.kalshi_env != "production"
        if not allow_secondary:
            return

        markets: list[dict[str, Any]] = []
        series_by: dict[str, dict[str, Any]] = {}
        if self._kalshi:
            try:
                cats = [
                    c.strip()
                    for c in s.kalshi_category_allowlist.split(",")
                    if c.strip()
                ]
                for cat in cats:
                    series_resp = await self._kalshi.list_series(category=cat)
                    for ser in series_resp.get("series") or []:
                        if self.state.market_filter.allow_series(ser):
                            series_by[str(ser.get("ticker"))] = ser
                for st in list(series_by.keys())[:25]:
                    try:
                        mresp = await self._kalshi.list_markets(
                            status="open", series_ticker=st, limit=20, mve_filter="exclude"
                        )
                    except Exception:
                        mresp = await self._kalshi.list_markets(
                            status="open", series_ticker=st, limit=20
                        )
                    for mkt in mresp.get("markets") or []:
                        if not mkt.get("series_ticker"):
                            mkt = {**mkt, "series_ticker": st}
                        markets.append(mkt)
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
                    "exchange_index": 0,
                }
            ]
            series_by["DEMO-FED"] = {"ticker": "DEMO-FED", "category": "Economics", "title": "Fed", "tags": []}

        edges = score_edges(
            nodes,
            markets,
            series_by,
            self.state.market_filter,
            min_edge=s.kalshi_min_edge,
            max_spread=s.kalshi_max_spread,
            min_liquidity=s.kalshi_min_liquidity,
        )
        rows: list[dict[str, Any]] = []
        for e in edges:
            row = e.model_dump()
            # Attach exchange_index from the matched market when present
            for m in markets:
                if str(m.get("ticker")) == row.get("ticker") and m.get("exchange_index") is not None:
                    try:
                        row["exchange_index"] = int(m.get("exchange_index"))
                    except (TypeError, ValueError):
                        pass
                    break
            rows.append(row)
        self.state.edges = rows
        await self.state.publish({"type": "edges", "edges": self.state.edges})

    async def _opportunity_loop(self) -> None:
        """Kalshi-first: pull markets → xAI+wheel eval → terminal feed → ENTER edges."""
        while not self._stop.is_set():
            try:
                if not self.state.worldmap_ready:
                    self.state.terminal("WAIT WorldMap not ready — opportunity scan paused")
                elif not self._evaluator or not self._kalshi:
                    self.state.terminal("WAIT Kalshi/xAI evaluator unavailable")
                else:
                    await self._scan_and_evaluate_opportunities()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("opportunity scan error")
                self.state.terminal("ERROR opportunity scan crashed — retrying")
            interval = max(15.0, float(self.state.settings.kalshi_opportunity_scan_sec))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _scan_and_evaluate_opportunities(self) -> None:
        assert self._kalshi and self._evaluator
        import time

        s = self.state.settings
        self.state.terminal(f"SCAN begin env={s.kalshi_env} book={self.state.active_book_key}")
        series_by: dict[str, dict[str, Any]] = {}
        markets: list[dict[str, Any]] = []
        try:
            cats = [c.strip() for c in s.kalshi_category_allowlist.split(",") if c.strip()]
            # Do NOT truncate cats[:6] — that dropped Crypto (exchange shard 2) and
            # left the desk with only unfunded shard-0 markets.
            ranked_series: list[dict[str, Any]] = []

            def _vol(ser: dict[str, Any]) -> float:
                for k in ("volume_fp", "volume", "open_interest"):
                    try:
                        return float(ser.get(k) or 0)
                    except (TypeError, ValueError):
                        continue
                return 0.0

            def _is_micro_horizon(ser: dict[str, Any]) -> bool:
                t = str(ser.get("ticker") or "").upper()
                return any(tag in t for tag in ("15M", "1H", "5M", "10M", "30M"))

            prefer_15m = bool(s.kalshi_prefer_15m)

            def _series_rank_key(ser: dict[str, Any]) -> tuple[int, float]:
                # Live desk: prefer 15m first on funded shards; else demote micro.
                micro = _is_micro_horizon(ser)
                if prefer_15m:
                    return (0 if micro else 1, -_vol(ser))
                return (1 if micro else 0, -_vol(ser))

            for cat in cats:
                try:
                    series_resp = await self._kalshi.list_series(
                        category=cat, include_volume="true"
                    )
                except Exception:
                    series_resp = await self._kalshi.list_series(category=cat)
                cat_series = [
                    ser
                    for ser in (series_resp.get("series") or [])
                    if self.state.market_filter.allow_series(ser)
                ]
                cat_series.sort(key=_series_rank_key)
                # Crypto / Financials often live on non-zero shards — take more series.
                # Daily KXBTCD/KXBTC dominate volume but sit at extreme mids; force-include
                # monthly MAXMON/MINMON so funded-shard scans have tradeable candidates.
                if cat.lower() == "crypto":
                    non_micro = [ser for ser in cat_series if not _is_micro_horizon(ser)]
                    monthly = [
                        ser
                        for ser in non_micro
                        if any(
                            tag in str(ser.get("ticker") or "").upper()
                            for tag in ("MAXMON", "MINMON", "MAXY", "MINY", "MAX150")
                        )
                    ]
                    rest = [ser for ser in non_micro if ser not in monthly]
                    micro = [ser for ser in cat_series if _is_micro_horizon(ser)]
                    if prefer_15m:
                        # Active desk: 15m first (SOL/BTC/ETH…), monthly as backup
                        picked = micro[:10] + monthly[:6] + rest[:4]
                    else:
                        picked = monthly[:10] + rest[:6] + micro[:2]
                elif cat.lower() == "financials":
                    picked = cat_series[:10]
                else:
                    picked = cat_series[:4]
                for ser in picked:
                    series_by[str(ser.get("ticker"))] = ser
                    ranked_series.append(ser)
                await asyncio.sleep(0.12)

            # Prefer non-micro then higher volume; keep category diversity
            ranked_series.sort(key=_series_rank_key)
            # Pin the active lane so PRES/FED volume giants don't crowd it out.
            if prefer_15m:
                pinned = [ser for ser in ranked_series if _is_micro_horizon(ser)]
            else:
                pinned = [
                    ser
                    for ser in ranked_series
                    if any(
                        tag in str(ser.get("ticker") or "").upper()
                        for tag in ("MAXMON", "MINMON", "MAXY", "MINY", "MAX150")
                    )
                ]
            pull_series: list[dict[str, Any]] = []
            seen_series: set[str] = set()
            for ser in pinned + ranked_series:
                st = str(ser.get("ticker") or "")
                if not st or st in seen_series:
                    continue
                seen_series.add(st)
                pull_series.append(ser)
            # Pull markets for top series (avoid 429 + empty MVE dump)
            seen: set[str] = set()
            for ser in pull_series[:36]:
                st = str(ser.get("ticker") or "")
                if not st:
                    continue
                try:
                    mresp = await self._kalshi.list_markets(
                        status="open",
                        series_ticker=st,
                        limit=20,
                        mve_filter="exclude",
                    )
                except Exception:
                    try:
                        mresp = await self._kalshi.list_markets(
                            status="open", series_ticker=st, limit=20
                        )
                    except Exception as exc:
                        self.state.terminal(f"WARN markets {st}: {exc}")
                        await asyncio.sleep(0.5)
                        continue
                for mkt in mresp.get("markets") or []:
                    t = str(mkt.get("ticker") or "")
                    if not t or t in seen or "MVE" in t.upper():
                        continue
                    if not mkt.get("series_ticker"):
                        mkt = {**mkt, "series_ticker": st}
                    seen.add(t)
                    markets.append(mkt)
                await asyncio.sleep(0.15)

            # If live cash is on shard N but we pulled zero markets there, force Crypto tops.
            shards_now = dict(self.state.book().live_shard_balances_cents or {})
            funded_now = funded_exchange_indexes(shards_now, min_cents=1)
            if s.kalshi_trading_mode == "live" and funded_now:
                on_funded = sum(
                    1
                    for m in markets
                    if m.get("exchange_index") is not None
                    and int(m.get("exchange_index")) in funded_now
                )
                if on_funded == 0:
                    self.state.terminal(
                        f"PULL supplement — 0 markets on funded shards {sorted(funded_now)}; "
                        "fetching Crypto tops"
                    )
                    try:
                        crypto = await self._kalshi.list_series(
                            category="Crypto", include_volume="true"
                        )
                    except Exception:
                        crypto = {"series": []}
                    crypto_series = [
                        ser
                        for ser in (crypto.get("series") or [])
                        if self.state.market_filter.allow_series(ser)
                    ]
                    crypto_series.sort(key=_vol, reverse=True)
                    for ser in crypto_series[:10]:
                        st = str(ser.get("ticker") or "")
                        if not st:
                            continue
                        series_by[st] = ser
                        try:
                            mresp = await self._kalshi.list_markets(
                                status="open",
                                series_ticker=st,
                                limit=20,
                                mve_filter="exclude",
                            )
                        except Exception:
                            continue
                        for mkt in mresp.get("markets") or []:
                            t = str(mkt.get("ticker") or "")
                            if not t or t in seen or "MVE" in t.upper():
                                continue
                            if not mkt.get("series_ticker"):
                                mkt = {**mkt, "series_ticker": st}
                            seen.add(t)
                            markets.append(mkt)
                        await asyncio.sleep(0.12)

            by_ex: dict[int, int] = {}
            for m in markets:
                try:
                    ex = int(m.get("exchange_index")) if m.get("exchange_index") is not None else -1
                except (TypeError, ValueError):
                    ex = -1
                by_ex[ex] = by_ex.get(ex, 0) + 1
            self.state.terminal(
                f"PULL series={len(series_by)} markets={len(markets)} "
                f"by_shard={dict(sorted(by_ex.items()))} (fundamentals only)"
            )
        except Exception as exc:
            self.state.terminal(f"ERROR Kalshi market pull failed: {exc}")
            self.state.failsafes.record_api_error()
            return

        shards = dict(self.state.book().live_shard_balances_cents or {})
        funded = funded_exchange_indexes(shards, min_cents=1)
        # Live only: filter to funded shards. Paper shadow can trade any open market.
        funded_filter: set[int] | None = funded if (
            s.kalshi_trading_mode == "live" and s.live_unlock and shards
        ) else None
        if funded_filter is not None:
            self.state.terminal(
                f"SHARDS funded={sorted(funded_filter)} "
                f"cash={{{', '.join(f'{i}:{shards.get(i, 0)}¢' for i in sorted(shards))}}}"
            )
            if not funded_filter:
                self.state.terminal("SCAN skip — no funded exchange shards (all cash=0)")
                self.state.edges = []
                await self.state.publish({"type": "edges", "edges": [], "scanned_at": _iso()})
                return

        scan_spread = max(float(s.kalshi_max_spread), 0.10)
        candidates = collect_candidate_markets(
            markets,
            series_by,
            self.state.market_filter,
            max_spread=scan_spread,
            min_liquidity=s.kalshi_min_liquidity,
            limit=max(12, int(s.kalshi_eval_batch_size) * 4),
            funded_shards=funded_filter,
            prefer_micro=bool(s.kalshi_prefer_15m),
        )
        # Optional cheap intel for top series (cached; provider=none → skip)
        if s.intel_provider and s.intel_provider != "none":
            for cand in candidates[:6]:
                series = str(cand.get("series_ticker") or "")
                if not series or series in self._intel_by_series:
                    continue
                brief = fetch_intel_brief(
                    title=str(cand.get("title") or series),
                    series=series,
                    provider=s.intel_provider,
                    api_key=s.intel_api_key,
                    gemini_api_key=s.gemini_api_key,
                    gemini_model=s.gemini_model,
                )
                if brief:
                    self._intel_by_series[series] = {
                        "bias": brief.bias,
                        "confidence": brief.confidence,
                        "bullets": brief.bullets,
                        "provider": brief.provider,
                    }

        ranked = rank_candidates(
            candidates,
            min_net_edge=float(s.kalshi_min_net_edge),
            intel_by_series=self._intel_by_series,
            limit=max(1, int(s.kalshi_max_trades_per_cycle)),
        )
        self.state.terminal(
            f"SCAN {len(markets)} open → {len(candidates)} cands → {len(ranked)} ranked "
            f"(min_net_edge={s.kalshi_min_net_edge})"
        )
        if not candidates:
            self.state.edges = []
            self.state.evaluations = []
            await self.state.publish({"type": "edges", "edges": [], "scanned_at": _iso()})
            await self.state.publish({"type": "terminal", "terminal": list(self.state.terminal_lines)[-80:]})
            return

        evaluations: list[dict[str, Any]] = []
        enter_edges: list[dict[str, Any]] = []
        # Paper: one position max — skip new ENTERs if already open
        if self._paper and self._paper.positions and s.kalshi_trading_mode == "paper":
            self.state.terminal(
                f"PAPER hold — {len(self._paper.positions)} open; manage only "
                f"eq=${self._paper.equity_cents()/100:.2f}"
            )
            ranked = []

        for row in ranked:
            ticker = str(row.get("ticker") or "")
            series = str(row.get("series_ticker") or "")
            if self._paper and not self._paper.can_enter_series(series):
                self.state.terminal(f"SKIP {ticker} — series cooldown after stop")
                continue
            self.state.terminal(
                f"RANK {ticker} {str(row.get('side')).upper()} net={float(row['net_edge']):+.2%} "
                f"score={row['score']} · {row.get('title') or ''}"[:160]
            )
            evaluations.append({**row, "verdict": "ENTER"})
            edge_row = EdgeCandidate(
                ticker=ticker,
                event_ticker=row.get("event_ticker"),
                category=row.get("category"),
                title=row.get("title"),
                model_prob=float(row.get("fair") or 0.5),
                market_prob=float(row.get("market_prob") or 0.5),
                edge=abs(float(row.get("net_edge") or 0)),
                side=row.get("side") or "yes",  # type: ignore[arg-type]
                liquidity=row.get("liquidity"),
                action="buy_yes" if row.get("side") == "yes" else "buy_no",
            ).model_dump()
            if row.get("exchange_index") is not None:
                edge_row["exchange_index"] = row.get("exchange_index")
            edge_row["micro_horizon"] = bool(row.get("micro_horizon"))
            edge_row["close_time"] = row.get("close_time")
            # Attach live book for paper fills
            raw = next((c.get("raw") for c in candidates if c.get("ticker") == ticker), {}) or {}
            edge_row["yes_bid"] = raw.get("yes_bid_dollars")
            edge_row["yes_ask"] = raw.get("yes_ask_dollars")
            enter_edges.append(edge_row)
            self.state.terminal(
                f"ENTER {ticker} {edge_row['side'].upper()} net={float(row['net_edge']):+.2%} "
                f":: ranker score={row['score']}"[:180]
            )

        if not enter_edges:
            self.state.terminal("NO ENTER this scan — waiting for next ranker signal")

        self.state.evaluations = (evaluations + self.state.evaluations)[:80]
        self.state.edges = enter_edges
        paper_note = ""
        if self._paper:
            paper_note = f" paper=${self._paper.equity_cents()/100:.2f}"
        self.state.terminal(
            f"SCAN done ranked={len(evaluations)} enter={len(enter_edges)} "
            f"book={self.state.active_book_key}{paper_note}"
        )
        await self.state.publish(
            {
                "type": "edges",
                "edges": self.state.edges,
                "evaluations": evaluations,
                "paper": self._paper.snapshot() if self._paper else None,
                "scanned_at": _iso(),
            }
        )
        await self.state.publish({"type": "terminal", "terminal": list(self.state.terminal_lines)[-120:]})
        if self._paper:
            self._paper.save(self.state.settings.paper_state_path)

    async def _trade_loop(self) -> None:
        """Auto-place trades from top Futures-Wheel-ranked Kalshi opportunities."""
        while not self._stop.is_set():
            try:
                import time as _time

                if not self.state.failsafes.can_place_orders():
                    if self.state.failsafes.snapshot()["state"] == "killed":
                        now = _time.time()
                        if now - self._last_block_log_ts > 60:
                            self.state.terminal(
                                "BLOCKED kill_switch — no auto trades until RESET LIVE PnL & RESUME"
                            )
                            self._last_block_log_ts = now
                elif not self.state.worldmap_ready:
                    now = _time.time()
                    if now - self._last_block_log_ts > 60:
                        self.state.terminal("BLOCKED WorldMap not ready")
                        self._last_block_log_ts = now
                elif self.state.edges:
                    max_n = max(1, int(self.state.settings.kalshi_max_trades_per_cycle))
                    open_count = len(self.state.positions)
                    room = max(0, int(self.state.settings.kalshi_max_open_positions) - open_count)
                    placed = 0
                    self._soft_reject_cycle = False
                    for edge in self.state.edges:
                        if placed >= max_n or placed >= room or self._soft_reject_cycle:
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

        # Strong wheel/xAI entries: allocate ~90% of cash on the market's exchange shard.
        # yes_price is always the YES-book limit (cents). Buying NO = ask YES at that price.
        yes_price = int(round(float(edge.get("market_prob", 0.5)) * 100))
        yes_price = min(99, max(1, yes_price))
        pay_cents = yes_price if edge.get("side") != "no" else max(1, 100 - yes_price)

        ex_raw = edge.get("exchange_index")
        try:
            exchange_index = int(ex_raw) if ex_raw is not None else None
        except (TypeError, ValueError):
            exchange_index = None

        shards = dict(self.state.book().live_shard_balances_cents or {})
        if s.kalshi_trading_mode == "paper" and self._paper is not None:
            cash = max(0, self._paper.cash_cents)
        elif s.kalshi_trading_mode == "live" and shards:
            if exchange_index is None:
                self.state.terminal(f"SKIP {ticker} — missing exchange_index (cannot route shard)")
                return False
            cash = max(0, shard_cash_cents(shards, exchange_index))
            if cash <= 0:
                self.state.terminal(
                    f"SKIP {ticker} — shard {exchange_index} unfunded "
                    f"(funded={sorted(funded_exchange_indexes(shards))})"
                )
                return False
        else:
            cash = max(0, self.state.book().cash_cents())

        # Cap entry so one stop-loss cannot wipe the shard (live-test default 50%).
        entry_pct = min(
            float(s.kalshi_strong_allocation_pct),
            float(getattr(s, "kalshi_max_entry_pct", 0.5) or 0.5),
        )
        alloc = int(cash * entry_pct)
        if alloc <= 0:
            self.state.terminal(f"SKIP {ticker} — no cash in book {self.state.active_book_key}")
            return False
        count = max(1, alloc // pay_cents)
        count = min(count, max(1, cash // pay_cents))
        notional = pay_cents * count
        shard_tag = f" shard={exchange_index}" if exchange_index is not None else ""
        self.state.terminal(
            f"SIZE {ticker} {edge.get('side')}{shard_tag} cash={cash}¢ alloc={alloc}¢ "
            f"yes_px={yes_price}¢ pay={pay_cents}¢ qty={count} notional={notional}¢"
        )

        intent = OrderIntent(
            ticker=ticker,
            side=edge["side"],
            count=count,
            yes_price=yes_price,
            client_order_id=f"nx-{uuid.uuid4().hex[:12]}",
            mode=s.kalshi_trading_mode,
            edge=edge.get("edge"),
            reason="futures_wheel_edge",
            meta={
                "wheel_driven": True,
                "category": edge.get("category"),
                "exchange_index": exchange_index,
            },
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

        # Live auto-orders require explicit unlock after paper $10→$100 gate
        if s.kalshi_trading_mode == "live" and not s.live_unlock:
            self.state.terminal(f"BLOCKED live order {ticker} — LIVE_UNLOCK=false (paper gate)")
            return False

        if s.kalshi_trading_mode == "paper" or not self._kalshi:
            ok = await self._paper_shadow_fill(intent, edge)
            self._recent_trades[ticker] = time.time()
            await self.state.publish({"type": "ledger"})
            return ok

        try:
            # Ensure UUID-shaped client_order_id for Kalshi V2
            coid = intent.client_order_id
            if len(coid) < 32 or coid.count("-") < 4:
                coid = str(uuid.uuid4())
            body = KalshiClient.build_v2_order(
                ticker=intent.ticker,
                side=intent.side,
                count=intent.count,
                yes_price_cents=int(intent.yes_price or 50),
                client_order_id=coid,
                exchange_index=exchange_index,
            )
            resp = await self._kalshi.create_order(body)
            oid = (resp or {}).get("order_id") if isinstance(resp, dict) else None
            self.state.ledger.append(
                kind="order",
                ticker=ticker,
                side=intent.side,
                qty=intent.count,
                price_cents=intent.yes_price,
                mode="live",
                status="submitted",
                message=(
                    f"Live order submitted (V2)"
                    f"{f' id={oid}' if oid else ''}"
                    f"{f' shard={exchange_index}' if exchange_index is not None else ''}"
                ),
                meta={"response": resp, "edge": edge.get("edge"), "request": body},
            )
            self.state.failsafes.record_api_success()
            self._recent_trades[ticker] = time.time()
            self._entry_marks[ticker] = {
                "side": intent.side,
                "entry_yes_prob": float(intent.yes_price or 50) / 100.0,
                "exchange_index": exchange_index,
                "opened_ts": time.time(),
                "count": intent.count,
            }
            await self.sync_live_portfolio()
            await self.state.publish({"type": "ledger"})
            return True
        except Exception as exc:
            friendly = _friendly_kalshi_error(exc)
            soft = is_soft_order_reject(exc)
            kind = classify_kalshi_error(exc)
            if soft:
                # Business rejects (wrong shard / no cash / closed market / 429)
                # must NOT trip error_streak auto-kill.
                self._soft_reject_cycle = True
                self._recent_trades[ticker] = time.time()
                status = "soft_reject"
                self.state.terminal(f"SOFT REJECT {ticker}: {friendly}")
            else:
                self.state.failsafes.record_api_error()
                status = "error"
                self.state.terminal(f"ORDER ERROR {ticker}: {friendly}")
            meta: dict[str, Any] = {
                "error": str(exc),
                "classify": kind,
                "exchange_index": exchange_index,
            }
            if isinstance(exc, KalshiAPIError):
                meta["kalshi_code"] = exc.code
                meta["kalshi_status"] = exc.status_code
                meta["kalshi_body"] = exc.body
            self.state.ledger.append(
                kind="order",
                ticker=ticker,
                side=intent.side,
                qty=intent.count,
                price_cents=intent.yes_price,
                mode="live",
                status=status,
                message=friendly,
                meta=meta,
            )
            await self.state.publish({"type": "ledger"})
            return False

    async def _paper_shadow_fill(self, intent: OrderIntent, edge: dict[str, Any]) -> bool:
        """Fill virtual book at live bid/ask (conservative)."""
        assert self._paper is not None
        yes_bid = edge.get("yes_bid")
        yes_ask = edge.get("yes_ask")
        try:
            yb = float(yes_bid) if yes_bid is not None else None
            ya = float(yes_ask) if yes_ask is not None else None
        except (TypeError, ValueError):
            yb, ya = None, None
        if yb is None or ya is None:
            # Fetch live book
            mark = await self._fetch_mark_yes(intent.ticker)
            if mark is None:
                self.state.terminal(f"PAPER SKIP {intent.ticker} — no live mark")
                return False
            yb = max(0.01, mark - 0.02)
            ya = min(0.99, mark + 0.02)
        # Size from paper cash
        pay = int(intent.yes_price or 50) if intent.side == "yes" else max(1, 100 - int(intent.yes_price or 50))
        qty = max(1, min(intent.count, self._paper.cash_cents // max(1, pay)))
        fill = self._paper.open_position(
            ticker=intent.ticker,
            side=intent.side,
            qty=qty,
            yes_bid=yb,
            yes_ask=ya,
            exchange_index=edge.get("exchange_index"),
        )
        if not fill:
            self.state.terminal(f"PAPER SKIP {intent.ticker} — insufficient virtual cash")
            return False
        self._entry_marks[intent.ticker] = {
            "side": intent.side,
            "entry_yes_prob": float(ya if intent.side == "yes" else yb),
            "exchange_index": edge.get("exchange_index"),
            "opened_ts": fill.ts,
            "count": fill.qty,
        }
        self.state.ledger.append(
            kind="fill",
            ticker=intent.ticker,
            side=intent.side,
            qty=fill.qty,
            price_cents=fill.price_cents,
            mode="paper",
            status="filled",
            message=(
                f"PAPER open vs live book eq=${self._paper.equity_cents()/100:.2f} "
                f"target=${self._paper.target_cents/100:.2f}"
            ),
            meta={"paper": self._paper.snapshot(), "edge": edge.get("edge")},
        )
        self.state.terminal(
            f"PAPER OPEN {intent.ticker} {intent.side.upper()} x{fill.qty} @ {fill.price_cents}¢ "
            f"eq=${self._paper.equity_cents()/100:.2f}"
        )
        self._paper.save(self.state.settings.paper_state_path)
        await self.state.publish({"type": "paper", "paper": self._paper.snapshot()})
        return True

    async def _paper_fill(self, intent: OrderIntent, edge: dict[str, Any]) -> None:
        # Legacy compat — route to shadow book
        await self._paper_shadow_fill(intent, edge)

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
            self.state.book().live_shard_balances_cents = dict(
                norms.get("shard_balances_cents") or {}
            )
            self.state.positions = positions
            self.state.portfolio_source = "live"
            self.state.portfolio_updated_ts = _iso()

            equity = norms["equity_cents"]
            # First live mark becomes session baseline (PnL starts at $0, not −$989)
            if not self.state.pnl.has_anchor() or not getattr(self.state.pnl, "_live_baseline_set", False):
                self.state.pnl.reset_anchor(equity, clear_history=True)
                self.state.failsafes.reset_equity_anchors(equity)
                # Recover from phantom book-switch kill
                snap = self.state.failsafes.snapshot()
                if snap["state"] == "killed":
                    self.state.failsafes.clear_kill(actor="pnl_reanchor")
                    self.state.failsafes.resume(actor="pnl_reanchor")
                    self.state.terminal(
                        "RECOVERED phantom PnL kill — live baseline set; trading resumed"
                    )
            else:
                self.state.pnl.record(equity, realized_pnl_cents=realized)
                self.state.failsafes.update_equity(equity)

            self.state.failsafes.record_api_success()
            await self.state.publish(
                {
                    "type": "portfolio",
                    "portfolio_source": "live",
                    "cash_cents": norms["cash_cents"],
                    "live_equity_cents": norms["equity_cents"],
                    "session_pnl_cents": self.state.pnl.daily_pnl_cents(),
                    "positions": positions,
                }
            )
            await self.state.publish({"type": "pnl", "pnl": self.state.pnl.all_horizons()})
            return {
                "ok": True,
                **norms,
                "positions": len(positions),
                "realized_pnl_cents": realized,
                "session_pnl_cents": self.state.pnl.daily_pnl_cents(),
            }
        except Exception as exc:
            logger.exception("live portfolio sync failed")
            self.state.failsafes.record_api_error()
            return {"ok": False, "error": str(exc)}

    def _paper_equity_cents(self) -> int:
        return self.state.book().equity_cents()

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
            if mode == "live" and not s.live_unlock:
                return {
                    "ok": False,
                    "error": (
                        "LIVE locked until paper shadow reaches $100 gate "
                        "and LIVE_UNLOCK=true is set."
                    ),
                    "paper": self._paper.snapshot() if self._paper else None,
                }
            s.kalshi_trading_mode = mode  # type: ignore[assignment]

        if kalshi_env is not None:
            env = kalshi_env.strip().lower()
            if env not in {"demo", "production"}:
                return {"ok": False, "error": "kalshi_env must be demo|production"}
            s.kalshi_env = env  # type: ignore[assignment]

        # Isolate PnL/ledger/positions — demo never bleeds into production
        book = self.state.switch_book(s.kalshi_env, s.kalshi_trading_mode)
        # Live books: do not arm risk on fake paper equity; wait for Kalshi sync
        if s.kalshi_trading_mode == "live":
            self.state.failsafes.reset_equity_anchors(None)

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
            equity = book.equity_cents()
            self.state.pnl.record(equity)
            await self.state.publish({"type": "portfolio", "portfolio_source": "paper", "book_key": book.key})

        await self.state.publish(
            {
                "type": "trading_config",
                "trading_mode": s.kalshi_trading_mode,
                "kalshi_env": s.kalshi_env,
                "book_key": book.key,
            }
        )
        return {
            "ok": True,
            "trading_mode": s.kalshi_trading_mode,
            "kalshi_env": s.kalshi_env,
            "book_key": book.key,
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

    async def _position_manage_loop(self) -> None:
        """Watch open positions; stop-loss / take-profit / time-stop (no LLM)."""
        while not self._stop.is_set():
            try:
                if not self.state.failsafes.can_place_orders() and self.state.settings.kalshi_trading_mode == "live":
                    pass
                elif self.state.settings.kalshi_trading_mode == "paper" and self._paper and self._paper.positions:
                    await self._manage_paper_positions()
                elif (
                    self.state.settings.kalshi_trading_mode == "live"
                    and self.state.settings.live_unlock
                    and self.state.positions
                ):
                    await self._manage_open_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("position manage loop error")
            interval = max(2.0, float(self.state.settings.kalshi_position_poll_sec))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _manage_paper_positions(self) -> None:
        assert self._paper is not None
        s = self.state.settings
        import time as _time

        for ticker, pos in list(self._paper.positions.items()):
            mark_yes = await self._fetch_mark_yes(ticker)
            if mark_yes is None:
                continue
            self._paper.update_mark(ticker, mark_yes)
            # Need bid/ask for conservative exit
            yes_bid, yes_ask = await self._fetch_bid_ask(ticker)
            if yes_bid is None or yes_ask is None:
                yes_bid = max(0.01, mark_yes - 0.02)
                yes_ask = min(0.99, mark_yes + 0.02)
            flip_side = None  # anti-churn: flips off in paper phase unless enabled
            if s.kalshi_allow_flip:
                flip_side = "no" if pos.side == "yes" else "yes"
            sec_left = await self._seconds_to_close(ticker)
            held = _time.time() - float(pos.opened_ts or _time.time())
            decision = decide_position_action(
                side=pos.side,
                entry_yes_prob=pos.entry_yes_prob,
                mark_yes_prob=mark_yes,
                stop_loss_prob=float(s.kalshi_stop_loss_prob),
                take_profit_prob=float(s.kalshi_take_profit_prob),
                flip_side=flip_side if s.kalshi_allow_flip else None,
                flip_min_edge=float(s.kalshi_flip_min_edge),
                seconds_to_close=sec_left,
                time_stop_sec=float(s.kalshi_time_stop_sec),
                held_sec=held,
                max_hold_sec=float(s.paper_max_hold_sec),
            )
            # Heartbeat so the UI/terminal show paper is alive while holding
            if _time.time() - self._last_paper_mark_log_ts >= 30:
                self.state.terminal(
                    (
                        f"PAPER MARK {ticker} {pos.side.upper()} x{pos.qty} "
                        f"entry={pos.entry_yes_prob:.2f} mark={mark_yes:.2f} "
                        f"pnl={decision.pnl_prob:+.3f} held={held:.0f}s "
                        f"eq=${self._paper.equity_cents()/100:.2f}"
                    )[:180]
                )
                self._last_paper_mark_log_ts = _time.time()
            if decision.action == "hold":
                await self.state.publish({"type": "paper", "paper": self._paper.snapshot()})
                continue
            # Map flip → stop_loss for paper (no churn re-entry)
            status = decision.action if decision.action != "flip" else "stop_loss"
            fill = self._paper.close_position(
                ticker=ticker,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                status=status,
                reason=decision.reason,
            )
            if not fill:
                continue
            self._entry_marks.pop(ticker, None)
            self.state.ledger.append(
                kind="fill",
                ticker=ticker,
                side=pos.side,
                qty=pos.qty,
                price_cents=fill.price_cents,
                mode="paper",
                status=status,
                message=(
                    f"PAPER {status} pnl={fill.pnl_cents}¢ eq=${self._paper.equity_cents()/100:.2f} "
                    f"::{decision.reason}"
                )[:220],
                meta={"paper": self._paper.snapshot()},
            )
            self.state.terminal(
                f"PAPER {status.upper()} {ticker} pnl={fill.pnl_cents}¢ "
                f"eq=${self._paper.equity_cents()/100:.2f} W/L={self._paper.wins}/{self._paper.losses}"
            )
            self._paper.save(s.paper_state_path)
            await self.state.publish({"type": "paper", "paper": self._paper.snapshot()})
            await self.state.publish({"type": "ledger"})

    async def _fetch_bid_ask(self, ticker: str) -> tuple[float | None, float | None]:
        if not self._kalshi:
            return None, None
        try:
            resp = await self._kalshi.list_markets(tickers=ticker, limit=1)
            markets = resp.get("markets") or []
            if not markets:
                return None, None
            m = markets[0]
            return float(m.get("yes_bid_dollars") or 0) or None, float(m.get("yes_ask_dollars") or 0) or None
        except Exception:
            return None, None

    async def _manage_open_positions(self) -> None:
        """Live position manager — only when LIVE_UNLOCK is on. No LLM re-eval."""
        assert self._kalshi
        s = self.state.settings
        if not s.live_unlock:
            return
        await self.sync_live_portfolio()
        for pos in list(self.state.positions):
            ticker = str(pos.get("ticker") or "")
            if not ticker:
                continue
            side = str(pos.get("side") or "yes").lower()
            qty = int(pos.get("qty") or 0)
            if qty <= 0:
                continue
            entry = self._entry_marks.get(ticker) or {}
            entry_yes = float(entry.get("entry_yes_prob") or 0)
            if entry_yes <= 0:
                avg = int(pos.get("avg_price_cents") or 0)
                entry_yes = (avg / 100.0) if side == "yes" else max(0.01, 1.0 - avg / 100.0)

            mark_yes = await self._fetch_mark_yes(ticker)
            if mark_yes is None:
                continue

            flip_side = None
            sec_left = await self._seconds_to_close(ticker)
            decision = decide_position_action(
                side=side,
                entry_yes_prob=entry_yes,
                mark_yes_prob=mark_yes,
                stop_loss_prob=float(s.kalshi_stop_loss_prob),
                take_profit_prob=float(s.kalshi_take_profit_prob),
                flip_side=flip_side,
                flip_min_edge=float(s.kalshi_flip_min_edge),
                seconds_to_close=sec_left,
                time_stop_sec=float(s.kalshi_time_stop_sec),
            )
            if decision.action == "hold":
                continue

            self.state.terminal(
                f"{decision.action.upper()} {ticker} {side} mark={mark_yes:.2f} "
                f"entry={entry_yes:.2f} pnl={decision.pnl_prob:+.3f} :: {decision.reason}"[:180]
            )
            await self._live_close_position(
                ticker=ticker,
                side=side,
                qty=qty,
                mark_yes=mark_yes,
                exchange_index=entry.get("exchange_index") or pos.get("exchange_index"),
                status=decision.action if decision.action != "flip" else "stop_loss",
                reason=decision.reason,
            )

    async def _fetch_mark_yes(self, ticker: str) -> float | None:
        if not self._kalshi:
            return None
        try:
            resp = await self._kalshi.list_markets(tickers=ticker, limit=1)
            markets = resp.get("markets") or []
            if not markets:
                return None
            mid = _mid_prob(markets[0])
            return float(mid) if mid is not None else None
        except Exception:
            logger.exception("mark fetch failed for %s", ticker)
            return None

    async def _seconds_to_close(self, ticker: str) -> float | None:
        if not self._kalshi:
            return None
        try:
            from datetime import datetime, timezone

            resp = await self._kalshi.list_markets(tickers=ticker, limit=1)
            markets = resp.get("markets") or []
            if not markets:
                return None
            ct = markets[0].get("close_time") or markets[0].get("expected_expiration_time")
            if not ct:
                return None
            if isinstance(ct, (int, float)):
                close_ts = float(ct)
            else:
                close_ts = datetime.fromisoformat(str(ct).replace("Z", "+00:00")).timestamp()
            return max(0.0, close_ts - datetime.now(timezone.utc).timestamp())
        except Exception:
            return None

    async def _live_close_position(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        mark_yes: float,
        exchange_index: Any,
        status: str,
        reason: str,
        actor: str = "system",
    ) -> bool:
        if not self._kalshi:
            return False
        try:
            ex = int(exchange_index) if exchange_index is not None else None
        except (TypeError, ValueError):
            ex = None
        body = build_close_v2_order(
            ticker=ticker,
            position_side=side,
            count=qty,
            mark_yes_prob=mark_yes,
            exchange_index=ex,
        )
        try:
            resp = await self._kalshi.create_order(body)
            self.state.failsafes.record_api_success()
            self._entry_marks.pop(ticker, None)
            self._recent_trades[ticker] = __import__("time").time()
            self.state.ledger.append(
                kind="order",
                ticker=ticker,
                side=side,
                qty=qty,
                price_cents=int(round(mark_yes * 100)),
                mode="live",
                status=status,
                message=f"{status} by {actor}: {reason}",
                meta={"request": body, "response": resp},
            )
            await self.sync_live_portfolio()
            await self.state.publish({"type": "ledger"})
            return True
        except Exception as exc:
            friendly = _friendly_kalshi_error(exc)
            if is_soft_order_reject(exc):
                self.state.terminal(f"SOFT REJECT close {ticker}: {friendly}")
                self.state.ledger.append(
                    kind="order",
                    ticker=ticker,
                    side=side,
                    qty=qty,
                    mode="live",
                    status="soft_reject",
                    message=f"close {status} soft: {friendly}",
                )
            else:
                self.state.failsafes.record_api_error()
                self.state.terminal(f"CLOSE ERROR {ticker}: {friendly}")
                self.state.ledger.append(
                    kind="order",
                    ticker=ticker,
                    side=side,
                    qty=qty,
                    mode="live",
                    status="error",
                    message=f"close {status} error: {friendly}",
                )
            await self.state.publish({"type": "ledger"})
            return False

    async def flatten_all(self, actor: str = "operator") -> None:
        """Manual fail-safe: flatten paper or live positions."""
        if self.state.settings.kalshi_trading_mode == "live" and self._kalshi:
            await self.sync_live_portfolio()
            for pos in list(self.state.positions):
                ticker = str(pos.get("ticker") or "")
                side = str(pos.get("side") or "yes")
                qty = int(pos.get("qty") or 0)
                if not ticker or qty <= 0:
                    continue
                mark = await self._fetch_mark_yes(ticker) or 0.5
                await self._live_close_position(
                    ticker=ticker,
                    side=side,
                    qty=qty,
                    mark_yes=mark,
                    exchange_index=(self._entry_marks.get(ticker) or {}).get("exchange_index"),
                    status="flattened",
                    reason=f"flatten_all by {actor}",
                    actor=actor,
                )
            return

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
        if self.state.settings.kalshi_trading_mode == "live" and self._kalshi:
            await self.sync_live_portfolio()
            pos = next((p for p in self.state.positions if str(p.get("ticker")) == ticker), None)
            if not pos:
                return False
            mark = await self._fetch_mark_yes(ticker) or 0.5
            return await self._live_close_position(
                ticker=ticker,
                side=str(pos.get("side") or "yes"),
                qty=int(pos.get("qty") or 0),
                mark_yes=mark,
                exchange_index=(self._entry_marks.get(ticker) or {}).get("exchange_index"),
                status="closed",
                reason=f"Close by {actor}",
                actor=actor,
            )

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
