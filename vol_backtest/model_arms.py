"""model_arms.py — optional TSFM arms: TimesFM 2.5 (HTTP) and Chronos-2 (in-process).

Both degrade gracefully: if the package is not installed, the model ID is
unreachable, or a per-call error occurs, the arm reports itself unavailable
and the harness records the reason. **Model arms are NEVER required for the
harness, tests, or baselines to run.**

Extraction (both arms) — the 2026-10-18 review's fix:
    feed LOG RETURNS (not price levels) and read volatility from the model's
    own predictive QUANTILE SPREAD (q10..q90 per step):
        sigma_step = (q90_t - q10_t) / (2 * z_0.9);  RV_H = sqrt(sum sigma^2)
    The std of the point forecast measures the conditional mean's slope, not
    dispersion — it is ~0 for a mean-reverting return series regardless of
    model quality (this was the fatal flaw in the thesis's Task 0.2).

``timesfm25`` targets the repo's own service (``server.py``, port 8002,
X-API-Key auth, rate limit) — configure via TIMESFM_BASE_URL /
TIMESFM_API_KEY. The server returns quantile_forecast[series][horizon][q]
with layout "mean, then 10th..90th quantiles" (10 levels) per the TimesFM
2.5 model card; both 9- and 10-level layouts are handled.

``chronos2`` / ``chronos2_cov`` load ``amazon/chronos-2`` (Apache-2.0,
120M encoder) in-process via ``chronos-forecasting>=2.0``
(``Chronos2Pipeline.predict_df``), CPU or GPU (CHRONOS_DEVICE). The covariate
arm is the universal-forecasting endgame: past-only covariates in the context
frame + known-future covariates (calendar) in the future frame — the
capability TimesFM 3.0 would have provided but its non-commercial license
blocks. Chronos-2 quantile levels are user-specified, so tail-aware gates
(q05/q95) are available later without re-plumbing.
"""
from __future__ import annotations

import os
import time

import numpy as np

from .arms import Arm, ArmForecast, rv_from_quantile_spread, Z80

QUANTILE_LEVELS = [0.05, 0.10, 0.50, 0.90, 0.95]

# module-level pipeline cache — one load per model_id (Chronos2 and
# Chronos2Covariate share amazon/chronos-2 by default)
_PIPELINE_CACHE: dict[str, dict] = {}


def _get_pipeline(model_id: str):
    """Load + cache a Chronos2Pipeline. Returns (pipeline, err_reason)."""
    entry = _PIPELINE_CACHE.get(model_id)
    if entry is not None:
        return entry.get("pipeline"), entry.get("error", "")
    try:
        import chronos  # chronos-forecasting>=2.0

        device = os.getenv("CHRONOS_DEVICE", "cpu")
        pipe = chronos.Chronos2Pipeline.from_pretrained(model_id, device_map=device)
        _PIPELINE_CACHE[model_id] = {"pipeline": pipe, "error": ""}
        return pipe, ""
    except Exception as e:
        _PIPELINE_CACHE[model_id] = {"pipeline": None, "error": f"{e.__class__.__name__}: {e}"}
        return None, _PIPELINE_CACHE[model_id]["error"]


# ---------------------------------------------------------------------------
# TimesFM 2.5 via the repo's own server.py
# ---------------------------------------------------------------------------

class TimesFM25HTTP(Arm):
    """Incumbent arm: TimesFM 2.5 through the repo's :8002 /forecast service."""

    name = "timesfm25"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        context: int = 512,
    ) -> None:
        super().__init__()
        self.base_url = (base_url or os.getenv("TIMESFM_BASE_URL", "http://localhost:8002")).rstrip("/")
        self.api_key = api_key or os.getenv("TIMESFM_API_KEY", "")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.context = context
        self._client = None
        self._checked = False
        try:
            import httpx  # in requirements.txt

            self._client = httpx.Client(timeout=timeout_s)
        except Exception as e:  # pragma: no cover - environment dependent
            self.available = False
            self.unavailable_reason = f"httpx unavailable ({e.__class__.__name__})"

    def _probe(self) -> bool:
        """One health probe; caches the result."""
        if self._checked:
            return self.available
        self._checked = True
        if self._client is None:
            return False
        try:
            r = self._client.get(f"{self.base_url}/health", timeout=5.0)
            if r.status_code != 200:
                self.available = False
                self.unavailable_reason = f"health {r.status_code} at {self.base_url}"
                return False
            self.available = True
            return True
        except Exception as e:
            self.available = False
            self.unavailable_reason = f"unreachable {self.base_url} ({e.__class__.__name__})"
            return False

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        if not self._probe():
            return None
        r = np.diff(np.log(np.asarray(prices, dtype=np.float64)))
        r = r[-self.context:]
        if len(r) < 24:
            return None
        payload = {"horizon": int(horizon), "inputs": [np.asarray(r, dtype=float).tolist()]}
        headers = {"X-API-Key": self.api_key} if self.api_key else {}

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/forecast", json=payload, headers=headers
                )
                if resp.status_code == 200:
                    return self._extract(resp.json(), horizon)
                if resp.status_code == 429:  # rate limited — back off, retry
                    time.sleep(min(2.0 ** attempt, 8.0))
                    continue
                self.unavailable_reason = f"/forecast {resp.status_code}"
                return None
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        return None

    @staticmethod
    def _extract(body: dict, horizon: int) -> ArmForecast | None:
        """quantile_forecast layout (TimesFM 2.5 card): [series][horizon][q]
        with q = [mean, q10, q20, ..., q90] (10 levels) — or [q10..q90] (9).
        Extract per-step q10/q90, build RV via the quantile spread.
        """
        try:
            qf = np.asarray(body["quantile_forecast"][0], dtype=np.float64)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if qf.ndim != 2 or qf.shape[0] < horizon:
            return None
        path = qf[:horizon]
        n_levels = path.shape[1]
        if n_levels >= 10:
            lo, hi = path[:, 1], path[:, -1]     # [mean, q10, ..., q90]
        elif n_levels >= 2:
            lo, hi = path[:, 0], path[:, -1]     # [q10, ..., q90]
        else:
            return None
        rv = rv_from_quantile_spread(lo, hi, mass=0.80)
        if not np.isfinite(rv) or rv <= 0:
            return None
        # RV-level 80% interval from the same band (sqrt-scale symmetric)
        half = Z80 * rv / np.sqrt(2.0 * horizon)
        return ArmForecast(
            rv=float(rv),
            lo80=float(max(rv - half, 1e-16)),
            hi80=float(rv + half),
            extra={"elapsed_ms": body.get("elapsed_ms")},
        )


# ---------------------------------------------------------------------------
# Chronos-2 arms (in-process)
# ---------------------------------------------------------------------------

def _calendar_features(ts) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(hour_sin, hour_cos, dow_sin, dow_cos) for datetime64 timestamps."""
    import pandas as _pd

    idx = _pd.DatetimeIndex(ts)
    h = idx.hour.to_numpy().astype(np.float64)
    d = idx.dayofweek.to_numpy().astype(np.float64)
    return (
        np.sin(2 * np.pi * h / 24.0), np.cos(2 * np.pi * h / 24.0),
        np.sin(2 * np.pi * d / 7.0), np.cos(2 * np.pi * d / 7.0),
    )


class _ChronosBase(Arm):
    _model = None
    _model_id: str = ""
    _model_env: str = "CHRONOS_MODEL_ID"

    def __init__(self, context: int = 1024) -> None:
        super().__init__()
        self.context = context
        model_id = os.getenv(self._model_env, self._model_id)
        self.model_id = model_id
        pipe, err = _get_pipeline(model_id)
        if pipe is None:
            self.available = False
            self.unavailable_reason = f"chronos-forecasting not usable ({err})"
            self._model = None
        else:
            self.available = True
            self._model = pipe

    def _predict(self, context_df, future_df, horizon):
        pred = self._model.predict_df(
            context_df,
            future_df=future_df,
            prediction_length=horizon,
            quantile_levels=QUANTILE_LEVELS,
            id_column="id",
            timestamp_column="timestamp",
            target="target",
        )
        return pred

    def _rv_from_pred(self, pred, horizon: int) -> ArmForecast | None:
        """pred: DataFrame with columns like 'target', '0.05', '0.1', ..."""
        import pandas as _pd

        if not isinstance(pred, _pd.DataFrame) or len(pred) < horizon:
            return None
        q10 = _pd.to_numeric(pred.get("0.1"), errors="coerce").to_numpy()[:horizon]
        q90 = _pd.to_numeric(pred.get("0.9"), errors="coerce").to_numpy()[:horizon]
        q05 = _pd.to_numeric(pred.get("0.05"), errors="coerce").to_numpy()[:horizon]
        q95 = _pd.to_numeric(pred.get("0.95"), errors="coerce").to_numpy()[:horizon]
        rv = rv_from_quantile_spread(q10, q90, mass=0.80)
        if not np.isfinite(rv) or rv <= 0:
            return None
        # 90% central band from q05/q95 gives a coverage cross-check level
        half = Z80 * rv / np.sqrt(2.0 * horizon)
        return ArmForecast(
            rv=float(rv),
            lo80=float(max(rv - half, 1e-16)),
            hi80=float(rv + half),
            extra={"q05_last": float(q05[-1]) if np.isfinite(q05[-1]) else None},
        )


class Chronos2(_ChronosBase):
    """Chronos-2 univariate: log returns in, quantile spread out."""

    name = "chronos2"
    _model_env = "CHRONOS_MODEL_ID"
    _model_id = "amazon/chronos-2"

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        if not self.available or self._model is None:
            return None
        import pandas as _pd

        r = np.diff(np.log(np.asarray(prices, dtype=np.float64)))[-self.context:]
        if len(r) < 24:
            return None
        if timestamps is not None and len(timestamps) >= len(r) + 1:
            ts = np.asarray(timestamps)[-(len(r) + 1):-1]
        else:
            # no usable timestamps: fabricate an hourly grid ending "now"
            end = np.datetime64("2026-01-01T00:00", "ns")
            ts = end - np.arange(len(r), 0, -1).astype("timedelta64[h]")
        df = _pd.DataFrame({
            "id": ["symbol"] * len(r),
            "timestamp": ts,
            "target": r,
        })
        try:
            pred = self._predict(df, None, horizon)
            return self._rv_from_pred(pred, horizon)
        except Exception:
            return None


class Chronos2Covariate(_ChronosBase):
    """Chronos-2 with past-only + known-future covariates — the endgame arm.

    Past covariates (aligned with the target's history):
        hour/dow sin-cos encodings, lagged |return|, log trailing 24-bar RV.
    Known-future covariates (calendar only — genuinely known in advance):
        hour/dow sin-cos encodings for the forecast window.

    Nothing here peeks ahead: past covariates are shifted to be visible at
    their own bar; future covariates are pure calendar functions of time.
    """

    name = "chronos2_cov"
    _model_env = "CHRONOS_MODEL_ID"
    _model_id = "amazon/chronos-2"

    def forecast(self, prices, timestamps, horizon) -> ArmForecast | None:
        if not self.available or self._model is None:
            return None
        import pandas as _pd

        p = np.asarray(prices, dtype=np.float64)
        r_full = np.diff(np.log(p))
        if len(r_full) < self.context // 2 or timestamps is None:
            return None

        n = min(len(r_full), self.context)
        r = r_full[-n:]
        ts = np.asarray(timestamps)[-n:]            # timestamps of the returns
        hs, hc, ds, dc = _calendar_features(ts)

        r2 = r * r
        c = np.concatenate(([0.0], np.cumsum(r2)))
        m24 = np.array([
            (c[i] - c[max(i - 24, 0)]) / (i - max(i - 24, 0) + 1) for i in range(n)
        ])
        log_rv24 = np.log(np.maximum(m24, 1e-12))
        abs_r_lag1 = np.concatenate(([0.0], np.abs(r[:-1])))  # visible at bar t

        ctx = _pd.DataFrame({
            "id": ["symbol"] * n,
            "timestamp": ts,
            "target": r,
            "hour_sin": hs, "hour_cos": hc,
            "dow_sin": ds, "dow_cos": dc,
            "abs_ret_lag1": abs_r_lag1,
            "log_rv24": log_rv24,
        })

        step = ts[1] - ts[0]
        future_ts = ts[-1] + step * np.arange(1, horizon + 1)
        fh, fhc, fds, fdc = _calendar_features(future_ts)
        fut = _pd.DataFrame({
            "id": ["symbol"] * horizon,
            "timestamp": future_ts,
            "hour_sin": fh, "hour_cos": fhc,
            "dow_sin": fds, "dow_cos": fdc,
        })
        try:
            pred = self._predict(ctx, fut, horizon)
            return self._rv_from_pred(pred, horizon)
        except Exception:
            return None


def default_arms(model_arms_enabled: bool = True) -> list[Arm]:
    """The standard Phase 0 arm set, availability-resolved at call time."""
    arms: list[Arm] = [
        NaiveRV(),
        EWMARV(),
        LogHAR(),
        HAR_EWMA(),
        GARCH11(),
    ]
    if model_arms_enabled:
        arms += [TimesFM25HTTP(), Chronos2(), Chronos2Covariate()]
    return arms
