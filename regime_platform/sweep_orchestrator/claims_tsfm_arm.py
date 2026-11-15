"""Claims layer, TimesFM vol-fan arm — the sidecar seam (rebuild T1).

worklog/20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md §2 + §4. The TimesFM
rebuild's one real deliverable: a ``variance_path`` arm whose forecast comes
from the repo's own TimesFM service (``server.py`` :8002) so the claims
layer's existing fan builder, recalibration, resolver, and leaderboard all
work on it unchanged.

What this arm is (the design doc's §1, one sentence): the TimesFM sidecar
becomes the vol_fan arm — its native quantile spread supplies the variance
path of the existing price fan. What it is NOT: no new fan shape (the fan
stays ``entry · exp(z · σ_raw · m)`` on the fixed q05..q95 grid), no service
change, no direction-path change (``p_timesfm`` is untouched; its decommission
is the design doc's separate T4).

The variance-path derivation (design §2, the one real math)
──────────────────────────────────────────────────────────
The GARCH arms' seam is ``variance_path(prices, horizon)`` — per-bar
conditional variance in original log-return units, deterministic for a frozen
window. This arm derives the same object from the model's own term structure:

1. Input: the last ``context`` (=512) hourly closes of the claim window,
   LOG-transformed. The forecast point at horizon h is then the log PRICE at
   T+h, so the model's q10..q90 spread at h IS the h-step cumulative return
   σ — the term structure, unmodified (this is the deliberate difference
   from the research arm ``TimesFM25HTTP``, which feeds per-bar log RETURNS
   and reads per-STEP spreads for the harness's RV aggregate).
2. ``σ_total(h) = (q90(h) − q10(h)) / (2 · Φ⁻¹(0.90))``.
3. Per-bar variance by telescoping:
   ``var_path[h−1] = σ_total(h)² − σ_total(h−1)²`` so that ``cumsum``
   reproduces the model's own ``σ_total(h)`` at every h. A vol-contraction
   forecast can make an increment negative — clamped to a small positive
   floor and COUNTED (``last_negative_increments``), which the registration
   stamps into ``claimed_probs`` (honesty over silence). Quantile CROSSING
   (q90 < q10 — malformed output, not contraction) rejects the whole path.

Fail-open everywhere (Phase A/D convention): httpx missing, service
unreachable, malformed response, degenerate spread ⇒ ``None`` ⇒ no claim,
never an exception, never a sweep impact (the caller runs this inside the
fire-and-forget claim task via ``asyncio.to_thread``).

Determinism: the arm itself contains zero randomness — a frozen window and
an unchanged service response produce a byte-equal fan. Server-side, the
torch eval-mode forward with ``fix_quantile_crossing=True`` is the model's
own determinism bar; first-live deployment should re-run the frozen-window
byte-equality probe (design §7.1) against the real service.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("regime.sweep.claims.tsfm")

# Mirrors vol_backtest.arms.Z80 (kept local so this module stays free of the
# vol_backtest import graph — the claims layer owns its seam).
_Z80 = 1.2815515655446004  # Phi^-1(0.90); central 80% width = 2*Z80*sigma

# Floor for negative telescoping increments (vol contraction): variance of
# hourly log returns is ~1e-5..1e-3, so 1e-12 is numerically zero but keeps
# cumsum strictly positive for the fan builder's guards.
_NEG_VAR_FLOOR = 1e-12

TSFM_CONTEXT_BARS = 512  # the design's fill-the-context-exactly (200M class)
TSFM_TIMEOUT_S = 10.0    # the orchestrator's TimesFM HTTP timeout convention
TSFM_PROBE_TIMEOUT_S = 5.0
TSFM_MAX_RETRIES = 1     # bounded: worst case ~2 calls + 0.5s backoff inside the detached task


def _normalize_forecast_url(raw: str) -> str:
    """Accept both conventions: a full ``.../forecast`` URL (the
    orchestrator's TIMESFM_INFERENCE_URL) or a base URL (the research arm's
    TIMESFM_BASE_URL)."""
    u = str(raw or "").strip().rstrip("/")
    return u if u.endswith("/forecast") else f"{u}/forecast"


class TimesFMVolFanArm:
    """The claims layer's TimesFM arm — ``name='tsfm'``, ``source`` stamped
    ``'tsfm_sidecar'`` by the registration (already legal in migration 218's
    CHECK, so no migration).

    Availability follows the ``TimesFM25HTTP`` pattern: constructible whenever
    httpx imports (``available=True``); service reachability is probed lazily
    on the first ``variance_path`` call and cached per instance, so a down
    service costs one 5 s health probe, not a retry storm.
    """

    name = "tsfm"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = TSFM_TIMEOUT_S,
        context: int = TSFM_CONTEXT_BARS,
        max_retries: int = TSFM_MAX_RETRIES,
        min_bars: int = 25,
    ) -> None:
        # URL priority: the orchestrator's production chain first (explicit
        # override, legacy alias), then the research arm's TIMESFM_BASE_URL,
        # then the server.py default. /health derives by suffix swap.
        raw = (
            base_url
            or os.getenv("TIMESFM_INFERENCE_URL")
            or os.getenv("TIMESFM_URL")
            or os.getenv("TIMESFM_BASE_URL")
            or "http://localhost:8002"
        )
        self._forecast_url = _normalize_forecast_url(raw)
        self._health_url = (
            self._forecast_url[: -len("/forecast")] + "/health"
            if self._forecast_url.endswith("/forecast")
            else self._forecast_url + "/health"
        )
        # Key: the production convention (comma list, first entry) with the
        # research arm's single-key name as fallback.
        self.api_key = (
            api_key
            or (os.getenv("TIMESFM_API_KEYS", "").split(",")[0].strip()
                or os.getenv("TIMESFM_API_KEY", ""))
        )
        self.timeout_s = float(timeout_s)
        self.context = int(context)
        self.max_retries = int(max_retries)
        self.min_bars = int(min_bars)
        self.last_negative_increments: int = 0
        self._client: Any = None
        self._checked = False
        self.available: bool = True
        self.unavailable_reason: str = ""
        try:
            import httpx  # in requirements.txt

            self._client = httpx.Client(timeout=self.timeout_s)
        except Exception as exc:  # pragma: no cover - environment dependent
            self._client = None
            self.available = False
            self.unavailable_reason = f"httpx unavailable ({exc.__class__.__name__})"

    # ── service plumbing (the TimesFM25HTTP pattern, bounded for claims) ──

    def _probe(self) -> bool:
        """One /health probe; caches the result on this instance."""
        if self._checked:
            return self.available
        self._checked = True
        if self._client is None:
            return False
        try:
            r = self._client.get(self._health_url, timeout=TSFM_PROBE_TIMEOUT_S)
            if r.status_code != 200:
                self.available = False
                self.unavailable_reason = f"health {r.status_code} at {self._health_url}"
                return False
            return True
        except Exception as exc:
            self.available = False
            self.unavailable_reason = f"unreachable {self._health_url} ({exc.__class__.__name__})"
            return False

    def _post_forecast(self, series: list[float], horizon: int) -> dict[str, Any] | None:
        """POST /forecast; None on failure (bounded retries, no raise)."""
        payload = {"horizon": int(horizon), "inputs": [series]}
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(self._forecast_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:  # rate limited — back off, retry
                    time.sleep(min(2.0**attempt, 4.0))
                    continue
                self.unavailable_reason = f"/forecast {resp.status_code}"
                return None
            except Exception:
                time.sleep(0.25 * (attempt + 1))
        return None

    # ── the claims seam ────────────────────────────────────────────────────

    def variance_path(self, prices, horizon: int):
        """Per-bar conditional variance in original log-return units.

        Length = ``horizon``; deterministic for a frozen window; ``None`` on
        any guard (never a partial path). See the module docstring for the
        spread → σ_total → telescoping derivation.
        """
        self.last_negative_increments = 0
        if not self._probe():
            return None
        try:
            import numpy as np
        except Exception as exc:  # pragma: no cover - numpy is in requirements
            self.unavailable_reason = f"numpy unavailable ({exc.__class__.__name__})"
            return None

        try:
            horizon = int(horizon)
            px = np.asarray(list(prices), dtype=np.float64)
        except (TypeError, ValueError):
            return None
        if horizon < 1 or px.ndim != 1 or px.size < self.min_bars:
            return None
        if not np.isfinite(px).all() or np.any(px <= 0):
            return None

        # The design's input: last `context` closes, log-transformed — the
        # forecast spread at h is then the h-step cumulative log-return σ.
        lp = np.log(px[-self.context :])
        body = self._post_forecast(lp.tolist(), horizon)
        if body is None:
            return None

        try:
            qf = np.asarray(body["quantile_forecast"][0], dtype=np.float64)
        except (KeyError, IndexError, TypeError, ValueError):
            self.unavailable_reason = "quantile_forecast missing/malformed"
            return None
        if qf.ndim != 2 or qf.shape[0] < horizon:
            self.unavailable_reason = f"quantile_forecast shape {tuple(qf.shape)} < horizon {horizon}"
            return None
        path = qf[:horizon]
        n_levels = path.shape[1]
        # Layout (TimesFM 2.5 card, the TimesFM25HTTP precedent): [mean, q10,
        # ..., q90] (10 levels) or [q10, ..., q90] (9). q10/q90 = first/last
        # quantile column either way.
        if n_levels >= 10:
            lo, hi = path[:, 1], path[:, -1]
        elif n_levels >= 2:
            lo, hi = path[:, 0], path[:, -1]
        else:
            self.unavailable_reason = f"quantile levels {n_levels} unusable"
            return None
        if not (np.isfinite(lo).all() and np.isfinite(hi).all()):
            return None
        if np.any(hi < lo):  # quantile CROSSING = malformed output, not contraction
            self.unavailable_reason = "quantile crossing (q90 < q10)"
            return None

        sigma_total = (hi - lo) / (2.0 * _Z80)
        if float(np.nanmax(sigma_total)) <= 0.0:  # all-zero spread = degenerate
            self.unavailable_reason = "degenerate zero spread"
            return None

        # Telescoping: cumsum(var_path) == σ_total(h)² at every h.
        s2 = np.empty(horizon + 1, dtype=np.float64)
        s2[0] = 0.0
        s2[1:] = sigma_total**2
        var_path = np.diff(s2)
        n_neg = int(np.count_nonzero(var_path < 0.0))
        if n_neg:
            var_path = np.maximum(var_path, _NEG_VAR_FLOOR)
        self.last_negative_increments = n_neg
        if not np.isfinite(var_path).all() or np.any(var_path <= 0):
            return None
        return var_path
