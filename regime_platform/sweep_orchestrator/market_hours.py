"""
Market Hours Check
══════════════════

Ported from supabase/functions/_shared/utils.ts → isMarketOpen().
Determines whether a given asset class is currently tradeable.

Asset class rules
─────────────────
  crypto       : 24/7
  stocks       : Mon–Fri 9:30–16:00 America/New_York (DST-aware)
  indices      : same US cash-equity session as stocks (index CFDs publish
                 during cash hours; the ~23h futures tape is data-only)
  commodities  : Sun 22:00 – Fri 22:00 UTC (gold/silver spot via OANDA —
                 trades like forex, NOT stock market hours)
  metals       : same forex-like session as commodities (2026-10-21 registry-
                 drift correction: post-migration nt_symbol rows for XAU*/
                 XAG* carry "metals"; spot metals trade Sun 22:00 – Fri
                 22:00 UTC, so they must hit THIS arm, not the unknown-class
                 "always open" fall-through)
  forex        : Sun 22:00 – Fri 22:00 UTC
  default      : always True
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def is_market_open(asset_class: str, now: datetime | None = None) -> bool:
    """Return True if the given asset class is currently tradeable.

    Parameters
    ----------
    asset_class
        One of ``crypto``, ``stocks``, ``commodities``, ``forex``.
    now
        Optional timestamp (UTC).  Defaults to ``datetime.now(timezone.utc)``.
        Naive datetimes are treated as UTC (the pre-2026-10-19 contract).

    Returns
    -------
    bool
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    day = now.weekday()         # 0=Mon … 6=Sun
    minute_of_day = now.hour * 60 + now.minute

    match asset_class:
        case "crypto":
            return True

        case "forex" | "commodities" | "metals":
            # Forex AND commodities (gold/silver spot via OANDA) trade
            # Sun 22:00 UTC through Fri 22:00 UTC.
            # Gold spot (XAUUSD) is a forex-like instrument — it trades
            # nearly 24/5, NOT US stock market hours.
            # "metals" (2026-10-21): the post-migration registry value for
            # the XAU*/XAG* rows — same session, and critically NOT the
            # unknown-class "always open" fall-through.
            #
            # Port fixed 2026-10-19: the TS original (supabase/functions/
            # _shared/utils.ts:1753-1758, JS getUTCDay 0=Sun) was mis-
            # ported — its day-6 Saturday-closed branch was dropped and
            # its day-5 Friday rule was left unrenumbered (Python weekday
            # 5 = Saturday), so Saturday ran under the Friday rule and
            # real Fridays fell to the Mon–Thu "always open" fallthrough.
            if day == 6:                        # Sunday
                return minute_of_day >= 1320     # 22:00
            if day == 5:                        # Saturday
                return False
            if day == 4:                        # Friday
                return minute_of_day < 1320      # before 22:00
            # Mon–Thu — full days
            return True

        case "stocks" | "indices" | "index":
            # US market hours: Mon–Fri 9:30–16:00 America/New_York.
            # DST-aware (2026-10-19): the previous 14:30–21:00 UTC hardcode
            # was the EST mapping — an hour off the entire US summer (EDT).
            # "index" (2026-10-21): canonical-key safety alias so a singular
            # registry value cannot fall through to "always open".
            et = now.astimezone(ZoneInfo("America/New_York"))
            return et.weekday() < 5 and 570 <= et.hour * 60 + et.minute <= 960

        case _:
            # Unknown asset class — assume open
            return True
