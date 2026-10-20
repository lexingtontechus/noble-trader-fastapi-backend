"""barrier_floor.py — LIVE physics floor on published bracket geometry.

══════════════════════════════════════════════════════════════════════════
2026-10-19. Task 40-b (W-G2/G4 of the 2026-10-19 weaknesses evaluation,
kairos-review-worklogs/20261019_KAIROS_WEAKNESSES_EVALUATION.md Part 3).

THE DEFECT
──────────
Tight-SL bands are SL-farms by physics. Computed with the repo's OWN
``barrier.barrier_null`` (σ = 1 ATR/bar, T = 48 bars):

    XAUUSD band centre 4.94 / 1.00  → π_L = P(SL first) = 0.831
    BTCUSD band centre 8.07 / 1.77  → π_L = 0.791
    every configured band-centre has EV ≤ −cost under the driftless null

and NOTHING physics- or reachability-based constrained the published
bracket: the entire barrier machinery (PBM null/posterior, first-passage,
reachability modes) was shadow/off/dead. The only live constraints were
the 10-day empirical envelope-walk inside operator bands plus downstream
EV floors — risk was moved, not removed (the 1-2 ATR bucket measures
6.9% TP / 93.1% SL).

THE FIX (LIVE, per the no-more-shadow mandate)
──────────────────────────────────────────────
``barrier_floor`` flag block: mode live, pi_sl_cap 0.62 (measured: the
FOREX band centre 4.5/2.75 sits at π_L = 0.614 — the last geometry family
that clears 0.62 without starvation), pi_e_cap 0.25 stamp+warn only while
settlement expiry is globally fixed at 48h (enforcing it would starve the
wide equity bands — US500 π_E = 0.70 — outright).

``apply_barrier_floor(pick, cfg)`` runs where the pick passes its band
checks (tiered_atr read-side clamp + the walked pick before persist):

  * computes π_SL (and π_E) with ``barrier.barrier_null`` — the repo's
    physics authority — and ALWAYS stamps pi_tp / pi_sl / pi_e onto the
    pick (live and shadow; DynamicAtrPick metadata + param-store JSON);
  * over cap → minimally adjust WITHIN the symbol's band: widen sl_atr
    to the band max first, then narrow tp_atr to the band min (the RR
    side-effect is judged downstream — the rr ≥ min_rr gate tests the
    shipped geometry post-W-C1);
  * unachievable → the pick is REJECTED with reason ``barrier_floor_sl_cap``
    (returned as None — the existing no-pick fallback path handles
    absence: light keeps the brick bracket, heavy keeps its own pick);
  * ``pi_first_passage`` is the closed-form gambler's-ruin twin (no
    deadline, drift-capable) — cross-checked against ``barrier_null`` in
    tests/test_w_g2_barrier_floor.py; ``barrier_probs_mc`` is the
    vectorized Monte-Carlo cross-check.

Fail-safe: any internal failure returns the pick unchanged (a stamping
problem must never block geometry). Pure math + flags; no I/O.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.barrier_floor")


# ── Closed forms ────────────────────────────────────────────────────────────


def pi_first_passage(
    sl_atr: float,
    tp_atr: float,
    drift_atr_bar: float = 0.0,
    expiry_bars: Optional[int] = None,
) -> float:
    """P(price hits SL before TP), σ = 1 ATR/bar.

    Driftless, no deadline (gambler's ruin): ``tp / (tp + sl)`` — the
    farther the TP relative to the SL, the likelier the SL is hit first.
    With per-bar drift μ (ATR units), κ = 2μ/σ² = 2μ:

        P(SL first) = (e^{−κ·sl} − e^{−κ(sl+tp)}) / (1 − e^{−κ(sl+tp)})

    which reduces to the ruin form as κ → 0 and DECREASES for positive
    (upward) drift, as it must.

    When ``expiry_bars`` is given and drift ≈ 0, this delegates to
    ``barrier.barrier_null`` (the repo's physics authority — method-of-
    images double-barrier first passage) for the deadline-truncated
    probability: at the shipped bands the 48h deadline is binding enough
    that the no-deadline ruin overstates π_SL by up to ~0.03 (BTCUSD
    8.07/1.77: ruin 0.820 vs null 0.791 — the difference is the 5.9%
    expiry mass). Lazy import: barrier imports tiered_atr at module load,
    and tiered_atr imports THIS module lazily (same cycle discipline as
    the eod_resolver/posterior pair).
    """
    a, b = float(sl_atr), float(tp_atr)
    if a <= 0 or b <= 0:
        return 0.5
    if expiry_bars is not None and abs(float(drift_atr_bar)) < 1e-12:
        from regime_platform.sweep_orchestrator.barrier import barrier_null

        return float(barrier_null(b, a, t_bars=int(expiry_bars)).p_loss)
    kappa = 2.0 * float(drift_atr_bar)
    if abs(kappa) < 1e-12:
        return b / (a + b)
    num = math.exp(-kappa * a) - math.exp(-kappa * (a + b))
    den = 1.0 - math.exp(-kappa * (a + b))
    if abs(den) < 1e-15:
        return b / (a + b)
    return num / den


def barrier_probs_mc(
    sl_atr: float,
    tp_atr: float,
    expiry_bars: int = 48,
    n_paths: int = 20000,
    seed: int = 7,
    drift_atr_bar: float = 0.0,
) -> tuple[float, float, float]:
    """Vectorized Monte-Carlo first-passage cross-check (σ = 1 ATR/bar,
    hourly steps). Returns ``(pi_tp, pi_sl, pi_e)``.

    Independent verification of ``barrier_null`` on the same contract:
    driftless BM, first barrier touched wins, unresolved at T = expired.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    steps = np.cumsum(
        rng.normal(float(drift_atr_bar), 1.0, size=(int(n_paths), int(expiry_bars))),
        axis=1,
    )
    hit_tp = steps >= float(tp_atr)
    hit_sl = steps <= -float(sl_atr)
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), int(expiry_bars))
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), int(expiry_bars))
    # a single step cannot touch both barriers, so ti == si is impossible
    # for resolved paths; ties resolve to expiry only when neither hit.
    pi_tp = float((hit_tp.any(1) & (ti < si)).mean())
    pi_sl = float((hit_sl.any(1) & (si < ti)).mean())
    pi_e = 1.0 - pi_tp - pi_sl
    return pi_tp, pi_sl, max(0.0, pi_e)


# ── Flag reads (explicit override dict for tests) ───────────────────────────


def _floor_flags(flags: Optional[dict[str, Any]]) -> tuple[str, float, float]:
    """(mode, pi_sl_cap, pi_e_cap) — ``flags`` wins per-key, else the
    feature_flags block; malformed values fall back to the defaults."""
    try:
        from regime_platform.core.feature_flags import (
            get_barrier_floor_mode,
            get_barrier_floor_pi_e_cap,
            get_barrier_floor_pi_sl_cap,
        )
        mode = str(get_barrier_floor_mode()).lower()
        sl_cap = float(get_barrier_floor_pi_sl_cap())
        e_cap = float(get_barrier_floor_pi_e_cap())
    except Exception:
        mode, sl_cap, e_cap = "live", 0.62, 0.25
    if isinstance(flags, dict):
        if flags.get("mode") is not None:
            mode = str(flags["mode"]).lower()
        try:
            if flags.get("pi_sl_cap") is not None:
                sl_cap = float(flags["pi_sl_cap"])
            if flags.get("pi_e_cap") is not None:
                e_cap = float(flags["pi_e_cap"])
        except (TypeError, ValueError):
            pass
    return mode, sl_cap, e_cap


def _stamp(pick, null) -> Any:
    """Attach the analytic null probabilities to the pick's metadata."""
    import dataclasses

    return dataclasses.replace(
        pick,
        pi_tp=round(float(null.p_win), 4),
        pi_sl=round(float(null.p_loss), 4),
        pi_e=round(float(null.p_expire), 4),
    )


def _band_extreme(cfg: Optional[dict[str, Any]], side: str, hi: bool) -> Optional[float]:
    """The in-band extreme for a side ('sl'|'tp'), preferring an exact grid
    member (keeps _pick_in_band grid membership intact) over the clamp."""
    if not isinstance(cfg, dict):
        return None
    grid = [float(x) for x in (cfg.get(f"{side}_atr") or []) if x is not None]
    lo = cfg.get(f"{side}_atr_min")
    hi_v = cfg.get(f"{side}_atr_max")
    try:
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi_v) if hi_v is not None else None
    except (TypeError, ValueError):
        return None
    in_band = [
        g for g in grid
        if (lo_f is None or g >= lo_f - 1e-9) and (hi_f is None or g <= hi_f + 1e-9)
    ]
    if in_band:
        return max(in_band) if hi else min(in_band)
    return hi_f if hi else lo_f


def apply_barrier_floor(
    pick,
    cfg: Optional[dict[str, Any]] = None,
    *,
    flags: Optional[dict[str, Any]] = None,
    symbol: Optional[str] = None,
    expiry_bars: Optional[int] = None,
):
    """Enforce/stamp the barrier-physics floor on one geometry pick.

    Args:
        pick: a ``DynamicAtrPick`` (or None — passthrough).
        cfg: the symbol's dynamic_atr band config (grids + clamps) the
            adjustment must stay inside.
        flags: explicit {mode, pi_sl_cap, pi_e_cap} override (tests);
            None reads the feature_flags block.
        symbol: log context only.
        expiry_bars: deadline in bars; None → tiered_atr.EXPIRY_BARS (48).

    Returns:
        The (possibly adjusted, always stamped) pick — or ``None`` when the
        pick is over cap and no in-band adjustment can bring π_SL under it
        (reason ``barrier_floor_sl_cap``; the existing no-pick fallback path
        handles absence). Any internal failure returns the pick unchanged.
    """
    if pick is None:
        return None
    try:
        import dataclasses

        from regime_platform.sweep_orchestrator.barrier import barrier_null
        from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS

        T = int(expiry_bars) if expiry_bars else int(EXPIRY_BARS)
        mode, pi_sl_cap, pi_e_cap = _floor_flags(flags)
        if mode == "off":
            return pick

        null = barrier_null(float(pick.tp_atr), float(pick.sl_atr), t_bars=T)
        stamped = _stamp(pick, null)
        _sym = symbol or "?"

        # π_E is stamp+warn only while the deadline is globally fixed: a hard
        # cap at 0.25 would starve the wide equity bands (US500 π_E = 0.70).
        if null.p_expire > pi_e_cap:
            logger.warning(
                "[barrier:floor] %s expiry-farm geometry — pi_e=%.4f > %.2f "
                "(stamp+warn only; deadline fixed at %dh): %s",
                _sym, null.p_expire, pi_e_cap, T, null.as_log(),
            )

        if null.p_loss <= pi_sl_cap:
            return stamped

        if mode != "live":
            logger.info(
                "[barrier:floor] %s SHADOW would adjust/reject — pi_sl=%.4f > "
                "%.2f (%s)",
                _sym, null.p_loss, pi_sl_cap, null.as_log(),
            )
            return stamped

        # Over cap: minimally adjust WITHIN the band — widen SL to band max
        # first (lowers π_SL monotonically), then also narrow TP to band min.
        sl_hi = _band_extreme(cfg, "sl", hi=True)
        tp_lo = _band_extreme(cfg, "tp", hi=False)
        candidates: list[tuple[float, float, str]] = []
        if sl_hi is not None and sl_hi > float(pick.sl_atr) + 1e-9:
            candidates.append((float(pick.tp_atr), sl_hi, "sl_widened_to_band_max"))
            if tp_lo is not None and tp_lo < float(pick.tp_atr) - 1e-9:
                candidates.append((tp_lo, sl_hi, "sl_max_tp_narrowed_to_band_min"))
        elif tp_lo is not None and tp_lo < float(pick.tp_atr) - 1e-9:
            candidates.append((tp_lo, float(pick.sl_atr), "tp_narrowed_to_band_min"))

        for tp_c, sl_c, how in candidates:
            adj_null = barrier_null(tp_c, sl_c, t_bars=T)
            if adj_null.p_loss <= pi_sl_cap:
                adjusted = dataclasses.replace(
                    stamped, tp_atr=tp_c, sl_atr=sl_c,
                )
                adjusted = _stamp(adjusted, adj_null)
                logger.info(
                    "[barrier:floor] %s ADJUSTED (%s) — pi_sl %.4f → %.4f "
                    "(cap %.2f): tp %.2f→%.2f sl %.2f→%.2f",
                    _sym, how, null.p_loss, adj_null.p_loss, pi_sl_cap,
                    float(pick.tp_atr), tp_c, float(pick.sl_atr), sl_c,
                )
                return adjusted
            logger.info(
                "[barrier:floor] %s adjustment candidate insufficient (%s): "
                "pi_sl=%.4f > %.2f",
                _sym, how, adj_null.p_loss, pi_sl_cap,
            )

        logger.info(
            "[barrier:floor] %s REJECT (barrier_floor_sl_cap) — pi_sl=%.4f > "
            "%.2f and no in-band adjustment achieves the cap (%s); falling "
            "back to the no-pick path",
            _sym, null.p_loss, pi_sl_cap, null.as_log(),
        )
        return None
    except Exception as exc:  # fail-safe: never block geometry on a stamp bug
        logger.debug(
            "[barrier:floor] %s failed (%s: %s) — pick unchanged",
            symbol or "?", type(exc).__name__, exc,
        )
        return pick
