"""arms.py — forecast arms for the rolling-origin RV backtest.

Two families:

  **Baselines (always available, pure numpy)** — these ARE the bar:
    * ``naive``       RV over the last H bars (persistence level)
    * ``ewma``        RiskMetrics EWMA of squared returns (lambda=0.94)
    * ``har``         Log-HAR (Corsi) on hourly RV — THE reference bar per
                      Brini (2026): only TTM beat Log-HAR, by 1.3–1.8%;
                      every other TSFM including TimesFM did not.
    * ``har_ewma``    equal-weight geometric blend (the Brini-style
                      "simple average matches the best single" fallback)
    * ``garch``       GARCH(1,1) via the `arch` package — OPTIONAL, skipped
                      with a reason when not installed
    * ``gjr``         GJR-GARCH(1,1)-t (threshold/TGARCH, Student-t)
                      via `arch` — the asymmetry variant the crypto
                      GARCH-selection literature keeps flagging for BTC
    * ``egarch``      EGARCH(1,1)-t via `arch` — log-variance asymmetry,
                      multi-step by seeded simulation (arch has no analytic
                      multi-step for EGARCH)

  **Model arms (optional, degrade gracefully)** — the thesis's candidates:
    * ``timesfm25``      incumbent TimesFM 2.5 via the repo's own server.py
                         :8002 `/forecast` — vol extracted from the QUANTILE
                         SPREAD (q10/q90), NOT the std of the point forecast
                         (the 2026-10-18 review's critical fix: the std of a
                         conditional-mean path measures trend slope, not
                         dispersion, and is guaranteed ~0 on returns)
    * ``chronos2``       Chronos-2 univariate — log-return input, per-step
                         sigma from quantile spread
    * ``chronos2_cov``  Chronos-2 with past + known-future covariates
                         (calendar, lagged |r|, trailing RV) — the universal-
                         forecasting endgame arm (TimesFM 3.0 is blocked by
                         its non-commercial license; Chronos-2 is Apache-2.0)

Every arm implements::

    name: str
    available: bool                 # False -> harness skips with reason
    unavailable_reason: str
    forecast(prices, timestamps, horizon) -> ArmForecast | None

`ArmForecast.rv` is on the sqrt(sum r^2 over H bars) scale; `lo80/hi80` are
the central 80% interval when the arm produces one.

NO-LOOKAHEAD CONTRACT: `forecast()` receives ONLY bars up to the cutoff.
"""
from __future__ import annotations

from dataclasses import dataclass
import os

import numpy as np

from .realized import log_returns, rv_trailing

EPS = 1e-12
Z80 = 1.2815515655446004  # Phi^-1(0.90); central 80% width = 2*Z80*sigma


@dataclass
class ArmForecast:
    rv: float                 # forecast of sqrt(sum r^2) over next H bars
    lo80: float | None = None  # central 80% interval lower bound
    hi80: float | None = None  # central 80% interval upper bound
    extra: dict | None = None


class Arm:
    name: str = "base"

    def __init__(self) -> None:
        self.available: bool = True
        self.unavailable_reason: str = ""

    def forecast(
        self,
        prices: np.ndarray,
        timestamps: np.ndarray | None,
        horizon: int,
    ) -> ArmForecast | None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Naive (persistence)
# ---------------------------------------------------------------------------

class NaiveRV(Arm):
    """RV over the last `horizon` bars — the persistence-level null."""

    name = "naive"

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        r = log_returns(prices)
        if len(r) < horizon:
            return None
        rv = float(np.sqrt(np.sum(r[-horizon:] ** 2)))
        if not np.isfinite(rv) or rv <= 0:
            return None
        return ArmForecast(rv=rv)


# ---------------------------------------------------------------------------
# EWMA (RiskMetrics)
# ---------------------------------------------------------------------------

class EWMARV(Arm):
    """EWMA of squared returns; H-step variance = H * ewma variance."""

    name = "ewma"

    def __init__(self, lam: float = 0.94, min_bars: int = 120) -> None:
        super().__init__()
        self.lam = lam
        self.min_bars = min_bars

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        r = log_returns(prices)
        if len(r) < self.min_bars:
            return None
        w = self.lam ** np.arange(len(r) - 1, -1, -1)  # oldest .. newest
        w = w / w.sum()
        var = float(np.dot(w, r * r))
        sigma = float(np.sqrt(max(var, 0.0)))
        if not np.isfinite(sigma) or sigma <= 0:
            return None
        rv = sigma * float(np.sqrt(horizon))
        # Normal approx on the sqrt scale: RV_hat ~ sigma*sqrt(H),
        # with the interval width shrinking with the effective sample 2H
        half = Z80 * sigma * np.sqrt(horizon) / np.sqrt(2.0 * horizon)
        return ArmForecast(
            rv=float(rv),
            lo80=float(max(rv - half, 1e-16)),
            hi80=float(rv + half),
        )


# ---------------------------------------------------------------------------
# Log-HAR (Corsi) — the reference bar
# ---------------------------------------------------------------------------

class LogHAR(Arm):
    """Log-HAR on hourly squared returns.

    Target (log space): mean r^2 over next H bars.
    Regressors (log space): mean r^2 over last 1, 24, 120 bars
    (current-hour, daily, weekly components — Corsi 2009, hourly version).

    Refit by OLS (with a tiny ridge for singularity) on a trailing window at
    every cutoff — point-in-time, no lookahead. The 80% interval comes from
    the empirical residual quantiles of the training window (q10/q90 in log
    space, exponentiated onto the forecast).
    """

    name = "har"

    def __init__(self, train_window: int = 2160, min_train: int = 720) -> None:
        super().__init__()
        self.train_window = train_window
        self.min_train = min_train

    @staticmethod
    def _features(r2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(X, y) design matrices on log-mean-squared-return scale.

        Row t uses r2[:t+1] only; y[t] = log(mean r^2 over next H). The H
        offset is applied by the caller (features are H-independent).
        """
        n = len(r2)
        c = np.concatenate(([0.0], np.cumsum(r2)))
        # component means ending at t (inclusive)
        def mean_end(h: int) -> np.ndarray:
            hi = np.arange(1, n + 1)
            lo = np.maximum(hi - h, 0)
            s = c[hi] - c[lo]
            cnt = (hi - lo).astype(np.float64)
            return s / cnt

        m1 = mean_end(1)
        m24 = mean_end(24)
        m120 = mean_end(120)
        X = np.column_stack([
            np.log(np.maximum(m1, EPS)),
            np.log(np.maximum(m24, EPS)),
            np.log(np.maximum(m120, EPS)),
        ])
        return X

    def _fit_predict(self, r2: np.ndarray, horizon: int) -> tuple[float, float, float] | None:
        n = len(r2)
        if n < self.min_train + horizon + 1:
            return None
        c = np.concatenate(([0.0], np.cumsum(r2)))

        # Training pairs: features X_all[t] (uses r2[0..t] ONLY) predict the
        # target over the NEXT horizon bars [t+1, t+1+H) — the +1 shift is
        # the no-lookahead contract; pairing row t with [t, t+H) would leak
        # r2[t] (already inside the features) into the target.
        t_hi = n - horizon - 1                     # last valid training t
        t_lo = max(119, 0)                         # m120 feature needs 120 bars
        if t_hi - t_lo + 1 < self.min_train // 2:
            return None
        t_idx = np.arange(t_lo, t_hi + 1)
        y = np.log(np.maximum((c[t_idx + 1 + horizon] - c[t_idx + 1]) / horizon, EPS))

        X_all = self._features(r2)
        X = X_all[t_idx]
        # trailing training window
        lo = max(0, len(y) - self.train_window)
        Xw, yw = X[lo:], y[lo:]

        A = np.column_stack([np.ones(len(Xw)), Xw])
        ridge = 1e-8 * np.eye(A.shape[1])
        try:
            beta, *_ = np.linalg.lstsq(A.T @ A + ridge, A.T @ yw, rcond=None)
        except np.linalg.LinAlgError:
            return None

        x_new = X_all[n - 1]  # latest visible feature row
        a = np.concatenate(([1.0], x_new))
        y_hat = float(a @ beta)
        rv = float(np.exp(y_hat) * horizon) ** 0.5

        # residual empirical quantiles on the training window (log space)
        resid = yw - A @ beta
        q10, q90 = np.quantile(resid, [0.10, 0.90])
        lo80 = float(np.sqrt(np.maximum(np.exp(y_hat + q10) * horizon, EPS)))
        hi80 = float(np.sqrt(np.exp(y_hat + q90) * horizon))
        return rv, lo80, hi80

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        r = log_returns(prices)
        r2 = r * r
        out = self._fit_predict(r2, horizon)
        if out is None:
            return None
        rv, lo, hi = out
        if not np.isfinite(rv) or rv <= 0:
            return None
        return ArmForecast(rv=rv, lo80=lo, hi80=hi)


# ---------------------------------------------------------------------------
# HAR + EWMA geometric blend (Brini-style "simple average" fallback)
# ---------------------------------------------------------------------------

class HAR_EWMA(Arm):
    """Equal-weight geometric blend of Log-HAR and EWMA in variance space."""

    name = "har_ewma"

    def __init__(self, har: LogHAR | None = None, ewma: EWMARV | None = None) -> None:
        super().__init__()
        self.har = har or LogHAR()
        self.ewma = ewma or EWMARV()

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        f_h = self.har.forecast(prices, timestamps, horizon)
        f_e = self.ewma.forecast(prices, timestamps, horizon)
        if f_h is None or f_e is None:
            return None
        var = 0.5 * (f_h.rv ** 2 + f_e.rv ** 2)  # equal weight in variance space
        rv = float(np.sqrt(var))
        lo = None
        hi = None
        if f_h.lo80 is not None and f_e.lo80 is not None:
            lo = float(np.sqrt(0.5 * (f_h.lo80 ** 2 + f_e.lo80 ** 2)))
            hi = float(np.sqrt(0.5 * (f_h.hi80 ** 2 + f_e.hi80 ** 2)))
        return ArmForecast(rv=rv, lo80=lo, hi80=hi)


# ---------------------------------------------------------------------------
# GARCH family — optional (`arch` package), unit-variance rescaled
#
# Standardization note: returns are rescaled to unit variance before the MLE
# (r' = r / s0, s0 = std of the window's returns) and the forecast variance
# is converted back — the lab-proven convention (omega on the raw FX scale
# ~1e-8 starves the optimizer; the historical *100 hack still left EGARCH
# fits diverging on USDCHF with alpha=-68/beta=1.0). Fits remain per-call
# (rolling window semantics, frozen at each cutoff).
# ---------------------------------------------------------------------------

class _ArchArmBase(Arm):
    """Shared machinery: availability guard, unit-variance rescale, H-step
    aggregate variance, sqrt-scale 80% interval."""

    vol: str = "Garch"
    dist: str = "normal"
    order: dict | None = None

    def __init__(self, min_bars: int = 720) -> None:
        super().__init__()
        self.min_bars = min_bars
        try:
            import arch  # noqa: F401

            self._arch = True
        except Exception as e:  # pragma: no cover - environment dependent
            self._arch = False
            self.available = False
            self.unavailable_reason = f"arch not installed ({e.__class__.__name__})"

    def _fit_forecast_variance(self, r: np.ndarray, horizon: int) -> np.ndarray | None:
        """H-step conditional-variance path in ORIGINAL return units, or None."""
        s0 = float(np.std(r))
        if not np.isfinite(s0) or s0 <= 0:
            return None
        try:
            from arch import arch_model

            kw = self.order or {}
            am = arch_model(r / s0, vol=self.vol, dist=self.dist, rescale=False, **kw)
            res = am.fit(disp="off", show_warning=False)
            f = self._forecast(res, horizon)
            var_path = np.asarray(f.variance.values[0], dtype=float)[:horizon]
            var_path = var_path * (s0 * s0)  # back to original units
            if not np.isfinite(var_path).all() or np.any(var_path <= 0):
                return None
            return var_path
        except Exception:
            return None

    def _forecast(self, res, horizon: int):
        """Analytic multi-step forecast (GARCH/GJR). EGARCH overrides: arch
        has no analytic multi-step for EGARCH, so seeded simulation."""
        return res.forecast(horizon=horizon, reindex=False)

    def variance_path(self, prices, horizon: int) -> np.ndarray | None:
        """Public H-step conditional-variance path in ORIGINAL return units.

        The exact per-horizon path ``forecast()`` collapses into the H-bar
        RV aggregate — exposed as a thin public seam for the claims layer's
        Phase D vol_fan price fan (regime_platform.sweep_orchestrator.
        claims_vol_fan; worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4).
        Same guards as ``forecast``: unavailable arm, short window, or a
        degenerate fit ⇒ None. Deterministic for a frozen window (analytic
        for GARCH/GJR, seeded simulation for EGARCH).
        """
        if not self.available:
            return None
        r = log_returns(prices)
        if len(r) < self.min_bars:
            return None
        return self._fit_forecast_variance(r, horizon)

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        if not self.available:
            return None
        r = log_returns(prices)
        if len(r) < self.min_bars:
            return None
        var_path = self._fit_forecast_variance(r, horizon)
        if var_path is None:
            return None
        var_h = float(np.sum(var_path[:horizon]))
        rv = float(np.sqrt(max(var_h, 0.0)))
        if not np.isfinite(rv) or rv <= 0:
            return None
        # interval: normal approx on the sqrt scale (sampling noise of RV_H
        # shrinks with the effective sample 2H — same convention as EWMA)
        half = Z80 * rv / np.sqrt(2.0 * horizon)
        return ArmForecast(rv=rv, lo80=float(max(rv - half, 1e-16)), hi80=float(rv + half))


class GARCH11(_ArchArmBase):
    """GARCH(1,1), normal innovations; H-step variance via the analytic
    recursive forecast. The classic conditional-variance incumbent."""

    name = "garch"
    vol = "Garch"
    dist = "normal"
    order = {"p": 1, "q": 1}


class GJRGARCH11T(_ArchArmBase):
    """GJR-GARCH(1,1) (threshold/TGARCH) with Student-t innovations —
    negative shocks hit the variance harder; the asymmetry variant the
    crypto GARCH-selection literature keeps flagging for BTC. H-step
    variance via arch's analytic recursive forecast (the gamma/2
    persistence term is handled internally)."""

    name = "gjr"
    vol = "Garch"
    dist = "t"
    order = {"p": 1, "o": 1, "q": 1}


class EGARCH11T(_ArchArmBase):
    """EGARCH(1,1) with Student-t innovations — asymmetry in log-variance
    space, no positivity constraints on parameters, multiplicative
    volatility dynamics. arch has NO analytic multi-step forecast for
    EGARCH, so the H-step variance path is the mean of seeded simulations
    (fixed RNG -> reproducible; non-finite paths degrade to None)."""

    name = "egarch"
    vol = "EGARCH"
    dist = "t"
    order = {"p": 1, "o": 1, "q": 1}

    def __init__(self, min_bars: int = 720, simulations: int = 300, seed: int = 7) -> None:
        super().__init__(min_bars=min_bars)
        self.simulations = simulations
        self.seed = seed

    def _forecast(self, res, horizon: int):
        sim_rng = np.random.default_rng(self.seed)
        return res.forecast(
            horizon=horizon,
            method="simulation",
            simulations=self.simulations,
            rng=sim_rng.standard_normal,
            reindex=False,
        )


# ---------------------------------------------------------------------------
# Quantile-spread extraction shared by the TSFM arms
# ---------------------------------------------------------------------------

def rv_from_quantile_spread(
    q_lo: np.ndarray,
    q_hi: np.ndarray,
    mass: float = 0.80,
) -> float:
    """Per-step sigma from a central quantile band, aggregated to H-bar RV.

    sigma_step = (q_hi - q_lo) / (2 * Phi^-1(0.5 + mass/2))
    RV_H = sqrt(sum sigma_step^2)

    This is the corrected extraction: dispersion comes from the MODEL'S OWN
    predictive quantiles, not from the point path's slope.
    """
    from scipy import stats as _st

    zq = float(_st.norm.ppf(0.5 + mass / 2.0))
    q_lo = np.asarray(q_lo, dtype=np.float64)
    q_hi = np.asarray(q_hi, dtype=np.float64)
    ok = np.isfinite(q_lo) & np.isfinite(q_hi) & (q_hi > q_lo)
    if not np.any(ok):
        return float("nan")
    spread = (q_hi[ok] - q_lo[ok]) / (2.0 * zq)
    var = float(np.sum(spread ** 2))
    return float(np.sqrt(var))
