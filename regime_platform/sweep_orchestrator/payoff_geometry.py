"""Payoff geometry — per-asset-class TP/SL in ATR units (2026-09-26).

worklog/kairos/20260926_PAYOFF_GEOMETRY.md. The geometry replay
(``scripts/kairos_geometry_replay.py``, scope §14.5) re-walked 8,252 past
signals under alternative brackets: the heavy-sweep pick (median TP ≈16 /
SL ≈7.5 ATR) expires 43% of the time at −0.87 ATR/signal, while a far TP
with a close SL is positive in both time halves for forex, equity and
commodities. ``dynamic_atr_mode`` cannot reach that region (TP band capped
at 1.5–2 ATR, heavy grid max TP:SL = 4:1), so this module sets the bracket
directly from config instead of routing it through the heavy grid.

Scope — one lever:
  * Changes ONLY the light-path bracket (brick_size / sl_bricks / tp_bricks
    → stop_loss / take_profit, and therefore EV's profit/stake/RR).
  * Leaves p_win untouched: the orchestrator keeps the heavy-sweep
    ``tp_bricks`` for the Markov T^N horizon, so the blended probability,
    every probability gate and the calibration loop see exactly what they
    see today.
  * Asset classes without an entry (or ``null``) keep the heavy geometry
    (crypto: walk-forward did not beat its recorded geometry).

The bracket is expressed in bricks of ``brick_atr`` × ATR so the existing
brick-count plumbing (payload, dedup hash, settlement contract, pnl_bricks)
is unchanged: e.g. brick_atr 0.5, TP 24 / SL 1.5 ATR → 48 / 3 bricks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key


@dataclass(frozen=True)
class PayoffGeometry:
    asset_class: str          # normalised key (asset_class_key)
    tp_atr: float
    sl_atr: float
    brick_atr: float
    brick_size: float         # price units = brick_atr × ATR
    sl_bricks: int
    tp_bricks: int

    def as_log(self) -> str:
        return (
            f"ac={self.asset_class} tp={self.tp_atr:g}ATR sl={self.sl_atr:g}ATR "
            f"brick={self.brick_size:.6g} ({self.brick_atr:g}ATR) "
            f"sl_bricks={self.sl_bricks} tp_bricks={self.tp_bricks}"
        )


def class_geometry(
    asset_class: str | None, table: Mapping[str, Any] | None,
) -> tuple[float, float] | None:
    """(tp_atr, sl_atr) configured for ``asset_class``, or None (keep heavy)."""
    if not table:
        return None
    entry = table.get(asset_class_key(asset_class))
    if not isinstance(entry, Mapping):
        return None
    try:
        tp, sl = float(entry["tp_atr"]), float(entry["sl_atr"])
    except (KeyError, TypeError, ValueError):
        return None
    if tp <= 0 or sl <= 0 or tp <= sl:
        return None
    return tp, sl


def resolve_payoff_geometry(
    asset_class: str | None,
    atr: float | None,
    *,
    table: Mapping[str, Any] | None,
    brick_atr: float,
) -> PayoffGeometry | None:
    """Bracket for this tick, or None when the heavy geometry should stand.

    None when: no ATR, no/invalid class entry, or a non-positive brick unit.
    Brick counts round to the nearest brick (min 1); the realised ATR
    distances are ``count × brick_atr`` so a config value that isn't a
    multiple of ``brick_atr`` is rounded, not silently distorted.
    """
    geo = class_geometry(asset_class, table)
    if geo is None or not atr or atr <= 0 or brick_atr <= 0:
        return None
    tp_atr, sl_atr = geo
    sl_bricks = max(1, int(round(sl_atr / brick_atr)))
    tp_bricks = max(sl_bricks + 1, int(round(tp_atr / brick_atr)))
    return PayoffGeometry(
        asset_class=asset_class_key(asset_class),
        tp_atr=tp_bricks * brick_atr,
        sl_atr=sl_bricks * brick_atr,
        brick_atr=brick_atr,
        brick_size=float(brick_atr) * float(atr),
        sl_bricks=sl_bricks,
        tp_bricks=tp_bricks,
    )
