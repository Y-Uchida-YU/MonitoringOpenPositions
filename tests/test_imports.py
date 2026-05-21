from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1] / "bitget_position_notifier"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def test_core_modules_import() -> None:
    import bitget_client  # noqa: F401
    import config  # noqa: F401
    import dashboard  # noqa: F401
    import discord_notifier  # noqa: F401
    import main  # noqa: F401
    import market_metrics  # noqa: F401
    import smart_signal  # noqa: F401
