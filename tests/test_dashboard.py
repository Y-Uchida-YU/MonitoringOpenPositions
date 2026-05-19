from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "bitget_position_notifier"
sys.path.insert(0, str(APP_DIR))

from dashboard import (  # noqa: E402
    build_aggregated_series,
    calculate_normalized_points,
    read_snapshot,
)
from market_metrics import ExchangeMetric, MarketMetricStore, normalize_symbol  # noqa: E402


def create_market_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE market_samples (
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
        rows = [
            ("Binance", "BTCUSDT", 1000, "100", "10", "1.2", "0.6", "0.4", "1.5", "7", "3", "2.333", "0.7", "0.3", "2.333", "0.65", "0.35", "1.857"),
            ("Bybit", "BTCUSDT", 1000, "200", "20", "0.8", None, None, None, None, None, None, None, None, None, None, None, None),
            ("Binance", "BTCUSDT", 1600, "150", "15", "1.1", "0.65", "0.35", "1.857", "9", "6", "1.5", "0.72", "0.28", "2.571", "0.66", "0.34", "1.941"),
            ("Bybit", "BTCUSDT", 1600, "300", "30", "1.0", None, None, None, None, None, None, None, None, None, None, None, None),
        ]
        conn.executemany(
            """
            INSERT INTO market_samples VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            rows,
        )


def test_aggregated_oi_calculation() -> None:
    rows = [
        {"exchange": "A", "observed_at": 1000, "oi_value": "100"},
        {"exchange": "B", "observed_at": 1001, "oi_value": "250"},
        {"exchange": "A", "observed_at": 1600, "oi_value": "120"},
    ]

    series = build_aggregated_series(rows, value_key="oi_value", bucket_seconds=600)

    assert series[0]["y"] == 350.0
    assert series[1]["y"] == 120.0


def test_aggregated_volume_calculation() -> None:
    rows = [
        {"exchange": "A", "observed_at": 1000, "volume_30m": "10"},
        {"exchange": "B", "observed_at": 1001, "volume_30m": "20"},
        {"exchange": "A", "observed_at": 1600, "volume_30m": "5"},
    ]

    series = build_aggregated_series(rows, value_key="volume_30m", bucket_seconds=600)

    assert series[0]["y"] == 30.0
    assert series[1]["y"] == 5.0


def test_normalized_oi_calculation() -> None:
    rows = [
        {"observed_at": 1000, "oi_value": "100"},
        {"observed_at": 1600, "oi_value": "125"},
        {"observed_at": 2200, "oi_value": "80"},
    ]

    normalized = calculate_normalized_points(rows)

    assert [point["y"] for point in normalized] == [100.0, 125.0, 80.0]


def test_missing_long_short_values_handling(tmp_path: Path) -> None:
    db_path = tmp_path / "market.sqlite3"
    create_market_db(db_path)

    payload = read_snapshot(db_path, symbol="BTCUSDT")
    bybit = next(row for row in payload["latestMarketTable"] if row["exchange"] == "Bybit")

    assert bybit["longRatio"] is None
    assert bybit["shortRatio"] is None
    assert payload["marketSummary"]["averageLongRatio"] == 0.65


def test_dashboard_json_response_format(tmp_path: Path) -> None:
    db_path = tmp_path / "market.sqlite3"
    create_market_db(db_path)

    payload = read_snapshot(db_path, symbol="BTCUSDT")

    assert payload["selectedSymbol"] == "BTCUSDT"
    assert "marketSummary" in payload
    assert "latestMarketTable" in payload
    assert "charts" in payload
    assert {"exchangeOi", "exchangeVolume", "aggregatedOi", "aggregatedVolume"}.issubset(payload["charts"])


def test_sqlite_migration_backward_compatibility(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE market_samples (
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

    MarketMetricStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(market_samples)").fetchall()}

    assert {"normalized_symbol", "quote_volume_30m", "raw_json", "source", "source_symbol"}.issubset(columns)


def test_normalize_symbol() -> None:
    assert normalize_symbol("BTCUSDT") == "BTC-USDT"
    assert normalize_symbol("ETHUSDT") == "ETH-USDT"
    assert normalize_symbol("SOLUSDT") == "SOL-USDT"


def test_normalize_symbol_unknown_does_not_crash() -> None:
    assert normalize_symbol("UNKNOWN") == "UNKNOWN"
    assert normalize_symbol("") == ""


def test_market_sample_quote_volume_can_be_none_and_source_is_public_api(tmp_path: Path) -> None:
    db_path = tmp_path / "market.sqlite3"
    store = MarketMetricStore(db_path)
    metric = ExchangeMetric(
        exchange="Binance",
        source_symbol="BTCUSDT",
        oi_value=None,
        latest_volume=None,
    )

    store.save_market_sample("BTCUSDT", 1000, metric)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT normalized_symbol, quote_volume_30m, source, source_symbol
            FROM market_samples
            WHERE exchange = ? AND symbol = ?
            """,
            ("Binance", "BTCUSDT"),
        ).fetchone()

    assert row["normalized_symbol"] == "BTC-USDT"
    assert row["quote_volume_30m"] is None
    assert row["source"] == "public_market_api"
    assert row["source_symbol"] == "BTCUSDT"
