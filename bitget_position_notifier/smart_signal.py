from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_metrics import normalize_symbol


@dataclass(frozen=True)
class SmartSignalSample:
    observed_at: int
    source: str
    symbol: str
    normalized_symbol: str
    side: str | None = None
    avg_entry_price: Decimal | None = None
    current_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    unrealized_pnl_pct: Decimal | None = None
    position_size: Decimal | None = None
    profitable_ratio: Decimal | None = None
    raw_json: str | None = None


class SmartSignalClient:
    """Safe extension point for Binance Smart Money / Smart Signal data.

    Binance does not currently expose a documented, stable public API for the
    Smart Signal page that this project can rely on. To avoid login-gated or
    protected scraping, the client intentionally returns no samples until a
    stable public endpoint is confirmed.
    """

    def __init__(self, *, enabled: bool = False, timeout_seconds: float = 10.0) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds

    def fetch_current_positions(self, symbols: list[str]) -> list[SmartSignalSample]:
        if not self.enabled:
            return []
        return []


class SmartSignalStore:
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
                CREATE TABLE IF NOT EXISTS smart_signal_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    normalized_symbol TEXT NOT NULL,
                    side TEXT,
                    avg_entry_price TEXT,
                    current_price TEXT,
                    unrealized_pnl TEXT,
                    unrealized_pnl_pct TEXT,
                    position_size TEXT,
                    profitable_ratio TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_smart_signal_samples_lookup
                ON smart_signal_samples(normalized_symbol, observed_at)
                """
            )

    def save_samples(self, samples: list[SmartSignalSample]) -> None:
        def value(item: Decimal | None) -> str | None:
            return str(item) if item is not None else None

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO smart_signal_samples(
                    observed_at, source, symbol, normalized_symbol, side,
                    avg_entry_price, current_price, unrealized_pnl,
                    unrealized_pnl_pct, position_size, profitable_ratio, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample.observed_at,
                        sample.source,
                        sample.symbol,
                        sample.normalized_symbol or normalize_symbol(sample.symbol),
                        sample.side,
                        value(sample.avg_entry_price),
                        value(sample.current_price),
                        value(sample.unrealized_pnl),
                        value(sample.unrealized_pnl_pct),
                        value(sample.position_size),
                        value(sample.profitable_ratio),
                        sample.raw_json,
                    )
                    for sample in samples
                ],
            )


def sample_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "observedAt": row["observed_at"],
        "source": row["source"],
        "symbol": row["symbol"],
        "normalizedSymbol": row["normalized_symbol"],
        "side": row["side"],
        "avgEntryPrice": row["avg_entry_price"],
        "currentPrice": row["current_price"],
        "unrealizedPnl": row["unrealized_pnl"],
        "unrealizedPnlPct": row["unrealized_pnl_pct"],
        "positionSize": row["position_size"],
        "profitableRatio": row["profitable_ratio"],
    }
