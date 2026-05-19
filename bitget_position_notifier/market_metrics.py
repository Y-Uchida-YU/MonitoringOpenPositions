from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean
from typing import Any

import requests


LOGGER = logging.getLogger("market-metrics")


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized.endswith("USDT") and len(normalized) > 4:
        return f"{normalized[:-4]}-USDT"
    return normalized


@dataclass(frozen=True)
class ExchangeMetric:
    exchange: str
    source_symbol: str | None = None
    is_listed: bool = True
    has_previous_oi: bool = False
    oi_value: Decimal | None = None
    oi_change_pct: Decimal | None = None
    latest_volume: Decimal | None = None
    volume_spike: Decimal | None = None
    long_ratio: Decimal | None = None
    short_ratio: Decimal | None = None
    long_short_ratio: Decimal | None = None
    taker_buy_volume: Decimal | None = None
    taker_sell_volume: Decimal | None = None
    taker_buy_sell_ratio: Decimal | None = None
    top_account_long_ratio: Decimal | None = None
    top_account_short_ratio: Decimal | None = None
    top_account_long_short_ratio: Decimal | None = None
    top_position_long_ratio: Decimal | None = None
    top_position_short_ratio: Decimal | None = None
    top_position_long_short_ratio: Decimal | None = None
    error: str | None = None


@dataclass(frozen=True)
class SymbolMarketMetrics:
    symbol: str
    exchange_metrics: list[ExchangeMetric]


class MarketMetricStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oi_samples (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    oi_value TEXT NOT NULL,
                    PRIMARY KEY (exchange, symbol, observed_at)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_oi_samples_lookup
                ON oi_samples(exchange, symbol, observed_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_samples (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    oi_value TEXT,
                    volume_30m TEXT,
                    volume_spike TEXT,
                    long_ratio TEXT,
                    short_ratio TEXT,
                    long_short_ratio TEXT,
                    taker_buy_volume TEXT,
                    taker_sell_volume TEXT,
                    taker_buy_sell_ratio TEXT,
                    top_account_long_ratio TEXT,
                    top_account_short_ratio TEXT,
                    top_account_long_short_ratio TEXT,
                    top_position_long_ratio TEXT,
                    top_position_short_ratio TEXT,
                    top_position_long_short_ratio TEXT,
                    PRIMARY KEY (exchange, symbol, observed_at)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_samples_lookup
                ON market_samples(exchange, symbol, observed_at)
                """
            )
            self._migrate_market_samples(conn)

    @staticmethod
    def _migrate_market_samples(conn: sqlite3.Connection) -> None:
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(market_samples)").fetchall()
        }
        optional_columns = {
            "normalized_symbol": "TEXT",
            "quote_volume_30m": "TEXT",
            "raw_json": "TEXT",
            "source": "TEXT",
            "source_symbol": "TEXT",
        }
        for column_name, column_type in optional_columns.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE market_samples ADD COLUMN {column_name} {column_type}")

    def save_oi(self, exchange: str, symbol: str, observed_at: int, oi_value: Decimal) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO oi_samples(exchange, symbol, observed_at, oi_value)
                VALUES (?, ?, ?, ?)
                """,
                (exchange, symbol, observed_at, str(oi_value)),
            )
            cutoff = observed_at - 7 * 24 * 60 * 60
            conn.execute("DELETE FROM oi_samples WHERE observed_at < ?", (cutoff,))

    def save_market_sample(self, symbol: str, observed_at: int, metric: ExchangeMetric) -> None:
        def value(item: Decimal | None) -> str | None:
            return str(item) if item is not None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO market_samples(
                    exchange, symbol, observed_at, oi_value, volume_30m, volume_spike,
                    long_ratio, short_ratio, long_short_ratio,
                    taker_buy_volume, taker_sell_volume, taker_buy_sell_ratio,
                    top_account_long_ratio, top_account_short_ratio, top_account_long_short_ratio,
                    top_position_long_ratio, top_position_short_ratio, top_position_long_short_ratio,
                    normalized_symbol, quote_volume_30m, raw_json, source, source_symbol
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric.exchange,
                    symbol,
                    observed_at,
                    value(metric.oi_value),
                    value(metric.latest_volume),
                    value(metric.volume_spike),
                    value(metric.long_ratio),
                    value(metric.short_ratio),
                    value(metric.long_short_ratio),
                    value(metric.taker_buy_volume),
                    value(metric.taker_sell_volume),
                    value(metric.taker_buy_sell_ratio),
                    value(metric.top_account_long_ratio),
                    value(metric.top_account_short_ratio),
                    value(metric.top_account_long_short_ratio),
                    value(metric.top_position_long_ratio),
                    value(metric.top_position_short_ratio),
                    value(metric.top_position_long_short_ratio),
                    normalize_symbol(symbol),
                    None,
                    None,
                    "public_market_api",
                    metric.source_symbol,
                ),
            )
            cutoff = observed_at - 7 * 24 * 60 * 60
            conn.execute("DELETE FROM market_samples WHERE observed_at < ?", (cutoff,))

    def previous_oi(self, exchange: str, symbol: str, before_at: int) -> Decimal | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT oi_value
                FROM oi_samples
                WHERE exchange = ? AND symbol = ? AND observed_at < ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (exchange, symbol, before_at),
            ).fetchone()
        if row is None:
            return None
        return to_decimal(row[0])


class MarketDataError(Exception):
    pass


def to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def pct_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * Decimal("100")


def volume_spike(latest: Decimal | None, historical: list[Decimal]) -> Decimal | None:
    non_zero = [value for value in historical if value > 0]
    if latest is None or not non_zero:
        return None
    average = mean(non_zero)
    if average == 0:
        return None
    return latest / Decimal(str(average))


class PublicExchangeClient:
    name = "base"

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._symbol_cache: dict[str, str | None] = {}

    def resolve_symbol(self, symbol: str) -> str | None:
        return symbol

    def fetch_oi(self, symbol: str) -> Decimal | None:
        raise NotImplementedError

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        raise NotImplementedError

    def fetch_long_short_ratio(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        return None, None, None

    def fetch_taker_buy_sell_volume(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        return None, None, None

    def fetch_top_trader_ratios(
        self, symbol: str
    ) -> tuple[
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
    ]:
        return None, None, None, None, None, None

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            if method == "POST":
                response = requests.post(url, json=json_body, timeout=self.timeout_seconds)
            else:
                response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise MarketDataError(f"{self.name} request failed: {exc}") from exc
        except ValueError as exc:
            raise MarketDataError(f"{self.name} returned invalid JSON") from exc


class BinanceClient(PublicExchangeClient):
    name = "Binance"

    def resolve_symbol(self, symbol: str) -> str | None:
        if symbol not in self._symbol_cache:
            payload = self.get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
            symbols = {
                item.get("symbol")
                for item in payload.get("symbols", [])
                if item.get("quoteAsset") == "USDT"
                and item.get("contractType") == "PERPETUAL"
                and item.get("status") == "TRADING"
            }
            self._symbol_cache[symbol] = symbol if symbol in symbols else None
        return self._symbol_cache[symbol]

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self.get_json(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
        )
        return to_decimal(payload.get("openInterest"))

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        payload = self.get_json(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "30m", "limit": limit},
        )
        volumes = [to_decimal(row[7]) for row in payload if len(row) > 7]
        return list(reversed(volumes))

    def fetch_long_short_ratio(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        payload = self.get_json(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "30m", "limit": 1},
        )
        item = payload[-1] if payload else {}
        return (
            to_decimal(item.get("longAccount")) if item else None,
            to_decimal(item.get("shortAccount")) if item else None,
            to_decimal(item.get("longShortRatio")) if item else None,
        )

    def fetch_taker_buy_sell_volume(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        payload = self.get_json(
            "https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": "30m", "limit": 1},
        )
        item = payload[-1] if payload else {}
        return (
            to_decimal(item.get("buyVol")) if item else None,
            to_decimal(item.get("sellVol")) if item else None,
            to_decimal(item.get("buySellRatio")) if item else None,
        )

    def fetch_top_trader_ratios(
        self, symbol: str
    ) -> tuple[
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
    ]:
        accounts = self.get_json(
            "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": "30m", "limit": 1},
        )
        positions = self.get_json(
            "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": "30m", "limit": 1},
        )
        account = accounts[-1] if accounts else {}
        position = positions[-1] if positions else {}
        return (
            to_decimal(account.get("longAccount")) if account else None,
            to_decimal(account.get("shortAccount")) if account else None,
            to_decimal(account.get("longShortRatio")) if account else None,
            to_decimal(position.get("longAccount")) if position else None,
            to_decimal(position.get("shortAccount")) if position else None,
            to_decimal(position.get("longShortRatio")) if position else None,
        )


class BybitClient(PublicExchangeClient):
    name = "Bybit"

    def resolve_symbol(self, symbol: str) -> str | None:
        if symbol not in self._symbol_cache:
            payload = self.get_json(
                "https://api.bybit.com/v5/market/instruments-info",
                params={"category": "linear", "symbol": symbol},
            )
            rows = payload.get("result", {}).get("list", [])
            listed = any(item.get("symbol") == symbol and item.get("status") == "Trading" for item in rows)
            self._symbol_cache[symbol] = symbol if listed else None
        return self._symbol_cache[symbol]

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self.get_json(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": "30min", "limit": 1},
        )
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            return None
        return to_decimal(rows[0].get("openInterest"))

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        payload = self.get_json(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "30", "limit": limit},
        )
        rows = payload.get("result", {}).get("list", [])
        return [to_decimal(row[6]) for row in rows if len(row) > 6]

    def fetch_long_short_ratio(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        payload = self.get_json(
            "https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": symbol, "period": "30min", "limit": 1},
        )
        rows = payload.get("result", {}).get("list", [])
        item = rows[0] if rows else {}
        long_ratio = to_decimal(item.get("buyRatio")) if item else None
        short_ratio = to_decimal(item.get("sellRatio")) if item else None
        ratio = (long_ratio / short_ratio) if long_ratio is not None and short_ratio not in (None, Decimal("0")) else None
        return long_ratio, short_ratio, ratio


class BitgetPublicClient(PublicExchangeClient):
    name = "Bitget"

    def resolve_symbol(self, symbol: str) -> str | None:
        if symbol not in self._symbol_cache:
            payload = self.get_json(
                "https://api.bitget.com/api/v2/mix/market/contracts",
                params={"productType": "USDT-FUTURES"},
            )
            if payload.get("code") != "00000":
                raise MarketDataError(f"Bitget returned code {payload.get('code')}: {payload.get('msg')}")
            symbols = {item.get("symbol") for item in payload.get("data", []) if item.get("symbolStatus") == "normal"}
            self._symbol_cache[symbol] = symbol if symbol in symbols else None
        return self._symbol_cache[symbol]

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self.get_json(
            "https://api.bitget.com/api/v2/mix/market/open-interest",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
        )
        if payload.get("code") != "00000":
            raise MarketDataError(f"Bitget returned code {payload.get('code')}: {payload.get('msg')}")
        data = payload.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        open_interest_list = data.get("openInterestList", []) if isinstance(data, dict) else []
        if open_interest_list:
            return to_decimal(open_interest_list[0].get("size"))
        return to_decimal(data.get("openInterest") or data.get("amount") or data.get("size"))

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        payload = self.get_json(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params={"symbol": symbol, "productType": "USDT-FUTURES", "granularity": "30m", "limit": limit},
        )
        if payload.get("code") != "00000":
            raise MarketDataError(f"Bitget returned code {payload.get('code')}: {payload.get('msg')}")
        rows = payload.get("data", [])
        return [to_decimal(row[6] if len(row) > 6 else row[5]) for row in rows]

    def fetch_long_short_ratio(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        payload = self.get_json(
            "https://api.bitget.com/api/v2/mix/market/account-long-short",
            params={"symbol": symbol, "productType": "USDT-FUTURES", "period": "30m"},
        )
        if payload.get("code") != "00000":
            raise MarketDataError(f"Bitget returned code {payload.get('code')}: {payload.get('msg')}")
        rows = payload.get("data", [])
        item = rows[-1] if rows else {}
        long_ratio = to_decimal(item.get("longAccountRatio")) if item else None
        short_ratio = to_decimal(item.get("shortAccountRatio")) if item else None
        ratio = (long_ratio / short_ratio) if long_ratio is not None and short_ratio not in (None, Decimal("0")) else None
        return long_ratio, short_ratio, ratio


class OkxClient(PublicExchangeClient):
    name = "OKX"

    @staticmethod
    def inst_id(symbol: str) -> str:
        base = symbol.removesuffix("USDT")
        return f"{base}-USDT-SWAP"

    def resolve_symbol(self, symbol: str) -> str | None:
        if symbol not in self._symbol_cache:
            inst_id = self.inst_id(symbol)
            payload = self.get_json(
                "https://www.okx.com/api/v5/public/instruments",
                params={"instType": "SWAP", "instId": inst_id},
            )
            rows = payload.get("data", [])
            listed = any(item.get("instId") == inst_id and item.get("state") == "live" for item in rows)
            self._symbol_cache[symbol] = inst_id if listed else None
        return self._symbol_cache[symbol]

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self.get_json(
            "https://www.okx.com/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": symbol},
        )
        rows = payload.get("data", [])
        if not rows:
            return None
        return to_decimal(rows[0].get("oiUsd") or rows[0].get("oiCcy") or rows[0].get("oi"))

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        payload = self.get_json(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": symbol, "bar": "30m", "limit": limit},
        )
        rows = payload.get("data", [])
        return [to_decimal(row[7] if len(row) > 7 else row[6]) for row in rows]

    def fetch_long_short_ratio(self, symbol: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        base = symbol.split("-")[0]
        payload = self.get_json(
            "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": base, "period": "1H"},
        )
        rows = payload.get("data", [])
        item = rows[-1] if rows else []
        ratio = to_decimal(item[1]) if len(item) > 1 else None
        if ratio is None or ratio <= 0:
            return None, None, None
        long_ratio = ratio / (ratio + Decimal("1"))
        short_ratio = Decimal("1") / (ratio + Decimal("1"))
        return long_ratio, short_ratio, ratio


class GateClient(PublicExchangeClient):
    name = "Gate"

    @staticmethod
    def contract(symbol: str) -> str:
        base = symbol.removesuffix("USDT")
        return f"{base}_USDT"

    def resolve_symbol(self, symbol: str) -> str | None:
        if symbol not in self._symbol_cache:
            contract = self.contract(symbol)
            payload = self.get_json("https://api.gateio.ws/api/v4/futures/usdt/contracts")
            listed = any(item.get("name") == contract and not item.get("in_delisting", False) for item in payload)
            self._symbol_cache[symbol] = contract if listed else None
        return self._symbol_cache[symbol]

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self.get_json(
            "https://api.gateio.ws/api/v4/futures/usdt/tickers",
            params={"contract": symbol},
        )
        item = payload[0] if isinstance(payload, list) and payload else {}
        total_size = to_decimal(item.get("total_size"))
        multiplier = to_decimal(item.get("quanto_multiplier"))
        mark_price = to_decimal(item.get("mark_price"))
        if total_size > 0 and multiplier > 0 and mark_price > 0:
            return total_size * multiplier * mark_price
        return to_decimal(item.get("total_size"))

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        payload = self.get_json(
            "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
            params={"contract": symbol, "interval": "30m", "limit": limit},
        )
        return [to_decimal(row.get("sum") or row.get("v")) for row in payload if isinstance(row, dict)]


class HyperliquidClient(PublicExchangeClient):
    name = "Hyperliquid"

    def _post_info(self, body: dict[str, Any]) -> Any:
        return self.get_json("https://api.hyperliquid.xyz/info", method="POST", json_body=body)

    def resolve_symbol(self, symbol: str) -> str | None:
        base = symbol.removesuffix("USDT")
        payload = self._post_info({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            return None
        universe = payload[0].get("universe", [])
        symbols = {asset.get("name") for asset in universe}
        return base if base in symbols else None

    def fetch_oi(self, symbol: str) -> Decimal | None:
        payload = self._post_info({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            return None
        universe = payload[0].get("universe", [])
        contexts = payload[1]
        for index, asset in enumerate(universe):
            if asset.get("name") == symbol and index < len(contexts):
                ctx = contexts[index]
                oi = to_decimal(ctx.get("openInterest"))
                mark = to_decimal(ctx.get("markPx") or ctx.get("midPx"))
                return oi * mark if mark > 0 else oi
        return None

    def fetch_recent_30m_volumes(self, symbol: str, limit: int = 49) -> list[Decimal]:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - limit * 30 * 60 * 1000
        payload = self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": "30m",
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        volumes = [to_decimal(row.get("v")) * to_decimal(row.get("c")) for row in payload if isinstance(row, dict)]
        return list(reversed(volumes))


def build_clients(exchange_names: list[str], timeout_seconds: float) -> list[PublicExchangeClient]:
    factories: dict[str, type[PublicExchangeClient]] = {
        "binance": BinanceClient,
        "bybit": BybitClient,
        "bitget": BitgetPublicClient,
        "okx": OkxClient,
        "gate": GateClient,
        "hyperliquid": HyperliquidClient,
    }
    clients: list[PublicExchangeClient] = []
    for name in exchange_names:
        factory = factories.get(name.strip().lower())
        if factory is None:
            LOGGER.warning("Unknown market data exchange skipped: %s", name)
            continue
        clients.append(factory(timeout_seconds))
    return clients


class MarketMetricService:
    def __init__(
        self,
        *,
        exchange_names: list[str],
        timeout_seconds: float,
        db_path: str | Path,
    ) -> None:
        self.clients = build_clients(exchange_names, timeout_seconds)
        self.store = MarketMetricStore(db_path)

    def _fetch_exchange_metric(self, client: PublicExchangeClient, symbol: str, observed_at: int) -> ExchangeMetric:
        source_symbol = client.resolve_symbol(symbol)
        if source_symbol is None:
            return ExchangeMetric(exchange=client.name, source_symbol=None, is_listed=False)

        oi_value = client.fetch_oi(source_symbol)
        previous = self.store.previous_oi(client.name, symbol, observed_at)
        oi_change = pct_change(oi_value, previous)

        volumes = client.fetch_recent_30m_volumes(source_symbol)
        # Most exchanges include the still-forming candle first. Use the latest closed 30m candle.
        latest_volume = volumes[1] if len(volumes) > 1 else (volumes[0] if volumes else None)
        historical = volumes[2:] if len(volumes) > 2 else []
        long_ratio, short_ratio, long_short_ratio = client.fetch_long_short_ratio(source_symbol)
        taker_buy_volume, taker_sell_volume, taker_buy_sell_ratio = client.fetch_taker_buy_sell_volume(source_symbol)
        (
            top_account_long_ratio,
            top_account_short_ratio,
            top_account_long_short_ratio,
            top_position_long_ratio,
            top_position_short_ratio,
            top_position_long_short_ratio,
        ) = client.fetch_top_trader_ratios(source_symbol)

        return ExchangeMetric(
            exchange=client.name,
            source_symbol=source_symbol,
            has_previous_oi=previous is not None,
            oi_value=oi_value,
            oi_change_pct=oi_change,
            latest_volume=latest_volume,
            volume_spike=volume_spike(latest_volume, historical),
            long_ratio=long_ratio,
            short_ratio=short_ratio,
            long_short_ratio=long_short_ratio,
            taker_buy_volume=taker_buy_volume,
            taker_sell_volume=taker_sell_volume,
            taker_buy_sell_ratio=taker_buy_sell_ratio,
            top_account_long_ratio=top_account_long_ratio,
            top_account_short_ratio=top_account_short_ratio,
            top_account_long_short_ratio=top_account_long_short_ratio,
            top_position_long_ratio=top_position_long_ratio,
            top_position_short_ratio=top_position_short_ratio,
            top_position_long_short_ratio=top_position_long_short_ratio,
        )

    def fetch_for_symbols(self, symbols: list[str]) -> dict[str, SymbolMarketMetrics]:
        observed_at = int(time.time())
        results: dict[str, SymbolMarketMetrics] = {}
        for symbol in symbols:
            exchange_metrics: list[ExchangeMetric] = []
            with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as executor:
                futures = {
                    executor.submit(self._fetch_exchange_metric, client, symbol, observed_at): client
                    for client in self.clients
                }
                for future in as_completed(futures):
                    client = futures[future]
                    try:
                        metric = future.result()
                        if metric.oi_value is not None:
                            self.store.save_oi(metric.exchange, symbol, observed_at, metric.oi_value)
                        if metric.is_listed and metric.error is None:
                            self.store.save_market_sample(symbol, observed_at, metric)
                        exchange_metrics.append(metric)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("%s market metrics failed for %s: %s", client.name, symbol, exc)
                        exchange_metrics.append(ExchangeMetric(exchange=client.name, error=str(exc)))
            exchange_metrics.sort(key=lambda metric: [client.name for client in self.clients].index(metric.exchange))
            results[symbol] = SymbolMarketMetrics(symbol=symbol, exchange_metrics=exchange_metrics)
        return results
