"""
calibration_quality_gate.py — Masaniello urgency as a batch-management quality gate.

RETHINK (2026-10-02, worklog/20261002_masaniello_quality_gate.md):
Masaniello's M_i = (W − w) / (N − i + 1) is the batch's *urgency index* —
how many wins are still needed relative to how many trades remain. During
regime aging, edge decays; if wins aren't accumulating at the expected
rate, M_i rises (the batch is "behind"). When this happens across many
symbols at once, it signals systemic degradation, not unlucky batches.

This module turns M_i into a READ-ONLY, best-effort diagnostic layer that
writes ``nt_eod_actions`` rows (``parameter_decay_check`` action type, matching
the existing CHECK constraint on that table). It does **not** alter
sizing: M_i continues to feed the existing ``(0.5 + M_i)`` term in the
position sizer (regime_platform/core/position_sizer.py) unchanged. The
quality gate is an independent signal-quality monitor — Masaniello
repurposed from a sizing formula into a batch-management health check.

Pure functions (no I/O):
  urgency_index, urgency_drift_ratio, urgency_threshold, check_urgency_batch

Async entry-point:
  check_urgency(symbols) — reads nt_signal_sim, computes urgency drift per symbol,
  writes ``parameter_decay_check`` to nt_eod_actions. Best-effort:
  failures are logged and swallowed so a hiccup cannot break the sweep tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.sweep.quality_gate")


# ─── Default config ──────────────────────────────────────────────────────────

DEFAULT_BATCH_SIZE = 5       # N — matches SizingConfig.batch_size
DEFAULT_TARGET_WINS = 3      # W — matches SizingConfig.target_wins
DEFAULT_MIN_SIGNALS = 20     # min resolved sims per symbol to evaluate
DEFAULT_LOOKBACK_DAYS = 3    # how far back to reconstruct batch state
DEFAULT_MIN_SYMBOLS_TRIGGER = 3  # min symbols above threshold to fire the gate

# Regime-age brackets (hours) that map to urgency-drift thresholds.
# As a regime ages, edge naturally decays — so we tighten sensitivity:
#   young  → tolerate high drift (batch just started, noise dominates)
#   mature → moderate drift flags systemic issues
#   aged   → expect decay; even modest drift flags concern
YOUNG_AGE_H = 3.0
MATURE_AGE_H = 12.0

YOUNG_THRESHOLD = 2.0     # only flag extreme spikes in a fresh batch
MATURE_THRESHOLD = 1.5
AGED_THRESHOLD = 1.2


# ─── Pure functions ──────────────────────────────────────────────────────────


def urgency_index(wins_so_far: int, trade_index: int,
                  target_wins: int = DEFAULT_TARGET_WINS,
                  batch_size: int = DEFAULT_BATCH_SIZE) -> float:
    """M_i = (W − w) / (N − i + 1).

    ``wins_so_far`` = w (wins accumulated in the current batch).
    ``trade_index`` = i (1-based position within the batch).
    ``target_wins`` = W (target wins for the batch).
    ``batch_size``  = N (total trades in the batch).

    Returns 0.0 when the target is already met (no urgency to size up) or
    when the batch is exhausted (trade_index > batch_size — the last trade
    has resolved and the batch is complete).

    Matches the clamped definition in ``_masaniello_factor`` (clamp [0, 1.5]).
    """
    trades_left = max(0, batch_size - trade_index + 1)
    if trades_left == 0:
        return 0.0  # batch exhausted — no urgency left
    wins_needed = max(0, target_wins - wins_so_far)
    raw = wins_needed / trades_left
    return min(raw, 1.5)  # match _masaniello_factor clamp


def urgency_drift_ratio(
    wins_so_far: int,
    losses_so_far: int,
    target_wins: int = DEFAULT_TARGET_WINS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> float:
    """Ratio of current urgency M_i to the batch's initial urgency M_0.

    M_0 = W / N  (initial urgency: all wins still needed, first trade).
    M_i = (W − w) / (N − i + 1)  where i = w + l + 1 (next trade).

    drift = M_i / M_0

    * drift < 1 — more wins accumulated than expected; batch is "ahead"
    * drift ≈ 1 — batch is on track
    * drift > 1 — batch is "behind" (urgency elevated); the higher, the
      more wins are still needed relative to trades remaining

    Returning 0.0 at or above the target is correct: once W wins have been
    hit, M_i = 0 (no urgency left), so drift = 0.
    """
    w = wins_so_far
    n_losses = losses_so_far
    # If the batch is exhausted (all N trades resolved), no urgency remains.
    if w + n_losses >= batch_size:
        return 0.0
    i = w + n_losses + 1  # 1-based index of the *next* trade
    if i > batch_size:
        i = batch_size
    m_i = urgency_index(w, i, target_wins, batch_size)
    m_0 = target_wins / batch_size if batch_size > 0 else 0.0
    if m_0 <= 0:
        return 0.0
    return m_i / m_0


def urgency_threshold(age_hours: float) -> float:
    """Return the urgency-drift ratio threshold for a given regime age.

    Young regimes (age ≤ YOUNG_AGE_H): only flag extreme drift.
    Mature regimes (YOUNG_AGE_H < age ≤ MATURE_AGE_H): moderate threshold.
    Aged regimes (age > MATURE_AGE_H): tighten — expect edge decay, so
    even modest drift flags concern.

    The threshold is the *minimum* drift ratio at which the gate fires.
    """
    if age_hours <= YOUNG_AGE_H:
        return YOUNG_THRESHOLD
    if age_hours <= MATURE_AGE_H:
        return MATURE_THRESHOLD
    return AGED_THRESHOLD


@dataclass
class UrgencyReading:
    """Urgency state for one symbol at evaluation time."""
    symbol: str
    wins_so_far: int
    losses_so_far: int
    trade_index: int
    batch_size: int
    target_wins: int
    m_i: float                # current urgency index
    m_0: float                # initial urgency (target_wins / batch_size)
    drift_ratio: float        # m_i / m_0
    regime_age_hours: float
    threshold: float
    triggered: bool

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "wins_so_far": self.wins_so_far,
            "losses_so_far": self.losses_so_far,
            "trade_index": self.trade_index,
            "batch_size": self.batch_size,
            "target_wins": self.target_wins,
            "m_i": round(self.m_i, 4),
            "m_0": round(self.m_0, 4),
            "drift_ratio": round(self.drift_ratio, 4),
            "age_hours": round(self.regime_age_hours, 2),
            "threshold": self.threshold,
            "triggered": self.triggered,
        }


def check_urgency_batch(
    batch_states: list[dict[str, Any]],
    regime_ages: dict[str, float],
) -> list[UrgencyReading]:
    """Evaluate urgency drift for each symbol's batch state.

    Parameters
    ──────────
    batch_states : list of dicts with keys:
        ``symbol``, ``wins_so_far``, ``losses_so_far``, ``batch_size``,
        ``target_wins`` (all optional except symbol).
    regime_ages  : ``{symbol: age_hours}`` from the Kairos run tracker.

    Returns
    ───────
    List of :class:`UrgencyReading` (one per symbol with sufficient data).
    Symbols with no resolved trades are skipped.
    """
    readings: list[UrgencyReading] = []
    for bs in batch_states:
        symbol = bs.get("symbol", "")
        if not symbol:
            continue
        w = int(bs.get("wins_so_far", 0))
        n_losses = int(bs.get("losses_so_far", 0))
        n = int(bs.get("batch_size", DEFAULT_BATCH_SIZE))
        W = int(bs.get("target_wins", DEFAULT_TARGET_WINS))
        age = float(regime_ages.get(symbol, 0.0))

        i = w + n_losses + 1  # 1-based index of the next trade
        if i > n:
            i = n

        m_i = urgency_index(w, i, W, n)
        m_0 = W / n if n > 0 else 0.0
        drift = urgency_drift_ratio(w, n_losses, W, n)
        thresh = urgency_threshold(age)

        readings.append(UrgencyReading(
            symbol=symbol,
            wins_so_far=w,
            losses_so_far=n_losses,
            trade_index=i,
            batch_size=n,
            target_wins=W,
            m_i=m_i,
            m_0=m_0,
            drift_ratio=drift,
            regime_age_hours=age,
            threshold=thresh,
            triggered=drift > thresh,
        ))
    return readings


# ─── Async entry-point ───────────────────────────────────────────────────────

_PAGE_SIZE = 1000


async def _fetch_recent_sims(
    symbol: str, since_iso: str
) -> list[dict[str, Any]]:
    """Fetch resolved nt_signal_sim rows for one symbol since ``since_iso``."""
    if not settings.SUPABASE_URL:
        return []
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url
    rows: list[dict[str, Any]] = []
    offset = 0
    params: dict[str, str] = {
        "select": "symbol,ts,p_win,predicted_p_win_raw,outcome,regime",
        "symbol": f"eq.{symbol}",
        "outcome": "in.(tp_hit,sl_hit)",
        "ts": f"gte.{since_iso}",
        "order": "ts.desc",
        "limit": str(_PAGE_SIZE),
    }
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"), headers=_headers(),
                params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += len(batch)
    return rows


def _reconstruct_batch_state(
    sims: list[dict[str, Any]],
    batch_size: int = DEFAULT_BATCH_SIZE,
    target_wins: int = DEFAULT_TARGET_WINS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any] | None:
    """Reconstruct in-batch state from a flat list of resolved sim rows.

    Takes the last ``batch_size`` resolved sims (most recent) and counts
    wins (outcome == 'tp_hit') and losses (outcome == 'sl_hit'). The
    reconstructed ``trade_index`` is the position within the current
    (incomplete) batch: wins + losses + 1.

    Returns ``None`` if there are fewer than ``min_signals`` resolved sims
    overall (sample too thin to evaluate).
    """
    if len(sims) < min_signals:
        return None

    # Take the most recent batch_size sims (they're ordered by ts desc).
    recent = sims[:batch_size]
    wins = sum(1 for r in recent if r.get("outcome") == "tp_hit")
    losses = sum(1 for r in recent if r.get("outcome") == "sl_hit")

    return {
        "wins_so_far": wins,
        "losses_so_far": losses,
        "batch_size": batch_size,
        "target_wins": target_wins,
    }


async def check_urgency(
    symbols: list[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    min_symbols_trigger: int = DEFAULT_MIN_SYMBOLS_TRIGGER,
    batch_size: int = DEFAULT_BATCH_SIZE,
    target_wins: int = DEFAULT_TARGET_WINS,
) -> list[dict[str, Any]]:
    """Read batch state, compute urgency drift, write EOD action if triggered.

    For each symbol:
      1. Fetch resolved nt_signal_sim rows (last ``lookback_days``).
      2. Reconstruct batch state (wins/losses in the current batch).
      3. Compute urgency drift ratio and compare to regime-age threshold.
      4. If drift exceeds threshold AND enough symbols are simultaneously
         elevated, write a ``regime_degradation_warning`` to nt_eod_actions.

    Returns the list of written action rows (empty if nothing fired).
    Best-effort: any failure is logged and swallowed.
    """
    if not settings.SUPABASE_URL:
        logger.debug("Supabase not configured — skipping urgency gate")
        return []

    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    # Regime age is proxied by the hours span from the oldest resolved
    # sim in this window — best available proxy without live run-tracker
    # state. Open item in the worklog for wiring in live KairosRunTracker.
    regime_ages: dict[str, float] = {}
    batch_states: list[dict[str, Any]] = []

    for sym in symbols:
        try:
            sims = await _fetch_recent_sims(sym, since)
            if len(sims) < min_signals:
                continue
            bs = _reconstruct_batch_state(
                sims, batch_size=batch_size, target_wins=target_wins,
                min_signals=min_signals,
            )
            if bs is None:
                continue
            bs["symbol"] = sym
            batch_states.append(bs)

            # Estimate regime age: hours since the oldest resolved sim
            # in this window (best available proxy without live state).
            if sims:
                oldest = sims[-1].get("ts")
                if oldest:
                    try:
                        parsed = datetime.fromisoformat(
                            str(oldest).replace("Z", "+00:00")
                        )
                        regime_ages[sym] = max(
                            0.0,
                            (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0
                        )
                    except (ValueError, TypeError):
                        regime_ages[sym] = 0.0
        except Exception as exc:  # noqa: BLE001 — best-effort per symbol
            logger.warning("quality_gate %s: fetch failed: %s", sym, exc)

    if not batch_states:
        return []

    readings = check_urgency_batch(batch_states, regime_ages)
    triggered = [r for r in readings if r.triggered]

    actions: list[dict[str, Any]] = []
    if len(triggered) >= min_symbols_trigger:
        avg_drift = sum(r.drift_ratio for r in triggered) / len(triggered)
        avg_age = sum(r.regime_age_hours for r in triggered) / len(triggered)
        ts = datetime.now(timezone.utc).isoformat()
        systemic_detail = {
            "source": "calibration_quality_gate",
            "avg_urgency_drift": round(avg_drift, 4),
            "n_triggered_symbols": len(triggered),
            "n_total_symbols": len(batch_states),
            "avg_regime_age_hours": round(avg_age, 2),
            "triggered_symbols": [
                {"symbol": r.symbol, **r.detail} for r in triggered
            ],
        }
        try:
            from regime_platform.sweep_orchestrator.supabase_client import _insert_row
            # Write one row per triggered symbol — the nt_eod_actions CHECK
            # constraint requires action ∈ {emergency_heavy_sweep,
            # bayesian_penalty, kelly_reduction, parameter_decay_check},
            # and the idx_eod_actions_symbol_ts index assumes one symbol per
            # row. parameter_decay_check is the semantically closest match:
            # urgency drift = win rate decaying faster than the regime-age
            # bracket justifies. Audit-only — does NOT write
            # nt_symbol_calibration_state, so enforcement is unaffected.
            for r in triggered:
                row: dict[str, Any] = {
                    "symbol": r.symbol,
                    "ts": ts,
                    "action": "parameter_decay_check",
                    "bias": round(r.drift_ratio, 4),
                    "detail": {**r.detail, **systemic_detail},
                }
                ok = await _insert_row("nt_eod_actions", row)
                if ok:
                    actions.append(row)
                    logger.info(
                        "quality_gate: %s urgency drift=%.3f (threshold %.1f, "
                        "age %.1fh) → parameter_decay_check",
                        r.symbol, r.drift_ratio, r.threshold, r.regime_age_hours,
                    )
                else:
                    logger.warning(
                        "quality_gate: %s nt_eod_actions insert failed", r.symbol
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quality_gate: nt_eod_actions insert failed: %s", exc)

    return actions


# ─── Module entry-point ──────────────────────────────────────────────────────

async def run(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """CLI / cron entry point.

    If ``symbols`` is None, fetches active symbols from Supabase.
    """
    if symbols is None:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
        active = await fetch_active_symbols()
        symbols = [r["symbol"] for r in active if r.get("symbol")]
    return await check_urgency(symbols)
