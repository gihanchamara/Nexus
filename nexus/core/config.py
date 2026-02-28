"""
Central configuration loaded from environment variables.
Uses pydantic-settings so every field is typed and validated at startup.
"""

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── MySQL ──────────────────────────────────────────────────────────────────
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_db: str = "nexus"
    mysql_user: str = "nexus"
    mysql_password: str = Field(..., description="MySQL password — required")

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
        )

    # ── TimescaleDB ────────────────────────────────────────────────────────────
    timescale_host: str = "localhost"
    timescale_port: int = 5432
    timescale_db: str = "nexus_ts"
    timescale_user: str = "nexus_ts"
    timescale_password: str = Field(..., description="TimescaleDB password — required")

    @property
    def timescale_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.timescale_user}:{self.timescale_password}"
            f"@{self.timescale_host}:{self.timescale_port}/{self.timescale_db}"
        )

    # ── Redis Streams ──────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = Field(..., description="Redis password — required")
    redis_db: int = 0

    @property
    def redis_url(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ── IBKR ──────────────────────────────────────────────────────────────────
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497          # 7497 = TWS paper; 4002 = IB Gateway paper
    ibkr_client_id: int = 1
    ibkr_account: str = ""         # e.g. DU1234567

    # ── Trading Safety ─────────────────────────────────────────────────────────
    nexus_trading_mode: TradingMode = TradingMode.PAPER
    nexus_kill_switch: bool = False  # Set NEXUS_KILL_SWITCH=1 to halt all live orders

    @field_validator("nexus_trading_mode", mode="before")
    @classmethod
    def upper_mode(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v

    # ── API ────────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = Field(..., min_length=32, description="JWT signing key — min 32 chars")

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: LogLevel = LogLevel.INFO


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings singleton. Call once at startup."""
    return Settings()  # type: ignore[call-arg]
