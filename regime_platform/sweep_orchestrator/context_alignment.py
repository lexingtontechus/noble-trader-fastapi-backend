"""
Context-alignment gate — Tier 1, corrected (worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 10).

The review doc's original Tier 1 proposal was to scale ``p_win`` by a
"trend context scalar" before the qualification gate — that would mean
reworking the EV blend, which is off the table (see
[[ev_engine_no_rework]], the MSE precedent). This module achieves the
same intent — a signal that both longitudinal filters confidently
oppose should not qualify purely on the 5-minute EV blend's own
say-so — as a completely independent 8th gate instead. It never reads
or writes ``p_win``/``ev_result``; ``ev_engine.py`` is untouched.

Traced the real gap in ``orchestrator.py::_process_symbol`` before
building this (not assumed): ``kelly_min_ok`` already IS
context-aware, because ``kelly_macro_multiplier``/
``kelly_trend_multiplier`` mutate ``effective_kelly`` before that gate
reads it. ``p_win_ok``/``ev_allowed`` are the real gap — neither
formula references Kelly or either multiplier at all, so a confidently
counter-trend signal can clear both purely on the EV blend's own
5-minute read.
"""

from __future__ import annotations

_BEARISH_TREND_LABELS = ("reversal_down", "trending_down")
_BULLISH_TREND_LABELS = ("reversal_up", "trending_up")


def compute_context_alignment(
    signal: str,
    macro_label: str | None,
    trend_label: str | None,
) -> bool:
    """True (pass) unless BOTH available longitudinal reads confidently
    oppose the trade direction.

    Requiring both to agree in opposition — not just one — is
    deliberate: a single noisy source disagreeing is already handled
    proportionally by the existing Kelly multipliers (a partial
    penalty, not an outright reject). This gate targets only the case
    those multipliers cannot reach: everything says no, yet the
    5-minute EV blend still fired. A neutral/consolidating/missing read
    from either source is never treated as evidence against a trade —
    same no-op convention ``kelly_macro_multiplier``/
    ``kelly_trend_multiplier`` already use.
    """
    if signal not in ("buy", "sell"):
        return True

    implied = "bull" if signal == "buy" else "bear"

    macro_opposes = macro_label is not None and macro_label not in ("neutral", implied)

    trend_opposes = (
        (implied == "bull" and trend_label in _BEARISH_TREND_LABELS)
        or (implied == "bear" and trend_label in _BULLISH_TREND_LABELS)
    )

    return not (macro_opposes and trend_opposes)
