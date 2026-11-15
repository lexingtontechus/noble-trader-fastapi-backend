"""sessions.py — CRT session windows in US Eastern time (CRT-1).

Port of the EA's session labeling (``IsPreferredSession`` /
``InMinuteWindow``) on Kairos's UTC-native clock: the MQL4 broker-clock +
NthSunday DST code is NOT ported (assessment §12) — ``ZoneInfo("America/
New_York")`` handles DST exactly. Windows are INCLUSIVE at both ends (EA
``InMinuteWindow``: ``m >= start && m <= end``), NY checked before ASIA.

The session label is frozen per setup at C2 close from the C3 bar's OPEN
time — which, at arm time, is the boundary ``c2_close == c3_open`` (the EA
reads ``iTime(sym, tf, s2-1)`` of the then-forming C3 bar; see
worklog/20261115_KAIROS_CRT1_BUILD.md §2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SESS_IDS = {0: "OUT", 1: "NY", 2: "ASIA"}


def _parse_hhmm(text: str) -> int:
    """'09:30' -> minutes-of-day. Raises ValueError on malformed input."""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"session time must be HH:MM, got {text!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"session time out of range: {text!r}")
    return hh * 60 + mm


@dataclass(frozen=True)
class SessionWindows:
    """Two inclusive ET windows, NY evaluated first (EA defaults)."""

    ny_start_m: int = 9 * 60 + 30     # 09:30
    ny_end_m: int = 12 * 60           # 12:00
    asia_start_m: int = 20 * 60       # 20:00
    asia_end_m: int = 22 * 60         # 22:00

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "SessionWindows":
        cfg = cfg or {}
        return cls(
            ny_start_m=_parse_hhmm(cfg.get("ny_start", "09:30")),
            ny_end_m=_parse_hhmm(cfg.get("ny_end", "12:00")),
            asia_start_m=_parse_hhmm(cfg.get("asia_start", "20:00")),
            asia_end_m=_parse_hhmm(cfg.get("asia_end", "22:00")),
        )


def _in_window(m: int, start_m: int, end_m: int) -> bool:
    """EA InMinuteWindow verbatim: inclusive both ends, wraps at midnight."""
    if start_m <= end_m:
        return start_m <= m <= end_m
    return m >= start_m or m <= end_m


def session_of(ts_seconds: float, windows: SessionWindows) -> tuple[int, str, bool]:
    """Session of a UTC epoch timestamp in ET.

    Returns ``(sid, label, in_preferred)`` — sid 1=NY, 2=ASIA, 0=OUT; both
    sessions count as "preferred" (EA UseNewYorkSession/UseAsiaSession true).
    """
    et = datetime.fromtimestamp(float(ts_seconds), tz=timezone.utc).astimezone(ET)
    m = et.hour * 60 + et.minute
    if _in_window(m, windows.ny_start_m, windows.ny_end_m):
        return 1, "NY", True
    if _in_window(m, windows.asia_start_m, windows.asia_end_m):
        return 2, "ASIA", True
    return 0, "OUT", False
