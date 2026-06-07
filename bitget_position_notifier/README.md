# Bitget Position Notifier (Read-only)

This bot fetches your Bitget futures open positions every 15 minutes and sends a readable Discord Embed notification.
It does not place orders or execute trades.

## Features

- Bitget Futures API v2 private endpoint:
  - `GET /api/v2/mix/position/all-position`
- Read-only environment variable based authentication
- `.env` support
- Runs on JST wall-clock quarter hours: `00`, `15`, `30`, and `45`
- Discord Embed notifications with one field per symbol
- Sends error summary to Discord if API call fails
- Keeps running even if API or Discord fails temporarily
- Uses `Decimal` for numeric parsing and aggregation
- Optional free public market metrics from Binance, Bybit, Bitget, OKX, Gate, and Hyperliquid
- Dashboard and stored market metrics are limited to symbols with currently held non-zero Bitget positions

## Directory Structure

```text
bitget_position_notifier/
  main.py
  dashboard.py
  bitget_client.py
  discord_notifier.py
  config.py
  market_metrics.py
  risk_score.py
  smart_signal.py
  requirements.txt
  .env.example
  README.md
```

## Setup

1. Move into project directory:

```powershell
cd C:\MyProjects\MonitoringOpenPositions\bitget_position_notifier
```

2. Create and activate virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and set values:

```powershell
Copy-Item .env.example .env
```

## .env Example

```env
BITGET_API_KEY=your_bitget_api_key
BITGET_API_SECRET=your_bitget_api_secret
BITGET_PASSPHRASE=your_bitget_passphrase
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
# Optional split notifications:
# DISCORD_WEBHOOK_URL is used as the private fallback.
DISCORD_WEBHOOK_URL_PRIVATE=
DISCORD_WEBHOOK_URL_PUBLIC=

BITGET_BASE_URL=https://api.bitget.com
BITGET_PRODUCT_TYPE=USDT-FUTURES
BITGET_MARGIN_COIN=USDT
BITGET_LOCALE=en-US
REQUEST_TIMEOUT_SECONDS=10
POLL_INTERVAL_SECONDS=900
DISCORD_USERNAME=Bitget Position Bot

ENABLE_MARKET_METRICS=true
MARKET_DATA_EXCHANGES=binance,bybit,bitget,okx,gate,hyperliquid
MARKET_METRICS_DB_PATH=data/market_metrics.sqlite3
ENABLE_RISK_SCORE=true

ENABLE_BINANCE_SMART_SIGNAL=false
BINANCE_SMART_SIGNAL_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

## Run

```powershell
python main.py
```

Behavior:

- Waits until the next JST quarter-hour slot
- Then repeats on `00`, `15`, `30`, and `45` minutes every hour

## CI

GitHub Actions runs pytest automatically on pushes to `main` and pull requests targeting `main`.

The CI job:

- Uses `ubuntu-latest` and `windows-latest`
- Uses Python `3.11`
- Installs dependencies with `pip install -r requirements.txt`
- Runs `pytest ../tests` from `bitget_position_notifier`
- Uses dummy environment variables for Bitget and Discord settings

The tests do not call live Bitget APIs or Discord webhooks.

## Dashboard

The dashboard reads saved market samples from SQLite and runs locally without extra Python dependencies.

This is a held-position monitoring dashboard, not a full-market scanner. Each successful position polling cycle keeps only symbols with a current non-zero Bitget position. Historical `market_samples`, `oi_samples`, and Smart Signal samples for symbols no longer held are automatically removed.

```powershell
python dashboard.py
```

Then open:

```text
http://127.0.0.1:8765
```

It shows:

- Header with selected symbol, latest JST sample time, refresh button, and 60 second auto refresh
- Market summary cards for Total OI USDT, OI change %, Total 30m Volume USDT, volume change %, average long ratio, and average taker buy/sell ratio
- Exchange OI Trend with Raw OI USDT / Normalized OI toggle
- Exchange Volume Trend using 30m Volume USDT when conversion is available
- Aggregated Market Trend with 10 minute buckets across monitored exchanges
- Long/Short Structure for account ratios where public data is available
- Long/Short Volume Trend using Taker Buy/Sell Volume as the practical public-data fallback
- Binance Smart Money / Smart Signal section when enabled and data exists
- Latest Exchange Table with USDT values first and raw values as supporting context
- OI change heatmap for quick visual strength/weakness checks
- Position Risk Score panel with the selected position's score breakdown and explanatory risk reasons

### Dashboard v2 and USDT Normalization

Dashboard v2 uses USDT as the primary display and aggregation unit wherever possible.

Market metrics can arrive in different units depending on the exchange:

- Base quantity: converted with `raw_quantity * price_usdt`
- Quote or USDT value: kept as-is
- Unknown unit or unavailable price: USDT value is stored as `NULL` and shown as `N/A`

The SQLite `market_samples` table keeps both raw and converted values:

- Raw examples: `oi_raw`, `volume_30m_raw`, `taker_buy_volume_raw`
- Unit examples: `oi_raw_unit`, `volume_30m_raw_unit`
- USDT examples: `oi_usdt`, `volume_30m_usdt`, `taker_buy_volume_usdt`
- Price provenance: `price_usdt`, `conversion_source`

Existing compatibility columns such as `oi_value`, `volume_30m`, `taker_buy_volume`, and `taker_sell_volume` remain in place. New dashboard calculations prefer `*_usdt` columns and fall back to legacy columns only when USDT values are unavailable.

New OI change calculations prioritize the prior `market_samples.oi_usdt` value, then compatible `market_samples.oi_value`, and only use legacy `oi_samples.oi_value` as a final fallback.

The database is stored at:

```text
bitget_position_notifier/data/market_metrics.sqlite3
```

Older SQLite schemas are migrated on startup without a destructive table rebuild. During normal monitoring, historical rows for symbols no longer held are deleted by design.

### Indicator Notes

OI, volume, and taker metrics are not defined identically by every exchange. Cross-exchange totals are most useful after USDT conversion, but the original raw values are retained so you can inspect the source unit.

Long/Short Volume is also not standardized. Where direct long/short volume is unavailable, the dashboard displays Taker Buy/Sell Volume and labels it as such.

Binance Smart Money / Smart Signal is disabled by default. This project does not scrape protected, login-gated, bot-protected, or CAPTCHA-protected Binance pages. If no stable public API is available, the dashboard shows `Smart Signal data is not available or disabled.`

The dashboard is independent of the Bitget position notification loop. `python main.py` can run the notifier, and `python dashboard.py` can be started separately for local analysis.

### Position Risk Score

The notifier calculates a `0` to `100` Position Risk Score for each currently held position and shows it in Discord and the dashboard:

- `LOW` (`0-24`): routine monitoring
- `WATCH` (`25-49`): mild caution
- `HIGH` (`50-74`): increased risk requiring review
- `CRITICAL` (`75-100`): highly elevated market and position risk

The score combines six explainable inputs:

- Leveraged unrealized price-move PnL risk
- Crowding risk based on long/short account ratios and position direction
- OI expansion risk
- Volume spike risk
- Taker buy/sell imbalance risk
- Cross-exchange OI dispersion risk

Risk scores and their reasons are stored in the SQLite `risk_scores` table. They are automatically removed for symbols that are no longer held.

Risk Score is a monitoring aid only. It is not trading advice, and this bot never automatically closes, reduces, profits, stops, or opens a position.

## Discord Notification Content

By default, `DISCORD_WEBHOOK_URL` receives the private notification.

If you want to send both private and public notifications from one bot, set:

- `DISCORD_WEBHOOK_URL_PRIVATE`: private webhook with full position details
- `DISCORD_WEBHOOK_URL_PUBLIC`: public webhook with personal profit amounts hidden

`DISCORD_WEBHOOK_URL` remains supported as the private fallback for backward compatibility.

### Private Notification

Each private run includes:

- Notification time (JST)
- Symbol
- Direction (`long` / `short`)
- Average entry price
- Mark price
- Unrealized PnL (signed + / -)
- Leverage
- Position Risk Score and risk level (`LOW`, `WATCH`, `HIGH`, `CRITICAL`)
- Up to five explanatory risk reasons
- Major OI change from public exchange data
- 30m volume spike versus recent 30m average
- Long/Short account ratio where public data is available
- Binance taker buy/sell volume ratio
- Binance top trader account/position long ratio
- Exchange OI breakdown
- Total unrealized PnL across all positions

### Public Notification

The public webhook is designed for sharing held-position monitoring without revealing concrete personal profit amounts.

It can show:

- Symbol
- Direction (`long` / `short`)
- Average entry price
- Mark price
- PnL percentage
- Leverage
- Position Risk Score and risk reasons
- Market metrics such as OI change, 30m volume, Long/Short ratio, and taker buy/sell ratio

It hides:

- Personal unrealized PnL amount
- Personal realized PnL amount
- Total unrealized PnL amount

Market metrics use public market-data endpoints only. Binance, Bybit, OKX, Gate, and Hyperliquid do not require API keys. Bitget market metrics also use public market endpoints; your existing Bitget private API key remains used only for reading your own positions.

If no open positions exist, it sends:

- `Current positions: none (現在ポジションなし)`

## Windows Always-on Example (Task Scheduler)

Create a startup task:

```powershell
schtasks /Create `
  /TN "BitgetPositionNotifier" `
  /TR "C:\MyProjects\MonitoringOpenPositions\bitget_position_notifier\.venv\Scripts\python.exe C:\MyProjects\MonitoringOpenPositions\bitget_position_notifier\main.py" `
  /SC ONSTART /RL HIGHEST /F
```

Start immediately for test:

```powershell
schtasks /Run /TN "BitgetPositionNotifier"
```

## VPS systemd Example

Create `/etc/systemd/system/bitget-position-notifier.service`:

```ini
[Unit]
Description=Bitget Position Notifier
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/bitget_position_notifier
EnvironmentFile=/opt/bitget_position_notifier/.env
ExecStart=/opt/bitget_position_notifier/.venv/bin/python /opt/bitget_position_notifier/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bitget-position-notifier
sudo systemctl start bitget-position-notifier
sudo systemctl status bitget-position-notifier
```

## Security Notes

- Use Bitget API key with **Read-only** permission.
- Do not enable order/trade/withdraw permissions.
- Never hardcode API secrets or webhook URLs in source code.
- Keep `.env` private and out of version control.
