"""router.py — the /crt read surface (CRT-1, renko-seam mount in main.py).

Read-only diagnostics (the assessment: "read surface becomes REST + the
claims leaderboard" — CRT-2 adds claims). GET only, no auth (the renko
``/renko/health`` precedent for operator diagnostics), no writes, no
side effects:

- ``GET /crt/health``  — process health + mode + engine count (never lies
  about liveness; ``crt_mode`` says whether the engine is dark);
- ``GET /crt/state``   — per-(symbol, tf) snapshots: option monitor states
  (NORMAL/CAUTION/OFF/DISABLED, CUSUM S, live/rolling EV), open positions,
  frozen decisions, last tick.

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
        from crt import jobs
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
        view["mode_note"] = ("live has no live-execution path until CRT-3 "
                             "(leaderboard-gated); running paper semantics"
                             if mode == "live" else "")
        return view

else:
    crt_router = None  # type: ignore[assignment]
