from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bitget_client import BitgetApiError, BitgetClient
from config import load_config
from discord_notifier import DiscordNotifier, DiscordNotifierError
from market_metrics import MarketMetricService, SymbolMarketMetrics

JST = ZoneInfo("Asia/Tokyo")
MAX_FIELDS_PER_EMBED = 25
MAX_EMBEDS_PER_MESSAGE = 10


def decimal_from_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def format_decimal(value: Decimal, *, places: int = 4, signed: bool = False) -> str:
    quant = Decimal(1).scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    abs_text = f"{abs(rounded):,.{places}f}".rstrip("0").rstrip(".")
    if not abs_text:
        abs_text = "0"
    if signed:
        sign = "+" if rounded >= 0 else "-"
        return f"{sign}{abs_text}"
    if rounded < 0:
        return f"-{abs_text}"
    return abs_text


def average_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def format_optional_decimal(raw_value: Any, *, places: int = 4) -> str:
    if raw_value in (None, ""):
        return "N/A"
    return format_decimal(decimal_from_value(raw_value), places=places)


def pnl_indicator(value: Decimal) -> str:
    if value > 0:
        return "+"
    if value < 0:
        return "-"
    return "0"


def compact_exchange_name(name: str) -> str:
    aliases = {
        "Binance": "BN",
        "Bybit": "BB",
        "Bitget": "BG",
        "OKX": "OKX",
        "Gate": "GT",
        "Hyperliquid": "HL",
    }
    return aliases.get(name, name)


def format_exchange_metric_status(exchange_metric: Any) -> str:
    exchange = compact_exchange_name(exchange_metric.exchange)
    if not exchange_metric.is_listed:
        return f"{exchange}: not listed"
    if exchange_metric.error:
        return f"{exchange}: error"
    if exchange_metric.oi_change_pct is not None:
        return f"{exchange}: {format_decimal(exchange_metric.oi_change_pct, places=2, signed=True)}%"
    if exchange_metric.oi_value is not None and not exchange_metric.has_previous_oi:
        return f"{exchange}: first sample"
    if exchange_metric.oi_value is not None:
        return f"{exchange}: sampled"
    return f"{exchange}: no data"


def format_ratio_percent(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{format_decimal(value * Decimal('100'), places=1)}%"


def find_exchange_metric(metrics: SymbolMarketMetrics, exchange: str) -> Any | None:
    for metric in metrics.exchange_metrics:
        if metric.exchange == exchange:
            return metric
    return None


def format_market_metrics(metrics: SymbolMarketMetrics | None) -> list[str]:
    if metrics is None:
        return []

    oi_changes = [
        exchange_metric.oi_change_pct
        for exchange_metric in metrics.exchange_metrics
        if exchange_metric.oi_change_pct is not None
    ]
    volume_spikes = [
        exchange_metric.volume_spike
        for exchange_metric in metrics.exchange_metrics
        if exchange_metric.volume_spike is not None
    ]
    long_ratios = [
        exchange_metric.long_ratio
        for exchange_metric in metrics.exchange_metrics
        if exchange_metric.long_ratio is not None
    ]

    avg_oi_change = average_decimal(oi_changes)
    avg_volume_spike = average_decimal(volume_spikes)
    avg_long_ratio = average_decimal(long_ratios)
    binance = find_exchange_metric(metrics, "Binance")

    oi_text = (
        f"{format_decimal(avg_oi_change, places=2, signed=True)}%"
        if avg_oi_change is not None
        else f"N/A ({sum(1 for metric in metrics.exchange_metrics if metric.oi_value is not None)} first)"
    )
    volume_text = (
        f"{format_decimal(avg_volume_spike, places=2)}x"
        if avg_volume_spike is not None
        else "N/A"
    )

    listed_count = sum(1 for metric in metrics.exchange_metrics if metric.is_listed)
    exchange_count = len(metrics.exchange_metrics)
    breakdown = " | ".join(format_exchange_metric_status(metric) for metric in metrics.exchange_metrics)

    return [
        "**Market**",
        f"`OI` {oi_text}  |  `Vol` {volume_text}  |  `Listed` {listed_count}/{exchange_count}",
        f"`L/S` Long {format_ratio_percent(avg_long_ratio)}  |  Binance Smart {format_ratio_percent(binance.top_account_long_ratio if binance else None)}",
        f"`Taker` Buy/Sell {format_decimal(binance.taker_buy_sell_ratio, places=2) if binance and binance.taker_buy_sell_ratio is not None else 'N/A'}",
        f"`{breakdown}`",
    ]


def build_position_field(
    position: dict[str, Any],
    market_metrics: SymbolMarketMetrics | None = None,
) -> tuple[dict[str, Any], Decimal]:
    symbol = str(position.get("symbol") or "N/A")
    side = str(position.get("holdSide") or "N/A").lower()
    entry_price = format_optional_decimal(position.get("openPriceAvg"), places=4)
    mark_price = format_optional_decimal(position.get("markPrice"), places=4)
    unrealized_pnl = decimal_from_value(position.get("unrealizedPL"))
    leverage = str(position.get("leverage") or "N/A")

    field_value = "\n".join(
        [
            f"**Position**  `{side}`  |  `{leverage}x`  |  PnL `{format_decimal(unrealized_pnl, places=4, signed=True)}`",
            f"`Entry` {entry_price}  ->  `Mark` {mark_price}",
            *format_market_metrics(market_metrics),
        ]
    )

    field = {
        "name": f"{pnl_indicator(unrealized_pnl)} {symbol} ({side})",
        "value": field_value,
        "inline": False,
    }
    return field, unrealized_pnl


def build_position_embeds(
    positions: list[dict[str, Any]],
    *,
    product_type: str,
    market_metrics_by_symbol: dict[str, SymbolMarketMetrics] | None = None,
) -> list[dict[str, Any]]:
    observed_at_jst = datetime.now(JST)
    observed_text = observed_at_jst.strftime("%Y-%m-%d %H:%M:%S JST")
    discord_timestamp = datetime.now(timezone.utc).isoformat()
    title = f"Bitget Position Monitor ({product_type})"

    if not positions:
        return [
            {
                "title": title,
                "description": "Current positions: none",
                "color": 0x2B90D9,
                "timestamp": discord_timestamp,
                "fields": [
                    {"name": "Notified At (JST)", "value": observed_text, "inline": False},
                    {"name": "Total Unrealized PnL", "value": "`+0`", "inline": False},
                ],
            }
        ]

    fields: list[dict[str, Any]] = []
    total_unrealized = Decimal("0")
    for position in positions:
        symbol = str(position.get("symbol") or "N/A")
        field, unrealized = build_position_field(
            position,
            market_metrics=(market_metrics_by_symbol or {}).get(symbol),
        )
        fields.append(field)
        total_unrealized += unrealized

    first_chunk_limit = MAX_FIELDS_PER_EMBED - 2
    first_chunk = fields[:first_chunk_limit]
    remaining_fields = fields[first_chunk_limit:]
    remaining_chunks = [
        remaining_fields[i : i + MAX_FIELDS_PER_EMBED]
        for i in range(0, len(remaining_fields), MAX_FIELDS_PER_EMBED)
    ]
    chunks = [first_chunk] + remaining_chunks
    embeds: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        embed_fields = list(chunk)
        if index == 1:
            embed_fields.insert(
                0,
                {
                    "name": "Summary",
                    "value": (
                        f"`JST` {observed_text}\n"
                        f"`Total Unrealized PnL` {format_decimal(total_unrealized, places=4, signed=True)}"
                    ),
                    "inline": False,
                },
            )
            embed_fields.insert(
                1,
                {
                    "name": "Legend",
                    "value": "`BN` Binance | `BB` Bybit | `BG` Bitget | `GT` Gate | `HL` Hyperliquid",
                    "inline": False,
                },
            )

        title_suffix = f" [{index}/{len(chunks)}]" if len(chunks) > 1 else ""
        embeds.append(
            {
                "title": f"{title}{title_suffix}",
                "color": 0x00A884 if total_unrealized >= 0 else 0xE74C3C,
                "timestamp": discord_timestamp,
                "fields": embed_fields,
            }
        )

    return embeds


def build_error_embed(error: Exception) -> dict[str, Any]:
    observed_at_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    details = str(error)[:1000] if str(error) else error.__class__.__name__
    return {
        "title": "Bitget Position Monitor Error",
        "description": "An error occurred while fetching positions.",
        "color": 0xE74C3C,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Notified At (JST)", "value": observed_at_jst, "inline": False},
            {"name": "Error", "value": f"```{details}```", "inline": False},
        ],
    }


def send_embeds_in_batches(notifier: DiscordNotifier, embeds: list[dict[str, Any]]) -> None:
    for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        notifier.send_embeds(embeds[i : i + MAX_EMBEDS_PER_MESSAGE])


def next_aligned_run_at(interval_seconds: int, *, now: datetime | None = None) -> datetime:
    if interval_seconds <= 0 or 86400 % interval_seconds != 0:
        raise ValueError("POLL_INTERVAL_SECONDS must divide 86400 for wall-clock aligned scheduling")

    current = now or datetime.now(JST)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = int((current - midnight).total_seconds())
    next_slot_seconds = ((elapsed_seconds // interval_seconds) + 1) * interval_seconds

    if next_slot_seconds >= 86400:
        return midnight + timedelta(days=1)

    return midnight + timedelta(seconds=next_slot_seconds)


def sleep_until_next_aligned_run(interval_seconds: int, logger: logging.Logger) -> datetime:
    target = next_aligned_run_at(interval_seconds)
    sleep_seconds = max(0.0, (target - datetime.now(JST)).total_seconds())
    logger.info("Waiting %.2f seconds until aligned JST run at %s", sleep_seconds, target.strftime("%H:%M:%S"))
    time.sleep(sleep_seconds)
    return target


def run() -> None:
    config = load_config()
    app_dir = Path(__file__).resolve().parent

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger = logging.getLogger("bitget-position-notifier")

    client = BitgetClient(
        api_key=config.bitget_api_key,
        api_secret=config.bitget_api_secret,
        passphrase=config.bitget_passphrase,
        base_url=config.base_url,
        locale=config.bitget_locale,
        timeout_seconds=config.request_timeout_seconds,
    )
    notifier = DiscordNotifier(
        webhook_url=config.discord_webhook_url,
        timeout_seconds=config.request_timeout_seconds,
        username=config.discord_username,
    )
    market_metric_service = None
    if config.enable_market_metrics:
        market_metric_service = MarketMetricService(
            exchange_names=list(config.market_data_exchanges),
            timeout_seconds=config.request_timeout_seconds,
            db_path=app_dir / config.market_metrics_db_path,
        )

    logger.info(
        "Position monitor started (product_type=%s, margin_coin=%s, aligned_interval=%s sec)",
        config.product_type,
        config.margin_coin,
        config.poll_interval_seconds,
    )

    while True:
        scheduled_at = sleep_until_next_aligned_run(config.poll_interval_seconds, logger)
        cycle_start = time.monotonic()
        logger.info("Starting aligned monitor cycle scheduled for %s JST", scheduled_at.strftime("%Y-%m-%d %H:%M:%S"))

        try:
            positions = client.get_all_positions(
                product_type=config.product_type,
                margin_coin=config.margin_coin,
            )
            symbols = sorted({str(position.get("symbol")) for position in positions if position.get("symbol")})
            market_metrics_by_symbol = (
                market_metric_service.fetch_for_symbols(symbols)
                if market_metric_service is not None and symbols
                else {}
            )
            embeds = build_position_embeds(
                positions,
                product_type=config.product_type,
                market_metrics_by_symbol=market_metrics_by_symbol,
            )
            send_embeds_in_batches(notifier, embeds)
            logger.info("Position notification sent successfully (positions=%d)", len(positions))

        except BitgetApiError as exc:
            logger.exception("Bitget API error")
            try:
                notifier.send_embeds([build_error_embed(exc)])
            except DiscordNotifierError:
                logger.exception("Failed to send Bitget API error to Discord")

        except DiscordNotifierError as exc:
            logger.exception("Discord webhook error: %s", exc)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in monitor loop")
            try:
                notifier.send_embeds([build_error_embed(exc)])
            except DiscordNotifierError:
                logger.exception("Failed to send unexpected error to Discord")

        elapsed = time.monotonic() - cycle_start
        logger.info("Monitor cycle finished in %.2f seconds", elapsed)


if __name__ == "__main__":
    run()
