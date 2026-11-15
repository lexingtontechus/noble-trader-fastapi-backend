"""router.py — the /crt read surface (CRT-1, renko-seam mount in main.py).

Read-only diagnostics (the assessment: "read surface becomes REST + the
claims leaderboard" — CRT-2 adds claims). GET only, no auth (the renko
``/renko/health`` precedent for operator diagnostics), no writes, no
side effects:

- ``GET /crt/health``  — process health + mode + engine count (never lies
  about liveness; ``crt_mode`` says whether the engine is dark);
- ``GET /crt/state``   — per-(symbol, tf) snapshots: option monitor states
  (NORMAL/CAUTION/OFF/DISABLED, CUSUM S, live/rolling EV), open positions,
  frozen decisions, last tick + CRT-2 persistence status;
- ``GET /crt/claims``  — resolved ``crt_option`` claims: per-option and
  pooled evidence (n / win-rate / EV / t / LCB) — CRT-3's gate surface;
- ``GET /crt/promotion`` — the leaderboard-gated live verdict (bars vs
  evidence; the refusal is never silent).

FastAPI is optional (the renko pattern): without it ``crt_router`` is None
and main.py's guarded mount skips. Mount (main.py, next to the renko seam):

    try:
        from crt.router import crt_router
    except Exception:
        crt_router = None
    ...
    if crt_router is not None:
        app.include_router(crt_router)
"""
from __future__ import annotations

import time

try:
    from fastapi import APIRouter
    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - repo requirement, guarded like renko
    FASTAPI_AVAILABLE = False

if FASTAPI_AVAILABLE:
    crt_router = APIRouter(prefix="/crt", tags=["crt"])

    def _mode() -> str:
        try:
            from regime_platform.core.feature_flags import get_crt_mode
            return get_crt_mode()
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return "off"

    @crt_router.get("/health")
    async def crt_health() -> dict:
        from crt import jobs
        return {
            "status": "ok",
            "service": "crt-engine",
            "engine": "crt v1 (CRT-1)",
            "crt_mode": _mode(),
            "engines": len(jobs._ENGINES),
            "artifact_rows": (jobs._META or {}).get("rows") or 0,
            "timestamp": time.time(),
        }

    @crt_router.get("/state")
    async def crt_state() -> dict:
        from crt import jobs, promotion
        mode = _mode()
        if mode == "off":
            return {"crt_mode": "off",
                    "note": "CRT-1 ships dark — flip crt_mode to shadow to "
                            "arm the paper engine (config/feature_flags.yaml)"}
        try:
            view = jobs.engine_snapshots()
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            return {"crt_mode": mode, "error": repr(exc)}
        view["crt_mode"] = mode
        view["mode_note"] = ("live dispatch is CRT-3 leaderboard-gated "
                             "(claims evidence — /crt/promotion); unmet bars "
                             "run paper semantics"
                             if mode == "live" else "")
        try:
            view["promotion"] = await promotion.current_verdict(
                engines=list(jobs._ENGINES.values()))
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            view["promotion"] = {"error": repr(exc)}
        return view

    @crt_router.get("/claims")
    async def crt_claims(limit: int = 2000) -> dict:
        """CRT-2: resolved crt_option claims — the evidence pool.

        Read-only aggregation (n / win-rate / EV / t / LCB per option and
        pooled); the CRT-3 promotion gate reads the same kernels. Never
        raises; unconfigured Supabase or a failed fetch yields an honest
        empty view.
        """
        from crt import claims as crt_claims_mod
        limit = max(1, min(int(limit), 5000))
        try:
            rows = await crt_claims_mod.fetch_resolved_claims(limit=limit)
            view = crt_claims_mod.claims_view(rows)
            view["limit"] = limit
            return view
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            return {"kind": crt_claims_mod.CRT_CLAIM_KIND, "error": repr(exc),
                    "pooled": None, "per_option": [], "n_rows": 0}

    @crt_router.get("/promotion")
    async def crt_promotion() -> dict:
        """CRT-3: the leaderboard-gated live promotion verdict.

        The claims evidence vs the pre-registered bars (the optimizer's own
        enable thresholds applied to live paper evidence — n >= 30, EV >
        0.02R after costs, t >= 1.0, LCB > 0 at z=1.28, no enabled option in
        decay OFF). TTL-cached; never raises. "live" runs paper semantics
        until every bar holds — the operator flips the flag, the LEDGER
        arbitrates (no operator eyeballing; the TSFM-sidecar contract).
        """
        from crt import jobs, promotion
        mode = _mode()
        try:
            verdict = await promotion.current_verdict(
                engines=list(jobs._ENGINES.values()))
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            return {"crt_mode": mode, "error": repr(exc)}
        return {"crt_mode": mode, **verdict}

else:
    crt_router = None  # type: ignore[assignment]
