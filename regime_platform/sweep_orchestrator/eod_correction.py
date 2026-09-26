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

# ── Three calibration-trust checks, three cadences, three thresholds —
# a deliberately-tiered design, made explicit here (2026-09-09, worklog
# 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16) after a
# fresh-eyes review found the relationship between them undocumented
# anywhere, indistinguishable from accidental duplication:
#
#   Cadence         Threshold        Reads              Purpose
#   ~15 min/tick    0.15 (one-way,   predicted_p_win     Fast tripwire between
#                   overconfidence   (enforced value,     scheduled passes —
#                   only)            drift_monitor.py)    drift_monitor.py's own
#                                                          docstring: "detects
#                                                          bias drift BETWEEN
#                                                          [the 4h/24h] passes."
#                                                          Overconfidence-only
#                                                          by design (docstring:
#                                                          "underconfidence is
#                                                          conservative...
#                                                          handled by the
#                                                          scheduled passes").
#   4h              0.30 / -0.20     predicted_p_win_raw  Wider band because
#                   (this module,    (pre-enforcement,    raw model output is
#                   raw_bias_mode)   this module)         naturally more
#                                                          extreme than the
#                                                          enforcement-
#                                                          compensated value —
#                                                          measures true model
#                                                          drift, not the
#                                                          correction already
#                                                          applied on top of it.
#   24h (nightly)   0.10 (this       predicted_p_win      The narrow, "official"
#                   module,          (enforced value,     threshold that
#                   default mode)    this module)         actually drives
#                                                          kelly_reduction/
#                                                          bayesian_penalty/
#                                                          emergency_heavy_sweep
#                                                          — the least noisy
#                                                          signal (most data,
#                                                          slowest cadence), so
#                                                          it gets the tightest
#                                                          band.
#
# None of these three should be merged into one: each answers "is this
# symbol's confidence trustworthy" for a genuinely different combination
# of (how fast do we need to know, how noisy is the input at that
# cadence, what should touching it actually do). Consolidating them into
# a single check would either make the tick-level tripwire too slow to
# be useful, or make the low-noise nightly enforcement threshold too
# jumpy. If a fourth calibration-trust check is ever proposed, it must
# justify its own place in this table, not just add another number.

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

# ── Stagnation breaker (2026-08-22 audit fix #3c) ────────────────────────
# See settings.py's STAGNATION_* fields for the full rationale. Defaults
# mirrored here for the no-settings-available fallback path (same pattern
# as DEFAULT_RAW_OVERCONFIDENT above).
DEFAULT_STAGNATION_EPSILON = 0.02
DEFAULT_STAGNATION_TIER1_CYCLES = 2
DEFAULT_STAGNATION_TIER2_CYCLES = 4
DEFAULT_STAGNATION_RESET_CYCLES = 8
DEFAULT_STAGNATION_KELLY_REDUCTION_TIER1 = 0.25

# Action name for the Tier 2 hard block. Enforcement (calibration_enforcement.py)
# forces kelly_f=0.0 for this action regardless of the usual bayesian-shrink math.
STAGNATION_KILL_SWITCH_ACTION = "stagnation_kill_switch"

# Parameter-decay window: realized win rate over the LAST 3 days vs the 3
# days before that; decline beyond this threshold flags fatigue.
DECAY_LOOKBACK_DAYS = 3
DECAY_DECLINE_THRESHOLD = 0.05


def _settings_number(name: str, default: float) -> float:
    """Read a tunable numeric value: tuning registry file -> settings ->
    ``default``.

    2026-08-23 (Layer 1 E2E wiring, worklog/PARAMETER_TUNING_PLAYBOOK.md
    §2.5.2.1): checks ``tuning_registry.read_param()`` FIRST so the
    autonomous auto-tuner's writes actually reach this decision, not just
    the audit trail. Falls back to the ``settings`` value (today's
    behaviour), then to ``default``, if a field is missing or not
    numeric (e.g. a test double that auto-generates non-numeric
    attributes) — this module's design rule is "never raise out," so a
    bad value must degrade gracefully rather than crash the correction
    pass for that symbol.
    """
    try:
        from regime_platform.core import tuning_registry
        from regime_platform.core.settings import settings
        settings_value = getattr(settings, name, default)
        # isinstance, NOT float(settings_value) — a fully-mocked `settings`
        # object (patch(".settings") with no spec, common in this test
        # suite) auto-generates attributes that satisfy float() (returning
        # 1.0 via the mock's __float__) instead of raising, which would
        # silently ignore `default` and feed a wrong value into
        # tuning_registry.read_param(). isinstance is a real type check,
        # not a dunder call, so it correctly rejects a Mock.
        if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
            settings_default = float(settings_value)
        else:
            settings_default = default
        return tuning_registry.read_param(name, default=settings_default)
    except (TypeError, ValueError, ImportError, AttributeError):
        return default


def bayesian_adjusted_p_win(p_win: float, n_records: int) -> float:
    """Shrink ``p_win`` toward 0.5 using a weak neutral prior.

    p_win' = (p_win * n + 0.5 * prior_count) / (n + prior_count)
    ``prior_count`` (BAYES_PRIOR_COUNT) is registry-tunable (Layer 1);
    the 0.5 anchor (BAYES_PRIOR_PWIN) is not — see §2.5.2.1.
    """
    try:
        p = float(p_win)
    except (TypeError, ValueError):
        p = 0.5
    n = max(0, int(n_records or 0))
    prior_count = _settings_number("BAYES_PRIOR_COUNT", BAYES_PRIOR_COUNT)
    numerator = p * n + BAYES_PRIOR_PWIN * prior_count
    denominator = n + prior_count
    return round(max(0.001, min(0.999, numerator / denominator)), 4)


def _classify_bias(bias: float) -> str:
    """Classify enforced-bias using the (registry-tunable, symmetric)
    ``EOD_BIAS_ENFORCED_THRESHOLD`` — see §2.5.2.1."""
    threshold = _settings_number("EOD_BIAS_ENFORCED_THRESHOLD", OVERCONFIDENT_BIAS)
    if bias > threshold:
        return "OVERCONFIDENT"
    if bias < -threshold:
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
    but are registry-tunable (Layer 1) via ``_settings_number`` — see
    §2.5.2.1 — falling back to settings (EOD_BIAS_RAW_OVERCONFIDENT /
    EOD_BIAS_RAW_UNDERCONFIDENT) when the registry has no entry yet.
    """
    oc = overconfident_threshold
    if oc is None:
        oc = _settings_number("EOD_BIAS_RAW_OVERCONFIDENT", DEFAULT_RAW_OVERCONFIDENT)
    uc = underconfident_threshold
    if uc is None:
        uc = _settings_number("EOD_BIAS_RAW_UNDERCONFIDENT", DEFAULT_RAW_UNDERCONFIDENT)
    if bias >= oc:
        return "OVERCONFIDENT"
    if bias <= uc:
        return "UNDERCONFIDENT"
    return "CALIBRATED"


def _stagnation_streak(
    bias_history: list[float],
    current_bias: float,
    epsilon: float | None = None,
) -> int:
    """Count how many consecutive cycles (including the current one) have
    a bias within ``epsilon`` of the current bias — i.e. "no real movement."

    ``bias_history`` must be ordered NEWEST-FIRST and must NOT include
    ``current_bias`` (it's the prior cycles' recorded bias values, e.g.
    from ``fetch_recent_action_bias``). Counting stops at the first cycle
    that moved by more than ``epsilon`` — a genuinely-improving symbol
    should not be penalized just because it was once stagnant.
    """
    eps = epsilon if epsilon is not None else _settings_number(
        "STAGNATION_EPSILON", DEFAULT_STAGNATION_EPSILON
    )
    streak = 1
    for b in bias_history:
        if abs(b - current_bias) <= eps:
            streak += 1
        else:
            break
    return streak


def _stagnation_tier(streak: int) -> int:
    """Map a stagnation streak (see ``_stagnation_streak``) to an escalation
    tier: 0 (normal), 1 (deeper penalty), 2 (hard kill switch), or back to
    0 once ``STAGNATION_RESET_CYCLES`` is reached — a truly-stuck symbol
    gets a fresh start + forced re-sweep rather than a permanent block.
    """
    tier1 = _settings_number("STAGNATION_TIER1_CYCLES", DEFAULT_STAGNATION_TIER1_CYCLES)
    tier2 = _settings_number("STAGNATION_TIER2_CYCLES", DEFAULT_STAGNATION_TIER2_CYCLES)
    reset = _settings_number("STAGNATION_RESET_CYCLES", DEFAULT_STAGNATION_RESET_CYCLES)
    if streak >= reset:
        return 0
    if streak >= tier2:
        return 2
    if streak >= tier1:
        return 1
    return 0


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
        logger.warning("[eod:correction] %s: nt_eod_actions insert failed: %s: %r", symbol, type(exc).__name__, exc)


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


async def _apply_evidence_pool(
    symbol: str,
    venue: str | None,
    cal: dict[str, Any] | None,
    raw_bias_mode: bool,
    summary: dict[str, Any],
    fetch_bias,
    fetch_bias_raw,
) -> dict[str, Any] | None:
    """calibration_evidence_pool_mode (off | shadow | live) — Kairos V3
    §14.3. Returns the ``cal`` dict enforcement should use. Records one
    comparison row per symbol in ``summary["evidence_pool"]``. Never raises:
    any failure returns the all-tier ``cal`` unchanged.
    """
    from regime_platform.core.feature_flags import (
        get_calibration_evidence_pool_mode,
        get_calibration_evidence_tiers,
    )

    mode = get_calibration_evidence_pool_mode()
    if mode == "off":
        return cal
    tiers = get_calibration_evidence_tiers()
    try:
        fn = fetch_bias_raw if raw_bias_mode else fetch_bias
        pooled = await fn(symbol, venue=venue, evidence_tiers=tiers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[eod:pool] %s pooled bias failed (%s) — all-tier kept", symbol, exc)
        return cal

    def _s(c):
        return (c or {}).get("status") or "INSUFFICIENT"

    def _b(c):
        v = (c or {}).get("bias")
        return None if v is None else float(v)

    row = {
        "symbol": symbol, "mode": mode, "tiers": list(tiers),
        "all_status": _s(cal), "all_bias": _b(cal), "all_n": (cal or {}).get("n"),
        "pool_status": _s(pooled), "pool_bias": _b(pooled), "pool_n": (pooled or {}).get("n"),
    }
    use = cal
    if mode == "live":
        if pooled is not None and pooled.get("bias") is not None:
            use = pooled
            row["used"] = "pool"
        else:
            row["used"] = "all_tiers_fallback"
    else:
        row["used"] = "all_tiers"
    row["status_differs"] = row["all_status"] != row["pool_status"]
    summary.setdefault("evidence_pool", []).append(row)
    logger.info(
        "[eod:pool] %s %s — all-tier %s bias=%s n=%s | %s %s bias=%s n=%s | used=%s",
        symbol, mode, row["all_status"], row["all_bias"], row["all_n"],
        "+".join(tiers), row["pool_status"], row["pool_bias"], row["pool_n"], row["used"],
    )
    return use


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
    source_tag = "eod_correction_intraday" if raw_bias_mode else "eod_correction_nightly"
    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_active_symbols,
            fetch_calibration_bias,
            fetch_calibration_bias_raw,
            _clear_stagnation_history,
            upsert_calibration_state,
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

            # T3 (2026-09-19): geometry path — high long-lag / tp_atr from
            # recent sim → geometry_tuning_sweep (independent of bias).
            try:
                from regime_platform.core.feature_flags import (
                    get_dynamic_atr_risk_cap,
                    get_geometry_tuning_sweep_mode,
                )
                import httpx as _httpx
                from regime_platform.core.settings import settings as _settings
                if _settings.SUPABASE_URL and get_geometry_tuning_sweep_mode() != "off":
                    _headers = {
                        "apikey": _settings.SUPABASE_KEY,
                        "Authorization": f"Bearer {_settings.SUPABASE_KEY}",
                    }
                    async with _httpx.AsyncClient(timeout=8.0) as _c:
                        _r = await _c.get(
                            f"{_settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_signal_sim",
                            headers=_headers,
                            params={
                                "select": "geometry_risk_hat,tp_distance_atr,long_lag_risk,ts",
                                "symbol": f"eq.{symbol}",
                                "order": "ts.desc",
                                "limit": "20",
                            },
                        )
                        _r.raise_for_status()
                        _rows = _r.json() or []
                    _risks = [float(x["geometry_risk_hat"]) for x in _rows if x.get("geometry_risk_hat") is not None]
                    _cap = get_dynamic_atr_risk_cap()
                    if _risks and sum(1 for r in _risks if r > _cap) >= 2:
                        if get_geometry_tuning_sweep_mode() == "live":
                            await _write_eod_action(
                                symbol, "geometry_tuning_sweep", None,
                                {
                                    "reason": "intraday_eod_geometry",
                                    "n_risk_over_cap": sum(1 for r in _risks if r > _cap),
                                    "max_risk": max(_risks),
                                    "cap": _cap,
                                },
                            )
                            logger.info(
                                "[eod:correction] %s: geometry_tuning_sweep written (max_risk=%.3f cap=%.3f)",
                                symbol, max(_risks), _cap,
                            )
                        else:
                            logger.info(
                                "[eod:correction] %s: SHADOW geometry_tuning_sweep would write "
                                "(max_risk=%.3f cap=%.3f)",
                                symbol, max(_risks), _cap,
                            )
            except Exception as _geo_exc:
                logger.warning("[eod:correction] %s geometry check failed: %s", symbol, _geo_exc)

            if raw_bias_mode:
                # ── 4h intraday correction pass — raw-bias mode ──
                cal = await fetch_calibration_bias_raw(symbol, venue=venue)
            else:
                # ── Nightly pass — standard enforced-bias mode ──
                cal = await fetch_calibration_bias(symbol, venue=venue)

            # ── Evidence pool (2026-09-26, Kairos V3 §14.3, migration 171,
            # worklog/kairos/20260926_CALIBRATION_EVIDENCE_POOL.md) ──
            # shadow: also compute the bias from the configured tiers only
            # (default qualified + suppressed), log the pair, keep `cal`.
            # live:   the pooled bias drives enforcement; when the pool is
            #         too thin for a verdict, fall back to all tiers (logged)
            #         so a symbol never loses enforcement just from the switch.
            cal = await _apply_evidence_pool(
                symbol, venue, cal, raw_bias_mode, summary,
                fetch_calibration_bias, fetch_calibration_bias_raw,
            )

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
                # ── Cycle reset for insufficient data (2026-09-09) ──────
                # Replaces the old unconditional _clear_stale_eod_actions
                # call for exactly this case: only the intraday pass ever
                # cleared stale enforcement state (the nightly pass relies
                # on the ACTION_FRESH_WINDOW_HOURS freshness check to age
                # old state out naturally, unchanged). A symbol that drops
                # below the record floor this cycle should stop being
                # enforced immediately, not linger until its last
                # successful verdict ages past the freshness window.
                if raw_bias_mode:
                    await upsert_calibration_state(
                        symbol, action="none", bias=None, status=None,
                        n_records=0, source=source_tag,
                        detail={"reason": "insufficient_data_cycle_reset"},
                    )
                continue

            # ── Classification (2026-09-09, worklog
            # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
            # SS16): read it straight from cal["status"] instead of
            # re-deriving with a second _classify_bias/_classify_bias_raw
            # call — fetch_calibration_bias/_raw now classify internally
            # using these exact functions, so recomputing here was pure
            # duplication of an already-correct answer, not a safety net.
            status = cal["status"]
            # "source": "eod_correction" (2026-08-24 fix) tags every action
            # this module writes so fetch_recent_action_bias can tell them
            # apart from drift_monitor.py's OWN bayesian_penalty writes,
            # which use a different window/threshold and would otherwise
            # dilute _stagnation_streak's "same 4h cycle, unmoved" signal —
            # see worklog/20260824_STAGNATION_STREAK_SOURCE_FILTER.md.
            detail: dict[str, Any] = {"bias": bias, "status": status, "source": "eod_correction"}

            if status == "CALIBRATED":
                # ── Current-state reset (2026-09-09) ────────────────────
                # Every classification cycle fully overwrites this
                # symbol's nt_symbol_calibration_state row — a CALIBRATED
                # verdict means action="none", which is itself the
                # "cycle reset" (replaces the old unconditional
                # _clear_stale_eod_actions call; see this function's
                # module-level note for the one remaining case — bias is
                # None — that needed its own explicit reset).
                await upsert_calibration_state(
                    symbol, action="none", bias=bias, status=status,
                    n_records=int(cal.get("n") or 0), source=source_tag,
                    detail=detail,
                )

                # ── Stagnation-history auto-recovery (2026-08-23 fix) ──
                # Genuine recovery: clear this symbol's bayesian_penalty
                # history so a *future* stagnation streak starts fresh
                # instead of picking up stale pre-recovery bias values.
                # Only fires here (on CALIBRATED), not every cycle — see
                # _clear_stagnation_history's docstring and
                # worklog/20260823_CALIBRATION_BIAS_CHECK_AUDIT.md §2.
                # NOTE: this still operates on nt_eod_actions (the audit
                # log's bayesian_penalty history), unrelated to the
                # current-state table above — that history lookup is
                # explicitly out of scope for the state/history split
                # (worklog SS16 item #6).
                if raw_bias_mode:
                    await _clear_stagnation_history(symbol)

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
            # n_records is already clustering-discounted by fetch_calibration_bias(_raw)
            # (2026-08-25) — scaled down when the window's records span too few
            # distinct days, so a burst crammed into a few hours doesn't feed the
            # Bayesian shrink the same sample size as a full week would. raw_n /
            # distinct_days are the true (undiscounted) figures, kept for audit.
            n_records = int(cal.get("n") or 0)
            if raw_bias_mode:
                obs_p_win = float(cal.get("avg_pred_raw") or cal.get("avg_pred") or 0.5)
            else:
                obs_p_win = float(cal.get("avg_pred") or 0.5)
            adj_p_win = bayesian_adjusted_p_win(obs_p_win, n_records)
            detail["adjusted_p_win"] = adj_p_win
            detail["avg_pred"] = obs_p_win
            detail["n"] = n_records  # consumed by calibration_enforcement live
            detail["raw_n"] = cal.get("raw_n", n_records)
            detail["distinct_days"] = cal.get("distinct_days")

            # ── Stagnation breaker (fix #3c, 2026-08-22 audit) ──────────
            # Only meaningful at the 4h raw-bias cycle boundary, where
            # "consecutive cycles" has a clear definition. Fully automated,
            # tiered, no human gate — see settings.STAGNATION_* docstrings
            # and worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §4 fix #3c.
            tier = 0
            streak = 0
            if raw_bias_mode:
                try:
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        fetch_recent_action_bias,
                    )
                    # source="eod_correction" (2026-08-24 fix): drift_monitor.py
                    # also writes bayesian_penalty rows (its own 48h-window,
                    # different threshold) — without this filter they'd mix
                    # into the same 8-row lookback and dilute the "same 4h
                    # cycle, unmoved" signal this streak is meant to measure.
                    history = await fetch_recent_action_bias(
                        symbol, action="bayesian_penalty", limit=8,
                        source="eod_correction",
                    )
                except Exception:
                    history = []
                streak = _stagnation_streak(history, bias)
                tier = _stagnation_tier(streak)
                detail["stagnation_streak"] = streak
                detail["stagnation_tier"] = tier

            kelly_reduction = _settings_number("KELLY_REDUCTION", KELLY_REDUCTION)
            if tier == 1:
                kelly_reduction = _settings_number(
                    "STAGNATION_KELLY_REDUCTION_TIER1",
                    DEFAULT_STAGNATION_KELLY_REDUCTION_TIER1,
                )
                detail["kelly_reduction_override"] = kelly_reduction
                logger.warning(
                    "[eod:correction] %s: STAGNATION Tier 1 (streak=%d cycles unmoved) "
                    "— deeper penalty, Kelly×%.2f",
                    symbol, streak, kelly_reduction,
                )
            detail["kelly_factor"] = kelly_reduction

            for action in ("emergency_heavy_sweep", "bayesian_penalty", "kelly_reduction"):
                await _write_eod_action(symbol, action, bias, detail)
                summary["actions"].append({"symbol": symbol, "action": action, "bias": bias})

            # ── Current-state write (2026-09-09) ────────────────────────
            # nt_eod_actions still gets all three labeled rows above
            # (audit, unchanged — emergency_heavy_sweep is what
            # orchestrator.py's in-memory emergency-sweep dispatch reads
            # from THIS function's own return value, a different consumer
            # entirely, unaffected by this table). For live enforcement
            # purposes the three are identical (calibration_enforcement
            # only branches on stagnation_kill_switch vs everything else)
            # — "bayesian_penalty" is the canonical state-table action
            # name for "a shrink penalty is active."
            await upsert_calibration_state(
                symbol, action="bayesian_penalty", bias=bias, status=status,
                n_records=n_records, source=source_tag, detail=detail,
                kelly_reduction_override=detail.get("kelly_reduction_override"),
                stagnation_tier=tier,
            )

            if tier == 2:
                # Written LAST so it's the "latest" row fetch_latest_eod_action
                # reads — forces kelly_f=0.0 in enforcement (blocks publishing
                # entirely) regardless of the usual bayesian-shrink math.
                await _write_eod_action(symbol, STAGNATION_KILL_SWITCH_ACTION, bias, detail)
                summary["actions"].append({
                    "symbol": symbol, "action": STAGNATION_KILL_SWITCH_ACTION, "bias": bias,
                })
                logger.warning(
                    "[eod:correction] %s: STAGNATION Tier 2 (streak=%d) — "
                    "KILL SWITCH, kelly forced to 0.0",
                    symbol, streak,
                )
                # Explicit override — not implicit "written last wins" the
                # way nt_eod_actions' "latest row" reconstruction worked.
                # One more full upsert makes stagnation_kill_switch the
                # current state, unambiguously.
                await upsert_calibration_state(
                    symbol, action=STAGNATION_KILL_SWITCH_ACTION, bias=bias,
                    status=status, n_records=n_records, source=source_tag,
                    detail=detail, stagnation_tier=tier,
                )

            logger.warning(
                "[eod:correction] %s: %s bias=%.4f → emergency sweep + p_win'=%.4f + "
                "Kelly×%.2f (stagnation_tier=%d)",
                symbol, status, bias, adj_p_win, kelly_reduction, tier,
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
