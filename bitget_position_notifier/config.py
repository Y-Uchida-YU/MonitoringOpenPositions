from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bitget_api_key: str
    bitget_api_secret: str
    bitget_passphrase: str
    discord_webhook_url: str
    base_url: str = "https://api.bitget.com"
    product_type: str = "USDT-FUTURES"
    margin_coin: str = "USDT"
    bitget_locale: str = "en-US"
    request_timeout_seconds: float = 10.0
    poll_interval_seconds: int = 600
    discord_username: str = "Bitget Position Bot"
    enable_market_metrics: bool = True
    market_data_exchanges: tuple[str, ...] = ("binance", "bybit", "bitget", "okx", "gate", "hyperliquid")
    market_metrics_db_path: str = "data/market_metrics.sqlite3"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def load_config() -> Config:
    load_dotenv()

    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    if timeout <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS must be > 0")

    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))
    if interval <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be > 0")

    exchanges = tuple(
        exchange.strip()
        for exchange in os.getenv(
            "MARKET_DATA_EXCHANGES",
            "binance,bybit,bitget,okx,gate,hyperliquid",
        ).split(",")
        if exchange.strip()
    )

    return Config(
        bitget_api_key=_require_env("BITGET_API_KEY"),
        bitget_api_secret=_require_env("BITGET_API_SECRET"),
        bitget_passphrase=_require_env("BITGET_PASSPHRASE"),
        discord_webhook_url=_require_env("DISCORD_WEBHOOK_URL"),
        base_url=os.getenv("BITGET_BASE_URL", "https://api.bitget.com").rstrip("/"),
        product_type=os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES").strip(),
        margin_coin=os.getenv("BITGET_MARGIN_COIN", "USDT").strip(),
        bitget_locale=os.getenv("BITGET_LOCALE", "en-US").strip(),
        request_timeout_seconds=timeout,
        poll_interval_seconds=interval,
        discord_username=os.getenv("DISCORD_USERNAME", "Bitget Position Bot").strip(),
        enable_market_metrics=os.getenv("ENABLE_MARKET_METRICS", "true").strip().lower() in {"1", "true", "yes", "on"},
        market_data_exchanges=exchanges,
        market_metrics_db_path=os.getenv("MARKET_METRICS_DB_PATH", "data/market_metrics.sqlite3").strip(),
    )
