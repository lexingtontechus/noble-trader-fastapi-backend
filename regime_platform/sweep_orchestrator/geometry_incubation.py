"""
Geometry incubation — forward-test gate before a brick geometry goes live
═══════════════════════════════════════════════════════════════════════

worklog/20260923_GEOMETRY_FORWARD_TEST_SCOPE.md

Today ``write_brick_params()`` (regime_platform/config/brick_params.py —
"the SOLE writer" of optimised brick params) takes a heavy-sweep pick from
a single in-sample 90-day grid backtest straight to
``config/brick_params.yaml``, live on the very next light-sweep tick. No
held-out / out-of-sample check exists — ``tests/test_renko_backtest.py``
still references a ``walk_forward()`` method removed in v7.3 and never
rebuilt.

This module adds that missing forward-test layer without touching the
YAML write contract when the feature is off, and without changing live
behavior at all in shadow mode (same off → shadow → live convention as
every other flag in ``config/feature_flags.yaml``):

  * ``start_incubation()`` — records a new heavy-sweep candidate. In
    ``shadow`` mode the caller (heavy_sweep.py) still calls
    ``write_brick_params()`` immediately afterward, unchanged; this is
    purely a tracked-for-review shadow. In ``live`` mode the caller skips
    the YAML write until the candidate is promoted.
  * ``compute_forward_stats()`` / ``evaluate_promotion()`` — pure,
    unit-testable functions: given the forward-RESOLVED
    ``nt_signal_sim`` rows generated under the *exact* candidate geometry
    since ``started_at``, and the previous live combo's own
    ``pct_expired`` baseline, decide promote / reject / keep-incubating.
  * ``reconcile_incubations()`` — async orchestration: for every
    ``status=incubating`` row, fetch forward evidence, evaluate, and
    (only in ``live`` mode) call ``write_brick_params()`` on promotion or
    mark ``rejected``/leave incubating otherwise. Meant to run on the
    same cadence as the heavy sweep (or more often) — cheap, idempotent,
    best-effort (never raises out, mirrors ``paper_book.reconcile()``).

Pure math (``compute_forward_stats``, ``evaluate_promotion``) has no I/O
and is fully unit-testable offline; everything else is best-effort
Supabase REST, same pattern as ``supabase_client.py`` / ``paper_book.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.geometry_incubation")


# ─── Pure math (unit-testable, no I/O) ─────────────────────────────────


@dataclass(frozen=True)
class ForwardStats:
    n_resolved: int          # wins + losses (closed trades only)
    wins: int
    losses: int
    expired: int
    pct_expired: Optional[float]   # expired / (wins+losses+expired)
    expectancy_r: Optional[float]  # win_rate * avg_rr - (1 - win_rate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_resolved": self.n_resolved,
            "wins": self.wins,
            "losses": self.losses,
            "expired": self.expired,
            "pct_expired": self.pct_expired,
            "expectancy_r": self.expectancy_r,
        }


def compute_forward_stats(
    rows: list[dict[str, Any]],
    *,
    sl_bricks: int,
    tp_bricks: int,
) -> ForwardStats:
    """Aggregate forward-resolved ``nt_signal_sim`` rows for one candidate.

    ``rows`` are outcome-resolved rows (``outcome`` in tp_hit/sl_hit/expired)
    already filtered by the caller to the exact candidate geometry and to
    ``resolved_at >= started_at`` (i.e. genuinely forward, not the in-sample
    backtest). ``sl_bricks``/``tp_bricks`` come from the candidate itself
    (not re-derived per row) — the RR used for expectancy is the bracket's
    designed RR, matching how ``pick_best_combo``/heavy_sweep score
    candidates in-sample.
    """
    wins = sum(1 for r in rows if r.get("outcome") == "tp_hit")
    losses = sum(1 for r in rows if r.get("outcome") == "sl_hit")
    expired = sum(1 for r in rows if r.get("outcome") == "expired")
    n_resolved = wins + losses
    total = n_resolved + expired

    pct_expired = (expired / total) if total > 0 else None

    expectancy_r: Optional[float] = None
    if n_resolved > 0:
        win_rate = wins / n_resolved
        rr = float(tp_bricks) / max(float(sl_bricks), 1.0)
        expectancy_r = win_rate * rr - (1.0 - win_rate)

    return ForwardStats(
        n_resolved=n_resolved,
        wins=wins,
        losses=losses,
        expired=expired,
        pct_expired=round(pct_expired, 4) if pct_expired is not None else None,
        expectancy_r=round(expectancy_r, 4) if expectancy_r is not None else None,
    )


@dataclass(frozen=True)
class PromotionVerdict:
    status: str    # "incubating" | "promoted" | "rejected"
    reason: str


def evaluate_promotion(
    *,
    days_elapsed: float,
    stats: ForwardStats,
    baseline_pct_expired: Optional[float],
    min_days: float,
    min_resolved: int,
    max_expiry_regression: float,
) -> PromotionVerdict:
    """Pure promote/reject/hold decision for one incubating candidate.

    Order matters (first applicable reason wins):
      1. Not enough elapsed time yet → keep incubating.
      2. Not enough forward-resolved samples yet → keep incubating.
      3. Negative expectancy on the forward sample → reject outright,
         regardless of sample-size floor being met (a candidate that is
         already losing money out-of-sample should never wait around for
         more samples to confirm it).
      4. Expiry rate regressed materially vs. the combo it would replace
         → reject (never trade a bracket that resolves worse than the
         one already live).
      5. Otherwise → promote.

    ``baseline_pct_expired=None`` (no prior combo / first-ever geometry
    for this symbol) skips gate 4 — nothing to regress against.
    """
    if days_elapsed < min_days:
        return PromotionVerdict("incubating", "insufficient_days")
    if stats.n_resolved < min_resolved:
        return PromotionVerdict("incubating", "insufficient_samples")
    if stats.expectancy_r is not None and stats.expectancy_r <= 0:
        return PromotionVerdict("rejected", "expectancy_r_non_positive")
    if (
        baseline_pct_expired is not None
        and stats.pct_expired is not None
        and stats.pct_expired > baseline_pct_expired + max_expiry_regression
    ):
        return PromotionVerdict("rejected", "pct_expired_regression")
    return PromotionVerdict("promoted", "promoted")


def adaptive_min_days(
    resolution_rate: Optional[float],
    *,
    min_resolved: int,
    floor_days: float,
    cap_days: float,
) -> float:
    """Evidence-relative time floor for the incubation gate.

    The static ``min_days=7`` assumed a uniform ~1 resolution/day cadence.
    Live data shows a 3-5x spread (XAGUSD ~101/24h … US500 ~29/24h), so a
    fixed 7-day floor is either unreachable (fast symbols never survive the
    no-change supersede churn that resets the clock) or far too generous.

    This computes the number of *days* the symbol needs, at its own observed
    resolution rate, to accumulate ``min_resolved`` forward samples:

        days_for_samples = min_resolved / resolution_rate

    then clamps it to ``[floor_days, cap_days]``:

      * ``floor_days`` — never demand less than a minimum wall-clock window
        (at least a full trading day, so a promotion is not decided on an
        intraday slice). Default 1.
      * ``cap_days`` — never relax *below* the old conservative floor when
        there is no usable evidence; a cold/unknown rate returns ``cap_days``
        so the fail-open behaviour is identical to the pre-change gate.

    ``resolution_rate`` is resolved rows per *active day* (distinct
    ``resolved_at`` dates), computed from fresh ``nt_signal_sim`` data — never
    from a stale/cached dataset. A non-positive/None rate means "no evidence",
    which returns ``cap_days``.
    """
    if resolution_rate is None or resolution_rate <= 0:
        return cap_days
    days_for_samples = float(min_resolved) / float(resolution_rate)
    # ceil so a fractional day counts as a whole day (0.2 -> 1, 1.5 -> 2).
    import math
    days_needed = math.ceil(days_for_samples)
    if days_needed < floor_days:
        days_needed = floor_days
    if days_needed > cap_days:
        days_needed = cap_days
    return float(days_needed)


# ─── I/O helpers (best-effort Supabase REST, mirrors paper_book.py) ────


def _table_url(table: str) -> str:
    from regime_platform.core.settings import settings
    return f"{settings.SUPABASE_URL}/rest/v1/{table}"


def _headers() -> dict[str, str]:
    from regime_platform.core.settings import settings
    return {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


async def _insert_row(table: str, payload: dict[str, Any]) -> bool:
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return False
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _table_url(table),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=payload,
            )
            if not resp.is_success:
                logger.error(
                    "geometry_incubation: %s insert HTTP %d — %s",
                    table, resp.status_code, resp.text[:300],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("geometry_incubation: %s insert failed: %s", table, exc)
        return False


async def _patch_rows(table: str, filters: dict[str, str], patch: dict[str, Any]) -> bool:
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return False
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                _table_url(table),
                headers={**_headers(), "Prefer": "return=minimal"},
                params=filters,
                json=patch,
            )
            if not resp.is_success:
                logger.error(
                    "geometry_incubation: %s patch HTTP %d — %s",
                    table, resp.status_code, resp.text[:300],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("geometry_incubation: %s patch failed: %s", table, exc)
        return False


async def _fetch_incubating() -> list[dict[str, Any]]:
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return []
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _table_url("nt_geometry_incubation"),
                headers=_headers(),
                params={
                    "select": "id,symbol,brick_size,sl_bricks,tp_bricks,"
                              "asset_class,started_at,baseline_pct_expired",
                    "status": "eq.incubating",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("geometry_incubation: fetch incubating failed: %s", exc)
        return []


async def _fetch_forward_resolved(
    symbol: str,
    brick_size: float,
    sl_bricks: int,
    tp_bricks: int,
    since_iso: str,
) -> list[dict[str, Any]]:
    """Forward-resolved nt_signal_sim rows for the exact candidate geometry.

    ``resolved_at >= since_iso`` (the incubation start) is what makes this
    genuinely forward/out-of-sample rather than a re-read of the same
    in-sample backtest window the heavy sweep already scored.
    """
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return []
        import httpx
        cutoff = since_iso.replace("+00:00", "Z")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                _table_url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "outcome,resolved_at",
                    "symbol": f"eq.{symbol}",
                    "brick_size": f"eq.{brick_size}",
                    "sl_bricks": f"eq.{sl_bricks}",
                    "tp_bricks": f"eq.{tp_bricks}",
                    "outcome": "in.(tp_hit,sl_hit,expired)",
                    "resolved_at": f"gte.{cutoff}",
                    "limit": "5000",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning(
            "geometry_incubation: forward-resolved fetch failed for %s: %s",
            symbol, exc,
        )
        return []


async def fetch_resolution_rate(symbol: str, *, window_days: int = 7) -> Optional[float]:
    """Fresh resolved-rows-per-active-day for one symbol.

    Reads ``nt_signal_sim`` over the last ``window_days`` (default 7),
    counting RESOLVED rows (tp_hit/sl_hit/expired) grouped by the distinct
    ``resolved_at`` DATE. The rate is ``total_resolved / active_days``, where
    ``active_days`` is the number of distinct dates that actually had a
    resolution — not the raw window length — so a weekend with no forex
    resolves does not dilute the per-active-day rate.

    This is deliberately a FRESH read every reconcile call (the cadence is
    hourly, the cost is one small indexed query per incubating symbol). It
    must never be cached or derived from a stale dataset: the entire point
    of the adaptive floor is to track each symbol's *current* cadence.

    Returns None (no evidence) when there are fewer than 2 active days, so
    the caller fails open to the conservative ``cap_days`` floor.
    """
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return None
        import httpx
        from datetime import datetime as _dt, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                _table_url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "resolved_at",
                    "symbol": f"eq.{symbol}",
                    "outcome": "in.(tp_hit,sl_hit,expired)",
                    "resolved_at": f"gte.{cutoff}",
                    "limit": "5000",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            active_dates = {
                str(r.get("resolved_at"))[:10]
                for r in rows
                if r.get("resolved_at")
            }
            active_days = len(active_dates)
            if active_days < 2:
                return None
            return len(rows) / active_days
    except Exception as exc:
        logger.warning(
            "geometry_incubation: resolution-rate fetch failed for %s: %s",
            symbol, exc,
        )
        return None


async def fetch_baseline_pct_expired(symbol: str) -> Optional[float]:
    """Best-effort: previous live combo's pct_expired, from
    nt_brick_params_state's ATR-at-write joined against the pooled
    (signal='all') reachability bucket. Returns None (skip gate 4) on any
    lookup miss — a missing baseline should never block a promotion by
    itself.
    """
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return None
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _table_url("nt_brick_params_state"),
                headers=_headers(),
                params={
                    "select": "brick_size,sl_bricks,tp_bricks,atr_at_write",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            prev = rows[0]
            atr = prev.get("atr_at_write")
            tp_bricks = prev.get("tp_bricks")
            brick_size = prev.get("brick_size")
            if not atr or tp_bricks is None or brick_size is None:
                return None
            from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for
            tp_atr = abs(float(tp_bricks) * float(brick_size)) / float(atr)
            bucket = _bucket_for(tp_atr)
            if bucket is None:
                return None
            resp2 = await client.get(
                _table_url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "pct_expired",
                    "symbol": f"eq.{symbol}",
                    "distance_atr_bucket": f"eq.{bucket}",
                    "signal": "eq.all",
                    "limit": "1",
                },
            )
            resp2.raise_for_status()
            rows2 = resp2.json()
            if isinstance(rows2, list) and rows2 and rows2[0].get("pct_expired") is not None:
                return float(rows2[0]["pct_expired"])
            return None
    except Exception as exc:
        logger.warning(
            "geometry_incubation: baseline pct_expired lookup failed for %s: %s",
            symbol, exc,
        )
        return None


# ─── Public orchestration API ───────────────────────────────────────────


async def start_incubation(
    symbol: str,
    brick_size: float,
    sl_bricks: int,
    tp_bricks: int,
    *,
    asset_class: Optional[str] = None,
    source: str = "heavy_sweep",
) -> bool:
    """Record a new candidate. Best-effort, never raises.

    Supersedes any existing ``incubating`` row for this symbol first (the
    unique partial index on ``status='incubating'`` means a stale row
    left incubating would otherwise block the insert). Fetches the
    previous live combo's ``pct_expired`` as the regression baseline.
    """
    try:
        await _patch_rows(
            "nt_geometry_incubation",
            {"symbol": f"eq.{symbol}", "status": "eq.incubating"},
            {"status": "superseded", "decided_at": datetime.now(timezone.utc).isoformat(),
             "reason": "superseded_by_new_candidate"},
        )
        baseline = await fetch_baseline_pct_expired(symbol)
        ok = await _insert_row("nt_geometry_incubation", {
            "symbol": symbol,
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "asset_class": asset_class,
            "source": source,
            "status": "incubating",
            "baseline_pct_expired": baseline,
        })
        if ok:
            logger.info(
                "[geometry_incubation] %s: candidate brick=%.4f sl=%d tp=%d "
                "started incubating (baseline_pct_expired=%s)",
                symbol, brick_size, sl_bricks, tp_bricks, baseline,
            )
        return ok
    except Exception as exc:
        logger.warning("geometry_incubation: start_incubation %s failed: %s", symbol, exc)
        return False


async def reconcile_incubations() -> dict[str, Any]:
    """Re-evaluate every incubating candidate against forward evidence.

    Idempotent — safe to run on any cadence (alongside the heavy sweep or
    more often). In ``live`` mode, a promoted candidate is actually
    written to ``config/brick_params.yaml`` here (the only place besides
    heavy_sweep.py itself that calls ``write_brick_params``). In
    ``shadow``/``off`` mode this only updates row status/stats for
    review — never touches the YAML.
    """
    from regime_platform.core.feature_flags import (
        get_geometry_incubation_mode,
        get_geometry_incubation_min_days,
        get_geometry_incubation_min_resolved,
        get_geometry_incubation_max_expiry_regression,
        get_geometry_incubation_adaptive_days_mode,
        get_geometry_incubation_adaptive_floor_days,
        get_geometry_incubation_adaptive_cap_days,
    )

    mode = get_geometry_incubation_mode()
    adaptive_mode = get_geometry_incubation_adaptive_days_mode()
    summary = {"checked": 0, "promoted": 0, "rejected": 0, "held": 0, "errors": 0}
    if mode == "off":
        return summary

    min_days = get_geometry_incubation_min_days()
    min_resolved = get_geometry_incubation_min_resolved()
    max_regression = get_geometry_incubation_max_expiry_regression()
    adaptive_floor_days = get_geometry_incubation_adaptive_floor_days()
    adaptive_cap_days = get_geometry_incubation_adaptive_cap_days()
    now = datetime.now(timezone.utc)

    candidates = await _fetch_incubating()
    for row in candidates:
        summary["checked"] += 1
        try:
            symbol = row["symbol"]
            brick_size = float(row["brick_size"])
            sl_bricks = int(row["sl_bricks"])
            tp_bricks = int(row["tp_bricks"])
            started_at_raw = row.get("started_at")
            started_at = datetime.fromisoformat(
                str(started_at_raw).replace("Z", "+00:00")
            )
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            days_elapsed = (now - started_at).total_seconds() / 86400.0

            # Adaptive, evidence-relative time floor (Part B). Fetch the
            # symbol's FRESH resolution rate and derive the floor it actually
            # needs to accumulate min_resolved samples. off → static floor;
            # shadow → compute + log but gate on static (zero live change);
            # live → gate on the adaptive floor.
            symbol_min_days = min_days
            if adaptive_mode != "off":
                resolution_rate = await fetch_resolution_rate(symbol)
                adaptive_days = adaptive_min_days(
                    resolution_rate,
                    min_resolved=min_resolved,
                    floor_days=adaptive_floor_days,
                    cap_days=adaptive_cap_days,
                )
                if adaptive_mode == "live":
                    symbol_min_days = adaptive_days
                else:  # shadow — observe only
                    logger.info(
                        "[geometry_incubation] %s adaptive_days=%.1f "
                        "(rate=%s/active-day, static=%.1f, would_use=%s)",
                        symbol, adaptive_days,
                        "none" if resolution_rate is None else f"{resolution_rate:.1f}",
                        min_days, adaptive_days,
                    )

            forward_rows = await _fetch_forward_resolved(
                symbol, brick_size, sl_bricks, tp_bricks,
                started_at.isoformat(),
            )
            stats = compute_forward_stats(
                forward_rows, sl_bricks=sl_bricks, tp_bricks=tp_bricks,
            )
            verdict = evaluate_promotion(
                days_elapsed=days_elapsed,
                stats=stats,
                baseline_pct_expired=row.get("baseline_pct_expired"),
                min_days=symbol_min_days,
                min_resolved=min_resolved,
                max_expiry_regression=max_regression,
            )

            patch: dict[str, Any] = {
                "last_checked_at": now.isoformat(),
                "n_resolved": stats.n_resolved,
                "wins": stats.wins,
                "losses": stats.losses,
                "expired": stats.expired,
                "pct_expired": stats.pct_expired,
                "expectancy_r": stats.expectancy_r,
                "reason": verdict.reason,
            }

            if verdict.status == "incubating":
                summary["held"] += 1
                await _patch_rows(
                    "nt_geometry_incubation", {"id": f"eq.{row['id']}"}, patch,
                )
                continue

            patch["status"] = verdict.status
            patch["decided_at"] = now.isoformat()
            await _patch_rows(
                "nt_geometry_incubation", {"id": f"eq.{row['id']}"}, patch,
            )

            if verdict.status == "rejected":
                summary["rejected"] += 1
                logger.info(
                    "[geometry_incubation] %s REJECTED brick=%.4f sl=%d tp=%d "
                    "reason=%s stats=%s",
                    symbol, brick_size, sl_bricks, tp_bricks,
                    verdict.reason, stats.as_dict(),
                )
            elif verdict.status == "promoted":
                summary["promoted"] += 1
                logger.info(
                    "[geometry_incubation] %s PROMOTED brick=%.4f sl=%d tp=%d "
                    "stats=%s mode=%s",
                    symbol, brick_size, sl_bricks, tp_bricks,
                    stats.as_dict(), mode,
                )
                if mode == "live":
                    from regime_platform.config.brick_params import write_brick_params
                    import asyncio
                    await asyncio.to_thread(
                        write_brick_params,
                        {symbol: {
                            "brick_size": brick_size,
                            "sl_bricks": sl_bricks,
                            "tp_bricks": tp_bricks,
                        }},
                    )
        except Exception as exc:
            summary["errors"] += 1
            logger.warning(
                "geometry_incubation: reconcile failed for %s: %s",
                row.get("symbol"), exc,
            )

    logger.info(
        "[geometry_incubation] reconcile done mode=%s — checked=%d promoted=%d "
        "rejected=%d held=%d errors=%d",
        mode, summary["checked"], summary["promoted"], summary["rejected"],
        summary["held"], summary["errors"],
    )
    return summary
