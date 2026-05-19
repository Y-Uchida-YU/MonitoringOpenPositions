from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR / "data" / "market_metrics.sqlite3"
JST_OFFSET_SECONDS = 9 * 60 * 60
DEFAULT_BUCKET_SECONDS = 10 * 60

MARKET_SAMPLE_COLUMNS: dict[str, str] = {
    "normalized_symbol": "TEXT",
    "quote_volume_30m": "TEXT",
    "raw_json": "TEXT",
    "source": "TEXT",
    "source_symbol": "TEXT",
}


def to_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def pct_change(current: Decimal | None, previous: Decimal | None) -> float | None:
    if current is None or previous in (None, Decimal("0")):
        return None
    return float(((current - previous) / previous) * Decimal("100"))


def jst_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp + JST_OFFSET_SECONDS, timezone.utc).strftime("%Y-%m-%d %H:%M")


def bucket_timestamp(timestamp: int, bucket_seconds: int = DEFAULT_BUCKET_SECONDS) -> int:
    return (timestamp // bucket_seconds) * bucket_seconds


def ensure_market_samples_migration(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'market_samples'"
    ).fetchone()
    if table is None:
        return

    existing = {row[1] for row in conn.execute("PRAGMA table_info(market_samples)").fetchall()}
    for column_name, column_type in MARKET_SAMPLE_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE market_samples ADD COLUMN {column_name} {column_type}")


def row_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else None
    return row.get(key)


def point(timestamp: int, value: Decimal | None) -> dict[str, Any]:
    return {"x": jst_iso(timestamp), "y": decimal_to_float(value)}


def calculate_normalized_points(rows: list[sqlite3.Row | dict[str, Any]], value_key: str = "oi_value") -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: int(row_value(row, "observed_at")))
    first_value = next((to_decimal(row_value(row, value_key)) for row in ordered if to_decimal(row_value(row, value_key))), None)
    if first_value in (None, Decimal("0")):
        return []

    normalized: list[dict[str, Any]] = []
    for row in ordered:
        value = to_decimal(row_value(row, value_key))
        if value is None:
            continue
        normalized.append(point(int(row_value(row, "observed_at")), (value / first_value) * Decimal("100")))
    return normalized


def build_exchange_series(
    grouped_rows: dict[str, list[sqlite3.Row]],
    *,
    value_key: str,
    normalized: bool = False,
) -> list[dict[str, Any]]:
    series = []
    for exchange, rows in sorted(grouped_rows.items()):
        ordered = sorted(rows, key=lambda row: int(row["observed_at"]))
        data = calculate_normalized_points(ordered, value_key) if normalized else [
            point(int(row["observed_at"]), to_decimal(row_value(row, value_key)))
            for row in ordered
            if to_decimal(row_value(row, value_key)) is not None
        ]
        if data:
            series.append({"exchange": exchange, "data": data})
    return series


def build_aggregated_series(
    rows: list[sqlite3.Row],
    *,
    value_key: str,
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
) -> list[dict[str, Any]]:
    buckets: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        value = to_decimal(row_value(row, value_key))
        if value is None:
            continue
        buckets[bucket_timestamp(int(row["observed_at"]), bucket_seconds)] += value
    return [point(timestamp, value) for timestamp, value in sorted(buckets.items())]


def aggregate_latest_total(summary: list[dict[str, Any]], key: str) -> Decimal | None:
    values = [to_decimal(item.get(key)) for item in summary]
    values = [value for value in values if value is not None]
    return sum(values, Decimal("0")) if values else None


def aggregate_series_change(series: list[dict[str, Any]]) -> float | None:
    if len(series) < 2:
        return None
    current = to_decimal(series[-1]["y"])
    previous = to_decimal(series[-2]["y"])
    return pct_change(current, previous)


def average_latest(summary: list[dict[str, Any]], key: str) -> float | None:
    values = [item.get(key) for item in summary if item.get(key) is not None]
    return sum(float(value) for value in values) / len(values) if values else None


def read_rows(db_path: Path, selected_symbol: str | None) -> tuple[list[str], str | None, list[sqlite3.Row], bool]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_market_samples_migration(conn)
        has_market_samples = (
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'market_samples'").fetchone()
            is not None
        )
        table_name = "market_samples" if has_market_samples else "oi_samples"
        symbols = [row["symbol"] for row in conn.execute(f"SELECT DISTINCT symbol FROM {table_name} ORDER BY symbol")]
        symbol = selected_symbol if selected_symbol in symbols else (symbols[0] if symbols else None)
        if symbol is None:
            return symbols, None, [], has_market_samples
        rows = list(
            conn.execute(
                f"SELECT * FROM {table_name} WHERE symbol = ? ORDER BY observed_at ASC, exchange ASC",
                (symbol,),
            )
        )
    return symbols, symbol, rows, has_market_samples


def build_latest_summary(grouped_rows: dict[str, list[sqlite3.Row]], has_market_samples: bool) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for exchange, rows in sorted(grouped_rows.items()):
        ordered = sorted(rows, key=lambda row: int(row["observed_at"]))
        latest = ordered[-1]
        previous = ordered[-2] if len(ordered) > 1 else None
        latest_oi = to_decimal(row_value(latest, "oi_value"))
        previous_oi = to_decimal(row_value(previous, "oi_value")) if previous else None
        summary.append(
            {
                "exchange": exchange,
                "latestOi": decimal_to_float(latest_oi),
                "oiChangePct": pct_change(latest_oi, previous_oi),
                "latestVolume30m": decimal_to_float(to_decimal(row_value(latest, "volume_30m"))) if has_market_samples else None,
                "volumeSpike": decimal_to_float(to_decimal(row_value(latest, "volume_spike"))) if has_market_samples else None,
                "longRatio": decimal_to_float(to_decimal(row_value(latest, "long_ratio"))) if has_market_samples else None,
                "shortRatio": decimal_to_float(to_decimal(row_value(latest, "short_ratio"))) if has_market_samples else None,
                "longShortRatio": decimal_to_float(to_decimal(row_value(latest, "long_short_ratio"))) if has_market_samples else None,
                "takerBuyVolume": decimal_to_float(to_decimal(row_value(latest, "taker_buy_volume"))) if has_market_samples else None,
                "takerSellVolume": decimal_to_float(to_decimal(row_value(latest, "taker_sell_volume"))) if has_market_samples else None,
                "takerBuySellRatio": decimal_to_float(to_decimal(row_value(latest, "taker_buy_sell_ratio"))) if has_market_samples else None,
                "topAccountLongRatio": decimal_to_float(to_decimal(row_value(latest, "top_account_long_ratio"))) if has_market_samples else None,
                "topPositionLongRatio": decimal_to_float(to_decimal(row_value(latest, "top_position_long_ratio"))) if has_market_samples else None,
                "samples": len(ordered),
                "lastUpdatedJst": jst_iso(int(latest["observed_at"])),
            }
        )
    return summary


def read_snapshot(db_path: Path, *, symbol: str | None = None, limit: int = 240) -> dict[str, object]:
    if not db_path.exists():
        return {
            "dbPath": str(db_path),
            "generatedAt": jst_iso(int(time.time())),
            "symbols": [],
            "selectedSymbol": symbol,
            "error": "Database not found. Run main.py first to collect market samples.",
        }

    symbols, selected_symbol, rows, has_market_samples = read_rows(db_path, symbol)
    if selected_symbol is None:
        return {
            "dbPath": str(db_path),
            "generatedAt": jst_iso(int(time.time())),
            "symbols": symbols,
            "selectedSymbol": None,
            "error": "No samples yet. Wait until the notifier collects at least one market sample.",
        }

    recent_rows = rows[-limit * max(1, len({row["exchange"] for row in rows})) :]
    grouped_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in recent_rows:
        grouped_rows[row["exchange"]].append(row)

    latest_summary = build_latest_summary(grouped_rows, has_market_samples)
    exchange_oi_raw = build_exchange_series(grouped_rows, value_key="oi_value")
    exchange_oi_normalized = build_exchange_series(grouped_rows, value_key="oi_value", normalized=True)
    exchange_volume = build_exchange_series(grouped_rows, value_key="volume_30m") if has_market_samples else []
    aggregated_oi = build_aggregated_series(recent_rows, value_key="oi_value")
    aggregated_volume = build_aggregated_series(recent_rows, value_key="volume_30m") if has_market_samples else []
    long_ratio = build_exchange_series(grouped_rows, value_key="long_ratio") if has_market_samples else []
    short_ratio = build_exchange_series(grouped_rows, value_key="short_ratio") if has_market_samples else []
    long_short_ratio = build_exchange_series(grouped_rows, value_key="long_short_ratio") if has_market_samples else []
    taker_buy_volume = build_exchange_series(grouped_rows, value_key="taker_buy_volume") if has_market_samples else []
    taker_sell_volume = build_exchange_series(grouped_rows, value_key="taker_sell_volume") if has_market_samples else []
    taker_buy_sell_ratio = build_exchange_series(grouped_rows, value_key="taker_buy_sell_ratio") if has_market_samples else []

    latest_times = [int(row["observed_at"]) for row in recent_rows]
    total_oi = aggregate_latest_total(latest_summary, "latestOi")
    total_volume = aggregate_latest_total(latest_summary, "latestVolume30m")

    return {
        "dbPath": str(db_path),
        "generatedAt": jst_iso(int(time.time())),
        "symbols": symbols,
        "selectedSymbol": selected_symbol,
        "latestSampleAt": jst_iso(max(latest_times)) if latest_times else None,
        "hasMarketSamples": has_market_samples,
        "marketSummary": {
            "totalAggregatedOi": decimal_to_float(total_oi),
            "aggregatedOiChangePct": aggregate_series_change(aggregated_oi),
            "totalAggregatedVolume30m": decimal_to_float(total_volume),
            "aggregatedVolumeChangePct": aggregate_series_change(aggregated_volume),
            "averageLongRatio": average_latest(latest_summary, "longRatio"),
            "averageTakerBuySellRatio": average_latest(latest_summary, "takerBuySellRatio"),
        },
        "latestMarketTable": latest_summary,
        "charts": {
            "exchangeOi": {"raw": exchange_oi_raw, "normalized": exchange_oi_normalized},
            "exchangeVolume": exchange_volume,
            "aggregatedOi": aggregated_oi,
            "aggregatedVolume": aggregated_volume,
            "longShortTraderRatio": {
                "longRatio": long_ratio,
                "shortRatio": short_ratio,
                "longShortRatio": long_short_ratio,
            },
            "longShortVolume": {
                "takerBuyVolume": taker_buy_volume,
                "takerSellVolume": taker_sell_volume,
                "takerBuySellRatio": taker_buy_sell_ratio,
            },
        },
        "notes": [
            "Long/Short Volume is represented by taker buy/sell volume when available.",
            "Definitions differ by exchange; unavailable metrics are shown as N/A.",
        ],
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
    :root {
      --ink: #17201c;
      --muted: #66736d;
      --paper: #f6f7f3;
      --panel: #ffffff;
      --line: #dce3d7;
      --good: #11835b;
      --bad: #c23b3b;
      --accent: #0d6a7d;
      --accent-2: #7a5c19;
      --shadow: 0 14px 34px rgba(43, 54, 48, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", "Yu Gothic UI", sans-serif;
      background:
        linear-gradient(135deg, rgba(13, 106, 125, .09), transparent 32%),
        linear-gradient(225deg, rgba(122, 92, 25, .10), transparent 30%),
        var(--paper);
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      padding: 24px clamp(16px, 3vw, 40px);
      border-bottom: 1px solid var(--line);
      background: rgba(246, 247, 243, .9);
      backdrop-filter: blur(12px);
    }
    .topline, .controls, .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }
    h1 { margin: 0 0 6px; font-size: clamp(28px, 4vw, 44px); line-height: 1; }
    .subtitle, .note, .section-head span { color: var(--muted); font-size: 13px; }
    select, button {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }
    button { background: var(--accent); border-color: var(--accent); color: white; font-weight: 700; cursor: pointer; }
    main { width: min(1520px, 100%); margin: 0 auto; padding: 22px clamp(14px, 3vw, 40px) 48px; }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, .panel, .notice {
      background: rgba(255,255,255,.92);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .metric { padding: 15px; }
    .metric span { display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .metric strong { display: block; margin-top: 8px; font-size: 23px; line-height: 1.15; }
    .section { margin-top: 16px; }
    .section-head { margin: 0 0 10px; }
    .section-head h2 { margin: 0; font-size: 18px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .panel { overflow: hidden; }
    .panel-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      align-items: center;
    }
    .panel-title h3 { margin: 0; font-size: 15px; }
    .chart-wrap { height: 340px; padding: 14px; }
    .wide .chart-wrap { height: 390px; }
    .toggle {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      background: #fff;
    }
    .toggle button { border: 0; border-radius: 0; background: transparent; color: var(--ink); min-height: 32px; }
    .toggle button.active { background: var(--accent); color: #fff; }
    .notice { padding: 13px 15px; margin-top: 14px; color: var(--muted); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; background: rgba(246,247,243,.9); }
    .good { color: var(--good); font-weight: 750; }
    .bad { color: var(--bad); font-weight: 750; }
    .flat { color: var(--muted); font-weight: 650; }
    .error { padding: 18px; color: var(--bad); font-weight: 700; }
    @media (max-width: 1050px) {
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid-2 { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      header { position: static; }
      .summary-grid { grid-template-columns: 1fr; }
      .controls, select, button { width: 100%; }
      .chart-wrap, .wide .chart-wrap { height: 300px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topline">
      <div>
        <h1>Market Structure Dashboard</h1>
        <div class="subtitle" id="subtitle">Loading market structure...</div>
      </div>
      <div class="controls">
        <select id="symbolSelect" aria-label="Symbol"></select>
        <button id="refreshButton">Refresh</button>
      </div>
    </div>
  </header>
  <main>
    <section class="summary-grid">
      <div class="metric"><span>Symbol</span><strong id="metricSymbol">-</strong></div>
      <div class="metric"><span>Latest Sample</span><strong id="metricLatest">-</strong></div>
      <div class="metric"><span>Total Aggregated OI</span><strong id="metricTotalOi">-</strong></div>
      <div class="metric"><span>Aggregated OI Change</span><strong id="metricOiChange">-</strong></div>
      <div class="metric"><span>Total 30m Volume</span><strong id="metricTotalVolume">-</strong></div>
      <div class="metric"><span>Aggregated Volume Change</span><strong id="metricVolumeChange">-</strong></div>
      <div class="metric"><span>Average Long Ratio</span><strong id="metricLongRatio">-</strong></div>
      <div class="metric"><span>Avg Taker Buy/Sell</span><strong id="metricTakerRatio">-</strong></div>
    </section>

    <section class="section">
      <div class="section-head">
        <h2>Exchange Trends</h2>
        <div class="toggle" aria-label="OI scale">
          <button id="rawOiButton" class="active">Raw OI</button>
          <button id="normalizedOiButton">Normalized OI</button>
        </div>
      </div>
      <div class="grid-2">
        <div class="panel wide">
          <div class="panel-title"><h3>Exchange OI Trend</h3><span>Series: exchange</span></div>
          <div class="chart-wrap"><canvas id="exchangeOiChart"></canvas></div>
        </div>
        <div class="panel wide">
          <div class="panel-title"><h3>Exchange Volume Trend</h3><span>30m volume</span></div>
          <div class="chart-wrap"><canvas id="exchangeVolumeChart"></canvas></div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Aggregated Market</h2><span>Bucketed by 10 minutes, no missing-value fill</span></div>
      <div class="grid-2">
        <div class="panel">
          <div class="panel-title"><h3>Aggregated OI Trend</h3><span>Sum of monitored exchanges</span></div>
          <div class="chart-wrap"><canvas id="aggregatedOiChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="panel-title"><h3>Aggregated Volume Trend</h3><span>Sum of 30m volume</span></div>
          <div class="chart-wrap"><canvas id="aggregatedVolumeChart"></canvas></div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Long/Short Structure</h2><span>Only exchanges with available public metrics are plotted</span></div>
      <div class="grid-2">
        <div class="panel">
          <div class="panel-title"><h3>Long/Short Trader Ratio Trend</h3><span>long, short, long/short ratio</span></div>
          <div class="chart-wrap"><canvas id="longShortRatioChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="panel-title"><h3>Long/Short Volume Trend</h3><span>Taker buy/sell proxy</span></div>
          <div class="chart-wrap"><canvas id="takerVolumeChart"></canvas></div>
        </div>
      </div>
      <div class="notice">
        Long/Short Volume is shown with Taker Buy Volume, Taker Sell Volume, and Buy/Sell Ratio when available.
        This is not identical across exchanges, and definitions differ by venue.
        Binance Smart Money / Smart Trader Metrics are shown as Global Long/Short Account Ratio,
        Top Trader Account Long/Short Ratio, Top Trader Position Long/Short Ratio, and Taker Buy/Sell Volume.
      </div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Latest Market Table</h2><span id="tableNote">Latest sample by exchange</span></div>
      <div class="panel table-wrap" id="tableMount"></div>
    </section>
  </main>
  <script>
    let dashboardData = null;
    let oiMode = "raw";
    let loading = false;
    const charts = {};
    const colors = ["#0d6a7d", "#b14b2c", "#11835b", "#7a5c19", "#5c6470", "#8f3f65", "#2f6f3e", "#365a9b"];

    function formatNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
    }
    function formatPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      const sign = value >= 0 ? "+" : "";
      return `${sign}${value.toFixed(2)}%`;
    }
    function formatRatioPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return `${(value * 100).toFixed(1)}%`;
    }
    function clsFor(value) {
      if (value > 0) return "good";
      if (value < 0) return "bad";
      return "flat";
    }
    function datasets(series, suffix = "") {
      return (series || []).map((item, index) => ({
        label: `${item.exchange}${suffix}`,
        data: item.data,
        borderColor: colors[index % colors.length],
        backgroundColor: colors[index % colors.length],
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.25
      }));
    }
    function singleDataset(label, data, color) {
      return [{ label, data: data || [], borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25 }];
    }
    function renderChart(id, chartData, yTitle) {
      if (charts[id]) charts[id].destroy();
      charts[id] = new Chart(document.getElementById(id), {
        type: "line",
        data: { datasets: chartData },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          parsing: { xAxisKey: "x", yAxisKey: "y" },
          plugins: {
            legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
            tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${formatNumber(ctx.parsed.y)}` } }
          },
          scales: {
            x: { type: "category", ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { display: false } },
            y: { title: { display: true, text: yTitle }, grid: { color: "rgba(23,32,28,.08)" } }
          }
        }
      });
    }
    function renderLongShortChart(data) {
      const long = datasets(data.longRatio, " Long");
      const short = datasets(data.shortRatio, " Short").map(item => ({ ...item, borderDash: [5, 4] }));
      const ratio = datasets(data.longShortRatio, " L/S").map(item => ({ ...item, borderWidth: 1.5, borderDash: [2, 4] }));
      renderChart("longShortRatioChart", [...long, ...short, ...ratio], "Ratio");
    }
    function renderTakerChart(data) {
      const buy = datasets(data.takerBuyVolume, " Buy");
      const sell = datasets(data.takerSellVolume, " Sell").map(item => ({ ...item, borderDash: [5, 4] }));
      const ratio = datasets(data.takerBuySellRatio, " B/S").map(item => ({ ...item, yAxisID: "y" }));
      renderChart("takerVolumeChart", [...buy, ...sell, ...ratio], "Volume / Ratio");
    }
    function renderTable(rows) {
      if (!rows || !rows.length) {
        document.getElementById("tableMount").innerHTML = `<div class="error">No market samples yet.</div>`;
        return;
      }
      document.getElementById("tableMount").innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Exchange</th><th>Latest OI</th><th>OI Change</th><th>30m Volume</th><th>Vol Spike</th>
              <th>Long</th><th>Short</th><th>L/S</th><th>Taker Buy</th><th>Taker Sell</th><th>Buy/Sell</th>
              <th>Top Acct Long</th><th>Top Pos Long</th><th>Last Updated JST</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(row => `
              <tr>
                <td>${row.exchange}</td>
                <td>${formatNumber(row.latestOi)}</td>
                <td class="${clsFor(row.oiChangePct)}">${formatPct(row.oiChangePct)}</td>
                <td>${formatNumber(row.latestVolume30m)}</td>
                <td>${row.volumeSpike === null || row.volumeSpike === undefined ? "N/A" : `${row.volumeSpike.toFixed(2)}x`}</td>
                <td>${formatRatioPct(row.longRatio)}</td>
                <td>${formatRatioPct(row.shortRatio)}</td>
                <td>${formatNumber(row.longShortRatio)}</td>
                <td>${formatNumber(row.takerBuyVolume)}</td>
                <td>${formatNumber(row.takerSellVolume)}</td>
                <td>${formatNumber(row.takerBuySellRatio)}</td>
                <td>${formatRatioPct(row.topAccountLongRatio)}</td>
                <td>${formatRatioPct(row.topPositionLongRatio)}</td>
                <td>${row.lastUpdatedJst || "N/A"}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }
    function renderAll() {
      if (!dashboardData || dashboardData.error) return;
      const summary = dashboardData.marketSummary;
      document.getElementById("metricSymbol").textContent = dashboardData.selectedSymbol;
      document.getElementById("metricLatest").textContent = dashboardData.latestSampleAt || "N/A";
      document.getElementById("metricTotalOi").textContent = formatNumber(summary.totalAggregatedOi);
      document.getElementById("metricOiChange").textContent = formatPct(summary.aggregatedOiChangePct);
      document.getElementById("metricOiChange").className = clsFor(summary.aggregatedOiChangePct);
      document.getElementById("metricTotalVolume").textContent = formatNumber(summary.totalAggregatedVolume30m);
      document.getElementById("metricVolumeChange").textContent = formatPct(summary.aggregatedVolumeChangePct);
      document.getElementById("metricVolumeChange").className = clsFor(summary.aggregatedVolumeChangePct);
      document.getElementById("metricLongRatio").textContent = formatRatioPct(summary.averageLongRatio);
      document.getElementById("metricTakerRatio").textContent = formatNumber(summary.averageTakerBuySellRatio);
      document.getElementById("tableNote").textContent = `${dashboardData.latestMarketTable.length} exchanges`;

      const oiSeries = oiMode === "raw" ? dashboardData.charts.exchangeOi.raw : dashboardData.charts.exchangeOi.normalized;
      renderChart("exchangeOiChart", datasets(oiSeries), oiMode === "raw" ? "OI" : "Normalized OI");
      renderChart("exchangeVolumeChart", datasets(dashboardData.charts.exchangeVolume), "30m Volume");
      renderChart("aggregatedOiChart", singleDataset("Aggregated OI", dashboardData.charts.aggregatedOi, "#0d6a7d"), "Sum OI");
      renderChart("aggregatedVolumeChart", singleDataset("Aggregated Volume", dashboardData.charts.aggregatedVolume, "#7a5c19"), "Sum 30m Volume");
      renderLongShortChart(dashboardData.charts.longShortTraderRatio);
      renderTakerChart(dashboardData.charts.longShortVolume);
      renderTable(dashboardData.latestMarketTable);
    }
    async function loadDashboard(symbol) {
      if (loading) return;
      loading = true;
      try {
        const qs = symbol ? `?symbol=${encodeURIComponent(symbol)}&ts=${Date.now()}` : `?ts=${Date.now()}`;
        const response = await fetch(`/api/snapshot${qs}`, { cache: "no-store" });
        dashboardData = await response.json();
        const select = document.getElementById("symbolSelect");
        select.innerHTML = (dashboardData.symbols || []).map(item => `<option value="${item}">${item}</option>`).join("");
        if (dashboardData.selectedSymbol) select.value = dashboardData.selectedSymbol;
        if (dashboardData.error) {
          document.getElementById("subtitle").textContent = dashboardData.error;
          return;
        }
        document.getElementById("subtitle").textContent = `Generated ${dashboardData.generatedAt} JST from ${dashboardData.dbPath}`;
        renderAll();
      } finally {
        loading = false;
      }
    }
    document.getElementById("symbolSelect").addEventListener("change", event => loadDashboard(event.target.value));
    document.getElementById("refreshButton").addEventListener("click", () => loadDashboard(document.getElementById("symbolSelect").value));
    document.getElementById("rawOiButton").addEventListener("click", () => {
      oiMode = "raw";
      document.getElementById("rawOiButton").classList.add("active");
      document.getElementById("normalizedOiButton").classList.remove("active");
      renderAll();
    });
    document.getElementById("normalizedOiButton").addEventListener("click", () => {
      oiMode = "normalized";
      document.getElementById("normalizedOiButton").classList.add("active");
      document.getElementById("rawOiButton").classList.remove("active");
      renderAll();
    });
    loadDashboard();
    setInterval(() => loadDashboard(document.getElementById("symbolSelect").value), 60000);
  </script>
</body>
</html>
"""


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
