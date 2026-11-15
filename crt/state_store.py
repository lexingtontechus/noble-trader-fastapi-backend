"""state_store.py — CRT-2 monitor persistence (state rows + rolling table).

Two tables (migration 221), two writers, one discipline:

- ``nt_crt_monitor_state`` — ONE row per (symbol, tf, dir, session) option:
  the CONFIG-DEPLOY PRE-REGISTRATION (the CRT-0 artifact row verbatim —
  oos_ev/sd/t/n, train_end, thresholds, rolling-gate config, runid) plus
  the live monitor state (CUSUM S, live n/EV, rolling n/EV, decay state),
  refreshed by a throttled flush. The nt_symbol_calibration_state
  precedent (migration 150): exactly one current-state row per key,
  upserted through exactly one writer — never insert-and-orphaned, never
  multi-writer contaminated. The in-memory OptionCfg stays the operating
  truth; this row is the durable pre-registration + audit trail.
- ``nt_crt_rolling_daily`` — one row per (UTC day, option): the 45d
  rolling snapshot + the (symbol, tf) pool columns, upserted at most once
  per UTC day (and once per process start — idempotent merge). The
  nt_live_expectancy_daily precedent (migration 163): daily snapshot,
  survives eod_retention, the CRT-3 promotion evidence surface.

Upserts ride PostgREST ``?on_conflict=...`` + ``Prefer:
resolution=merge-duplicates`` (the repo's upsert convention —
upsert_bracket_reachability / upsert_live_expectancy_daily). All I/O
lazy-imported and fail-open: persistence failure never breaks the tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("crt.state_store")

MONITOR_TABLE = "nt_crt_monitor_state"
ROLLING_TABLE = "nt_crt_rolling_daily"

MONITOR_CONFLICT = "symbol,tf,dir,session"
ROLLING_CONFLICT = "day,symbol,tf,dir,session"

SESS_NAMES = {0: "OUT", 1: "NY", 2: "ASIA"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_monitor_row(
    option: Any, *, decay_cfg: dict[str, Any] | None = None,
    roll_cfg: dict[str, Any] | None = None,
    artifact_runid: str | None = None,
) -> dict[str, Any]:
    """One nt_crt_monitor_state upsert row (pure; OptionCfg + deploy config).

    The pre-registration half is the artifact row verbatim; the live half
    is the option's CURRENT monitor fields (whatever the throttled flush
    observed). ``thresholds``/``rolling_gate`` jsonb carry the deploy-time
    gate settings — the pre-registered kill-switch + rolling verdict rules
    the live evidence is judged by.
    """
    thresholds = {k: decay_cfg[k] for k in (
        "kappa_frac", "kappa_min", "h_caution", "h_off",
        "roll_n", "roll_off_ev", "react_n", "use_monitor", "caution_mult")
        if decay_cfg and k in decay_cfg}
    rolling_gate = {k: roll_cfg[k] for k in (
        "use_gate", "roll_days", "roll_min_n", "roll_min_ev", "scope")
        if roll_cfg and k in roll_cfg}
    return {
        "symbol": option.symbol, "tf": option.tf,
        "dir": str(option.dir), "session": SESS_NAMES.get(option.sess, str(option.sess)),
        "model": {0: "STOP", 1: "C3C", 2: "CONT"}.get(option.model, str(option.model)),
        "enabled": bool(option.enabled),
        "eb": round(float(option.eb), 6), "sb": round(float(option.sb), 6),
        "r": round(float(option.r), 6), "hold": int(option.hold),
        "max_sweep": round(float(option.max_sweep), 6),
        "chop": int(option.chop), "vol_mask": int(option.vol_mask),
        "c3_dir": int(option.c3_dir),
        "base_ev": round(float(option.base_ev), 6),
        "base_sd": round(float(option.base_sd), 6),
        "oos_t": round(float(option.oos_t), 6), "oos_n": int(option.oos_n),
        "train_end": (option.train_end.isoformat()
                      if option.train_end is not None else None),
        "generated": (str(option.generated) if option.generated is not None else None),
        "artifact_runid": str(artifact_runid) if artifact_runid else None,
        "thresholds": thresholds, "rolling_gate": rolling_gate,
        "state": option.state_name(),
        "s_cusum": round(float(option.s_cusum), 6),
        "live_n": int(option.live_n), "live_ev": round(float(option.live_ev), 6),
        "roll_ev": round(float(option.roll_ev), 6),
        "rl_n": int(option.rl_n), "rl_ev": round(float(option.rl_ev), 6),
        "updated_at": now_iso(),
    }


def tf_pool(options: list[Any]) -> tuple[int, float]:
    """The (symbol, tf) pool from the options' rolling fields (pure).

    Algebraic identity with ``monitors.compute_rolling``'s pool: the pool
    is the ENABLED options' label rows in the SAME 45d window, and pooling
    the per-option (n, mean) subsets by weight reconstructs exactly
    sum(r)/n over the pooled set. (Enabled options only — the EA's
    ``if enabled`` pool rule.)
    """
    n = 0
    total = 0.0
    for o in options:
        if not o.enabled:
            continue
        n += int(o.rl_n)
        total += float(o.rl_ev) * int(o.rl_n)
    return n, (total / n if n else 0.0)


def build_rolling_row(option: Any, *, day: str, tf_pool: tuple[int, float],
                       open_positions: int = 0) -> dict[str, Any]:
    """One nt_crt_rolling_daily upsert row (pure; a UTC-day snapshot)."""
    tf_n, tf_ev = tf_pool
    return {
        "day": str(day),
        "symbol": option.symbol, "tf": option.tf,
        "dir": str(option.dir), "session": SESS_NAMES.get(option.sess, str(option.sess)),
        "enabled": bool(option.enabled), "state": option.state_name(),
        "rl_n": int(option.rl_n), "rl_ev": round(float(option.rl_ev), 6),
        "tf_n": int(tf_n), "tf_ev": round(float(tf_ev), 6),
        "open_positions": int(open_positions),
        "computed_at": now_iso(),
    }


def _monitor_columns() -> tuple[str, ...]:
    return ("symbol", "tf", "dir", "session", "model", "enabled",
            "eb", "sb", "r", "hold", "max_sweep", "chop", "vol_mask",
            "c3_dir", "base_ev", "base_sd", "oos_t", "oos_n", "train_end",
            "generated", "artifact_runid", "thresholds", "rolling_gate",
            "state", "s_cusum", "live_n", "live_ev", "roll_ev",
            "rl_n", "rl_ev", "updated_at")


def _rolling_columns() -> tuple[str, ...]:
    return ("day", "symbol", "tf", "dir", "session", "enabled", "state",
            "rl_n", "rl_ev", "tf_n", "tf_ev", "open_positions", "computed_at")


async def _upsert_rows(table: str, rows: list[dict[str, Any]],
                       columns: tuple[str, ...], conflict: str) -> int:
    """POST one upsert batch (merge-duplicates). Fail-open: 0 on any error."""
    if not rows:
        return 0
    clean = [{k: v for k, v in r.items() if k in columns and v is not None}
             for r in rows]
    if not clean:
        return 0
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return 0
        import httpx
        url = f"{settings.SUPABASE_URL}/rest/v1/{table}"
        headers = {
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, headers=headers,
                params={"on_conflict": conflict},
                json=clean,
            )
            if not resp.is_success:
                logger.error("crt.state_store: %s upsert HTTP %d — %s",
                             table, resp.status_code, resp.text[:200])
                return 0
        return len(clean)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.state_store: %s upsert failed: %s", table, exc)
        return 0


async def upsert_monitor_rows(rows: list[dict[str, Any]]) -> int:
    """Upsert nt_crt_monitor_state rows (one per option key)."""
    return await _upsert_rows(MONITOR_TABLE, rows, _monitor_columns(),
                              MONITOR_CONFLICT)


async def upsert_rolling_rows(rows: list[dict[str, Any]]) -> int:
    """Upsert nt_crt_rolling_daily rows (one per UTC-day x option)."""
    return await _upsert_rows(ROLLING_TABLE, rows, _rolling_columns(),
                              ROLLING_CONFLICT)


def utc_day(now_epoch: float) -> str:
    """The UTC date string for a rolling-daily row key."""
    return datetime.fromtimestamp(float(now_epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def monitor_signature(option: Any) -> tuple:
    """The change-detection signature (flush when ANY option's differs)."""
    return (option.state, round(float(option.s_cusum), 3),
            int(option.live_n), round(float(option.live_ev), 3),
            int(option.rl_n), round(float(option.rl_ev), 3))
