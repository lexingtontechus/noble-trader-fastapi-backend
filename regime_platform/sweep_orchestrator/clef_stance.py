"""Multi-asset buy/sell/hold stance via Clef-Flash (R5, shadow).

══════════════════════════════════════════════════════════════════════════
2026-10-10, docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §5
Role B. Clef-Flash already returns a three-way {buy, sell, wait} — that IS
buy/sell/hold. This module runs that classifier ACROSS the whole symbol
basket with each symbol's momentum/vol/trend/interaction indicators, and
produces a cross-asset directional STANCE per symbol: which symbols are
buy, which hold, which sell *right now*. It is the paper's multi-asset
trend-following / macro-momentum (151-Trading-Strategies §4.6, §19.2) done
as a learned classifier instead of a fixed rule.

This is a BATCH job, not a per-tick stamp: it runs on the light-sweep slot
over all active symbols and persists one stance row per symbol (an upsert
keyed on symbol, like nt_macro_regime). It does NOT change any signal — it
is a portfolio-level overlay stamped for shadow measurement (does the
cross-asset stance predict per-symbol outcome better than each symbol's own
EV read?).

RESEARCH ONLY. Never an EV source, never a gate, never a live order. Same
fail-open contract as every other Clef-Flash caller: any failure returns
None and the caller leaves the stamp untouched. If CF_* is unconfigured the
whole batch is a no-op (no Worker call, nothing written).
══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.clef_stance")

_STANCE_OPTIONS = {
    "buy": "The setup favours a long / upward resolution for this asset.",
    "sell": "The setup favours a short / downward resolution.",
    "hold": "No directional edge right now — stay flat / wait.",
}

_STANCE_INSTRUCTIONS = (
    "Given this asset's momentum, volatility, trend reads, and how they "
    "INTERACT, is the current setup a buy, a sell, or neither (hold)? "
    "Answer 'hold' when the context does not support a directional stance."
)


@dataclass(frozen=True)
class Stance:
    """One symbol's cross-asset stance."""

    symbol: str
    stance: str            # buy | sell | hold
    confidence: float      # max prob
    probabilities: dict[str, float]
    model: str

    def as_stamp(self) -> str:
        return self.stance


def build_stance_state(
    *,
    symbol: str,
    regime: str | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    momentum_24h: float | None = None,
    vol_ratio: float | None = None,
    deviation_z: float | None = None,
) -> dict[str, Any]:
    """The per-symbol indicator block for the stance classifier.

    Note: NO stated direction is passed (unlike the per-signal direction
    stamp) — the stance classifier is asked to FORM the direction from the
    indicators, not to judge a pre-formed one. That is the whole point of a
    portfolio overlay vs a per-signal veto.
    """
    raw = {
        "symbol": symbol,
        "regime": regime,
        "macro_label": macro_label,
        "trend_label": trend_label,
        "momentum_24h": momentum_24h,
        "vol_ratio": vol_ratio,
        "deviation_z": deviation_z,
    }
    return {k: v for k, v in raw.items() if v is not None}


async def classify_stance(
    *,
    symbol: str,
    regime: str | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    momentum_24h: float | None = None,
    vol_ratio: float | None = None,
    deviation_z: float | None = None,
    model: str | None = None,
) -> Optional[Stance]:
    """Classify one symbol's stance. Returns None on any failure (fail-open)."""
    from regime_platform.services.decision.clef_flash import classify_choice, _MODEL
    state = build_stance_state(
        symbol=symbol, regime=regime, macro_label=macro_label,
        trend_label=trend_label, momentum_24h=momentum_24h,
        vol_ratio=vol_ratio, deviation_z=deviation_z,
    )
    probs = await classify_choice(
        instructions=_STANCE_INSTRUCTIONS,
        options=_STANCE_OPTIONS,
        state=state,
        question_id="stance",
    )
    if not probs:
        return None
    top = max(probs, key=probs.get)
    return Stance(
        symbol=symbol, stance=top, confidence=probs[top],
        probabilities=probs, model=model or _MODEL,
    )
