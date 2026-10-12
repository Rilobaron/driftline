"""Central application configuration (Pydantic settings, env-driven)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
