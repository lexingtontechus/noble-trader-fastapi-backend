"""Variable binary Kelly (shadow) — 2026-09-27.

Sizing follows the same tiering idea as dynamic ATR, but the context is
Kairos **event-time transition** (type + freshness), never regime age
(V2/V4 disproved age-based knobs).

Base is classic binary Kelly on the signal's own p_win and payoff:

    f* = (p·b − q) / b ,  b = TP/SL reward-risk ,  q = 1−p

Then a context multiplier m (shrink toward 1.0) scales size:

    size_shadow = f* × kelly_fraction × m

**Shadow only.** Nothing here gates publish or writes live position size.
Historical-mu Kelly (PositionSizer) is no longer a quality gate (see
worklog/20260927_CHANGELOG_EV_ENGINE_KELLY_GATE.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Context priors — frozen from Kairos R1 event-time / type contrasts
# (kairos_rank.FRESHNESS_W / TYPE_W). NOT age. NOT re-fit per day.
_FRESH_M = {
    "fresh": 1.12,
    "warm": 1.00,
    "pre": 0.95,
    "stale": 0.88,
    "deep": 0.85,
}
_TYPE_M = {
    "dir_flip": 1.12,
    "both": 1.06,
    "none": 1.00,
    "vol_expand": 1.02,
    "vol_contract": 0.98,
}
# Vol tier (dynamic-ATR parallel): mild, measurement-first.
_VOL_M = {
    "low_vol": 1.05,
    "med_low_vol": 1.00,
    "med_high_vol": 0.98,
    "high_vol": 0.92,
}

_M_MIN, _M_MAX = 0.70, 1.35


@dataclass(frozen=True)
class VariableKellyResult:
    p_win: float | None
    reward_risk: float | None
    binary_f: float | None  # full f* in [0, 1]
    context_mult: float | None
    effective_shadow: float | None  # binary_f * fraction * m
    notes: tuple[str, ...] = ()


def _finite(x: float | None) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def binary_kelly(
    p_win: float | None,
    reward_risk: float | None,
    *,
    fraction: float = 0.5,
) -> float | None:
    """Classic two-outcome Kelly, fractional. None if inputs unusable.

    f* = max(0, (p*b − (1−p)) / b); returns fraction × f* clipped to [0, 1].
    """
    p = _finite(p_win)
    b = _finite(reward_risk)
    if p is None or b is None or b <= 0:
        return None
    p = min(1.0, max(0.0, p))
    q = 1.0 - p
    f_star = (p * b - q) / b
    if f_star <= 0:
        return 0.0
    frac = min(1.0, max(0.0, float(fraction)))
    return min(1.0, f_star * frac)


def context_multiplier(
    *,
    event_window: str | None = None,
    transition_type: str | None = None,
    vol_tier: str | None = None,
) -> float | None:
    """Size multiplier from Kairos transition context. Never uses regime age.

    m = product of (freshness, type, vol-tier) priors, clipped to [_M_MIN, _M_MAX].
    """
    parts = []
    mf = _FRESH_M.get(event_window or "", None)
    mt = _TYPE_M.get(transition_type or "", None)
    mv = _VOL_M.get(vol_tier or "", None)
    if mf is not None:
        parts.append(mf)
    if mt is not None:
        parts.append(mt)
    if mv is not None:
        parts.append(mv)
    if not parts:
        return None
    # Geometric mean keeps one loud prior from dominating when others are absent.
    log_sum = sum(math.log(x) for x in parts)
    m = math.exp(log_sum / len(parts))
    return max(_M_MIN, min(_M_MAX, m))


def variable_kelly(
    *,
    p_win: float | None,
    reward_risk: float | None,
    event_window: str | None = None,
    transition_type: str | None = None,
    vol_tier: str | None = None,
    fraction: float = 0.5,
) -> VariableKellyResult:
    """Shadow variable binary Kelly. Fail-open to None fields on bad input."""
    notes: list[str] = []
    b = binary_kelly(p_win, reward_risk, fraction=fraction)
    if b is None:
        notes.append("binary_f=none: bad p_win/rr")
    m = context_multiplier(
        event_window=event_window,
        transition_type=transition_type,
        vol_tier=vol_tier,
    )
    if m is None:
        notes.append("m=none: no recognized context (default 1.0 applied to size)")
        m_eff = 1.0
    else:
        m_eff = m
    eff = None if b is None else min(1.0, b * m_eff)
    return VariableKellyResult(
        p_win=_finite(p_win),
        reward_risk=_finite(reward_risk),
        binary_f=b,
        context_mult=m,
        effective_shadow=eff,
        notes=tuple(notes),
    )
