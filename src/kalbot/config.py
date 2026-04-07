from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class FeedsConfig(BaseModel):
    chainlink_ws_url: str = "wss://ws-live-data.polymarket.com"
    chainlink_ping_interval_s: int = 5
    chainlink_stale_threshold_s: int = 10
    spot_sources: list[str] = Field(default_factory=lambda: ["coinbase", "kraken"])
    spot_poll_interval_s: float = 2.0
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    market_discovery_interval_s: int = 30
    market_series_ticker: str = "btc-up-or-down-5m"
    chainlink_source: str = "coinbase_rest"  # "coinbase_rest" | "onchain_rpc"
    chainlink_poll_interval_s: float = 1.0


class EngineConfig(BaseModel):
    min_elapsed_seconds: int = 60
    max_elapsed_seconds: int = 270
    min_displacement_pct: float = 0.02
    min_direction_consistency: float = 0.60
    max_cross_count: int = 6
    min_time_above_yes: float = 0.55
    max_time_above_no: float = 0.45
    spot_trend_conflict_threshold: float = 0.0008
    require_spot_confirmation: bool = True
    scorer: str = "rules"  # "rules" or "ml"

    @field_validator("scorer")
    @classmethod
    def validate_scorer(cls, v: str) -> str:
        if v not in ("rules", "ml"):
            raise ValueError(f"engine.scorer must be 'rules' or 'ml', got {v!r}")
        return v


class ExecutionConfig(BaseModel):
    mode: str = "paper"
    default_order_size_usd: float = 10.0
    maker_timeout_seconds: int = 30
    taker_threshold_seconds: int = 60
    cancel_on_reversal: bool = True

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"execution.mode must be 'paper' or 'live', got {v!r}")
        return v


class RiskConfig(BaseModel):
    max_position_usd: float = 25.0
    max_daily_loss_usd: float = 15.0
    max_drawdown_pct: float = 15.0
    max_concurrent_positions: int = 2
    min_edge_pct: float = 3.0
    starting_bankroll_usd: float = 100.0


class DataConfig(BaseModel):
    db_path: str = "data/kalbot.db"
    log_all_windows: bool = True
    tick_logging: bool = True
    snapshot_at_seconds: list[int] = Field(default_factory=lambda: [120, 150, 180, 210, 240])


class KalbotConfig(BaseModel):
    feeds: FeedsConfig = Field(default_factory=FeedsConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)

    # Secrets — loaded from env, never from TOML
    polymarket_api_key: str = Field(default="", repr=False)
    polymarket_private_key: str = Field(default="", repr=False)
    discord_webhook_url: str = Field(default="", repr=False)
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_chat_id: str = Field(default="", repr=False)
    dashboard_secret_key: str = Field(default="change-me", repr=False)


def load_config() -> KalbotConfig:
    env = os.getenv("KALBOT_ENV", "paper").lower()

    raw = _load_toml(_CONFIG_DIR / "default.toml")
    override_path = _CONFIG_DIR / f"{env}.toml"
    override = _load_toml(override_path)
    merged = _deep_merge(raw, override)

    cfg = KalbotConfig(**merged)

    # Inject secrets from environment
    cfg.polymarket_api_key = os.getenv("POLYMARKET_API_KEY", "")
    cfg.polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    cfg.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    cfg.dashboard_secret_key = os.getenv("DASHBOARD_SECRET_KEY", "change-me")

    return cfg
