"""
Bracket Reachability — per-symbol, per-distance/ATR-bucket diagnostic
═══════════════════════════════════════════════════════════════════

Phase 1 of the calibration reframe
(worklog/20260825_CALIBRATION_REFRAME_SCOPE.md). Root cause 3 of that
investigation found that most "expired" outcomes in ``nt_signal_sim`` are
not a directional-calibration failure — they're a bracket placed wider
than the symbol reliably moves within the live 48h deadline. Raw
%-distance is meaningless across symbols (a 4% target is trivial for
XAGUSD, essentially unreachable for XAUUSD in the same window), so
reachability has to be measured as ``distance / ATR``, per symbol.

This module is a READ-ONLY DIAGNOSTIC (Phase 1 scope only): it reads
resolved ``nt_signal_sim`` rows — qualified and unqualified both, since
this stage only answers "does this bracket width get reached", which
unqualified rows can answer just as honestly as qualified ones, unlike
calibration bias itself — computes ``distance / atr`` per row, buckets
per symbol, and upserts hit-rate + time-to-resolution percentiles per
bucket into ``nt_bracket_reachability`` (migration 120). Nothing
downstream reads this table yet; wiring it into calibration bias
(Phase 2) and heavy-sweep combo selection (Phase 3) is separate,
not-yet-done work.

Historical rows predate ``nt_signal_sim.atr`` (added by this same
migration), so they have no stored value. Those are backfilled with each
symbol's CURRENT ATR(14) as a proxy — not point-in-time accurate, but
volatility regimes change slowly enough that this is an honest
approximation, consistent with the worklog's own rejection of a more
"precise" but biased velocity metric (see the worklog's methodology
note). Rows are tagged ``atr_source`` = ``stored`` or ``proxy_current``
so the distinction stays visible downstream.

Usage (standalone):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.bracket_reachability

Design rules: best-effort only, matching ``eod_resolver.py`` /
``auto_tuner.py`` — a failure here is logged and never raised into the
sweep tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.heavy_sweep import compute_atr
from regime_platform.sweep_orchestrator.price_fetcher import (
    SWEEP_CANDLE_HOURS,
    fetch_candles_for_symbols,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    _delete_old_rows,
    _headers,
    _url,
    fetch_active_symbols,
    upsert_bracket_reachability,
)

logger = logging.getLogger("regime.sweep.bracket_reachability")

# (lo, hi, label) — hi=None means open-ended ("4+"). Same shape as the
# worklog's ad-hoc tables, generalized from raw %-distance to distance/ATR.
ATR_BUCKETS: tuple[tuple[float, float | None, str], ...] = (
    (0.0, 0.5, "<0.5"),
    (0.5, 1.0, "0.5-1"),
    (1.0, 2.0, "1-2"),
    (2.0, 4.0, "2-4"),
    (4.0, None, "4+"),
)

_PAGE_SIZE = 1000


async def _fetch_cutover_at() -> str | None:
    """The resolver-basis cutover timestamp (nt_calibration_cutover), or None.

    Reads the SAME single-row marker v_calibration_records_current and
    calibration_bias_bucket use, so reachability, the bias table, and the live
    calibration gate all exclude the same pre-intrabar era. Returns the ISO
    timestamp with a PostgREST-safe ``Z`` suffix. Missing table/row/URL → None
    (caller skips the era filter, fail-open). Kept local (not imported from
    calibration_bias_bucket) to avoid a circular import — both read the same
    table, which is the coherence point.
    """
    try:
        if not settings.SUPABASE_URL:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_calibration_cutover"),
                headers=_headers(),
                params={"select": "cutover_at", "limit": "1"},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            raw = rows[0].get("cutover_at")
            if not raw:
                return None
            return str(raw).replace("+00:00", "Z")
    except Exception as exc:
        logger.warning("bracket_reachability: cutover marker fetch failed: %s", exc)
        return None


def _bucket_for(ratio: float) -> str | None:
    for lo, hi, label in ATR_BUCKETS:
        if ratio >= lo and (hi is None or ratio < hi):
            return label
    return None


# vol_tier — 2026-09-13, worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md.
# ATR itself is regime-conditional (the HMM's volatility dimension is fit
# on the same price history ATR is computed from), so the same
# distance/ATR bucket represents a different absolute price distance
# depending on which of the 4 vol tiers is active. Bucketing by
# (symbol, vol_tier, distance_atr_bucket) instead of (symbol,
# distance_atr_bucket) is the same fix Phase 1 already applied once (raw
# %-distance -> distance/ATR), one dimension further.
_UNKNOWN_VOL_TIER = "unknown"


def _vol_tier(regime_label: str | None) -> str:
    """Extract the HMM volatility tier from a combined regime label.

    ``regime_label`` is built as ``f"{vol_state}_vol_{trend_state}"``
    (``regime_engine.py:562``), e.g. ``"med_low_vol_strong_bear"`` ->
    ``"med_low_vol"``. Returns ``"unknown"`` (not None) for a missing or
    unparseable label (e.g. the pre-HMM-bootstrap ``"unknown"`` fallback)
    — a concrete sentinel so it can bucket cleanly rather than being
    dropped, matching ``atr_source``'s ``stored``/``proxy_current``
    convention rather than a nullable column (see migration 152's
    docstring for why: PostgREST's upsert ``on_conflict`` needs a real
    unique constraint on the exact column list, not an expression index
    over a nullable column).
    """
    if not regime_label or "_vol_" not in regime_label:
        return _UNKNOWN_VOL_TIER
    return regime_label.split("_vol_")[0] + "_vol"


def _dedupe_episodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate captures of the SAME economic signal to one row.

    2026-10-04 (weekend investigation): the light sweep runs every 15 min
    and re-captures a still-valid signal on EVERY tick, so one economic
    signal lands as N identical ``nt_signal_sim`` rows (same symbol +
    entry + take_profit + stop_loss), all resolving the same way. On
    Sat 2026-10-03 XAUAUD wrote 75 identical rows (10.5x duplication
    across the day) — counting them as 75 independent trials poisoned
    this table's hit-rate buckets and the calibration bias built on them.

    Episode key is ``(symbol, signal, regime, entry_price, take_profit,
    stop_loss)`` — the same "same signal" content the orchestrator's own
    content-hash dedup uses (orchestrator.py D5 hashes symbol + direction +
    regime + brick params + price band). ``regime`` belongs in the key: a
    capture that spans a regime shift is a genuinely different signal, not a
    re-capture. ``stop_loss`` may be NULL on older rows (pre-migration);
    those collapse by the remaining key rather than being dropped.

    Keeps the EARLIEST row per episode (lowest ``ts``) so the episode is
    anchored at its origin, and is stable across repeated builds.
    """
    seen: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (
            r.get("symbol"),
            r.get("signal"),
            r.get("regime"),
            r.get("entry_price"),
            r.get("take_profit"),
            r.get("stop_loss"),
        )
        prev = seen.get(key)
        if prev is None or str(r.get("ts") or "") < str(prev.get("ts") or ""):
            seen[key] = r
    return list(seen.values())


async def _fetch_resolved_rows(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Paginate resolved ``nt_signal_sim`` rows (any outcome).

    Mirrors ``eod_resolver._fetch_pending`` / ``noble_calibration.py``'s
    offset-loop pagination, but the mirror-image filter: ``outcome`` NOT
    null (resolved), instead of IS null (pending).

    Rows are collapsed to one per economic episode by ``_dedupe_episodes``
    (2026-10-04) before being returned, so every bucket ``n``/hit-rate is
    an independent-signal count, not a per-tick re-capture count.
    """
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params: dict[str, str] = {
        "select": "symbol,ts,entry_price,take_profit,stop_loss,atr,outcome,resolved_at,regime,signal",
        "outcome": "not.is.null",
        "contaminated": "eq.false",
        "order": "ts.asc",
        "limit": str(_PAGE_SIZE),
    }
    if symbols:
        params["symbol"] = f"in.({','.join(symbols)})"
    # Era filter (worklog 20261009_CALIBRATION_REACHABILITY_TRIAGE.md §4/§5 and
    # 20261009_P_WIN_BRACKET_COHERENCE.md): restrict to post-cutover (intrabar)
    # evidence, reading the SAME nt_calibration_cutover marker the bias-by-bucket
    # recompute and v_calibration_records_current use. Without this, reachability
    # (and therefore p_win_bracket, which reads it) spans the pre-2026-10-02
    # close-walk era — the tight buckets then read p_win_resolve≈0.27 when the
    # clean post-cutover WR is 0.056, a ~5x overstatement feeding the EV engine.
    # Missing marker → no filter (fail-open to prior behaviour).
    cutover_at = await _fetch_cutover_at()
    if cutover_at:
        params["resolved_at"] = f"gte.{cutover_at}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    # Collapse per-tick re-captures of the same signal to one episode each
    # (2026-10-04) — see _dedupe_episodes. Applied here so every caller
    # (build_reachability_table, the CLI report, verification) gets the
    # same independent-signal population.
    return _dedupe_episodes(rows)


async def _current_atr_by_symbol(symbols: list[str]) -> dict[str, float]:
    """Proxy ATR(14) per symbol from current price history.

    Backfill for historical rows captured before migration 120 added
    ``nt_signal_sim.atr``. Same fetch + compute_atr call heavy_sweep makes
    at sweep time (90-day hourly closes, period=14).
    """
    if not symbols:
        return {}
    active = await fetch_active_symbols()
    by_symbol = {s["symbol"]: s for s in active if s.get("symbol")}
    wanted = [by_symbol[s] for s in symbols if s in by_symbol]
    if not wanted:
        return {}
    batch = await fetch_candles_for_symbols(wanted, n_bars=SWEEP_CANDLE_HOURS)
    proxies: dict[str, float] = {}
    for sym in symbols:
        prices = batch.get(sym, [])
        atr = compute_atr(prices, period=14) if len(prices) >= 15 else 0.0
        if atr > 0:
            proxies[sym] = atr
    return proxies


def _signal_side(raw: str | None) -> str:
    """Normalize ``nt_signal_sim.signal`` to a reachability side key.

    ``buy`` / ``sell`` keep their side (migration 159 — direction-asymmetric
    TP/SL reach is the whole point). ``neutral`` and anything else collapse
    into ``all`` only via the pooled rollup path; a non-side row is skipped
    for per-side buckets and counted in ``all``.
    """
    s = (raw or "").strip().lower()
    if s in ("buy", "sell"):
        return s
    return "other"


def _hours_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.0, (t1 - t0).total_seconds() / 3600.0)
    except Exception:
        return None


async def build_reachability_table(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert the reachability table.

    Returns the computed rows (used by the CLI report / verification).
    Best-effort: logs and returns an empty list on failure, never raises.
    """
    try:
        rows = await _fetch_resolved_rows(symbols)
    except Exception as exc:
        logger.error("bracket_reachability: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("bracket_reachability: no resolved nt_signal_sim rows found")
        return []

    missing_atr_symbols = sorted({
        r["symbol"] for r in rows if not r.get("atr") and r.get("symbol")
    })
    try:
        proxy_atr = await _current_atr_by_symbol(missing_atr_symbols)
    except Exception as exc:
        logger.warning("bracket_reachability: ATR proxy fetch failed: %s", exc)
        proxy_atr = {}

    bucket_data: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0, "tp_hit": 0, "sl_hit": 0, "expired": 0,
            "hours": [], "n_stored": 0, "n_proxy": 0,
        }
    )

    for row in rows:
        symbol = row.get("symbol")
        entry = row.get("entry_price")
        tp = row.get("take_profit")
        outcome = row.get("outcome")
        if (
            symbol is None or entry is None or tp is None
            or outcome not in ("tp_hit", "sl_hit", "expired")
        ):
            continue

        atr = row.get("atr")
        atr_source = "stored"
        if not atr:
            atr = proxy_atr.get(symbol)
            atr_source = "proxy_current"
        if not atr:
            continue

        distance = abs(float(tp) - float(entry))
        ratio = distance / atr
        bucket = _bucket_for(ratio)
        if bucket is None:
            continue
        vol_tier = _vol_tier(row.get("regime"))
        side = _signal_side(row.get("signal"))
        # Per-side rows only for real buy/sell; every row (including
        # neutral/other) still rolls into signal='all' so pooled stats match
        # the pre-159 population.
        sides = ["all"] + ([side] if side in ("buy", "sell") else [])

        for sig in sides:
            d = bucket_data[(symbol, vol_tier, bucket, sig)]
            d["n"] += 1
            d[outcome] += 1
            if atr_source == "stored":
                d["n_stored"] += 1
            else:
                d["n_proxy"] += 1
            if outcome in ("tp_hit", "sl_hit"):
                hrs = _hours_between(row.get("ts"), row.get("resolved_at"))
                if hrs is not None:
                    d["hours"].append(hrs)

    output_rows: list[dict[str, Any]] = []
    for (symbol, vol_tier, bucket, sig), d in bucket_data.items():
        n = d["n"]
        hours = d["hours"]
        # Batch B: dominant source is informational; consumers that need
        # point-in-time ATR must filter n_proxy==0 (or n_stored/n).
        atr_source = "stored" if d["n_stored"] >= d["n_proxy"] else "proxy_current"
        output_rows.append({
            "symbol": symbol,
            "vol_tier": vol_tier,
            "distance_atr_bucket": bucket,
            "signal": sig,
            "n": n,
            "n_stored": d["n_stored"],
            "n_proxy": d["n_proxy"],
            "pct_hit_tp": round(d["tp_hit"] / n, 4),
            "pct_hit_sl": round(d["sl_hit"] / n, 4),
            "pct_expired": round(d["expired"] / n, 4),
            "median_hours_to_resolve": (
                round(float(np.percentile(hours, 50)), 2) if hours else None
            ),
            "p75_hours": round(float(np.percentile(hours, 75)), 2) if hours else None,
            "p90_hours": round(float(np.percentile(hours, 90)), 2) if hours else None,
            "atr_source": atr_source,
        })

    # Delete-then-insert: remove old rows for these symbols before upserting
    # fresh ones, so the table always reflects the latest build (2026-10-07).
    # Without this, old rows accumulate and a limit=50 query returns an
    # arbitrary subset that looks stale even though the latest build is fresh.
    try:
        symbols_in_build = sorted({r["symbol"] for r in output_rows})
        await _delete_old_rows("nt_bracket_reachability", symbols_in_build)
    except Exception as exc:
        logger.warning("bracket_reachability: delete old rows failed: %s", exc)

    try:
        await upsert_bracket_reachability(output_rows)
    except Exception as exc:
        logger.error("bracket_reachability: upsert failed: %s", exc)

    logger.info(
        "bracket_reachability: computed %d bucket row(s) across %d symbol(s)",
        len(output_rows), len({r["symbol"] for r in output_rows}),
    )
    return output_rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No reachability rows computed.")
        return
    print(
        f"{'Symbol':<10} {'VolTier':<13} {'Bucket':<8} {'Side':<5} {'n':>6} {'%TP':>7} {'%SL':>7} "
        f"{'%Exp':>7} {'medHr':>7} {'p75':>7} {'p90':>7} {'src':<14}"
    )
    print("-" * 111)
    for row in sorted(
        rows,
        key=lambda r: (
            r["symbol"],
            r.get("vol_tier", ""),
            r["distance_atr_bucket"],
            r.get("signal", "all"),
        ),
    ):
        med = row["median_hours_to_resolve"]
        p75 = row["p75_hours"]
        p90 = row["p90_hours"]
        print(
            f"{row['symbol']:<10} {row.get('vol_tier', 'unknown'):<13} {row['distance_atr_bucket']:<8} "
            f"{row.get('signal', 'all'):<5} {row['n']:>6} "
            f"{row['pct_hit_tp'] * 100:>6.1f}% {row['pct_hit_sl'] * 100:>6.1f}% "
            f"{row['pct_expired'] * 100:>6.1f}% "
            f"{med if med is not None else '-':>7} "
            f"{p75 if p75 is not None else '-':>7} "
            f"{p90 if p90 is not None else '-':>7} "
            f"{row['atr_source']:<14}"
        )


async def main() -> None:
    """CLI entry: build the reachability table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await build_reachability_table()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
