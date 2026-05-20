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

## Directory Structure

```text
bitget_position_notifier/
  main.py
  dashboard.py
  bitget_client.py
  discord_notifier.py
  config.py
  market_metrics.py
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

## Dashboard

The dashboard reads saved market samples from SQLite and runs locally. It is independent from the Discord notification loop: `main.py` collects data and sends notifications, while `dashboard.py` only reads the SQLite database and renders a browser UI.

```powershell
python dashboard.py
```

Then open:

```text
http://127.0.0.1:8765
```

It shows:

- Symbol selector
- Exchange OI Trend with raw / normalized toggle
- Exchange Volume Trend for `volume_30m`
- Aggregated OI Trend, bucketed by 10 minutes
- Aggregated Volume Trend, bucketed by 10 minutes
- Long/Short Trader Ratio Trend
- Long/Short Volume Trend using taker buy/sell volume where available
- Binance Smart Money / Smart Signal section
- Latest Market Table with OI, volume, long/short, taker, and smart trader columns
- Market structure summary cards
- Auto refresh every 60 seconds

SQLite database location:

```text
bitget_position_notifier/data/market_metrics.sqlite3
```

Dashboard indicators:

- `OI`: Open interest reported by each exchange's public market API. Units and definitions can differ by venue.
- `Normalized OI`: Each exchange's first visible OI sample is set to `100`, making relative changes easier to compare.
- `Aggregated OI`: Sum of monitored exchange OI inside a 10-minute bucket. Missing exchange values are not filled.
- `volume_30m`: Latest saved 30-minute volume sample.
- `Aggregated Volume`: Sum of monitored exchange `volume_30m` inside a 10-minute bucket.
- `long_ratio` / `short_ratio`: Account long/short ratios when the exchange exposes them.
- `long_short_ratio`: Long ratio divided by short ratio, or the exchange-provided equivalent.
- `Taker Buy Volume` / `Taker Sell Volume`: Binance public taker buy/sell volume when available.
- `Buy/Sell Ratio`: Taker buy volume divided by taker sell volume.
- `Top Trader Account Long/Short Ratio`: Binance top trader account ratio.
- `Top Trader Position Long/Short Ratio`: Binance top trader position ratio.

Binance Smart Money / Smart Trader Metrics are displayed without mixing definitions:

- `Global Long/Short Account Ratio`
- `Top Trader Account Long/Short Ratio`
- `Top Trader Position Long/Short Ratio`
- `Taker Buy/Sell Volume`

Long/Short Volume note:

`Taker Buy/Sell Volume` is not strictly the same as long/short position volume. It is used as a public proxy where available. Exchanges expose different definitions and some venues do not provide comparable long/short or taker volume fields; unavailable values are shown as `N/A`.

Binance Smart Money / Smart Signal:

- Disabled by default with `ENABLE_BINANCE_SMART_SIGNAL=false`.
- `SmartSignalClient.fetch_current_positions()` is currently a safe stub that returns no samples because no stable official public endpoint for the requested Binance Smart Signal current-position data has been wired in.
- The dashboard includes the database and UI extension point for Avg Entry Price, Unrealized PnL, and Unrealized PnL % / ROI.
- The implementation does not scrape protected, login-gated, captcha-gated, or bot-protected page data.
- If no stable public API is available, the dashboard shows `Smart Signal data is not available or disabled.`

## Discord Notification Content

Each run includes:

- Notification time (JST)
- Symbol
- Direction (`long` / `short`)
- Average entry price
- Mark price
- Unrealized PnL (signed + / -)
- Leverage
- Major OI change from public exchange data
- 30m volume spike versus recent 30m average
- Long/Short account ratio where public data is available
- Binance taker buy/sell volume ratio
- Binance top trader account/position long ratio
- Exchange OI breakdown
- Total unrealized PnL across all positions

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
