"""Environment-driven settings for NexusPMT."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nexuspmt_operator_token: str = Field(default="change-me", alias="NEXUSPMT_OPERATOR_TOKEN")
    nexuspmt_host: str = Field(default="0.0.0.0", alias="NEXUSPMT_HOST")
    nexuspmt_port: int = Field(default=8080, alias="NEXUSPMT_PORT")

    worldmap_base_url: str = Field(
        default="http://sk-ai-worldmap:8080",
        alias="WORLDMAP_BASE_URL",
    )
    worldmap_health_path: str = Field(
        default="/api/sidecar-health",
        alias="WORLDMAP_HEALTH_PATH",
    )
    worldmap_poll_interval_sec: float = Field(default=30.0, alias="WORLDMAP_POLL_INTERVAL_SEC")
    worldmap_required: bool = Field(default=True, alias="WORLDMAP_REQUIRED")
    worldmap_container_name: str = Field(
        default="sk-ai-worldmap",
        alias="WORLDMAP_CONTAINER_NAME",
    )
    worldmap_compose_project: str = Field(
        default="sk-ai-worldmap",
        alias="WORLDMAP_COMPOSE_PROJECT",
    )

    kalshi_key_id: str = Field(default="", alias="KALSHI_KEY_ID")
    kalshi_private_key_path: str = Field(
        default="/run/secrets/kalshi_private.pem",
        alias="KALSHI_PRIVATE_KEY_PATH",
    )
    kalshi_env: Literal["demo", "production"] = Field(default="demo", alias="KALSHI_ENV")
    kalshi_trading_mode: Literal["paper", "live"] = Field(
        default="paper",
        alias="KALSHI_TRADING_MODE",
    )
    # Hard gate: live auto-orders require explicit unlock after paper $10→$100
    live_unlock: bool = Field(default=False, alias="LIVE_UNLOCK")
    paper_start_cents: int = Field(default=1000, alias="PAPER_START_CENTS")
    paper_target_cents: int = Field(default=10000, alias="PAPER_TARGET_CENTS")
    paper_state_path: str = Field(
        default="data/paper_shadow_book.json",
        alias="PAPER_STATE_PATH",
    )
    kalshi_sports_filter: Literal["strict", "off"] = Field(
        default="strict",
        alias="KALSHI_SPORTS_FILTER",
    )
    kalshi_category_allowlist: str = Field(
        default=(
            "Economics,Politics,Elections,Climate and Weather,Companies,"
            "Financials,Science and Technology,Crypto,World"
        ),
        alias="KALSHI_CATEGORY_ALLOWLIST",
    )
    kalshi_category_blocklist: str = Field(
        default="Sports,Entertainment",
        alias="KALSHI_CATEGORY_BLOCKLIST",
    )
    kalshi_min_edge: float = Field(default=0.05, alias="KALSHI_MIN_EDGE")
    kalshi_max_spread: float = Field(default=0.08, alias="KALSHI_MAX_SPREAD")
    kalshi_min_liquidity: float = Field(default=0.0, alias="KALSHI_MIN_LIQUIDITY")
    kalshi_max_notional_cents: int = Field(default=2500, alias="KALSHI_MAX_NOTIONAL_CENTS")
    kalshi_require_approval_above_cents: int = Field(
        default=50000,
        alias="KALSHI_REQUIRE_APPROVAL_ABOVE_CENTS",
    )
    kalshi_trade_interval_sec: float = Field(default=8.0, alias="KALSHI_TRADE_INTERVAL_SEC")
    kalshi_max_trades_per_cycle: int = Field(default=2, alias="KALSHI_MAX_TRADES_PER_CYCLE")
    kalshi_opportunity_scan_sec: float = Field(default=12.0, alias="KALSHI_OPPORTUNITY_SCAN_SEC")
    kalshi_contract_count: int = Field(default=1, alias="KALSHI_CONTRACT_COUNT")
    kalshi_strong_allocation_pct: float = Field(default=0.90, alias="KALSHI_STRONG_ALLOCATION_PCT")
    kalshi_max_entry_pct: float = Field(default=0.50, alias="KALSHI_MAX_ENTRY_PCT")
    kalshi_enter_voi_threshold: float = Field(default=0.28, alias="KALSHI_ENTER_VOI_THRESHOLD")
    kalshi_require_strong_enter: bool = Field(
        default=False,
        alias="KALSHI_REQUIRE_STRONG_ENTER",
    )
    kalshi_prefer_15m: bool = Field(default=False, alias="KALSHI_PREFER_15M")
    kalshi_stop_loss_prob: float = Field(default=0.18, alias="KALSHI_STOP_LOSS_PROB")
    kalshi_take_profit_prob: float = Field(default=0.20, alias="KALSHI_TAKE_PROFIT_PROB")
    kalshi_flip_min_edge: float = Field(default=0.20, alias="KALSHI_FLIP_MIN_EDGE")
    kalshi_allow_flip: bool = Field(default=False, alias="KALSHI_ALLOW_FLIP")
    kalshi_min_net_edge: float = Field(default=0.06, alias="KALSHI_MIN_NET_EDGE")
    kalshi_position_poll_sec: float = Field(default=5.0, alias="KALSHI_POSITION_POLL_SEC")
    kalshi_time_stop_sec: float = Field(default=120.0, alias="KALSHI_TIME_STOP_SEC")
    # Paper testing: force flat after this many seconds even if bands not hit
    paper_max_hold_sec: float = Field(default=900.0, alias="PAPER_MAX_HOLD_SEC")
    kalshi_eval_batch_size: int = Field(default=4, alias="KALSHI_EVAL_BATCH_SIZE")
    kalshi_max_open_positions: int = Field(default=1, alias="KALSHI_MAX_OPEN_POSITIONS")
    kalshi_filter_mode: Literal["blocklist", "allowlist"] = Field(
        default="blocklist",
        alias="KALSHI_FILTER_MODE",
    )
    kalshi_allow_secondary_edges: bool = Field(
        default=False,
        alias="KALSHI_ALLOW_SECONDARY_EDGES",
    )

    xai_api_key: str = Field(default="", alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="grok-4.6", alias="XAI_MODEL")
    xai_enabled: bool = Field(default=False, alias="XAI_ENABLED")
    xai_max_calls_per_hour: int = Field(default=10, alias="XAI_MAX_CALLS_PER_HOUR")
    xai_wheel_min_interval_sec: float = Field(
        default=900.0,
        alias="XAI_WHEEL_MIN_INTERVAL_SEC",
    )

    # Cheap intel: none | brave | serp | gemini (official APIs only — no UI scraping)
    intel_provider: Literal["none", "brave", "serp", "gemini"] = Field(
        default="none",
        alias="INTEL_PROVIDER",
    )
    intel_api_key: str = Field(default="", alias="INTEL_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", alias="GEMINI_MODEL")

    risk_max_daily_loss_cents: int = Field(default=5000, alias="RISK_MAX_DAILY_LOSS_CENTS")
    risk_max_drawdown_pct: float = Field(default=55.0, alias="RISK_MAX_DRAWDOWN_PCT")
    risk_auto_kill_on_errors: int = Field(default=5, alias="RISK_AUTO_KILL_ON_ERRORS")

    @property
    def worldmap_health_url(self) -> str:
        base = self.worldmap_base_url.rstrip("/")
        path = self.worldmap_health_path if self.worldmap_health_path.startswith("/") else f"/{self.worldmap_health_path}"
        return f"{base}{path}"

    @property
    def kalshi_base_url(self) -> str:
        if self.kalshi_env == "production":
            return "https://external-api.kalshi.com/trade-api/v2"
        return "https://external-api.demo.kalshi.co/trade-api/v2"

    @property
    def allowlist(self) -> set[str]:
        return {c.strip().lower() for c in self.kalshi_category_allowlist.split(",") if c.strip()}

    @property
    def blocklist(self) -> set[str]:
        return {c.strip().lower() for c in self.kalshi_category_blocklist.split(",") if c.strip()}

    def private_key_bytes(self) -> bytes:
        path = Path(self.kalshi_private_key_path)
        if not path.exists():
            # Dev fallback: local secrets path
            alt = Path("secrets/kalshi_private.pem")
            if alt.exists():
                return alt.read_bytes()
            raise FileNotFoundError(f"Kalshi private key not found: {path}")
        return path.read_bytes()


@lru_cache
def get_settings() -> Settings:
    return Settings()
