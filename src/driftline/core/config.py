"""Central application configuration (Pydantic settings, env-driven)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MARKET_STREAMS = "binance:BTC/USDT:1h,binance:ETH/USDT:1h,binance:SOL/USDT:1h"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DRIFTLINE_", extra="ignore")

    env: str = Field(default="development", alias="DRIFTLINE_ENV")
    database_url: str = Field(default="sqlite:///./driftline.db", alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="DRIFTLINE_LOG_LEVEL")

    exchange: str = Field(default="binance", alias="DRIFTLINE_EXCHANGE")
    symbol: str = Field(default="BTC/USDT", alias="DRIFTLINE_SYMBOL")
    timeframe: str = Field(default="1h", alias="DRIFTLINE_TIMEFRAME")
    lookback_months: int = Field(default=12, alias="DRIFTLINE_LOOKBACK_MONTHS")
    max_candles: int = Field(default=5000, alias="DRIFTLINE_MAX_CANDLES")

    initial_capital: float = Field(default=10_000.0, alias="DRIFTLINE_INITIAL_CAPITAL")
    fee_rate: float = Field(default=0.001, alias="DRIFTLINE_FEE_RATE")
    slippage_rate: float = Field(default=0.0005, alias="DRIFTLINE_SLIPPAGE_RATE")
    allocation: float = Field(default=1.0, alias="DRIFTLINE_ALLOCATION")

    api_host: str = Field(default="0.0.0.0", alias="DRIFTLINE_API_HOST")
    api_port: int = Field(default=8000, alias="DRIFTLINE_API_PORT")

    # --- v0.2b market automation ---
    market_streams: str = Field(default=DEFAULT_MARKET_STREAMS, alias="DRIFTLINE_MARKET_STREAMS")
    sync_delay_seconds: int = Field(default=90, alias="DRIFTLINE_SYNC_DELAY_SECONDS")
    bootstrap_lookback_months: int = Field(default=12, alias="DRIFTLINE_BOOTSTRAP_MONTHS")
    scheduler_enabled: bool = Field(default=False, alias="DRIFTLINE_SCHEDULER_ENABLED")
    scheduler_interval_seconds: int = Field(default=60, alias="DRIFTLINE_SCHEDULER_INTERVAL")
    stale_after_seconds: int = Field(default=8100, alias="DRIFTLINE_STALE_AFTER_SECONDS")
    fetch_timeout_seconds: float = Field(default=30.0, alias="DRIFTLINE_FETCH_TIMEOUT")
    fetch_max_retries: int = Field(default=5, alias="DRIFTLINE_FETCH_MAX_RETRIES")
    ops_token: str = Field(default="", alias="DRIFTLINE_OPS_TOKEN")

    # --- v0.2c paper trading (simulated funds only) ---
    paper_interval_seconds: int = Field(default=60, alias="DRIFTLINE_PAPER_INTERVAL")
    paper_max_new_candles: int = Field(default=48, alias="DRIFTLINE_PAPER_MAX_CANDLES")
    paper_fee_rate: float = Field(default=0.001, alias="DRIFTLINE_PAPER_FEE_RATE")
    paper_slippage_rate: float = Field(default=0.0005, alias="DRIFTLINE_PAPER_SLIPPAGE_RATE")
    paper_allocation: float = Field(default=1.0, alias="DRIFTLINE_PAPER_ALLOCATION")
    paper_min_quantity: float = Field(default=0.00001, alias="DRIFTLINE_PAPER_MIN_QTY")
    paper_max_daily_loss_pct: float = Field(default=0.05, alias="DRIFTLINE_PAPER_MAX_DAILY_LOSS")
    paper_max_drawdown: float = Field(default=0.20, alias="DRIFTLINE_PAPER_MAX_DRAWDOWN")
    paper_min_balance: float = Field(default=10.0, alias="DRIFTLINE_PAPER_MIN_BALANCE")
    paper_max_stale_seconds: int = Field(default=7200, alias="DRIFTLINE_PAPER_MAX_STALE")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
