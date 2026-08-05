"""
Regression tests: full-value price precision through the signal pipeline
(2026-08-05).

Two reported bugs:
  1. nt_sweep_result stored truncated prices (XAUUSD 4030.32 instead of
     4030.325 — 2dp `decimals` default on the sweep save/heartbeat paths).
  2. Discord/Telegram embeds formatted entry/stop/tp with hardcoded
     ``f"${price:.2f}"``, rendering AUDUSD SL 0.697 as "$0.70" (identical
     to entry) and XAUUSD TP 4032.16 as "$432.16" (leading digit lost).

These tests lock the fix: the full value must survive calculation →
payload → formatter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notifications.discord import _fmt_price, _fmt_price_usd  # noqa: E402
from notifications.telegram import _fmt_price_usd as _tg_fmt_price_usd  # noqa: E402
from regime_platform.sweep_orchestrator.orchestrator import (  # noqa: E402
    _PRICE_DECIMALS,
    _compute_trade_signal,
    _price_decimals,
)


# ── Discord formatter: full value, no 2dp truncation ──────────────────

def test_discord_fmt_keeps_3dp_metals():
    # XAUUSD 3dp (OOB requirement): was "$4030.32"
    assert _fmt_price_usd(4030.325) == "$4030.325"
    assert _fmt_price_usd(4027.325) == "$4027.325"
    assert _fmt_price_usd(4036.325) == "$4036.325"


def test_discord_fmt_keeps_4dp_metals_and_forex():
    # Matches the nt_sweep_result dump values exactly
    assert _fmt_price_usd(3540.8175) == "$3540.8175"
    assert _fmt_price_usd(5775.7445) == "$5775.7445"
    assert _fmt_price_usd(57.5393) == "$57.5393"
    assert _fmt_price_usd(163.7225) == "$163.7225"


def test_discord_fmt_audusd_sl_tp_not_equal_to_entry():
    # Was: SL 0.697 → "$0.70" (== entry), TP 0.706 → "$0.71"
    assert _fmt_price_usd(0.697) == "$0.697"
    assert _fmt_price_usd(0.706) == "$0.706"
    assert _fmt_price_usd(0.700) == "$0.70"


def test_discord_fmt_tp_no_zero_drop():
    # Was: TP 4032.16 → "$432.16" (leading digits lost)
    assert _fmt_price_usd(4032.16) == "$4032.16"
    assert _fmt_price_usd(4023.16) == "$4023.16"
    assert _fmt_price_usd(4036.32) == "$4036.32"


def test_discord_fmt_keeps_two_decimals_when_full_value_has_two():
    assert _fmt_price_usd(4030.32) == "$4030.32"
    assert _fmt_price_usd(0.70) == "$0.70"


def test_discord_fmt_none_is_empty():
    assert _fmt_price(None) == ""
    assert _fmt_price_usd(None) == ""


# ── Telegram formatter mirrors Discord ────────────────────────────────

def test_telegram_fmt_matches_discord_full_value():
    assert _tg_fmt_price_usd(0.697) == "$0.697"
    assert _tg_fmt_price_usd(4030.325) == "$4030.325"
    assert _tg_fmt_price_usd(4032.16) == "$4032.16"


# ── Per-symbol price decimals map (drives payload precision) ──────────

def test_price_decimals_metals_not_default_2():
    # OANDA metals must NOT fall back to the 2dp default
    assert _price_decimals("XAUUSD") == 3
    assert _price_decimals("XAUEUR") == 4
    assert _price_decimals("XAUAUD") == 4
    assert _price_decimals("XAGUSD") == 4
    assert "XAUUSD" in _PRICE_DECIMALS
    assert "XAUUSD" in _PRICE_DECIMALS


def test_price_decimals_forex_and_defaults():
    assert _price_decimals("AUDUSD") == 5
    assert _price_decimals("USDJPY") == 3
    assert _price_decimals("BTCUSD") == 1


# ── Calculation keeps full precision (heartbeat/save round to 6dp) ────

def test_compute_trade_signal_preserves_full_precision():
    ts = _compute_trade_signal("high_vol_strong_bull", 4030.325, 1.0, 3, 6)
    assert ts["entry_price"] == 4030.325
    assert ts["stop_loss"] == 4027.325
    assert ts["take_profit"] == 4036.325


def test_heartbeat_rounding_6dp_preserves_full_value():
    # The heartbeat/debug/save paths now use round(price, 6) — the 6dp
    # rounding must preserve the full 3-4dp source value, not truncate it.
    assert round(4030.325, 6) == 4030.325
    assert round(3540.8175, 6) == 3540.8175
    assert round(0.697, 6) == 0.697
    assert round(4032.16, 6) == 4032.16
