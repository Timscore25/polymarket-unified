from __future__ import annotations

from pydantic_settings import BaseSettings
from pydantic import Field, model_validator


class Settings(BaseSettings):
    # API URLs
    polymarket_api_url: str = Field(default="https://clob.polymarket.com")
    polymarket_ws_url: str = Field(default="wss://ws-subscriptions-clob.polymarket.com/ws/market")
    gamma_api_url: str = Field(default="https://gamma-api.polymarket.com")

    # Authentication
    private_key: str = Field(default="")
    public_address: str = Field(default="")

    # Mode
    dry_run: bool = Field(default=True)
    log_level: str = Field(default="INFO")

    # Market Discovery
    market_type: str = Field(default="btc")  # btc, eth, sol, xrp
    timeframes: str = Field(default="5m,15m")  # comma-separated

    # Market Making Settings
    mm_enabled: bool = Field(default=True)
    mm_spread_bps: int = Field(default=10)  # 0.1% spread
    mm_default_size: float = Field(default=100.0)
    mm_refresh_ms: int = Field(default=500)
    mm_order_lifetime_ms: int = Field(default=3000)

    # Arbitrage Settings
    arb_enabled: bool = Field(default=True)
    arb_threshold: float = Field(default=0.99)  # Sum of asks threshold
    arb_size: float = Field(default=50.0)
    arb_verify_fill_secs: int = Field(default=10)

    # Risk Management
    max_exposure_usd: float = Field(default=10000.0)
    min_exposure_usd: float = Field(default=-10000.0)
    max_position_size_usd: float = Field(default=5000.0)
    max_skew: float = Field(default=0.3)

    # Performance
    tick_interval_ms: int = Field(default=100)  # Main loop interval
    batch_cancellations: bool = Field(default=True)

    # Metrics
    metrics_enabled: bool = Field(default=True)
    metrics_port: int = Field(default=9306)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @model_validator(mode='after')
    def validate_live_trading_config(self) -> 'Settings':
        """Ensure required fields are set for live trading."""
        if not self.dry_run:
            if not self.private_key or len(self.private_key) < 64:
                raise ValueError(
                    "PRIVATE_KEY must be set for live trading. "
                    "Use DRY_RUN=true for simulation mode."
                )
            if not self.public_address or not self.public_address.startswith("0x"):
                raise ValueError(
                    "PUBLIC_ADDRESS must be a valid Ethereum address for live trading."
                )
        return self

    @property
    def timeframe_list(self) -> list[str]:
        return [t.strip() for t in self.timeframes.split(",")]


def get_settings() -> Settings:
    return Settings()
