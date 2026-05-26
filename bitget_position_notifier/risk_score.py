from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import pstdev
from typing import Any

from market_metrics import SymbolMarketMetrics, normalize_symbol


@dataclass(frozen=True)
class RiskScoreResult:
    symbol: str
    side: str
    score: int
    level: str
    pnl_score: int
    crowding_score: int
    oi_score: int
    volume_score: int
    taker_score: int
    dispersion_score: int
    reasons: list[str]


def optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def calculate_position_pnl_pct(position: dict[str, Any]) -> Decimal | None:
    entry = optional_decimal(position.get("openPriceAvg"))
    mark = optional_decimal(position.get("markPrice"))
    leverage = optional_decimal(position.get("leverage"))
    if entry is None or entry <= 0 or mark is None:
        return None
    leverage = leverage if leverage is not None and leverage > 0 else Decimal("1")
    side = str(position.get("holdSide") or "").lower()
    change = (entry - mark) / entry if side == "short" else (mark - entry) / entry
    return change * Decimal("100") * leverage


def _values(metrics: SymbolMarketMetrics | None, name: str) -> list[Decimal]:
    if metrics is None:
        return []
    return [
        value
        for metric in metrics.exchange_metrics
        if (value := getattr(metric, name, None)) is not None
    ]


def _average(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def average_oi_change_pct(metrics: SymbolMarketMetrics | None) -> Decimal | None:
    return _average(_values(metrics, "oi_change_pct"))


def average_volume_spike(metrics: SymbolMarketMetrics | None) -> Decimal | None:
    return _average(_values(metrics, "volume_spike"))


def average_long_ratio(metrics: SymbolMarketMetrics | None) -> Decimal | None:
    return _average(_values(metrics, "long_ratio"))


def taker_buy_sell_ratio(metrics: SymbolMarketMetrics | None) -> Decimal | None:
    if metrics is None:
        return None
    binance = next((item for item in metrics.exchange_metrics if item.exchange == "Binance"), None)
    if binance is not None and binance.taker_buy_sell_ratio is not None:
        return binance.taker_buy_sell_ratio
    return _average(_values(metrics, "taker_buy_sell_ratio"))


def oi_change_dispersion(metrics: SymbolMarketMetrics | None) -> Decimal | None:
    values = _values(metrics, "oi_change_pct")
    if len(values) < 2:
        return None
    return Decimal(str(pstdev(float(value) for value in values)))


def risk_level_from_score(score: int) -> str:
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "WATCH"
    return "LOW"


def _pnl_score(value: Decimal | None) -> int:
    if value is None:
        return 12
    if value >= Decimal("20"):
        return 5
    if value >= Decimal("5"):
        return 10
    if value >= Decimal("-5"):
        return 12
    if value >= Decimal("-15"):
        return 18
    if value >= Decimal("-30"):
        return 22
    return 25


def _crowding_score(side: str, ratio: Decimal | None) -> int:
    if ratio is None:
        return 8
    if side == "short":
        if ratio <= Decimal("0.30"):
            return 25
        if ratio <= Decimal("0.35"):
            return 20
        if ratio <= Decimal("0.40"):
            return 15
        if ratio <= Decimal("0.45"):
            return 10
        return 5
    if ratio >= Decimal("0.70"):
        return 25
    if ratio >= Decimal("0.65"):
        return 20
    if ratio >= Decimal("0.60"):
        return 15
    if ratio >= Decimal("0.55"):
        return 10
    return 5


def _oi_score(value: Decimal | None) -> int:
    if value is None:
        return 6
    if value >= Decimal("15"):
        return 20
    if value >= Decimal("10"):
        return 16
    if value >= Decimal("5"):
        return 12
    if value >= Decimal("0"):
        return 7
    if value >= Decimal("-5"):
        return 4
    return 2


def _volume_score(value: Decimal | None) -> int:
    if value is None:
        return 5
    if value >= Decimal("3"):
        return 15
    if value >= Decimal("2"):
        return 12
    if value >= Decimal("1.5"):
        return 9
    if value >= Decimal("1"):
        return 6
    return 3


def _taker_score(side: str, value: Decimal | None) -> int:
    if value is None:
        return 4
    if side == "short":
        if value <= Decimal("0.5"):
            return 10
        if value <= Decimal("0.67"):
            return 8
        if value <= Decimal("0.83"):
            return 6
        if value <= Decimal("1.25"):
            return 4
        return 2
    if value >= Decimal("2"):
        return 10
    if value >= Decimal("1.5"):
        return 8
    if value >= Decimal("1.2"):
        return 6
    if value >= Decimal("0.8"):
        return 4
    return 2


def _dispersion_score(value: Decimal | None) -> int:
    if value is None:
        return 1
    if value >= Decimal("10"):
        return 5
    if value >= Decimal("5"):
        return 3
    return 1


def calculate_position_risk(
    position: dict[str, Any],
    market_metrics: SymbolMarketMetrics | None,
) -> RiskScoreResult:
    symbol = str(position.get("symbol") or "N/A")
    side = str(position.get("holdSide") or "long").lower()
    pnl_pct = calculate_position_pnl_pct(position)
    long_ratio = average_long_ratio(market_metrics)
    oi_change = average_oi_change_pct(market_metrics)
    volume_spike = average_volume_spike(market_metrics)
    taker_ratio = taker_buy_sell_ratio(market_metrics)
    dispersion = oi_change_dispersion(market_metrics)

    pnl_score = _pnl_score(pnl_pct)
    crowding_score = _crowding_score(side, long_ratio)
    oi_score = _oi_score(oi_change)
    volume_score = _volume_score(volume_spike)
    taker_score = _taker_score(side, taker_ratio)
    dispersion_score = _dispersion_score(dispersion)
    score = min(100, pnl_score + crowding_score + oi_score + volume_score + taker_score + dispersion_score)

    reasons: list[str] = []
    if pnl_pct is not None and pnl_pct < Decimal("-5"):
        reasons.append(f"Leveraged PnL is {pnl_pct:.1f}%, loss is expanding.")
    if long_ratio is not None and crowding_score >= 15:
        direction = "Long" if side != "short" else "Short"
        reasons.append(f"{direction} crowding is high: avg long ratio {long_ratio * Decimal('100'):.1f}%.")
    if oi_change is not None and oi_score >= 12:
        reasons.append(f"OI expanded {oi_change:+.1f}% across monitored exchanges.")
    if volume_spike is not None and volume_score >= 9:
        reasons.append(f"Volume spike is {volume_spike:.1f}x vs recent average.")
    if taker_ratio is not None and taker_score >= 6:
        flow = "long" if side != "short" else "short"
        reasons.append(f"Taker buy/sell ratio shows aggressive {flow} flow ({taker_ratio:.2f}).")
    if dispersion is not None and dispersion_score >= 3:
        reasons.append(f"Exchange OI dispersion is elevated ({dispersion:.1f}).")
    if not reasons:
        reasons.append("No elevated risk trigger detected; continue routine monitoring.")

    return RiskScoreResult(
        symbol=symbol,
        side=side,
        score=score,
        level=risk_level_from_score(score),
        pnl_score=pnl_score,
        crowding_score=crowding_score,
        oi_score=oi_score,
        volume_score=volume_score,
        taker_score=taker_score,
        dispersion_score=dispersion_score,
        reasons=reasons[:5],
    )


class RiskScoreStore:
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
                CREATE TABLE IF NOT EXISTS risk_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    normalized_symbol TEXT NOT NULL,
                    side TEXT,
                    score INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    pnl_score INTEGER,
                    crowding_score INTEGER,
                    oi_score INTEGER,
                    volume_score INTEGER,
                    taker_score INTEGER,
                    dispersion_score INTEGER,
                    reasons_json TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_risk_scores_lookup ON risk_scores(normalized_symbol, observed_at)"
            )

    def save(self, observed_at: int, result: RiskScoreResult, *, raw_json: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_scores(
                    observed_at, symbol, normalized_symbol, side, score, level,
                    pnl_score, crowding_score, oi_score, volume_score, taker_score,
                    dispersion_score, reasons_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    result.symbol,
                    normalize_symbol(result.symbol),
                    result.side,
                    result.score,
                    result.level,
                    result.pnl_score,
                    result.crowding_score,
                    result.oi_score,
                    result.volume_score,
                    result.taker_score,
                    result.dispersion_score,
                    json.dumps(result.reasons, ensure_ascii=False),
                    raw_json or json.dumps(asdict(result), ensure_ascii=False),
                ),
            )
            cutoff = observed_at - 7 * 24 * 60 * 60
            conn.execute("DELETE FROM risk_scores WHERE observed_at < ?", (cutoff,))
