from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from market_metrics import normalize_symbol

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR / "data" / "market_metrics.sqlite3"
JST_OFFSET_SECONDS = 9 * 60 * 60
BUCKET_SECONDS = 10 * 60


def to_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def pct_change(current: Decimal | None, previous: Decimal | None) -> float | None:
    if current is None or previous in (None, Decimal("0")):
        return None
    return float(((current - previous) / previous) * Decimal("100"))


def jst_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp + JST_OFFSET_SECONDS, timezone.utc).strftime("%Y-%m-%d %H:%M")


def compact_decimal(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    abs_value = abs(value)
    for suffix, divisor in (("B", Decimal("1000000000")), ("M", Decimal("1000000")), ("K", Decimal("1000"))):
        if abs_value >= divisor:
            return f"{value / divisor:.2f}{suffix} USDT"
    return f"{value:.2f} USDT"


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone() is not None


def row_value(row: sqlite3.Row, columns: set[str], name: str) -> object | None:
    return row[name] if name in columns else None


def preferred_decimal(row: sqlite3.Row, columns: set[str], primary: str, fallback: str) -> Decimal | None:
    primary_value = to_decimal(row_value(row, columns, primary))
    return primary_value if primary_value is not None else to_decimal(row_value(row, columns, fallback))


def bucket_at(timestamp: int) -> int:
    return (timestamp // BUCKET_SECONDS) * BUCKET_SECONDS


def point(timestamp: int, value: Decimal | None) -> dict[str, object]:
    return {"x": jst_iso(timestamp), "y": as_float(value)}


def normalize_points(rows: list[sqlite3.Row], columns: set[str]) -> list[dict[str, object]]:
    first = next((preferred_decimal(row, columns, "oi_usdt", "oi_value") for row in rows if preferred_decimal(row, columns, "oi_usdt", "oi_value") not in (None, Decimal("0"))), None)
    output: list[dict[str, object]] = []
    for row in rows:
        value = preferred_decimal(row, columns, "oi_usdt", "oi_value")
        normalized = (value / first * Decimal("100")) if value is not None and first not in (None, Decimal("0")) else None
        output.append(point(int(row["observed_at"]), normalized))
    return output


def latest_smart_signal(conn: sqlite3.Connection, selected_symbol: str, limit: int) -> dict[str, object]:
    if not has_table(conn, "smart_signal_samples"):
        return empty_smart_signal(False)
    normalized = normalize_symbol(selected_symbol)
    rows = list(
        conn.execute(
            """
            SELECT * FROM smart_signal_samples
            WHERE normalized_symbol = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (normalized, limit),
        )
    )
    ordered = list(reversed(rows))
    return {
        "enabled": os.getenv("ENABLE_BINANCE_SMART_SIGNAL", "false").strip().lower() in {"1", "true", "yes", "on"},
        "note": "Smart Signal metrics depend on Binance's public availability. If no stable public API is available, this dashboard does not scrape protected or login-gated data.",
        "samples": [dict(row) for row in ordered],
        "avgEntryPrice": [point(int(row["observed_at"]), to_decimal(row["avg_entry_price"])) for row in ordered],
        "unrealizedPnl": [point(int(row["observed_at"]), to_decimal(row["unrealized_pnl"])) for row in ordered],
        "unrealizedPnlPct": [point(int(row["observed_at"]), to_decimal(row["unrealized_pnl_pct"])) for row in ordered],
    }


def empty_smart_signal(enabled: bool) -> dict[str, object]:
    return {
        "enabled": enabled,
        "note": "Smart Signal data is not available or disabled.",
        "samples": [],
        "avgEntryPrice": [],
        "unrealizedPnl": [],
        "unrealizedPnlPct": [],
    }


def empty_risk_scores() -> dict[str, object]:
    return {"selected": [], "latestBySymbol": [], "highest": None}


def latest_risk_scores(conn: sqlite3.Connection, selected_symbol: str) -> dict[str, object]:
    if not has_table(conn, "risk_scores"):
        return empty_risk_scores()

    rows = list(
        conn.execute(
            """
            SELECT r.*
            FROM risk_scores r
            INNER JOIN (
                SELECT normalized_symbol, side, MAX(observed_at) AS latest_at
                FROM risk_scores
                GROUP BY normalized_symbol, side
            ) latest ON latest.normalized_symbol = r.normalized_symbol
                AND latest.side = r.side AND latest.latest_at = r.observed_at
            ORDER BY r.score DESC, r.symbol, r.side
            """
        )
    )

    def serialize(row: sqlite3.Row) -> dict[str, object]:
        try:
            reasons = json.loads(row["reasons_json"] or "[]")
        except json.JSONDecodeError:
            reasons = []
        return {
            "symbol": row["symbol"],
            "side": row["side"],
            "score": row["score"],
            "level": row["level"],
            "pnlScore": row["pnl_score"],
            "crowdingScore": row["crowding_score"],
            "oiScore": row["oi_score"],
            "volumeScore": row["volume_score"],
            "takerScore": row["taker_score"],
            "dispersionScore": row["dispersion_score"],
            "reasons": reasons,
            "observedAt": jst_iso(int(row["observed_at"])),
        }

    serialized = [serialize(row) for row in rows]
    normalized_selected = normalize_symbol(selected_symbol)
    selected = [item for item, row in zip(serialized, rows) if row["normalized_symbol"] == normalized_selected]
    return {
        "selected": selected,
        "latestBySymbol": serialized,
        "highest": serialized[0] if serialized else None,
    }


def read_snapshot(db_path: Path, *, symbol: str | None = None, limit: int = 220) -> dict[str, object]:
    if load_dotenv is not None:
        load_dotenv(APP_DIR / ".env")
    smart_enabled = os.getenv("ENABLE_BINANCE_SMART_SIGNAL", "false").strip().lower() in {"1", "true", "yes", "on"}

    if not db_path.exists():
        return {
            "dbPath": str(db_path),
            "generatedAt": jst_iso(int(time.time())),
            "symbols": [],
            "selectedSymbol": symbol,
            "summaryCards": {},
            "series": {},
            "latestTable": [],
            "smartSignal": empty_smart_signal(smart_enabled),
            "riskScores": empty_risk_scores(),
            "error": "Database not found. Run main.py first to collect market samples.",
        }

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        table_name = (
            "market_samples"
            if has_table(conn, "market_samples")
            else ("oi_samples" if has_table(conn, "oi_samples") else None)
        )
        columns = table_columns(conn, table_name) if table_name is not None else set()
        symbol_values = (
            {row["symbol"] for row in conn.execute(f"SELECT DISTINCT symbol FROM {table_name}")}
            if table_name is not None
            else set()
        )
        if has_table(conn, "risk_scores"):
            symbol_values.update(row["symbol"] for row in conn.execute("SELECT DISTINCT symbol FROM risk_scores"))
        symbols = sorted(symbol_values)
        selected_symbol = symbol if symbol in symbols else (symbols[0] if symbols else None)
        if selected_symbol is None:
            return {
                "dbPath": str(db_path),
                "generatedAt": jst_iso(int(time.time())),
                "symbols": [],
                "selectedSymbol": None,
                "summaryCards": {},
                "series": {},
                "latestTable": [],
                "smartSignal": empty_smart_signal(smart_enabled),
                "riskScores": empty_risk_scores(),
                "error": "No current held symbols. Dashboard will populate after a position is opened.",
            }
        rows_desc = (
            list(conn.execute(f"SELECT * FROM {table_name} WHERE symbol = ? ORDER BY exchange, observed_at DESC", (selected_symbol,)))
            if table_name is not None
            else []
        )
        smart_signal = latest_smart_signal(conn, selected_symbol, limit) if smart_enabled else empty_smart_signal(False)
        risk_scores = latest_risk_scores(conn, selected_symbol)

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows_desc:
        grouped[row["exchange"]].append(row)

    exchange_oi_raw = []
    exchange_oi_normalized = []
    exchange_volume = []
    long_ratio_series = []
    short_ratio_series = []
    long_short_ratio_series = []
    taker_buy_series = []
    taker_sell_series = []
    taker_ratio_series = []
    latest_table = []
    latest_times: list[int] = []
    latest_oi_values: list[Decimal] = []
    previous_oi_values: list[Decimal] = []
    latest_volume_values: list[Decimal] = []
    previous_volume_values: list[Decimal] = []
    avg_long_ratios: list[float] = []
    avg_taker_ratios: list[float] = []
    aggregate_buckets: dict[int, dict[str, Decimal]] = defaultdict(lambda: {"oi": Decimal("0"), "volume": Decimal("0")})

    for exchange, exchange_rows_desc in sorted(grouped.items()):
        latest = exchange_rows_desc[0]
        previous = exchange_rows_desc[1] if len(exchange_rows_desc) > 1 else None
        latest_time = int(latest["observed_at"])
        latest_times.append(latest_time)
        latest_oi = preferred_decimal(latest, columns, "oi_usdt", "oi_value")
        previous_oi = preferred_decimal(previous, columns, "oi_usdt", "oi_value") if previous is not None else None
        latest_volume = preferred_decimal(latest, columns, "volume_30m_usdt", "volume_30m")
        previous_volume = preferred_decimal(previous, columns, "volume_30m_usdt", "volume_30m") if previous is not None else None
        if latest_oi is not None:
            latest_oi_values.append(latest_oi)
        if previous_oi is not None:
            previous_oi_values.append(previous_oi)
        if latest_volume is not None:
            latest_volume_values.append(latest_volume)
        if previous_volume is not None:
            previous_volume_values.append(previous_volume)

        ordered = list(reversed(exchange_rows_desc[:limit]))
        raw_points = [point(int(row["observed_at"]), preferred_decimal(row, columns, "oi_usdt", "oi_value")) for row in ordered]
        volume_points = [point(int(row["observed_at"]), preferred_decimal(row, columns, "volume_30m_usdt", "volume_30m")) for row in ordered]
        exchange_oi_raw.append({"exchange": exchange, "data": raw_points})
        exchange_oi_normalized.append({"exchange": exchange, "data": normalize_points(ordered, columns)})
        exchange_volume.append({"exchange": exchange, "data": volume_points})
        long_ratio_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), to_decimal(row_value(row, columns, "long_ratio"))) for row in ordered]})
        short_ratio_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), to_decimal(row_value(row, columns, "short_ratio"))) for row in ordered]})
        long_short_ratio_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), to_decimal(row_value(row, columns, "long_short_ratio"))) for row in ordered]})
        taker_buy_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), preferred_decimal(row, columns, "taker_buy_volume_usdt", "taker_buy_volume")) for row in ordered]})
        taker_sell_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), preferred_decimal(row, columns, "taker_sell_volume_usdt", "taker_sell_volume")) for row in ordered]})
        taker_ratio_series.append({"exchange": exchange, "data": [point(int(row["observed_at"]), to_decimal(row_value(row, columns, "taker_buy_sell_ratio"))) for row in ordered]})

        for row in ordered:
            bucket = bucket_at(int(row["observed_at"]))
            oi_value = preferred_decimal(row, columns, "oi_usdt", "oi_value")
            volume_value = preferred_decimal(row, columns, "volume_30m_usdt", "volume_30m")
            if oi_value is not None:
                aggregate_buckets[bucket]["oi"] += oi_value
            if volume_value is not None:
                aggregate_buckets[bucket]["volume"] += volume_value

        long_ratio = to_decimal(row_value(latest, columns, "long_ratio"))
        taker_ratio = to_decimal(row_value(latest, columns, "taker_buy_sell_ratio"))
        if long_ratio is not None:
            avg_long_ratios.append(float(long_ratio))
        if taker_ratio is not None:
            avg_taker_ratios.append(float(taker_ratio))
        latest_table.append({
            "exchange": exchange,
            "oiUsdt": as_float(latest_oi),
            "oiUsdtText": compact_decimal(latest_oi),
            "oiRaw": as_float(to_decimal(row_value(latest, columns, "oi_raw"))),
            "oiRawUnit": row_value(latest, columns, "oi_raw_unit"),
            "oiChangePct": pct_change(latest_oi, previous_oi),
            "volume30mUsdt": as_float(latest_volume),
            "volume30mUsdtText": compact_decimal(latest_volume),
            "volumeRaw": as_float(to_decimal(row_value(latest, columns, "volume_30m_raw"))),
            "volumeRawUnit": row_value(latest, columns, "volume_30m_raw_unit"),
            "volumeSpike": as_float(to_decimal(row_value(latest, columns, "volume_spike"))),
            "longRatio": as_float(long_ratio),
            "shortRatio": as_float(to_decimal(row_value(latest, columns, "short_ratio"))),
            "longShortRatio": as_float(to_decimal(row_value(latest, columns, "long_short_ratio"))),
            "takerBuyUsdt": as_float(preferred_decimal(latest, columns, "taker_buy_volume_usdt", "taker_buy_volume")),
            "takerSellUsdt": as_float(preferred_decimal(latest, columns, "taker_sell_volume_usdt", "taker_sell_volume")),
            "takerBuySellRatio": as_float(taker_ratio),
            "lastUpdatedJst": jst_iso(latest_time),
        })

    aggregate_points = [{"x": jst_iso(ts), "oi": as_float(values["oi"]), "volume": as_float(values["volume"])} for ts, values in sorted(aggregate_buckets.items())]
    total_oi = sum(latest_oi_values, Decimal("0")) if latest_oi_values else None
    previous_total_oi = sum(previous_oi_values, Decimal("0")) if previous_oi_values else None
    total_volume = sum(latest_volume_values, Decimal("0")) if latest_volume_values else None
    previous_total_volume = sum(previous_volume_values, Decimal("0")) if previous_volume_values else None

    return {
        "dbPath": str(db_path),
        "generatedAt": jst_iso(int(time.time())),
        "symbols": symbols,
        "selectedSymbol": selected_symbol,
        "latestSampleAt": jst_iso(max(latest_times)) if latest_times else None,
        "summaryCards": {
            "totalOiUsdt": as_float(total_oi),
            "totalOiUsdtText": compact_decimal(total_oi),
            "totalOiChangePct": pct_change(total_oi, previous_total_oi),
            "totalVolume30mUsdt": as_float(total_volume),
            "totalVolume30mUsdtText": compact_decimal(total_volume),
            "totalVolumeChangePct": pct_change(total_volume, previous_total_volume),
            "averageLongRatio": sum(avg_long_ratios) / len(avg_long_ratios) if avg_long_ratios else None,
            "averageTakerBuySellRatio": sum(avg_taker_ratios) / len(avg_taker_ratios) if avg_taker_ratios else None,
        },
        "series": {
            "exchangeOiRaw": exchange_oi_raw,
            "exchangeOiNormalized": exchange_oi_normalized,
            "exchangeVolume": exchange_volume,
            "aggregated": aggregate_points,
            "longRatio": long_ratio_series,
            "shortRatio": short_ratio_series,
            "longShortRatio": long_short_ratio_series,
            "takerBuy": taker_buy_series,
            "takerSell": taker_sell_series,
            "takerBuySellRatio": taker_ratio_series,
        },
        "latestTable": latest_table,
        "smartSignal": smart_signal,
        "riskScores": risk_scores,
        "error": None,
    }

HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Market Structure Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root { --ink:#18211d; --muted:#68766f; --paper:#f4f7f0; --panel:#fffdf7; --line:#dde5d8; --good:#0f8a62; --bad:#c5463d; --accent:#0c6b78; --gold:#a66a00; --shadow:0 18px 42px rgba(36,48,41,.11); }
    *{box-sizing:border-box} body{margin:0;color:var(--ink);font-family:"Segoe UI","Yu Gothic UI",sans-serif;background:radial-gradient(circle at top left,rgba(12,107,120,.16),transparent 34%),radial-gradient(circle at 92% 8%,rgba(166,106,0,.14),transparent 28%),linear-gradient(180deg,#fbfcf7,var(--paper));}
    header{position:sticky;top:0;z-index:5;background:rgba(244,247,240,.88);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:22px clamp(16px,3vw,40px)}
    .bar{display:flex;align-items:end;justify-content:space-between;gap:18px;flex-wrap:wrap}.eyebrow{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em}h1{margin:4px 0 8px;font-size:clamp(28px,4vw,48px);line-height:1}.sub{color:var(--muted);font-size:14px}.controls{display:flex;gap:10px;flex-wrap:wrap}select,button{height:40px;border:1px solid var(--line);border-radius:10px;padding:0 13px;background:var(--panel);font:inherit}button{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700;cursor:pointer}
    main{width:min(1480px,100%);margin:auto;padding:22px clamp(14px,3vw,40px) 44px}.cards{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;margin-bottom:16px}.card,.panel{background:rgba(255,253,247,.92);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.card{padding:15px}.card span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.card strong{display:block;margin-top:8px;font-size:clamp(18px,2vw,28px);line-height:1.05}.grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(360px,.8fr);gap:16px}.section{margin-top:16px}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 17px;border-bottom:1px solid var(--line)}.panel-head h2{margin:0;font-size:16px}.panel-head small{color:var(--muted)}.chart{height:360px;padding:14px}.chart.short{height:280px}.split{display:grid;grid-template-columns:1fr 1fr;gap:16px}.toggle{background:#fff;color:var(--accent);border-color:var(--accent)}
    table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{background:rgba(244,247,240,.78);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}.scroll{overflow:auto}.good{color:var(--good);font-weight:800}.bad{color:var(--bad);font-weight:800}.flat{color:var(--muted);font-weight:700}.heat{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:14px}.heat div{border:1px solid var(--line);border-radius:13px;padding:12px;background:#fff}.heat b{display:block}.heat span{font-size:24px;font-weight:850}.note{color:var(--muted);padding:14px 17px;line-height:1.55}.error{padding:18px;color:var(--bad);font-weight:800}.risk-grid{display:grid;grid-template-columns:minmax(210px,.46fr) minmax(0,1fr);gap:18px;padding:18px}.risk-score{display:flex;flex-direction:column;justify-content:center;border-radius:14px;padding:18px;color:#fff}.risk-score strong{font-size:48px;line-height:1}.risk-score span{font-weight:800;letter-spacing:.08em}.risk-LOW{background:#117e71}.risk-WATCH{background:#c38b12}.risk-HIGH{background:#df7924}.risk-CRITICAL{background:#c5463d}.risk-parts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}.risk-parts div{background:#f4f7f0;border-radius:9px;padding:9px;text-align:center}.risk-parts small{display:block;color:var(--muted)}.risk-reasons{margin:0;padding-left:20px;line-height:1.7}
    @media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}.grid,.split,.risk-grid{grid-template-columns:1fr}}@media(max-width:640px){header{position:static}.cards{grid-template-columns:1fr}.controls,select,button{width:100%}.chart{height:300px}.heat{grid-template-columns:1fr}.risk-parts{grid-template-columns:repeat(2,1fr)}}
  </style>
</head>
<body>
<header><div class="bar"><div><div class="eyebrow">Local Dashboard · Auto refresh 60s</div><h1>Market Structure</h1><div class="sub" id="subtitle">Loading...</div></div><div class="controls"><select id="symbolSelect"></select><button id="oiToggle" class="toggle">Raw OI USDT</button><button id="refreshButton">Refresh</button></div></div></header>
<main>
  <section class="cards">
    <div class="card"><span>Selected Symbol</span><strong id="cSymbol">-</strong></div><div class="card"><span>Highest Risk</span><strong id="cRisk">N/A</strong></div><div class="card"><span>Total OI USDT</span><strong id="cOi">-</strong></div><div class="card"><span>Total OI Change</span><strong id="cOiChg">-</strong></div><div class="card"><span>Total 30m Volume</span><strong id="cVol">-</strong></div><div class="card"><span>Volume Change</span><strong id="cVolChg">-</strong></div><div class="card"><span>Avg Long / Taker</span><strong id="cBias">-</strong></div>
  </section>
  <section class="panel section"><div class="panel-head"><h2>Position Risk Score</h2><small>Monitoring aid only; no automated trading action</small></div><div id="riskMount"></div></section>
  <section class="grid">
    <div>
      <div class="panel"><div class="panel-head"><h2>Exchange OI Trend</h2><small id="oiMode">Raw OI USDT</small></div><div class="chart"><canvas id="oiChart"></canvas></div></div>
      <div class="panel section"><div class="panel-head"><h2>Exchange Volume Trend</h2><small>30m Volume USDT</small></div><div class="chart short"><canvas id="volumeChart"></canvas></div></div>
      <div class="panel section"><div class="panel-head"><h2>Aggregated Market Trend</h2><small>10m bucket, no gap filling</small></div><div class="chart short"><canvas id="aggChart"></canvas></div></div>
    </div>
    <div>
      <div class="panel"><div class="panel-head"><h2>OI Change Heatmap</h2><small>Latest vs previous</small></div><div id="heat" class="heat"></div></div>
      <div class="panel section"><div class="panel-head"><h2>Latest Exchange Table</h2><small>USDT first, raw fallback</small></div><div id="table" class="scroll"></div></div>
    </div>
  </section>
  <section class="split section">
    <div class="panel"><div class="panel-head"><h2>Long/Short Structure</h2><small>Account ratio by exchange</small></div><div class="chart short"><canvas id="lsChart"></canvas></div></div>
    <div class="panel"><div class="panel-head"><h2>Long/Short Volume Trend</h2><small>Taker buy/sell volume, definitions vary by exchange</small></div><div class="note">Long/Short Volume is not standardized across exchanges. Where direct long/short volume is unavailable, this dashboard shows Taker Buy/Sell Volume in USDT.</div><div class="chart short"><canvas id="takerChart"></canvas></div></div>
  </section>
  <section class="panel section" id="smartPanel"><div class="panel-head"><h2>Binance Smart Money / Smart Signal</h2><small>Safe public endpoint only</small></div><div class="note" id="smartNote"></div><div class="chart short"><canvas id="smartChart"></canvas></div></section>
</main>
<script>
let charts={}, currentData=null, oiNormalized=false; const colors=["#0c6b78","#c45b32","#0f8a62","#a66a00","#56616b","#954c72","#2f6f3e"];
const fmt=n=>n==null||Number.isNaN(n)?"N/A":new Intl.NumberFormat("en-US",{maximumFractionDigits:2}).format(n);
const pct=n=>n==null||Number.isNaN(n)?"N/A":`${n>=0?"+":""}${n.toFixed(2)}%`; const cls=n=>n>0?"good":n<0?"bad":"flat"; const ratio=n=>n==null?"N/A":`${(n*100).toFixed(1)}%`;
function compact(n){if(n==null||Number.isNaN(n))return"N/A"; const a=Math.abs(n); if(a>=1e9)return`${(n/1e9).toFixed(2)}B USDT`; if(a>=1e6)return`${(n/1e6).toFixed(2)}M USDT`; if(a>=1e3)return`${(n/1e3).toFixed(2)}K USDT`; return`${n.toFixed(2)} USDT`;}
function datasets(series){return series.map((s,i)=>({label:s.exchange,data:s.data,borderColor:colors[i%colors.length],backgroundColor:colors[i%colors.length],borderWidth:2,pointRadius:0,tension:.25,spanGaps:false}));}
function chart(id, type, data, yTitle){if(charts[id])charts[id].destroy(); charts[id]=new Chart(document.getElementById(id),{type,data,options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},parsing:{xAxisKey:"x",yAxisKey:"y"},plugins:{legend:{position:"bottom",labels:{boxWidth:12,usePointStyle:true}},tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${fmt(c.parsed.y)}`}}},scales:{x:{type:"category",ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:8},grid:{display:false}},y:{title:{display:true,text:yTitle},grid:{color:"rgba(24,33,29,.08)"}}}}});}
function renderCharts(d){const oiSeries=oiNormalized?d.series.exchangeOiNormalized:d.series.exchangeOiRaw; document.getElementById("oiMode").textContent=oiNormalized?"Normalized OI (first sample = 100)":"Raw OI USDT"; document.getElementById("oiToggle").textContent=oiNormalized?"Normalized OI":"Raw OI USDT"; chart("oiChart","line",{datasets:datasets(oiSeries)},oiNormalized?"Normalized OI":"OI USDT"); chart("volumeChart","line",{datasets:datasets(d.series.exchangeVolume)},"30m Volume USDT"); chart("aggChart","line",{datasets:[{label:"Aggregated OI USDT",data:d.series.aggregated.map(p=>({x:p.x,y:p.oi})),borderColor:colors[0],backgroundColor:colors[0],borderWidth:2,pointRadius:0,tension:.25},{label:"Aggregated Volume USDT",data:d.series.aggregated.map(p=>({x:p.x,y:p.volume})),borderColor:colors[3],backgroundColor:colors[3],borderWidth:2,pointRadius:0,tension:.25}]},"USDT"); chart("lsChart","line",{datasets:[...datasets(d.series.longRatio),...datasets(d.series.shortRatio).map(x=>({...x,borderDash:[5,4]}))]},"Ratio"); chart("takerChart","line",{datasets:[...datasets(d.series.takerBuy),...datasets(d.series.takerSell).map(x=>({...x,borderDash:[5,4]}))]},"USDT"); const smart=d.smartSignal; document.getElementById("smartNote").textContent=smart.samples.length?smart.note:"Smart Signal data is not available or disabled. "+smart.note; chart("smartChart","line",{datasets:[{label:"Avg Entry Price",data:smart.avgEntryPrice,borderColor:colors[0],backgroundColor:colors[0],pointRadius:0},{label:"Unrealized PnL USDT",data:smart.unrealizedPnl,borderColor:colors[1],backgroundColor:colors[1],pointRadius:0},{label:"Unrealized PnL % / ROI",data:smart.unrealizedPnlPct,borderColor:colors[2],backgroundColor:colors[2],pointRadius:0}]},"Value");}
function renderTable(rows){document.getElementById("table").innerHTML=`<table><thead><tr><th>Exchange</th><th>OI USDT</th><th>OI raw</th><th>OI Chg</th><th>30m Vol USDT</th><th>Vol raw</th><th>Spike</th><th>Long</th><th>Short</th><th>Taker Buy</th><th>Taker Sell</th><th>Updated</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${r.exchange}</td><td>${r.oiUsdtText}</td><td>${fmt(r.oiRaw)} ${r.oiRawUnit||""}</td><td class="${cls(r.oiChangePct)}">${pct(r.oiChangePct)}</td><td>${r.volume30mUsdtText}</td><td>${fmt(r.volumeRaw)} ${r.volumeRawUnit||""}</td><td>${r.volumeSpike==null?"N/A":r.volumeSpike.toFixed(2)+"x"}</td><td>${ratio(r.longRatio)}</td><td>${ratio(r.shortRatio)}</td><td>${compact(r.takerBuyUsdt)}</td><td>${compact(r.takerSellUsdt)}</td><td>${r.lastUpdatedJst}</td></tr>`).join("")}</tbody></table>`;}
function renderHeat(rows){document.getElementById("heat").innerHTML=rows.map(r=>`<div><b>${r.exchange}</b><span class="${cls(r.oiChangePct)}">${pct(r.oiChangePct)}</span></div>`).join("")||`<div>No data</div>`;}
function renderRisk(data){const rows=data.selected||[]; if(!rows.length){document.getElementById("riskMount").innerHTML=`<div class="note">Risk score data is not available yet. It will appear after the next notifier cycle.</div>`; return;} const detail=rows.map(r=>`<div class="risk-grid"><div class="risk-score risk-${r.level}"><span>${r.level}</span><strong>${r.score}</strong><small>/ 100 · ${r.symbol} ${r.side}</small></div><div><div class="risk-parts"><div><small>PnL</small>${r.pnlScore}</div><div><small>Crowding</small>${r.crowdingScore}</div><div><small>OI</small>${r.oiScore}</div><div><small>Volume</small>${r.volumeScore}</div><div><small>Taker</small>${r.takerScore}</div><div><small>Dispersion</small>${r.dispersionScore}</div></div><ul class="risk-reasons">${r.reasons.map(reason=>`<li>${reason}</li>`).join("")}</ul></div></div>`).join(""); const overview=`<div class="scroll"><table><thead><tr><th>Held Position</th><th>Side</th><th>Level</th><th>Score</th><th>Observed JST</th></tr></thead><tbody>${data.latestBySymbol.map(r=>`<tr><td>${r.symbol}</td><td>${r.side}</td><td class="${r.level==="LOW"?"good":r.level==="WATCH"?"flat":"bad"}">${r.level}</td><td>${r.score}/100</td><td>${r.observedAt}</td></tr>`).join("")}</tbody></table></div>`; document.getElementById("riskMount").innerHTML=detail+overview;}
async function load(symbol){const qs=symbol?`?symbol=${encodeURIComponent(symbol)}&ts=${Date.now()}`:`?ts=${Date.now()}`; const d=await (await fetch(`/api/snapshot${qs}`,{cache:"no-store"})).json(); currentData=d; const sel=document.getElementById("symbolSelect"); sel.innerHTML=d.symbols.map(s=>`<option value="${s}">${s}</option>`).join(""); if(d.selectedSymbol)sel.value=d.selectedSymbol; if(d.error){document.getElementById("subtitle").textContent=d.error; return;} document.getElementById("subtitle").textContent=`${d.selectedSymbol} · latest ${d.latestSampleAt||"N/A"} JST · generated ${d.generatedAt} JST`; const c=d.summaryCards; const risk=d.riskScores.highest; document.getElementById("cSymbol").textContent=d.selectedSymbol; document.getElementById("cRisk").textContent=risk?`${risk.level} ${risk.score}`:"N/A"; document.getElementById("cRisk").className=risk&&risk.level==="CRITICAL"?"bad":risk&&risk.level==="HIGH"?"bad":risk&&risk.level==="LOW"?"good":"flat"; document.getElementById("cOi").textContent=c.totalOiUsdtText; document.getElementById("cOiChg").textContent=pct(c.totalOiChangePct); document.getElementById("cOiChg").className=cls(c.totalOiChangePct); document.getElementById("cVol").textContent=c.totalVolume30mUsdtText; document.getElementById("cVolChg").textContent=pct(c.totalVolumeChangePct); document.getElementById("cVolChg").className=cls(c.totalVolumeChangePct); document.getElementById("cBias").textContent=`${ratio(c.averageLongRatio)} / ${fmt(c.averageTakerBuySellRatio)}`; renderRisk(d.riskScores); renderCharts(d); renderTable(d.latestTable); renderHeat(d.latestTable);}
document.getElementById("symbolSelect").addEventListener("change",e=>load(e.target.value)); document.getElementById("refreshButton").addEventListener("click",()=>load(document.getElementById("symbolSelect").value)); document.getElementById("oiToggle").addEventListener("click",()=>{oiNormalized=!oiNormalized;if(currentData)renderCharts(currentData);}); load(); setInterval(()=>load(document.getElementById("symbolSelect").value),60000);
</script>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML)
            return
        if parsed.path == "/api/snapshot":
            query = parse_qs(parsed.query)
            symbol = query.get("symbol", [None])[0]
            payload = read_snapshot(self.db_path, symbol=symbol)
            self._send_json(payload)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} - {fmt % args}")

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local dashboard for market structure samples.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    DashboardHandler.db_path = Path(args.db)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print(f"Reading database: {DashboardHandler.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
