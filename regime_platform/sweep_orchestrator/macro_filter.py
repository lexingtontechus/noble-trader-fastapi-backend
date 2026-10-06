"""
Macro Filter — long-range (multi-month) trend read
════════════════════════════════════════════════════════════════════

worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md, "Macro filter —
full build scope" (2026-08-26/28). Every existing signal source in this
platform looks at most ~8-12 days of data (the trend HMM's own refit
window); nothing has multi-month market memory. This is a pure
price-based daily-bar read — last close vs. its 200-day SMA,
EMA-smoothed so a single bad print doesn't flip the label — refreshed
once a day, independent of both sweep paths' own cadence.

Deliberately does NOT consider win/loss/PnL (that's a separate signal,
``calibration_bias_bucket.py`` / ``macro_edge.py`` — the latter reads
THIS module's captured output as evidence, but the two stay separate
numbers, not blended into one, per worklog "Confirmed: this is two
separate parts, not one metric").

Mode gate (``config/feature_flags.yaml``, ``macro_regime_mode``):
  off:    skip the read entirely (no Supabase round-trip).
  shadow: compute + capture macro_label/macro_strength on nt_signal_sim
          (so macro_edge.py can accumulate evidence), do NOT touch
          Kelly sizing. Default.
  live:   additionally multiply effective_kelly by
          ``kelly_macro_multiplier()``.

Usage (standalone daily refresh):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.macro_filter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from regime_platform.core.feature_flags import (
    get_macro_bear_bull_threshold,
    get_macro_boost_scale,
    get_macro_ema_half_life_days,
    get_macro_penalty_scale,
    get_macro_sma_window_days,
    get_macro_strength_saturation,
)
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_active_symbols,
    fetch_macro_regime,
    upsert_macro_regime,
)

logger = logging.getLogger("regime.sweep.macro_filter")

# Daily bars fetched per refresh — SMA window (default 200) plus buffer
# for the EMA to have settled, matching the original scope's "~300
# daily bars (~14 months)" candidate.
_FETCH_BUFFER_DAYS = 100


def compute_macro_regime(
    daily_closes: list[float],
    prev_score_smoothed: float | None = None,
) -> dict[str, Any] | None:
    """Pure calculation — no I/O. Returns ``None`` if there isn't enough
    history for a full SMA window.

    ``prev_score_smoothed`` is the PREVIOUS day's ``macro_score_smoothed``
    (read from ``nt_macro_regime`` before this call) so the EMA has real
    continuity across daily refreshes — the first-ever computation for a
    symbol has no previous value, so the raw score is used as its own
    bootstrap (matches ``RegimeHMM``'s own "no ``prev_*_means`` on the
    first fit" convention).
    """
    window = get_macro_sma_window_days()
    if len(daily_closes) < window:
        return None

    sma = sum(daily_closes[-window:]) / window
    if sma == 0:
        return None
    last_close = daily_closes[-1]
    macro_score = (last_close - sma) / sma

    half_life = get_macro_ema_half_life_days()
    alpha = 1.0 - 0.5 ** (1.0 / half_life) if half_life > 0 else 1.0
    if prev_score_smoothed is None:
        macro_score_smoothed = macro_score
    else:
        macro_score_smoothed = prev_score_smoothed + alpha * (macro_score - prev_score_smoothed)

    threshold = get_macro_bear_bull_threshold()
    if macro_score_smoothed > threshold:
        macro_label = "bull"
    elif macro_score_smoothed < -threshold:
        macro_label = "bear"
    else:
        macro_label = "neutral"

    saturation = get_macro_strength_saturation()
    macro_strength = (
        min(1.0, abs(macro_score_smoothed) / saturation) if saturation > 0 else 0.0
    )

    return {
        "macro_score": round(macro_score, 6),
        "macro_score_smoothed": round(macro_score_smoothed, 6),
        "macro_label": macro_label,
        "macro_strength": round(macro_strength, 4),
        "sma_window_days": window,
    }


def kelly_macro_multiplier(
    macro_label: str | None,
    macro_strength: float | None,
    direction: str,
) -> float:
    """Kelly multiplier for one trade given the current macro read.

    ``direction`` is the trade's own signal (``"buy"``/``"sell"`` —
    callers only invoke this for non-neutral signals). A neutral or
    missing macro read is a no-op (``1.0``); an aligned read boosts,
    boost/penalty scaled by ``macro_strength`` (0-1) and clamped to
    ``[0.25, 1.5]`` — this mechanism alone can never zero out or double
    a position, same hard safety band as every other multiplier in this
    codebase (``stagnation_kill_switch``, the ``p_win`` hard floor).
    """
    if not macro_label or macro_label == "neutral" or macro_strength is None:
        return 1.0

    implied = "bull" if direction == "buy" else "bear"
    strength = max(0.0, min(1.0, float(macro_strength)))
    if macro_label == implied:
        mult = 1.0 + strength * get_macro_boost_scale()
    else:
        mult = 1.0 - strength * get_macro_penalty_scale()
    return max(0.25, min(1.5, mult))


async def get_macro_regime(symbol: str) -> dict[str, Any] | None:
    """Live per-tick read used by both sweep paths — thin wrapper over
    ``supabase_client.fetch_macro_regime``, kept as its own function so
    the sweep paths import from this module (their existing convention
    for feature-specific reads) rather than reaching into
    ``supabase_client`` directly."""
    return await fetch_macro_regime(symbol)


async def _clef_macro_coherence(
    symbol: str,
    price_label: str,
    price_strength: float,
    narrative_label: str,
    narrative_strength: float,
    thesis: str,
    drivers: list[dict],
    risks: list[dict],
    regime_label: str | None = None,
    regime_conf: float | None = None,
    vol_tier: str | None = None,
) -> float | None:
    """Ask Clef-Flash: do the price trend and narrative forecast agree?

    This is a COHERENCE read, not a re-derivation. The model reads the
    narrative text (thesis/drivers/risks) and the price-based label,
    and answers whether they agree. It never sees price data.

    Returns P(agree) as a float, or None on any failure (fail-open).
    """
    if not thesis:
        return None

    try:
        from regime_platform.core.feature_flags import get_clef_flash_mode
        if get_clef_flash_mode() == "off":
            return None

        from regime_platform.services.decision.clef_flash import decide

        state = {
            "symbol": symbol,
            "price_macro_label": price_label,
            "price_macro_strength": round(price_strength, 4),
            "narrative_thesis": thesis,
            "narrative_direction": narrative_label,
            "narrative_strength": round(narrative_strength, 4),
            "drivers": [d.get("name", "") for d in drivers[:4]],
            "risks": [r.get("name", "") for r in risks[:3]],
        }
        if regime_label:
            state["regime_label"] = regime_label
        if regime_conf is not None:
            state["regime_conf"] = round(regime_conf, 4)
        if vol_tier:
            state["vol_tier"] = vol_tier

        questions = {
            "coherence": {
                "type": "noul",
                "instructions": (
                    "Does the price-based macro trend agree with the narrative "
                    "forecast direction? Answer true if they point the same way, "
                    "false if they conflict."
                ),
            },
        }

        answers = await decide(state, questions)
        if answers is None:
            return None

        coherence_answer = answers.get("coherence", {})
        p_agree = coherence_answer.get("noul")
        if p_agree is None:
            return None

        return float(p_agree)
    except Exception as exc:
        logger.debug("macro_filter: clef coherence failed for %s: %s", symbol, exc)
        return None


async def refresh_macro_regime(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Daily job: fetch fresh daily bars, recompute, upsert
    ``nt_macro_regime``. Returns the computed rows. Best-effort: logs and
    returns an empty list on failure, never raises.

    Also computes Clef-Flash macro coherence (shadow) when the narrative
    forecast is available. The coherence stamp is written to
    ``nt_macro_forecast.coherence`` — never read back into a decision.
    """
    try:
        active = await fetch_active_symbols()
    except Exception as exc:
        logger.error("macro_filter: fetch_active_symbols failed: %s", exc)
        return []
    if symbols:
        active = [s for s in active if s.get("symbol") in symbols]
    if not active:
        logger.info("macro_filter: no active symbols to refresh")
        return []

    window = get_macro_sma_window_days()
    n_bars = window + _FETCH_BUFFER_DAYS
    try:
        daily_closes_by_symbol = await fetch_tradingview_batch(active, n_bars, "1D")
    except Exception as exc:
        logger.error("macro_filter: daily bar fetch failed: %s", exc)
        return []

    output_rows: list[dict[str, Any]] = []
    for sym in active:
        symbol = sym.get("symbol")
        closes = daily_closes_by_symbol.get(symbol)
        if not symbol or not closes:
            continue

        prev_row = await fetch_macro_regime(symbol)
        prev_score_smoothed = (
            prev_row.get("macro_score_smoothed") if prev_row else None
        )

        computed = compute_macro_regime(closes, prev_score_smoothed)
        if computed is None:
            logger.warning(
                "macro_filter: %s: insufficient daily history (%d bars, need >=%d)",
                symbol, len(closes), window,
            )
            continue

        # computed_at set HERE, not inside compute_macro_regime() (that
        # function is pure/no-I/O by design — a wall-clock timestamp
        # doesn't belong in it). Bug found 2026-09-02, worklog
        # 20260902_MACRO_REGIME_COMPUTED_AT_BUG.md: compute_macro_regime()
        # never returned a computed_at key at all, so upsert_macro_regime's
        # merge-duplicates upsert (which only SETs columns present in the
        # payload) silently left every symbol's row frozen at whatever
        # computed_at its very first insert wrote (2026-08-29) — 5+
        # "successful" daily/catch-up refreshes since then never actually
        # advanced it.
        output_rows.append({
            "symbol": symbol,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            **computed,
        })

    try:
        await upsert_macro_regime(output_rows)
    except Exception as exc:
        logger.error("macro_filter: upsert failed: %s", exc)

    logger.info(
        "macro_filter: refreshed %d symbol(s)", len(output_rows),
    )

    # ── Clef-Flash macro coherence (shadow) ────────────────────────────
    # Runs after the price macro refresh. Reads the narrative forecast
    # from nt_macro_forecast, asks Clef-Flash whether price and narrative
    # agree, and stamps the result. Never read back into a decision.
    try:
        from regime_platform.core.feature_flags import get_clef_flash_mode
        if get_clef_flash_mode() != "off":
            from regime_platform.sweep_orchestrator.supabase_client import (
                _headers,
                _url,
            )
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _url("nt_macro_forecast"),
                    headers=_headers(),
                    params={
                        "select": "symbol,direction,strength,thesis,drivers,risks",
                        "limit": "100",
                    },
                )
            forecasts = resp.json() if resp.status_code == 200 else []

            for fc in forecasts:
                sym = fc.get("symbol")
                if not sym:
                    continue
                # Find the matching macro row
                macro_row = next((r for r in output_rows if r["symbol"] == sym), None)
                if not macro_row:
                    continue

                price_label = macro_row.get("macro_label", "neutral")
                price_strength = macro_row.get("macro_strength", 0.0)
                narrative_label = fc.get("direction", "neutral")
                narrative_strength = fc.get("strength", 0.0)
                thesis = fc.get("thesis", "")
                drivers = fc.get("drivers", [])
                risks = fc.get("risks", [])

                coherence = await _clef_macro_coherence(
                    symbol=sym,
                    price_label=price_label,
                    price_strength=price_strength,
                    narrative_label=narrative_label,
                    narrative_strength=narrative_strength,
                    thesis=thesis,
                    drivers=drivers,
                    risks=risks,
                )

                if coherence is not None:
                    # Stamp on nt_macro_forecast
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            await client.patch(
                                _url("nt_macro_forecast"),
                                headers={**_headers(), "Content-Type": "application/json"},
                                params={"symbol": f"eq.{sym}"},
                                json={
                                    "coherence": round(coherence, 4),
                                    "coherence_model": "@cf/cloudflare/clef-flash",
                                },
                            )
                        logger.info(
                            "macro_filter: %s coherence=%.4f (price=%s narrative=%s)",
                            sym, coherence, price_label, narrative_label,
                        )
                    except Exception as exc:
                        logger.debug("macro_filter: %s coherence stamp failed: %s", sym, exc)
    except Exception as exc:
        logger.debug("macro_filter: coherence pass failed: %s", exc)

    return output_rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No macro regime rows computed.")
        return
    print(f"{'Symbol':<10} {'Score':>8} {'Smoothed':>9} {'Label':<8} {'Strength':>9}")
    print("-" * 50)
    for row in sorted(rows, key=lambda r: r["symbol"]):
        print(
            f"{row['symbol']:<10} {row['macro_score']:>8.4f} "
            f"{row['macro_score_smoothed']:>9.4f} {row['macro_label']:<8} "
            f"{row['macro_strength']:>9.4f}"
        )


async def main() -> None:
    """CLI entry: refresh the macro regime table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await refresh_macro_regime()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
