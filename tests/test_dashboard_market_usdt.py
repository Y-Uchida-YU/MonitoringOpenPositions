from __future__ import annotations

import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1] / "bitget_position_notifier"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import read_snapshot
from market_metrics import ExchangeMetric, MarketMetricStore, convert_to_usdt, normalize_symbol
from smart_signal import SmartSignalSample, SmartSignalStore


def test_normalize_symbol() -> None:
    assert normalize_symbol("BTCUSDT") == "BTC-USDT"
    assert normalize_symbol("ETHUSDT") == "ETH-USDT"
    assert normalize_symbol("SOLUSDT") == "SOL-USDT"
    assert normalize_symbol("UNKNOWN") == "UNKNOWN"


def test_usdt_conversion_rules() -> None:
    assert convert_to_usdt(Decimal("2"), "base", Decimal("30000")) == Decimal("60000")
    assert convert_to_usdt(Decimal("1200"), "quote", Decimal("30000")) == Decimal("1200")
    assert convert_to_usdt(Decimal("2"), "base", None) is None


def test_market_sample_migration_and_source_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "metrics.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE market_samples (
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                oi_value TEXT,
                volume_30m TEXT,
                PRIMARY KEY(exchange, symbol, observed_at)
            )
            """
        )
        conn.execute("INSERT INTO market_samples(exchange, symbol, observed_at, oi_value, volume_30m) VALUES('Legacy','BTCUSDT',1,'10','20')")

    store = MarketMetricStore(db_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(market_samples)")}
        assert "oi_usdt" in columns
        assert "volume_30m_usdt" in columns
        assert "conversion_source" in columns
        assert conn.execute("SELECT oi_value FROM market_samples WHERE exchange='Legacy'").fetchone()[0] == "10"

    metric = ExchangeMetric(
        exchange="Binance",
        source_symbol="BTCUSDT",
        oi_value=Decimal("60000"),
        latest_volume=Decimal("100000"),
        price_usdt=Decimal("30000"),
        oi_raw=Decimal("2"),
        oi_raw_unit="base",
        oi_usdt=Decimal("60000"),
        volume_30m_raw=Decimal("100000"),
        volume_30m_raw_unit="quote",
        volume_30m_usdt=Decimal("100000"),
        taker_buy_volume_raw=Decimal("3"),
        taker_buy_volume_raw_unit="base",
        taker_buy_volume_usdt=Decimal("90000"),
        taker_sell_volume_raw=Decimal("1"),
        taker_sell_volume_raw_unit="base",
        taker_sell_volume_usdt=Decimal("30000"),
        taker_buy_sell_ratio=Decimal("3"),
        conversion_source="binance_mark_price",
    )
    store.save_market_sample("BTCUSDT", 2, metric)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT normalized_symbol, quote_volume_30m, source, source_symbol FROM market_samples WHERE exchange='Binance'").fetchone()
    assert row == ("BTC-USDT", "100000", "public_market_api", "BTCUSDT")


def test_dashboard_prefers_usdt_and_aggregates(tmp_path: Path) -> None:
    db_path = tmp_path / "metrics.sqlite3"
    store = MarketMetricStore(db_path)
    store.save_market_sample(
        "BTCUSDT",
        1_800,
        ExchangeMetric(exchange="Binance", oi_value=Decimal("10"), latest_volume=Decimal("20"), oi_raw=Decimal("1"), oi_raw_unit="base", oi_usdt=Decimal("100"), volume_30m_raw=Decimal("5"), volume_30m_raw_unit="base", volume_30m_usdt=Decimal("50")),
    )
    store.save_market_sample(
        "BTCUSDT",
        2_400,
        ExchangeMetric(exchange="Binance", oi_value=Decimal("20"), latest_volume=Decimal("30"), oi_raw=Decimal("2"), oi_raw_unit="base", oi_usdt=Decimal("200"), volume_30m_raw=Decimal("6"), volume_30m_raw_unit="base", volume_30m_usdt=Decimal("60"), long_ratio=Decimal("0.55")),
    )
    store.save_market_sample(
        "BTCUSDT",
        2_400,
        ExchangeMetric(exchange="Bybit", oi_value=Decimal("40"), latest_volume=Decimal("70"), oi_raw=Decimal("4"), oi_raw_unit="base", oi_usdt=Decimal("400"), volume_30m_raw=Decimal("7"), volume_30m_raw_unit="base", volume_30m_usdt=Decimal("70"), taker_buy_sell_ratio=Decimal("1.2")),
    )

    snapshot = read_snapshot(db_path, symbol="BTCUSDT")
    assert snapshot["summaryCards"]["totalOiUsdt"] == 600.0
    assert snapshot["summaryCards"]["totalVolume30mUsdt"] == 130.0
    assert snapshot["latestTable"][0]["oiUsdt"] == 200.0
    assert snapshot["series"]["aggregated"][-1]["oi"] == 600.0
    assert snapshot["series"]["aggregated"][-1]["volume"] == 130.0


def test_dashboard_fallbacks_to_legacy_values(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    store = MarketMetricStore(db_path)
    store.save_market_sample("ETHUSDT", 1_800, ExchangeMetric(exchange="OKX", oi_value=Decimal("123"), latest_volume=Decimal("456")))
    snapshot = read_snapshot(db_path, symbol="ETHUSDT")
    assert snapshot["latestTable"][0]["oiUsdt"] == 123.0
    assert snapshot["latestTable"][0]["volume30mUsdt"] == 456.0


def test_smart_signal_disabled_and_sample_series(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "smart.sqlite3"
    store = MarketMetricStore(db_path)
    store.save_market_sample("SOLUSDT", 1_800, ExchangeMetric(exchange="Binance", oi_value=Decimal("10")))
    monkeypatch.setenv("ENABLE_BINANCE_SMART_SIGNAL", "false")
    disabled = read_snapshot(db_path, symbol="SOLUSDT")
    assert disabled["smartSignal"]["samples"] == []

    smart_store = SmartSignalStore(db_path)
    smart_store.save_samples([
        SmartSignalSample(
            observed_at=1_800,
            source="binance_smart_signal_stub",
            symbol="SOLUSDT",
            normalized_symbol="SOL-USDT",
            avg_entry_price=Decimal("150"),
            current_price=Decimal("155"),
            unrealized_pnl=Decimal("42"),
            unrealized_pnl_pct=Decimal("0.12"),
        )
    ])
    monkeypatch.setenv("ENABLE_BINANCE_SMART_SIGNAL", "true")
    enabled = read_snapshot(db_path, symbol="SOLUSDT")
    assert enabled["smartSignal"]["avgEntryPrice"][0]["y"] == 150.0
    assert enabled["smartSignal"]["unrealizedPnl"][0]["y"] == 42.0
