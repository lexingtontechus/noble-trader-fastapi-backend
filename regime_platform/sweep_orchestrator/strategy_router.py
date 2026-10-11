"""Strategy Router (R1, shadow) — resolves a signal to one archetype.

══════════════════════════════════════════════════════════════════════════
Scope: docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §4, R1.

R1 is the DECISION half of the router+registry split. R0 (config/
strategy_registry.yaml + feature_flags loader) is the CATALOG; this module
reads that catalog and resolves each signal to exactly one archetype (or
stand_by). It does NOT hardcode archetype definitions — it reads them from
get_strategy_registry()/get_router_policy().

SHADOW ONLY. router.mode ships at "shadow": route() computes and returns the
archetype, and the caller stamps it on nt_signal_sim, but NO live geometry,
deadline, or calibration is changed. The first live consumer is R6, one
archetype at a time behind its own flag. Until then this is a pure
measurement instrument: R2's whole job is to ask "does the stamped archetype
predict outcome better than the uniform engine?" — which is impossible to
answer until the archetype is actually stamped, which is what R1 enables.

Deterministic + unit-tested: route() is a pure function of its inputs and the
catalog, so a given (symbol, direction, regime, macro, trend) always resolves
to the same archetype. No I/O, no randomness, no model call. Fail-open: on any
unexpected input it returns stand_by (the safe no-trade default), never raises
into the sweep tick.

The predicate vocabulary is defined in config/strategy_registry.yaml's header:
  asset_classes  : asset_class_key values this archetype may route on
  symbols        : optional explicit allow-list (FOREX = major FX)
  directions     : optional "buy"/"sell" allow-list
  requires_opposes : direction must OPPOSE both macro and trend (counter-trend)
  requires_aligned : direction must AGREE with macro and trend
  regime_vol     : optional vol-tier allow-list (from the HMM regime label)
  enabled        : master on/off (loader defaults False)
══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from regime_platform.core.feature_flags import (
    get_router_policy,
    get_strategy_registry,
)

logger = logging.getLogger("regime.sweep.strategy_router")

# Vol tier extracted from the HMM regime label, e.g. "med_high_vol_bull" ->
# "med_high_vol". Ordered longest-prefix-first so "med_high_vol" matches before
# "med_low_vol"/"med_vol" could collide (they don't overlap, but explicit is
# better). The registry's regime_vol allow-list uses these keys.
_VOL_TIERS = ("med_high_vol", "med_low_vol", "high_vol", "med_vol", "low_vol")

# Major FX pairs sharing the FOREX key (same set as dynamic_atr.by_symbol.FOREX).
_FOREX_MAJORS = frozenset({
    "AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY",
})

# Macro/trend vocabularies (macro_filter.py / trend_filter.py).
_BULLISH_TREND = ("trending_up", "reversal_up")
_BEARISH_TREND = ("trending_down", "reversal_down")


@dataclass(frozen=True)
class RoutedStrategy:
    """The router's verdict for one signal."""

    archetype: str            # resolved archetype name, or "stand_by"
    matched: bool             # True if a catalog archetype matched; False = default
    trade_shape: dict[str, Any] | None  # the archetype's entry/exit/risk block, if matched
    reason: str               # human-readable why (for the shadow log)

    def as_stamp(self) -> str:
        """The value persisted on nt_signal_sim.strategy_archetype."""
        return self.archetype


def _vol_tier(regime_label: str | None) -> str | None:
    """Extract the vol tier from a composite HMM regime label.

    "low_vol_bull" -> "low_vol"; "med_high_vol_bear" -> "med_high_vol";
    None/unknown -> None (a signal with no readable regime matches no
    regime_vol-gated archetype, which is the safe direction).
    """
    if not regime_label:
        return None
    low = str(regime_label).lower()
    for tier in _VOL_TIERS:
        if tier in low:
            return tier
    return None


def _direction_word(signal: str | None) -> str | None:
    """Normalize the sweep's buy/sell into the registry's direction vocab.

    The sweep emits "buy"/"sell"/"neutral"; the registry's `directions` and
    the macro/trend comparison use "buy"/"sell". neutral -> None (never routes
    to a directional archetype).
    """
    if not signal:
        return None
    s = str(signal).lower()
    return s if s in ("buy", "sell") else None


def _alignment(direction: str, macro_label: str | None, trend_label: str | None) -> str | None:
    """Classify the direction vs the slow reads as 'opposes' | 'aligned' | None.

    Tri-state, both-required for a definite verdict:
      * 'opposes'  — BOTH macro and trend are directional AND both point against
                    the trade (the mean-reversion condition).
      * 'aligned'  — BOTH macro and trend are directional AND both point with
                    the trade (the momentum condition).
      * None       — ANY read is neutral/missing (macro not bull/bear, trend
                    'consolidating'/None). A consolidating trend is neither
                    opposed nor aligned — it is UNKNOWN, and must route neither
                    to mean_reversion nor momentum (stand_by).

    This tri-state is what makes the consolidation regime route correctly:
    era-clean XAUUSD buy consolidating 8.1% vs trending_down 38.2% — the
    consolidating case has no edge in EITHER direction, so no directional
    archetype may claim it.
    """
    if not direction:
        return None
    want = "bull" if direction == "buy" else "bear"

    macro_dir = None  # 'with' | 'against'
    if macro_label is not None and str(macro_label).lower() in ("bull", "bear"):
        macro_dir = "with" if str(macro_label).lower() == want else "against"

    trend_dir = None
    if trend_label is not None:
        tl = str(trend_label).lower()
        if tl in _BULLISH_TREND or tl in _BEARISH_TREND:
            trend_is_bull = tl in _BULLISH_TREND
            trend_dir = "with" if trend_is_bull == (want == "bull") else "against"

    # Any neutral/missing read -> unknown (None). Both must be definite.
    if macro_dir is None or trend_dir is None:
        return None
    if macro_dir == "against" and trend_dir == "against":
        return "opposes"
    if macro_dir == "with" and trend_dir == "with":
        return "aligned"
    # Mixed (one with, one against) -> no confident verdict either way.
    return None


def _asset_class_allowed(blk: dict[str, Any], asset_class_key: str) -> bool:
    allowed = blk.get("asset_classes")
    if not allowed:
        return True  # no constraint
    return asset_class_key in allowed


def _symbol_allowed(blk: dict[str, Any], symbol: str) -> bool:
    allowed = blk.get("symbols")
    if not allowed:
        return True
    sym = str(symbol).strip().upper()
    if "FOREX" in allowed and sym in _FOREX_MAJORS:
        return True
    return sym in {str(s).upper() for s in allowed}


def _direction_allowed(blk: dict[str, Any], direction: str | None) -> bool:
    allowed = blk.get("directions")
    if not allowed or direction is None:
        return True
    return direction in allowed


def _regime_vol_allowed(blk: dict[str, Any], vol_tier: str | None) -> bool:
    allowed = blk.get("regime_vol")
    if not allowed:
        return True
    if vol_tier is None:
        return False  # a vol-gated archetype needs a readable regime
    return vol_tier in allowed


def _alignment_allowed(
    blk: dict[str, Any], alignment: str | None
) -> bool:
    """Apply requires_opposes / requires_aligned against the tri-state read.

    requires_opposes matches only on 'opposes'; requires_aligned only on
    'aligned'. A None (unknown — any neutral/missing read, or a mixed
    with/against pair) fails BOTH: the safe direction is to not claim a
    counter-trend or aligned setup we can't confidently evidence, so it
    falls through to stand_by.
    """
    if blk.get("requires_opposes") and alignment != "opposes":
        return False
    if blk.get("requires_aligned") and alignment != "aligned":
        return False
    return True


def route(
    *,
    symbol: str,
    signal: str | None,
    regime_label: str | None = None,
    asset_class: str | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
) -> RoutedStrategy:
    """Resolve one signal to a strategy archetype (shadow — no live effect).

    Iterates the catalog in router.precedence order and returns the FIRST
    enabled archetype whose predicate matches; if none match, returns
    router.default_on_no_match (stand_by). Deterministic: same inputs -> same
    verdict. Never raises (fail-open to stand_by on any unexpected input).

    ``asset_class`` is the raw nt_symbol.asset_class string; it is normalized
    here via the same asset_class_key() the rest of the config uses, so the
    registry's asset_classes list speaks the shared vocabulary.
    """
    try:
        return _route_impl(
            symbol=symbol, signal=signal, regime_label=regime_label,
            asset_class=asset_class, macro_label=macro_label, trend_label=trend_label,
        )
    except Exception as exc:  # noqa: BLE001 — router must never break a sweep tick
        logger.warning("[strategy:route] %s failed (%s: %s) — stand_by", symbol, type(exc).__name__, exc)
        return RoutedStrategy(
            archetype="stand_by", matched=False, trade_shape=None,
            reason=f"router error: {type(exc).__name__}",
        )


def _route_impl(
    *,
    symbol: str,
    signal: str | None,
    regime_label: str | None,
    asset_class: str | None,
    macro_label: str | None,
    trend_label: str | None,
) -> RoutedStrategy:
    # Normalize asset_class via the shared key (imported lazily to avoid a
    # module-level sweep_orchestrator dependency in core-facing code).
    from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key
    ack = asset_class_key(asset_class)
    direction = _direction_word(signal)
    vol_tier = _vol_tier(regime_label)
    alignment = _alignment(direction, macro_label, trend_label)

    registry = get_strategy_registry()
    policy = get_router_policy()
    precedence = policy.get("precedence") or list(registry.keys())

    # A neutral / unreadable direction never routes to a directional archetype.
    if direction is None:
        return RoutedStrategy(
            archetype=policy.get("default_on_no_match", "stand_by"),
            matched=False, trade_shape=None,
            reason="no directional signal (neutral)",
        )

    for name in precedence:
        blk = registry.get(name)
        if not isinstance(blk, dict) or not blk.get("enabled"):
            continue
        if not _asset_class_allowed(blk, ack):
            continue
        if not _symbol_allowed(blk, symbol):
            continue
        if not _direction_allowed(blk, direction):
            continue
        if not _regime_vol_allowed(blk, vol_tier):
            continue
        if not _alignment_allowed(blk, alignment):
            continue
        # Matched. trade_shape = the archetype's entry/exit/risk block.
        shape = {
            "entry": blk.get("entry"),
            "exit": blk.get("exit"),
            "risk": blk.get("risk"),
        }
        return RoutedStrategy(
            archetype=name, matched=True, trade_shape=shape,
            reason=(
                f"{name}: ack={ack} dir={direction} vol={vol_tier} "
                f"alignment={alignment}"
            ),
        )

    return RoutedStrategy(
        archetype=policy.get("default_on_no_match", "stand_by"),
        matched=False, trade_shape=None,
        reason=f"no enabled archetype matched (ack={ack} dir={direction} alignment={alignment})",
    )
