"""
regime_state_store.py — restart-safe per-symbol regime state (2026-09-25,
worklog/kairos/20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md).

Studio restarts (scheduled ~4h sleeps + code deploys) used to wipe the
orchestrator's in-memory regime bookkeeping. Measured 2026-09-22..25 over
22 restarts: 58.4% of (symbol, consecutive-tick) pairs changed regime cell
across a restart vs 9.0% between normal 15-min ticks, because

  • ``_regime_label_means`` (label-continuity maps) were lost, so the first
    post-restart fit re-sorted HMM components blind (label identity flips);
  • every ``RegimeLabelDebouncer`` cold-started and accepted the first raw
    read immediately;
  • ``KairosRunTracker`` restarted every run (age → 0, left-censored);
  • the Markov / trend-posterior EMAs restarted from the raw value.

This module snapshots exactly that state to a local JSON file after every
light-sweep group and restores it on start. Only numbers/strings — no model
objects. The file lives under the Studio's persistent disk (``.cache`` is
gitignored, so a deploy/pull never touches it).

Freshness rule: a snapshot older than ``regime_state_persist_max_age_hours``
(default 3h — the same >3h gap that ``v_kairos_regime_runs`` treats as a data
gap) is ignored, so a genuine outage/weekend still cold-starts. Pure
bookkeeping: nothing here changes a gate, threshold or delivery; with the
state restored the next tick behaves exactly as if the process had never
stopped. Never raises.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[2] / ".cache" / "kairos" / "regime_state.json"
)


def _clean_float_map(d: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (d or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[str(k)] = f
    return out


def snapshot(orch: Any, now: float | None = None) -> dict[str, Any]:
    """Serializable view of the orchestrator's restart-sensitive state."""
    return {
        "version": SCHEMA_VERSION,
        "saved_at": float(now if now is not None else time.time()),
        "label_means": {
            sym: {part: _clean_float_map(m) for part, m in (maps or {}).items()}
            for sym, maps in getattr(orch, "_regime_label_means", {}).items()
        },
        "debouncers": {
            sym: d.to_dict() for sym, d in getattr(orch, "_regime_debouncers", {}).items()
        },
        "kairos_runs": orch._kairos_runs.to_dict() if getattr(orch, "_kairos_runs", None) else {},
        "kairos_rank": orch._kairos_rank.to_dict() if getattr(orch, "_kairos_rank", None) else {},
        "markov_ema": _clean_float_map(getattr(orch, "_markov_ema", {})),
        "regime_trend_ema": {
            sym: _clean_float_map(m) for sym, m in getattr(orch, "_regime_trend_ema", {}).items()
        },
        "last_regime": {
            str(k): str(v) for k, v in getattr(orch, "_last_regime", {}).items() if v
        },
    }


def save(orch: Any, path: Path | None = None) -> bool:
    """Atomic write (tmp + replace) so a restart mid-write can't corrupt it."""
    path = path or DEFAULT_STATE_PATH
    try:
        data = snapshot(orch)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[regime-state] save failed: %s: %s", type(exc).__name__, exc)
        return False


def restore(orch: Any, *, max_age_hours: float, n_confirm: int,
            path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Load a fresh snapshot into ``orch``. Returns a summary dict
    ``{restored: bool, reason, age_h, symbols}``."""
    from regime_platform.core.regime_engine import RegimeLabelDebouncer
    from regime_platform.sweep_orchestrator.kairos import KairosRunTracker

    path = path or DEFAULT_STATE_PATH
    now = float(now if now is not None else time.time())
    out: dict[str, Any] = {"restored": False, "reason": "", "age_h": None, "symbols": 0}
    try:
        if not path.exists():
            out["reason"] = "no_snapshot"
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != SCHEMA_VERSION:
            out["reason"] = "version_mismatch"
            return out
        age_h = (now - float(data["saved_at"])) / 3600.0
        out["age_h"] = round(age_h, 3)
        if age_h < 0 or age_h > float(max_age_hours):
            out["reason"] = f"stale ({age_h:.2f}h > {max_age_hours}h)"
            return out
        orch._regime_label_means.update({
            sym: {part: dict(m) for part, m in maps.items()}
            for sym, maps in (data.get("label_means") or {}).items()
        })
        orch._regime_debouncers.update({
            sym: RegimeLabelDebouncer.from_dict(d, n_confirm)
            for sym, d in (data.get("debouncers") or {}).items()
        })
        orch._kairos_runs = KairosRunTracker.from_dict(data.get("kairos_runs") or {})
        try:
            from regime_platform.sweep_orchestrator.kairos_rank import RankScoreTracker
            orch._kairos_rank = RankScoreTracker.from_dict(data.get("kairos_rank") or {})
        except Exception:
            pass
        orch._markov_ema.update(_clean_float_map(data.get("markov_ema")))
        orch._regime_trend_ema.update({
            sym: _clean_float_map(m) for sym, m in (data.get("regime_trend_ema") or {}).items()
        })
        orch._last_regime.update(data.get("last_regime") or {})
        out.update(restored=True, reason="ok",
                   symbols=len(data.get("debouncers") or data.get("label_means") or {}))
        return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"error {type(exc).__name__}: {exc}"
        logger.warning("[regime-state] restore failed: %s", out["reason"])
        return out
