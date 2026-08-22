"""
EOD Self-Correction — calibration-bias-driven parameter/sizing adjustments
═══════════════════════════════════════════════════════════════════════════

Reads per-symbol calibration bias (from ``nt_calibration_records`` via
``fetch_calibration_bias``) and writes audit rows to ``nt_eod_actions``:

  • |bias| > 0.10  → OVERCONFIDENT / UNDERCONFIDENT
        - emergency_heavy_sweep : run an immediate heavy sweep for the symbol
        - bayesian_penalty      : shrink p_win toward 0.5 before qualification
        - kelly_reduction       : reduce Kelly sizing by 50%
  • |bias| <= 0.10 → CALIBRATED
        - parameter_decay_check : flag if realized win rate declined > 5pts
          between the last 3 days and the prior 3 days (a proxy for "sweep
          fatigue" — params aging out of regime).

Bayesian penalty formula (documented, used by the emergency path):
    p_win' = (p_win * n + 0.5 * 10) / (n + 10)
where n = number of calibration records for the symbol. With few records the
prior dominates; with many records the observed p_win dominates.

Design rules (mirror eod_resolver.py):
  • Best-effort only — every per-symbol failure is caught, logged and counted
    in the summary; this module never raises out.
  • It is an AUDIT + RECOMMENDATION pass. It writes ``nt_eod_actions`` rows and
    returns recommendations; the actual heavy-sweep trigger / sizing override
    is consumed by the orchestrator hook (wired separately) so the running
    scheduler instance stays the single actor for heavy sweeps.

CLI:
    python -m regime_platform.sweep_orchestrator.eod_correction
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.eod_correction")

# Bias thresholds (mirror the user's decision tree). MUST mirror
# settings.EOD_BIAS_ENFORCED_THRESHOLD (a test asserts they stay in sync) —
# the public /calibration/bias and /calibration/status endpoints
# (routers/calibration.py) read the settings value directly so they can
# never silently disagree with what this module enforces.
OVERCONFIDENT_BIAS = 0.10
UNDERCONFIDENT_BIAS = -0.10

# Raw-bias mode thresholds (2026-08-22): the 4h intraday correction pass
# evaluates model drift from ``predicted_p_win_raw`` (pre-enforcement), so the
# band is wider — raw model output is naturally more extreme than the
# enforcement-compensated value the nightly pass sees. Calibration band:
#   - Overconfident: raw bias >= 0.30 (raw p_win systematically above true win rate)
#   - Underconfident: raw bias <= -0.20 (raw p_win systematically below true win rate)
#   - Calibrated: -0.20 < bias < 0.30
# Configurable via EOD_BIAS_RAW_OVERCONFIDENT / EOD_BIAS_RAW_UNDERCONFIDENT.
DEFAULT_RAW_OVERCONFIDENT = 0.30
DEFAULT_RAW_UNDERCONFIDENT = -0.20

# Bayesian prior strength (pseudo-count) — how many "neutral 0.5" observations
# to blend in when no real records exist yet.
BAYES_PRIOR_COUNT = 10
BAYES_PRIOR_PWIN = 0.5

# Kelly reduction factor for overconfident symbols
KELLY_REDUCTION = 0.5

# Parameter-decay window: realized win rate over the LAST 3 days vs the 3
# days before that; decline beyond this threshold flags fatigue.
DECAY_LOOKBACK_DAYS = 3
DECAY_DECLINE_THRESHOLD = 0.05


def bayesian_adjusted_p_win(p_win: float, n_records: int) -> float:
    """Shrink ``p_win`` toward 0.5 using a weak neutral prior.

    p_win' = (p_win * n + 0.5 * prior_count) / (n + prior_count)
    """
    try:
        p = float(p_win)
    except (TypeError, ValueError):
        p = 0.5
    n = max(0, int(n_records or 0))
    numerator = p * n + BAYES_PRIOR_PWIN * BAYES_PRIOR_COUNT
    denominator = n + BAYES_PRIOR_COUNT
    return round(max(0.001, min(0.999, numerator / denominator)), 4)


def _classify_bias(bias: float) -> str:
    if bias > OVERCONFIDENT_BIAS:
        return "OVERCONFIDENT"
    if bias < UNDERCONFIDENT_BIAS:
        return "UNDERCONFIDENT"
    return "CALIBRATED"


def _classify_bias_raw(
    bias: float,
    overconfident_threshold: float | None = None,
    underconfident_threshold: float | None = None,
) -> str:
    """Classify bias using the RAW-bias thresholds (wider band).

    Mirrors ``_classify_bias`` but with configurable boundaries for the 4h
    intraday correction pass, which reads ``predicted_p_win_raw`` (pre-enforcement).
    Defaults come from ``DEFAULT_RAW_OVERCONFIDENT`` / ``DEFAULT_RAW_UNDERCONFIDENT``
    but can be overridden via settings (EOD_BIAS_RAW_OVERCONFIDENT /
    EOD_BIAS_RAW_UNDERCONFIDENT).
    """
    oc = overconfident_threshold
    if oc is None:
        try:
            from regime_platform.core.settings import settings
            oc = getattr(settings, "EOD_BIAS_RAW_OVERCONFIDENT", DEFAULT_RAW_OVERCONFIDENT)
        except Exception:
            oc = DEFAULT_RAW_OVERCONFIDENT
    uc = underconfident_threshold
    if uc is None:
        try:
            from regime_platform.core.settings import settings
            uc = getattr(settings, "EOD_BIAS_RAW_UNDERCONFIDENT", DEFAULT_RAW_UNDERCONFIDENT)
        except Exception:
            uc = DEFAULT_RAW_UNDERCONFIDENT
    if bias >= oc:
        return "OVERCONFIDENT"
    if bias <= uc:
        return "UNDERCONFIDENT"
    return "CALIBRATED"


def _days_ago(days: int) -> str:
    """ISO timestamp for ``days`` days ago (UTC)."""
    return (datetime.now(timezone.utc)).timestamp() - days * 86400


async def _write_eod_action(symbol: str, action: str, bias: Optional[float], detail: dict[str, Any]) -> None:
    """Best-effort insert into nt_eod_actions."""
    try:
        from regime_platform.sweep_orchestrator.supabase_client import _insert_row
        row = {
            "symbol": symbol,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "bias": bias,
            # JSONB column: send the dict (httpx serializes). The previous
            # json.dumps() double-encoded the object into a JSON *string*,
            # breaking calibration_enforcement's isinstance(detail, dict)
            # guard (H2).
            "detail": detail,
        }
        await _insert_row("nt_eod_actions", row)
    except Exception as exc:
        logger.warning("[eod:correction] %s: nt_eod_actions insert failed: %s", symbol, exc)


async def _fetch_recent_win_rates(symbol: str) -> dict[str, float]:
    """Return {recent_3d_wr, prior_3d_wr} from nt_signal_sim (None-safe)."""
    try:
        from regime_platform.core.settings import settings
        import httpx

        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        recent_start = _days_ago(DECAY_LOOKBACK_DAYS)
        prior_start = _days_ago(DECAY_LOOKBACK_DAYS * 2)
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            f"?select=outcome,ts&symbol=eq.{symbol}"
            f"&outcome=in.(tp_hit,sl_hit)&order=ts.asc&limit=1000"
        )
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            if not resp.is_success:
                return {"recent_3d_wr": None, "prior_3d_wr": None}
            rows = resp.json() or []
    except Exception as exc:
        logger.warning("[eod:correction] %s: win-rate fetch failed: %s", symbol, exc)
        return {"recent_3d_wr": None, "prior_3d_wr": None}

    recent_wins = recent_total = 0
    prior_wins = prior_total = 0
    for row in rows:
        ts = row.get("ts")
        if not ts:
            continue
        try:
            ts_val = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        is_win = row.get("outcome") == "tp_hit"
        if ts_val >= recent_start:
            recent_total += 1
            recent_wins += 1 if is_win else 0
        elif ts_val >= prior_start:
            prior_total += 1
            prior_wins += 1 if is_win else 0

    return {
        "recent_3d_wr": round(recent_wins / recent_total, 4) if recent_total else None,
        "prior_3d_wr": round(prior_wins / prior_total, 4) if prior_total else None,
    }


async def run_eod_correction(raw_bias_mode: bool = False) -> dict[str, Any]:
    """Run the self-correction audit pass over all active symbols.

    When ``raw_bias_mode`` is True (4h intraday correction pass), bias is
    computed from ``predicted_p_win_raw`` (pre-enforcement p_win) via
    ``fetch_calibration_bias_raw``, and stale penalty actions for
    CALIBRATED symbols are cleared before re-evaluation — the "cycle reset"
    mechanism that prevents compounding enforcement penalties.

    When False (nightly 00:30 pass), uses ``fetch_calibration_bias`` with
    the standard 0.10 thresholds — unchanged behaviour.
    """
    summary: dict[str, Any] = {"checked": 0, "actions": [], "errors": 0, "raw_bias_mode": raw_bias_mode}
    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_active_symbols,
            fetch_calibration_bias,
            fetch_calibration_bias_raw,
            _clear_stale_eod_actions,
        )
        from regime_platform.sweep_orchestrator.orchestrator import _derive_venue

        symbols = await fetch_active_symbols()
    except Exception as exc:
        logger.error("[eod:correction] cannot start: %s", exc)
        return {**summary, "errors": 1}

    for sym in symbols:
        symbol = sym.get("symbol")
        if not symbol:
            continue
        summary["checked"] += 1
        try:
            venue = _derive_venue(sym)

            # ── 4h cycle reset: clear stale enforcement actions for ALL symbols ──
            # At each 4h intraday correction boundary, stale penalty actions from the
            # previous cycle must be cleared before re-evaluation. This is a true reset:
            # symbols with insufficient calibration records (< EOD_BIAS_MIN_RECORDS)
            # previously entered the degeneracy path where clearing was skipped entirely,
            # leaving actions from Aug 18-19 persisting indefinitely.
            # See: worklog/20260822_STALE_ACTION_CLEARING_SCOPE.md
            if raw_bias_mode:
                await _clear_stale_eod_actions(symbol)

            if raw_bias_mode:
                # ── 4h intraday correction pass — raw-bias mode ──
                cal = await fetch_calibration_bias_raw(symbol, venue=venue)
            else:
                # ── Nightly pass — standard enforced-bias mode ──
                cal = await fetch_calibration_bias(symbol, venue=venue)

            bias = cal.get("bias") if cal else None

            if bias is None:
                # L3 fix (2026-08-21): No calibration records — check for HMM
                # degeneracy instead of silently skipping. When p_win is near 0
                # despite a valid regime signal, the symbol is stuck in the
                # degeneracy deadlock: p_win=0 → qualified=False → no resolved
                # outcomes → no calibration records → no EOD correction.
                # This path breaks the cycle by recommending an emergency
                # heavy sweep even without calibration data.
                try:
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        fetch_latest_sweep,
                    )
                    latest = await fetch_latest_sweep(symbol)
                    if latest is not None:
                        p_win_val = float(latest.get("p_win") or 1.0)
                        if p_win_val < 0.01:  # degenerate threshold
                            await _write_eod_action(
                                symbol, "degeneracy_breaker", bias,
                                {
                                    "reason": "p_win near 0 in latest sweep",
                                    "latest_p_win": p_win_val,
                                    "markov_p_up": latest.get("markov_p_up"),
                                    "markov_p_dn": latest.get("markov_p_dn"),
                                    "regime": latest.get("regime"),
                                    "qualified": latest.get("qualified"),
                                },
                            )
                            summary["actions"].append({
                                "symbol": symbol,
                                "action": "degeneracy_breaker",
                                "bias": bias,
                            })
                            logger.warning(
                                "[eod:correction] %s: degeneracy detected "
                                "(p_win=%.4f, markov_up=%s, markov_dn=%s) — "
                                "emergency sweep requested",
                                symbol, p_win_val,
                                latest.get("markov_p_up"),
                                latest.get("markov_p_dn"),
                            )
                        else:
                            logger.debug(
                                "[eod:correction] %s: no calibration records "
                                "but p_win=%.4f (not degenerate) — skipped",
                                symbol, p_win_val,
                            )
                except Exception as exc:
                    logger.warning(
                        "[eod:correction] %s: degeneracy check failed (%s: %s)",
                        symbol, type(exc).__name__, exc,
                    )
                continue

            # ── Classify using raw or enforced thresholds ──
            if raw_bias_mode:
                status = _classify_bias_raw(bias)
            else:
                status = _classify_bias(bias)
            detail: dict[str, Any] = {"bias": bias, "status": status}

            # ── Stale action clearing moved to unconditional 4h cycle reset ──
            # At: eod_correction.py L240 (before bias computation)
            # Previously cleared here only when status == "CALIBRATED" — but symbols
            # with insufficient records never reached this branch. Now cleared for ALL
            # symbols at the 4h boundary. See: worklog/20260822_STALE_ACTION_CLEARING_SCOPE.md

            if status == "CALIBRATED":
                # Parameter-decay check: realized win rate declining?
                wr = await _fetch_recent_win_rates(symbol)
                detail.update(wr)
                recent = wr.get("recent_3d_wr")
                prior = wr.get("prior_3d_wr")
                if recent is not None and prior is not None and (prior - recent) > DECAY_DECLINE_THRESHOLD:
                    await _write_eod_action(
                        symbol, "parameter_decay_check", bias,
                        {**detail, "decline": round(prior - recent, 4)},
                    )
                    summary["actions"].append({"symbol": symbol, "action": "parameter_decay_check", "bias": bias})
                    logger.info(
                        "[eod:correction] %s: parameter decay (wr %.4f → %.4f)",
                        symbol, prior, recent,
                    )
                continue

            # Overconfident / underconfident → correction actions
            # In raw_bias_mode, cal.avg_pred is the enforced average and cal.avg_pred_raw
            #   is the pre-enforcement average — use raw for the Bayesian shrink so
            #   the penalty is applied to the true model output, not on top of previous shrinks.
            # In standard mode, cal.avg_pred IS the observed mean predicted p_win (the blend
            #   input). The old `cal.get("predicted_p_win")` key never existed, so the Bayesian
            #   shrink was a constant 0.5 (H1).
            n_records = int(cal.get("n") or 0)
            if raw_bias_mode:
                obs_p_win = float(cal.get("avg_pred_raw") or cal.get("avg_pred") or 0.5)
            else:
                obs_p_win = float(cal.get("avg_pred") or 0.5)
            adj_p_win = bayesian_adjusted_p_win(obs_p_win, n_records)
            detail["adjusted_p_win"] = adj_p_win
            detail["avg_pred"] = obs_p_win
            detail["kelly_factor"] = KELLY_REDUCTION
            detail["n"] = n_records  # consumed by calibration_enforcement live

            for action in ("emergency_heavy_sweep", "bayesian_penalty", "kelly_reduction"):
                await _write_eod_action(symbol, action, bias, detail)
                summary["actions"].append({"symbol": symbol, "action": action, "bias": bias})
            logger.warning(
                "[eod:correction] %s: %s bias=%.4f → emergency sweep + p_win'=%.4f + Kelly×%.2f",
                symbol, status, bias, adj_p_win, KELLY_REDUCTION,
            )
        except Exception as exc:
            summary["errors"] += 1
            logger.warning("[eod:correction] %s: failed (%s)", symbol, exc)

    logger.info("[eod:correction] complete: %d symbols, %d actions, %d errors",
                summary["checked"], len(summary["actions"]), summary["errors"])
    return summary


async def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="EOD correction pass")
    ap.add_argument("--raw", action="store_true",
                    help="4h intraday mode: compute bias from predicted_p_win_raw")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    result = await run_eod_correction(raw_bias_mode=args.raw)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
