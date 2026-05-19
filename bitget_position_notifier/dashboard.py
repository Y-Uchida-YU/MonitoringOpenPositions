from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR / "data" / "market_metrics.sqlite3"
JST_OFFSET_SECONDS = 9 * 60 * 60


def to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def pct_change(current: Decimal, previous: Decimal | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return float(((current - previous) / previous) * Decimal("100"))


def fmt_decimal(value: Decimal) -> float:
    return float(value)


def jst_iso(timestamp: int) -> str:
    return datetime.utcfromtimestamp(timestamp + JST_OFFSET_SECONDS).strftime("%Y-%m-%d %H:%M")


def read_snapshot(db_path: Path, *, symbol: str | None = None, limit: int = 180) -> dict[str, object]:
    if not db_path.exists():
        return {
            "dbPath": str(db_path),
            "generatedAt": jst_iso(int(time.time())),
            "symbols": [],
            "selectedSymbol": symbol,
            "summary": [],
            "series": [],
            "error": "Database not found. Run main.py first to collect market samples.",
        }

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        has_market_samples = (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'market_samples'"
            ).fetchone()
            is not None
        )
        table_name = "market_samples" if has_market_samples else "oi_samples"
        symbols = [
            row["symbol"]
            for row in conn.execute(f"SELECT DISTINCT symbol FROM {table_name} ORDER BY symbol")
        ]
        selected_symbol = symbol if symbol in symbols else (symbols[0] if symbols else None)

        if selected_symbol is None:
            return {
                "dbPath": str(db_path),
                "generatedAt": jst_iso(int(time.time())),
                "symbols": [],
                "selectedSymbol": None,
                "summary": [],
                "series": [],
                "error": "No samples yet. Wait until the notifier collects at least one market sample.",
            }

        rows = list(
            conn.execute(
                f"""
                SELECT *
                FROM {table_name}
                WHERE symbol = ?
                ORDER BY exchange, observed_at DESC
                """,
                (selected_symbol,),
            )
        )

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["exchange"], []).append(row)

    summary: list[dict[str, object]] = []
    series: list[dict[str, object]] = []
    latest_times: list[int] = []

    for exchange, exchange_rows_desc in sorted(grouped.items()):
        latest = exchange_rows_desc[0]
        previous = exchange_rows_desc[1] if len(exchange_rows_desc) > 1 else None
        latest_value = to_decimal(latest["oi_value"])
        previous_value = to_decimal(previous["oi_value"]) if previous is not None else None
        latest_times.append(int(latest["observed_at"]))

        ordered = list(reversed(exchange_rows_desc[:limit]))
        first_value = to_decimal(ordered[0]["oi_value"]) if ordered else Decimal("0")
        normalized_points = []
        raw_points = []
        volume_points = []
        for item in ordered:
            value = to_decimal(item["oi_value"])
            normalized = ((value / first_value) * Decimal("100")) if first_value else Decimal("100")
            normalized_points.append({"x": jst_iso(int(item["observed_at"])), "y": float(normalized)})
            raw_points.append({"x": jst_iso(int(item["observed_at"])), "y": fmt_decimal(value)})
            if has_market_samples and item["volume_30m"] is not None:
                volume_points.append({"x": jst_iso(int(item["observed_at"])), "y": fmt_decimal(to_decimal(item["volume_30m"]))})

        summary.append(
            {
                "exchange": exchange,
                "latestOi": fmt_decimal(latest_value),
                "changePct": pct_change(latest_value, previous_value),
                "volume30m": fmt_decimal(to_decimal(latest["volume_30m"])) if has_market_samples and latest["volume_30m"] is not None else None,
                "volumeSpike": fmt_decimal(to_decimal(latest["volume_spike"])) if has_market_samples and latest["volume_spike"] is not None else None,
                "longRatio": fmt_decimal(to_decimal(latest["long_ratio"])) if has_market_samples and latest["long_ratio"] is not None else None,
                "shortRatio": fmt_decimal(to_decimal(latest["short_ratio"])) if has_market_samples and latest["short_ratio"] is not None else None,
                "longShortRatio": fmt_decimal(to_decimal(latest["long_short_ratio"])) if has_market_samples and latest["long_short_ratio"] is not None else None,
                "takerBuySellRatio": fmt_decimal(to_decimal(latest["taker_buy_sell_ratio"])) if has_market_samples and latest["taker_buy_sell_ratio"] is not None else None,
                "topAccountLongRatio": fmt_decimal(to_decimal(latest["top_account_long_ratio"])) if has_market_samples and latest["top_account_long_ratio"] is not None else None,
                "topPositionLongRatio": fmt_decimal(to_decimal(latest["top_position_long_ratio"])) if has_market_samples and latest["top_position_long_ratio"] is not None else None,
                "samples": len(exchange_rows_desc),
                "lastSeen": jst_iso(int(latest["observed_at"])),
            }
        )
        series.append(
            {
                "exchange": exchange,
                "normalized": normalized_points,
                "raw": raw_points,
                "volume": volume_points,
            }
        )

    changes = [item["changePct"] for item in summary if item["changePct"] is not None]
    market_average = sum(changes) / len(changes) if changes else None
    long_values = [item["longRatio"] for item in summary if item.get("longRatio") is not None]
    average_long_ratio = sum(long_values) / len(long_values) if long_values else None
    volume_spikes = [item["volumeSpike"] for item in summary if item.get("volumeSpike") is not None]
    average_volume_spike = sum(volume_spikes) / len(volume_spikes) if volume_spikes else None

    return {
        "dbPath": str(db_path),
        "generatedAt": jst_iso(int(time.time())),
        "symbols": symbols,
        "selectedSymbol": selected_symbol,
        "latestSampleAt": jst_iso(max(latest_times)) if latest_times else None,
        "marketAverageChangePct": market_average,
        "averageLongRatio": average_long_ratio,
        "averageVolumeSpike": average_volume_spike,
        "hasMarketSamples": has_market_samples,
        "summary": summary,
        "series": series,
        "error": None,
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Position Market Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --ink: #17201c;
      --muted: #66736d;
      --paper: #f4f6f1;
      --panel: #ffffff;
      --line: #dbe2d6;
      --good: #11835b;
      --bad: #c23b3b;
      --warn: #a96800;
      --accent: #0d6a7d;
      --accent-2: #7a5c19;
      --shadow: 0 18px 45px rgba(43, 54, 48, .11);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", "Yu Gothic UI", sans-serif;
      background:
        linear-gradient(135deg, rgba(13, 106, 125, .10), transparent 34%),
        linear-gradient(225deg, rgba(122, 92, 25, .12), transparent 28%),
        repeating-linear-gradient(0deg, rgba(23, 32, 28, .035), rgba(23, 32, 28, .035) 1px, transparent 1px, transparent 24px),
        var(--paper);
    }
    header {
      padding: 28px clamp(18px, 3vw, 42px) 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(244, 246, 241, .82);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .topline {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0 0 6px;
      font-size: clamp(26px, 4vw, 46px);
      letter-spacing: 0;
      line-height: 1;
    }
    .subtitle {
      color: var(--muted);
      font-size: 14px;
    }
    .controls {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    select, button {
      min-height: 38px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      box-shadow: 0 1px 0 rgba(23, 32, 28, .04);
    }
    button {
      cursor: pointer;
      font-weight: 650;
      color: white;
      background: var(--accent);
      border-color: var(--accent);
    }
    main {
      width: min(1420px, 100%);
      margin: 0 auto;
      padding: 24px clamp(14px, 3vw, 42px) 46px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      background: rgba(255,255,255,.88);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .metric strong {
      display: block;
      margin-top: 8px;
      font-size: 24px;
      line-height: 1.15;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(340px, .9fr);
      gap: 16px;
      align-items: start;
    }
    .panel {
      background: rgba(255,255,255,.9);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
    }
    .panel-title h2 {
      margin: 0;
      font-size: 16px;
    }
    .panel-title span {
      color: var(--muted);
      font-size: 13px;
    }
    .chart-wrap {
      height: 430px;
      padding: 16px;
    }
    .raw-chart {
      height: 280px;
      border-top: 1px solid var(--line);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      white-space: nowrap;
    }
    th:first-child, td:first-child { text-align: left; }
    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .06em;
      background: rgba(244, 246, 241, .82);
    }
    .good { color: var(--good); font-weight: 750; }
    .bad { color: var(--bad); font-weight: 750; }
    .flat { color: var(--muted); font-weight: 650; }
    .heat {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      padding: 14px;
    }
    .heat-cell {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 72px;
      background: #fff;
    }
    .heat-cell b { display: block; font-size: 14px; }
    .heat-cell span { display: block; margin-top: 8px; font-size: 22px; font-weight: 800; }
    .error {
      padding: 18px;
      color: var(--bad);
      font-weight: 700;
    }
    @media (max-width: 980px) {
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
      .chart-wrap { height: 340px; }
    }
    @media (max-width: 560px) {
      header { position: static; }
      .metrics { grid-template-columns: 1fr; }
      .controls { width: 100%; }
      select, button { width: 100%; }
      th, td { padding: 10px 8px; font-size: 12px; }
      .heat { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topline">
      <div>
        <h1>Market OI Dashboard</h1>
        <div class="subtitle" id="subtitle">Loading market samples...</div>
      </div>
      <div class="controls">
        <select id="symbolSelect" aria-label="Symbol"></select>
        <button id="refreshButton">Refresh</button>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics">
      <div class="metric"><span>Symbol</span><strong id="metricSymbol">-</strong></div>
      <div class="metric"><span>Market Avg OI Change</span><strong id="metricAverage">-</strong></div>
      <div class="metric"><span>Avg Long Bias</span><strong id="metricLongBias">-</strong></div>
      <div class="metric"><span>Avg Volume Spike</span><strong id="metricVolumeSpike">-</strong></div>
      <div class="metric"><span>Latest Sample</span><strong id="metricLatest">-</strong></div>
    </section>
    <section class="layout">
      <div class="panel">
        <div class="panel-title">
          <h2>Normalized OI Trend</h2>
          <span>First visible sample = 100</span>
        </div>
        <div class="chart-wrap"><canvas id="normalizedChart"></canvas></div>
        <div class="panel-title">
          <h2>Raw OI by Exchange</h2>
          <span>Use shape, not cross-exchange totals</span>
        </div>
        <div class="chart-wrap raw-chart"><canvas id="rawChart"></canvas></div>
        <div class="panel-title">
          <h2>30m Volume</h2>
          <span>Latest closed 30m candle</span>
        </div>
        <div class="chart-wrap raw-chart"><canvas id="volumeChart"></canvas></div>
      </div>
      <div class="panel">
        <div class="panel-title">
          <h2>Exchange Table</h2>
          <span id="tableNote">Latest rows</span>
        </div>
        <div id="tableMount"></div>
        <div class="panel-title">
          <h2>Change Heatmap</h2>
          <span>Last sample vs previous</span>
        </div>
        <div class="heat" id="heatMount"></div>
      </div>
    </section>
  </main>
  <script>
    let normalizedChart;
    let rawChart;
    let volumeChart;
    let dashboardLoading = false;
    const colors = ["#0d6a7d", "#b14b2c", "#11835b", "#7a5c19", "#5c6470", "#8f3f65", "#2f6f3e"];

    function formatNumber(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
    }

    function formatPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      const sign = value >= 0 ? "+" : "";
      return `${sign}${value.toFixed(2)}%`;
    }

    function formatRatioPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return `${(value * 100).toFixed(1)}%`;
    }

    function clsFor(value) {
      if (value > 0) return "good";
      if (value < 0) return "bad";
      return "flat";
    }

    function buildDatasets(series, key) {
      return series.map((item, index) => ({
        label: item.exchange,
        data: item[key],
        borderColor: colors[index % colors.length],
        backgroundColor: colors[index % colors.length],
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.25
      }));
    }

    function renderChart(canvasId, existing, series, key, yTitle) {
      if (existing) existing.destroy();
      const ctx = document.getElementById(canvasId);
      return new Chart(ctx, {
        type: "line",
        data: { datasets: buildDatasets(series, key) },
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

    function renderTable(summary) {
      if (!summary.length) {
        document.getElementById("tableMount").innerHTML = `<div class="error">No exchange samples yet.</div>`;
        return;
      }
      const rows = summary.map(item => `
        <tr>
          <td>${item.exchange}</td>
          <td>${formatNumber(item.latestOi)}</td>
          <td class="${clsFor(item.changePct)}">${formatPct(item.changePct)}</td>
          <td>${formatRatioPct(item.longRatio)}</td>
          <td>${item.volumeSpike === null || item.volumeSpike === undefined ? "-" : `${item.volumeSpike.toFixed(2)}x`}</td>
          <td>${item.takerBuySellRatio === null || item.takerBuySellRatio === undefined ? "-" : item.takerBuySellRatio.toFixed(2)}</td>
          <td>${formatRatioPct(item.topAccountLongRatio)}</td>
          <td>${item.samples}</td>
          <td>${item.lastSeen}</td>
        </tr>
      `).join("");
      document.getElementById("tableMount").innerHTML = `
        <table>
          <thead><tr><th>Exchange</th><th>Latest OI</th><th>OI Chg</th><th>Long</th><th>Vol</th><th>Taker B/S</th><th>Smart Long</th><th>Samples</th><th>Last Seen</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    }

    function renderHeat(summary) {
      const cells = summary.map(item => `
        <div class="heat-cell">
          <b>${item.exchange}</b>
          <span class="${clsFor(item.changePct)}">${formatPct(item.changePct)}</span>
        </div>
      `).join("");
      document.getElementById("heatMount").innerHTML = cells || `<div class="error">No changes yet.</div>`;
    }

    async function loadDashboard(symbol) {
      if (dashboardLoading) return;
      dashboardLoading = true;
      const qs = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
      try {
        const separator = qs ? "&" : "?";
        const response = await fetch(`/api/snapshot${qs}${separator}ts=${Date.now()}`, { cache: "no-store" });
        const data = await response.json();
        const select = document.getElementById("symbolSelect");
        select.innerHTML = data.symbols.map(item => `<option value="${item}">${item}</option>`).join("");
        if (data.selectedSymbol) select.value = data.selectedSymbol;

        if (data.error) {
          document.getElementById("subtitle").textContent = data.error;
          return;
        }

        document.getElementById("subtitle").textContent = `Generated ${data.generatedAt} JST from ${data.dbPath}`;
        document.getElementById("metricSymbol").textContent = data.selectedSymbol;
        document.getElementById("metricAverage").textContent = formatPct(data.marketAverageChangePct);
        document.getElementById("metricAverage").className = clsFor(data.marketAverageChangePct);
        document.getElementById("metricLongBias").textContent = formatRatioPct(data.averageLongRatio);
        document.getElementById("metricLongBias").className = data.averageLongRatio >= 0.5 ? "good" : "bad";
        document.getElementById("metricVolumeSpike").textContent = data.averageVolumeSpike === null || data.averageVolumeSpike === undefined ? "-" : `${data.averageVolumeSpike.toFixed(2)}x`;
        document.getElementById("metricLatest").textContent = data.latestSampleAt || "-";
        document.getElementById("tableNote").textContent = `${data.summary.length} exchanges`;

        normalizedChart = renderChart("normalizedChart", normalizedChart, data.series, "normalized", "Normalized OI");
        rawChart = renderChart("rawChart", rawChart, data.series, "raw", "Raw OI");
        volumeChart = renderChart("volumeChart", volumeChart, data.series, "volume", "30m Volume");
        renderTable(data.summary);
        renderHeat(data.summary);
      } finally {
        dashboardLoading = false;
      }
    }

    document.getElementById("symbolSelect").addEventListener("change", event => loadDashboard(event.target.value));
    document.getElementById("refreshButton").addEventListener("click", () => loadDashboard(document.getElementById("symbolSelect").value));
    loadDashboard();
    setInterval(() => loadDashboard(document.getElementById("symbolSelect").value), 20000);
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
    parser = argparse.ArgumentParser(description="Run a local dashboard for market OI samples.")
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
