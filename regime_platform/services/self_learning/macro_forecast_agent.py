"""
Long-term (12-month) macro-forecast agent — worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md §4 (deferred),
realized 2026-09-11 per explicit follow-up instruction.

Builds the "inline LLM agent" previously scoped (design-only) in
``docs/LLM/commodity_macro_context_scope.md`` §3.3: reads a static
12-month forecast-model document and, for each symbol it covers, asks
an LLM to classify today's position against the forecast into
STRUCTURED fields matching this codebase's existing label
vocabularies — not prose, so downstream code (macro_filter.py,
context_alignment.py) could eventually consume it:

- ``current_trend_label``: trend_filter.py's own 5-value vocab
  (trending_up / trending_down / reversal_up / reversal_down /
  consolidating).
- ``current_macro_label``: macro_filter.py's own 3-value vocab
  (bull / bear / neutral) — the "direction."
- ``current_macro_strength``: 0-1, position-within-forecast-range.

Writes to ``nt_macro_forecast`` (shadow-mode diagnostic — nothing
reads this table to make a live decision yet; that blend-into-
macro_filter.py step is the separately-scoped Option A extension in
commodity_macro_context_scope.md §3.4, not built here). Uses the same
``llm_client.call_llm_with_provider`` (OpenRouter primary, NVIDIA NIM
fallback) as ``triage.py`` — no new provider decision.

**Multi-source registry** (2026-09-12, worklog
20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md
§8): generalized from a single hardcoded (commodity doc, 12 symbols)
pair to a registry of ``ForecastSource`` entries, each its own
(doc, symbol-set) pair, evaluated independently — a doc load failure
for one source no longer blocks classification for another. Ready for
an existing-symbols (forex/equities/crypto) forecast doc to be added
as a second source once that document exists (scoped, not yet
written) — no other code change needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from regime_platform.services.self_learning.llm_client import call_llm_with_provider
from regime_platform.sweep_orchestrator.supabase_client import upsert_macro_forecast

logger = logging.getLogger("regime.self_learning.macro_forecast_agent")


@dataclass(frozen=True)
class ForecastSource:
    name: str
    doc_path: Path
    symbols: tuple[str, ...]


_DOCS_LLM_DIR = Path(__file__).resolve().parents[3] / "docs" / "LLM"

# Matches commodity_macro_context_scope.md §1.1's "what goes into
# nt_symbol" table — 12 commodities, DAP excluded (no live feed / no
# liquid futures contract, per that doc §1.2). These bare tickers match
# nt_symbol.symbol exactly (registered 2026-09-12, active=false —
# migration register_commodity_symbols_inactive, FK-enforced via
# nt_macro_forecast_symbol_fkey) — NOT nt_symbol.tradingview_symbol's
# "EXCHANGE:SYMBOL" form, matching every other per-symbol table in this
# schema (nt_signal_sim, nt_sweep_result, nt_macro_regime, ...), all of
# which key on the bare ticker too.
_COMMODITIES_SOURCE = ForecastSource(
    name="commodities_12m",
    doc_path=_DOCS_LLM_DIR / "commodity_12m_forecast_model_2027.md",
    symbols=("B", "RB", "HO", "JX", "VGT", "FCPO", "CT", "SB", "RR", "ZC", "ZW", "SI"),
)

# Existing-symbol (forex/equities/crypto) macro-outlook document — added
# 2026-09-28 (worklog/kairos/20260928_MACRO_FORECAST_REFACTOR.md). Bare
# tickers match nt_symbol.symbol (FK). Covers every non-commodity row.
_EXISTING_SYMBOLS_SOURCE = ForecastSource(
    name="existing_symbols_12m",
    doc_path=_DOCS_LLM_DIR / "existing_symbols_12m_forecast_model_2027.md",
    symbols=(
        # metals (active)
        "XAUUSD", "XAUEUR", "XAUAUD", "XAGUSD",
        # forex (active)
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD",
        # indices (active)
        "US500", "US30USD",
        # crypto (active)
        "BTCUSD",
        # equities / index names (active + inactive; classification needs only FK)
        "AAPL", "MSFT", "META", "NVDA", "TSLA", "INTC", "UK100", "RUSS2000",
    ),
)

FORECAST_SOURCES: tuple[ForecastSource, ...] = (
    _COMMODITIES_SOURCE,
    _EXISTING_SYMBOLS_SOURCE,
)

_TREND_LABELS = frozenset({
    "trending_up", "trending_down", "reversal_up", "reversal_down", "consolidating",
})
_MACRO_LABELS = frozenset({"bull", "bear", "neutral"})

_MAX_TOKENS = 700

_SYSTEM_PROMPT = """You are a macro-context classifier for a Renko-brick \
signal-generation and backtesting research platform (NOT a live \
trading system). You are given a 12-month forecast model (quarterly \
outlook + named risk drivers per instrument) and today's date. For \
the ONE symbol named in the user message, determine which forecast \
quarter today falls in and classify the current outlook. Also \
DECONSTRUCT the narrative into structured fields so downstream code \
does not re-parse prose.

Respond with ONLY a single JSON object, no other text, no markdown \
fencing, in exactly this shape:
{
  "narrative": "1-2 sentence summary",
  "trend": "TREND_LABEL",
  "direction": "DIRECTION_LABEL",
  "strength": 0.0,
  "thesis": "one sentence, falsifiable",
  "drivers": [{"name": "rates|usd|supply|demand|geopolitics|other", "stance": "bull|bear|neutral", "confidence": 0.0}],
  "risks": [{"name": "short risk name", "direction": "bull|bear|two-sided", "severity": 0.0}],
  "horizon_days": 365
}

TREND_LABEL must be exactly one of: trending_up, trending_down, \
reversal_up, reversal_down, consolidating.
DIRECTION_LABEL must be exactly one of: bull, bear, neutral.
strength / confidence / severity are numbers 0.0 to 1.0.
drivers: 1-4 items. risks: 0-3 items. Empty arrays allowed.
If you are not confident, use "consolidating" / "neutral" / low \
strength rather than guessing.
"""


@lru_cache(maxsize=None)
def _load_forecast_doc(doc_path: Path) -> str:
    return doc_path.read_text(encoding="utf-8")


def _build_prompt(symbol: str, doc_text: str, today: datetime) -> str:
    return (
        f"Today's date: {today.date().isoformat()}\n"
        f"Symbol: {symbol}\n\n"
        f"12-month forecast model:\n{doc_text}"
    )


def _validate_classification(parsed: dict[str, Any]) -> Optional[dict[str, Any]]:
    narrative = parsed.get("narrative")
    trend = parsed.get("trend")
    direction = parsed.get("direction")
    strength = parsed.get("strength")

    if not isinstance(narrative, str) or not narrative.strip():
        logger.warning("macro_forecast_agent: response missing a usable narrative — discarding")
        return None
    if trend not in _TREND_LABELS:
        logger.warning("macro_forecast_agent: out-of-vocab trend %r — discarding", trend)
        return None
    if direction not in _MACRO_LABELS:
        logger.warning("macro_forecast_agent: out-of-vocab direction %r — discarding", direction)
        return None
    try:
        strength_f = max(0.0, min(1.0, float(strength)))
    except (TypeError, ValueError):
        logger.warning("macro_forecast_agent: non-numeric strength %r — discarding", strength)
        return None

    thesis = parsed.get("thesis")
    thesis = thesis.strip() if isinstance(thesis, str) else None
    drivers = _clean_drivers(parsed.get("drivers"))
    risks = _clean_risks(parsed.get("risks"))
    try:
        horizon = int(parsed.get("horizon_days") or 365)
    except (TypeError, ValueError):
        horizon = 365

    return {
        "narrative": narrative.strip(),
        "current_trend_label": trend,
        "current_macro_label": direction,
        "current_macro_strength": strength_f,
        "thesis": thesis,
        "drivers": drivers,
        "risks": risks,
        "horizon_days": max(1, min(horizon, 3650)),
    }


def _clean_drivers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out = []
    for d in raw[:4]:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "other")[:40]
        stance = d.get("stance") if d.get("stance") in ("bull", "bear", "neutral") else "neutral"
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        out.append({"name": name, "stance": stance, "confidence": conf})
    return out


def _clean_risks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:3]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "")[:60]
        if not name:
            continue
        direction = r.get("direction") if r.get("direction") in ("bull", "bear", "two-sided") else "two-sided"
        try:
            sev = max(0.0, min(1.0, float(r.get("severity", 0.0))))
        except (TypeError, ValueError):
            sev = 0.0
        out.append({"name": name, "direction": direction, "severity": sev})
    return out


async def classify_symbol(symbol: str, doc_text: str, today: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """One symbol in, one ``nt_macro_forecast`` row dict out (minus
    ``symbol``/``updated_at``, added by the caller) — or ``None`` on
    any failure/invalid response. Best-effort, matches every other
    self-learning I/O function in this codebase."""
    prompt = _build_prompt(symbol, doc_text, today or datetime.now(timezone.utc))
    result = await call_llm_with_provider(_SYSTEM_PROMPT, prompt, max_tokens=_MAX_TOKENS)
    if result is None:
        return None
    parsed, provider = result
    validated = _validate_classification(parsed)
    if validated is None:
        return None
    validated["model_provider"] = provider
    return validated


async def _run_source(source: ForecastSource, now: datetime, summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify every symbol in one ``ForecastSource``. Mutates
    ``summary`` in place; returns the rows to upsert for this source.
    A doc-load failure only affects THIS source's symbols — other
    sources still run."""
    try:
        doc_text = _load_forecast_doc(source.doc_path)
    except Exception as exc:
        logger.error(
            "macro_forecast_agent: %s: failed to load forecast doc: %s: %r",
            source.name, type(exc).__name__, exc,
        )
        summary["errors"] += len(source.symbols)
        return []

    rows: list[dict[str, Any]] = []
    for symbol in source.symbols:
        try:
            result = await classify_symbol(symbol, doc_text, now)
        except Exception as exc:
            logger.error(
                "macro_forecast_agent: %s (%s) failed: %s: %r",
                symbol, source.name, type(exc).__name__, exc,
            )
            summary["errors"] += 1
            continue

        if result is None:
            summary["skipped"] += 1
            continue

        rows.append({
            "symbol": symbol,
            "updated_at": now.isoformat(),
            "source_doc": source.name,
            **result,
        })
        summary["classified"] += 1

    return rows


async def run_macro_forecast(sources: tuple[ForecastSource, ...] = FORECAST_SOURCES) -> dict[str, Any]:
    """Classify every symbol across every registered ``ForecastSource``
    and upsert the results into ``nt_macro_forecast``. Best-effort per
    symbol AND per source — one source's doc-load failure, or one
    symbol's classification failure, doesn't block the rest.

    P2: after classification, attach the latest typed pivot label from
    the economic-calendar cache and short-term market structure onto
    every row (same table / same upsert — not a second LLM dataset).
    """
    all_symbols = [s for source in sources for s in source.symbols]
    summary = {"symbols_seen": len(all_symbols), "classified": 0, "skipped": 0, "errors": 0, "pivot": None}

    now = datetime.now(timezone.utc)
    all_rows: list[dict[str, Any]] = []
    for source in sources:
        all_rows.extend(await _run_source(source, now, summary))

    if all_rows:
        all_rows = await _attach_calendar_pivots(all_rows, summary)
        all_rows = await _attach_market_structure(all_rows)
        all_rows = await _attach_asset_class(all_rows)
        await upsert_macro_forecast(all_rows)

    return summary


async def _attach_asset_class(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill asset_class from nt_symbol (uniform multi-asset store)."""
    import httpx

    from regime_platform.core.settings import settings

    if not settings.SUPABASE_URL:
        return rows
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_symbol",
                headers={
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                },
                params={"select": "symbol,asset_class"},
            )
            if not resp.is_success:
                return rows
            amap = {r.get("symbol"): r.get("asset_class") for r in resp.json()}
    except Exception as exc:
        logger.warning("macro_forecast_agent: asset_class lookup failed: %s", type(exc).__name__)
        return rows
    out = []
    for r in rows:
        row = dict(r)
        if not row.get("asset_class"):
            row["asset_class"] = amap.get(row.get("symbol"))
        out.append(row)
    return out


async def _attach_market_structure(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Short-term structure from latest sweep TA — strategic macro stays current_*."""
    import httpx

    from regime_platform.core.settings import settings

    if not settings.SUPABASE_URL:
        return rows
    out = []
    headers = {"apikey": settings.SUPABASE_KEY, "Authorization": f"Bearer {settings.SUPABASE_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for r in rows:
            sym = r.get("symbol")
            row = dict(r)
            try:
                resp = await client.get(
                    f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_sweep_result",
                    headers=headers,
                    params={
                        "select": "sweep_timestamp,ta_rsi,ta_adx,ta_macd,ta_bb_power,ta_ao",
                        "symbol": f"eq.{sym}",
                        "source": "eq.sweep-light",
                        "order": "sweep_timestamp.desc",
                        "limit": "1",
                    },
                )
                if resp.is_success and resp.json():
                    ta = resp.json()[0]
                    bias, strength, note = _structure_from_ta(ta)
                    if bias:
                        row["structure_bias"] = bias
                        row["structure_strength"] = strength
                        row["structure_note"] = note
                        row["structure_as_of"] = ta.get("sweep_timestamp")
                        row["structure_source"] = "ta_sweep"
            except Exception as exc:
                logger.debug("macro_forecast_agent: structure %s failed: %s", sym, type(exc).__name__)
            out.append(row)
    return out


def _structure_from_ta(ta: dict[str, Any]) -> tuple[str | None, float | None, str | None]:
    """Cheap TA → bias. RSI + MACD sign; ADX is trend strength, not direction."""
    try:
        rsi = float(ta.get("ta_rsi")) if ta.get("ta_rsi") is not None else None
        macd = float(ta.get("ta_macd")) if ta.get("ta_macd") is not None else None
        adx = float(ta.get("ta_adx")) if ta.get("ta_adx") is not None else None
    except (TypeError, ValueError):
        return None, None, None
    if rsi is None and macd is None:
        return None, None, None
    score = 0.0
    bits = []
    if rsi is not None:
        if rsi >= 55:
            score += 1
            bits.append(f"rsi={rsi:.0f}")
        elif rsi <= 45:
            score -= 1
            bits.append(f"rsi={rsi:.0f}")
    if macd is not None:
        if macd > 0:
            score += 1
            bits.append("macd+")
        elif macd < 0:
            score -= 1
            bits.append("macd-")
    if score > 0:
        bias = "bull"
    elif score < 0:
        bias = "bear"
    else:
        bias = "neutral"
    strength = min(1.0, abs(score) * 0.5 + (min(adx or 0.0, 50.0) / 100.0))
    note = " ".join(bits) if bits else "flat"
    return bias, round(strength, 3), note


async def _attach_calendar_pivots(rows: list[dict[str, Any]], summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Label recent calendar events once; stamp pivot_* on every forecast row."""
    import json

    from regime_platform.sweep_orchestrator.kairos import DEFAULT_CALENDAR_PATH
    from regime_platform.sweep_orchestrator.kairos_pivot import (
        attach_pivot_fields,
        latest_pivot_for_rows,
    )

    try:
        if not DEFAULT_CALENDAR_PATH.exists():
            return rows
        events = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))
        pivot = await latest_pivot_for_rows(rows, events)
    except Exception as exc:
        logger.warning("macro_forecast_agent: pivot attach failed: %s: %r", type(exc).__name__, exc)
        return rows

    summary["pivot"] = (pivot or {}).get("label")
    if not pivot:
        return rows
    event_rec = {
        "label": pivot.get("label"),
        "axis": pivot.get("axis"),
        "intensity": pivot.get("intensity"),
        "surprise": pivot.get("surprise"),
        "as_of": pivot.get("as_of"),
        "title": pivot.get("event_title"),
        "summary": pivot.get("summary"),
    }
    out = []
    for r in rows:
        row = attach_pivot_fields(r, pivot)
        prior = row.get("events") if isinstance(row.get("events"), list) else []
        # keep last 8 unique-by-as_of+title
        merged = [e for e in prior if not (
            e.get("as_of") == event_rec["as_of"] and e.get("title") == event_rec["title"]
        )]
        merged.append(event_rec)
        row["events"] = merged[-8:]
        out.append(row)
    return out
