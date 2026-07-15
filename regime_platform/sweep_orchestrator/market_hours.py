"""
Market Hours Check
══════════════════

Ported from supabase/functions/_shared/utils.ts → isMarketOpen().
Determines whether a given asset class is currently tradeable.

Asset class rules
─────────────────
  crypto       : 24/7
  stocks       : Mon–Fri 14:30–21:00 UTC  (9:30am–4:00pm ET)
  commodities  : Sun 22:00 – Fri 22:00 UTC (gold/silver spot via OANDA —
                 trades like forex, NOT stock market hours)
  forex        : Sun 22:00 – Fri 22:00 UTC
  default      : always True
"""

from __future__ import annotations

from datetime import datetime, timezone


def is_market_open(asset_class: str, now: datetime | None = None) -> bool:
    """Return True if the given asset class is currently tradeable.

    Parameters
    ----------
    asset_class
        One of ``crypto``, ``stocks``, ``commodities``, ``forex``.
    now
        Optional timestamp (UTC).  Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    bool
    """
    if now is None:
        now = datetime.now(timezone.utc)

    day = now.weekday()         # 0=Mon … 6=Sun
    minute_of_day = now.hour * 60 + now.minute

    match asset_class:
        case "crypto":
            return True

        case "forex" | "commodities":
            # Forex AND commodities (gold/silver spot via OANDA) trade
            # Sun 22:00 UTC through Fri 22:00 UTC.
            # Gold spot (XAUUSD) is a forex-like instrument — it trades
            # nearly 24/5, NOT US stock market hours.
            if day == 6:                        # Sunday
                return minute_of_day >= 1320     # 22:00
            if day == 5:                        # Friday
                return minute_of_day < 1320      # before 22:00
            # Mon–Thu — full days
            return True

        case "stocks":
            # US market hours: Mon–Fri 14:30–21:00 UTC
            if day >= 5:                         # Sat–Sun
                return False
            return 870 <= minute_of_day <= 1260   # 14:30–21:00

        case "indices":
            # Same as stocks
            if day >= 5:
                return False
            return 870 <= minute_of_day <= 1260

        case _:
            # Unknown asset class — assume open
            return True
