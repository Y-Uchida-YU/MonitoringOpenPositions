from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
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
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_current_positions(self, symbols: list[str]) -> list[SmartSignalSample]:
        # No stable official Binance Smart Signal public endpoint is currently wired in.
        # Keep this stub empty to avoid scraping protected or login-gated page data.
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

    @staticmethod
    def _value(value: Decimal | str | None) -> str | None:
        return str(value) if value is not None else None

    def save_samples(self, samples: list[SmartSignalSample]) -> None:
        if not samples:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO smart_signal_samples(
                    observed_at, source, symbol, normalized_symbol, side,
                    avg_entry_price, current_price, unrealized_pnl, unrealized_pnl_pct,
                    position_size, profitable_ratio, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample.observed_at,
                        sample.source,
                        sample.symbol,
                        sample.normalized_symbol,
                        sample.side,
                        self._value(sample.avg_entry_price),
                        self._value(sample.current_price),
                        self._value(sample.unrealized_pnl),
                        self._value(sample.unrealized_pnl_pct),
                        self._value(sample.position_size),
                        self._value(sample.profitable_ratio),
                        sample.raw_json,
                    )
                    for sample in samples
                ],
            )


def sample_from_payload(payload: dict[str, Any], *, source: str = "binance_smart_signal") -> SmartSignalSample:
    symbol = str(payload.get("symbol") or "")
    observed_at = int(payload.get("observed_at") or time.time())
    raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return SmartSignalSample(
        observed_at=observed_at,
        source=source,
        symbol=symbol,
        normalized_symbol=normalize_symbol(symbol),
        side=payload.get("side") or payload.get("direction"),
        avg_entry_price=_decimal_or_none(payload.get("avg_entry_price")),
        current_price=_decimal_or_none(payload.get("mark_price") or payload.get("current_price")),
        unrealized_pnl=_decimal_or_none(payload.get("unrealized_pnl")),
        unrealized_pnl_pct=_decimal_or_none(payload.get("unrealized_pnl_pct") or payload.get("roi")),
        position_size=_decimal_or_none(payload.get("position_size")),
        profitable_ratio=_decimal_or_none(payload.get("profitable_ratio")),
        raw_json=raw_json,
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
