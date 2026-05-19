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
```

## Run

```powershell
python main.py
```

Behavior:

- Waits until the next JST quarter-hour slot
- Then repeats on `00`, `15`, `30`, and `45` minutes every hour

## Dashboard

The dashboard reads saved OI samples from SQLite and runs locally without extra Python dependencies.

```powershell
python dashboard.py
```

Then open:

```text
http://127.0.0.1:8765
```

It shows:

- Symbol selector
- Normalized OI trend by exchange
- Raw OI trend by exchange
- 30m volume trend by exchange
- Latest exchange table
- Long/Short, volume spike, Binance taker buy/sell, and Binance top trader columns
- Last-sample OI change heatmap
- Auto refresh every 20 seconds so new bot samples appear shortly after notification

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
