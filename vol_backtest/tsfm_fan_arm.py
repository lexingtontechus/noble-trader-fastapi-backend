"""tsfm_fan_arm.py — the research-harness adapter for the claims layer's
TimesFM vol-fan arm (TimesFM rebuild T2; worklog/
20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md §5 T2 + the T1 build log §7 hooks).

What this is
────────────
``vol_backtest``'s arm contract is ``forecast(prices, timestamps, horizon) ->
ArmForecast`` with ``rv`` on the sqrt(sum r^2 over H bars) scale. The claims
layer's arm (``regime_platform.sweep_orchestrator.claims_tsfm_arm.
TimesFMVolFanArm``, rebuild T1) ships ``variance_path`` only. This file is
the adapter between them — THE ONE place where the research harness sees the
same quantity the live vol_fan fan asserts, so the MZ+k recalibration
(Phase 0.5) is fitted on the fan's own ``σ_raw``:

    rv(H) = sqrt(Σ var_path[:H])          (the cumulative derivation)

which is bit-identical to the fan builder's terminal ``σ_raw`` at h=H
(``claims_vol_fan.build_garch_price_fan``: ``cum_var = cumsum(var_path[:H])``,
``σ_raw = sqrt(cum_var[-1])``) — including the telescoping clamp behavior
(vol contractions floored at 1e-12 and counted, ``last_negative_increments``).
The recalib cell's ``m = k·exp(a)·F^(b-1)`` then multiplies the identical
quantity offline and live; nothing is fitted on a related-but-different
statistic.

The arm key is the contract
──────────────────────────
``name = "tsfm"`` is load-bearing, not cosmetic. The chain is:

    this class's ``name``  →  records.csv ``arm`` column  →  recalib cell key
    (symbol, horizon, arm)  →  claims lookup ``vol_fan_multiplier(cells,
    symbol, arm_name, h, σ_raw)`` — keyed off the ``claims_vol_fan_arm`` flag
    value, whose whitelist is ``garch|gjr|egarch|tsfm``.

Registering this adapter under ``timesfm25`` (or any other key) would mint
cells the claims layer can never find — identity calibration forever, the
silent-dead-artifact trap recorded in the T1 build log §7. ``timesfm25``
itself stays in the catalog untouched: it is a DIFFERENT arm answering a
different question (log-RETURN input, per-STEP q10/q90 spreads → per-step RV
aggregate — the Brini-style research comparison), while ``tsfm`` feeds
log-PRICE levels and reads the h-step TERM STRUCTURE (the claims derivation,
T1's design §2). Both are legitimate; neither is a cleanup of the other.

Availability / fail-open (the model-arms convention)
────────────────────────────────────────────────────
Constructible whenever httpx imports and the claims arm module is importable;
the :8002 service is probed lazily on the first ``forecast`` call and the
probe result is cached per instance (one 5 s probe, not a retry storm — the
T1 convention). Service down, malformed body, quantile crossing, degenerate
spread ⇒ ``None`` ⇒ the harness records NaN and reports the arm's
successes=0 in its runtime table. Never an exception, never a crash of the
run, never a fabricated number.

Determinism: the adapter adds zero randomness on top of the T1 arm (itself
randomness-free); a frozen window and an unchanged service response produce a
byte-equal rv. The no-lookahead contract is inherited from the harness (it
slices ``prices[:t+1]`` before the call); timestamps are ignored — bars are
treated as consecutive steps, the same univariate convention as the
``timesfm25``/``chronos2`` arms.

The mint this enables (operator action, offline — design §5 T2/§8):

    python scripts/run_vol_backtest.py --data-dir data/vol_backtest \
        --symbols BTCUSD,XAUUSD,XAGUSD,USDJPY,USDCHF \
        --arms garch,gjr,egarch,tsfm --horizons 6,12,24,48
    python -m vol_backtest.recalib --run-dir <run_dir>
    # → recalib.json cells keyed (symbol, horizon, 'tsfm') → copy/point at
    #   config/claims_vol_fan_recalib.json (mtime hot-reload, no restart).
"""
from __future__ import annotations

import numpy as np

from .arms import Z80, Arm, ArmForecast


class TimesFMVolFanResearchArm(Arm):
    """``tsfm`` — the claims vol_fan arm under the research-harness contract.

    Wraps ONE ``TimesFMVolFanArm`` instance (single source of truth for the
    spread → σ_total → telescoping derivation; this class contains no math of
    its own beyond the H-bar collapse and the interval convention).
    """

    name = "tsfm"  # the claims layer's arm key — see module docstring

    def __init__(self, inner: Arm | None = None) -> None:
        super().__init__()
        self._inner = None
        if inner is not None:  # tests may inject a stub
            self._inner = inner
            if not getattr(inner, "available", False):
                self.available = False
                self.unavailable_reason = getattr(inner, "unavailable_reason", "") or "inner arm unavailable"
            return
        # Lazy import: vol_backtest must stay importable without the
        # regime_platform tree (the package's research-tooling boundary);
        # the claims arm pulls in only stdlib + httpx, so no import cycle
        # (claims_vol_fan's vol_backtest.arms import is equally lazy).
        try:
            from regime_platform.sweep_orchestrator.claims_tsfm_arm import (
                TimesFMVolFanArm,
            )
        except Exception as exc:
            self.available = False
            self.unavailable_reason = f"claims_tsfm_arm import failed ({exc.__class__.__name__})"
            return
        try:
            self._inner = TimesFMVolFanArm()
        except Exception as exc:
            self.available = False
            self.unavailable_reason = f"TimesFMVolFanArm construction failed ({exc.__class__.__name__})"
            self._inner = None
            return
        if not self._inner.available:
            self.available = False
            self.unavailable_reason = self._inner.unavailable_reason or "tsfm arm unavailable"
            self._inner = None

    def forecast(
        self,
        prices: np.ndarray,
        timestamps: np.ndarray | None,
        horizon: int,
    ) -> ArmForecast | None:
        if not self.available or self._inner is None:
            return None
        horizon = int(horizon)
        if horizon < 1:
            return None
        try:
            var_path = self._inner.variance_path(prices, horizon)
        except Exception:
            return None  # fail-open, the model-arms convention
        if var_path is None:
            return None
        var_path = np.asarray(var_path, dtype=np.float64)
        if var_path.ndim != 1 or len(var_path) < horizon:
            return None
        if not np.isfinite(var_path).all() or np.any(var_path <= 0):
            return None
        # THE T2 derivation: rv = sqrt(sum var_path[:H]) — the fan's own
        # terminal σ_raw, clamp behavior included (the identical quantity
        # the recalib m multiplies). Never a re-read of the raw spread.
        var_h = float(np.sum(var_path[:horizon]))
        rv = float(np.sqrt(max(var_h, 0.0)))
        if not np.isfinite(rv) or rv <= 0:
            return None
        # Interval: the family convention (EWMA/GARCH/timesfm25) — normal
        # approx on the sqrt scale, half-width Z80·rv/√(2H). The recalib
        # standardizes by whatever the arm CLAIMS (σ_f from lo80/hi80), so
        # the claimed-but-understated interval is exactly what k repairs.
        half = Z80 * rv / np.sqrt(2.0 * horizon)
        clamped = int(getattr(self._inner, "last_negative_increments", 0) or 0)
        extra = {"tsfm_clamped_increments": clamped} if clamped else None
        return ArmForecast(
            rv=float(rv),
            lo80=float(max(rv - half, 1e-16)),
            hi80=float(rv + half),
            extra=extra,
        )
