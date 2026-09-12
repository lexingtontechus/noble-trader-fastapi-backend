"""
Long-term (12-month) macro-forecast agent — worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md §4 (deferred),
realized 2026-09-11 per explicit follow-up instruction.

Builds the "inline LLM agent" previously scoped (design-only) in
``docs/LLM/commodity_macro_context_scope.md`` §3.3: reads the static
12-month commodity forecast model
(``docs/LLM/commodity_12m_forecast_model_2027.md``) and, for each of
the 12 commodities it covers (DAP excluded — no live feed, see that
doc §1.2), asks an LLM to classify today's position against the
forecast into STRUCTURED fields matching this codebase's existing
label vocabularies — not prose, so downstream code (macro_filter.py,
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
``llm_client.call_llm`` (OpenRouter primary, NVIDIA NIM fallback) as
``triage.py`` — no new provider decision.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from regime_platform.services.self_learning.llm_client import call_llm_with_provider
from regime_platform.sweep_orchestrator.supabase_client import upsert_macro_forecast

logger = logging.getLogger("regime.self_learning.macro_forecast_agent")

# Matches commodity_macro_context_scope.md §1.1's "what goes into
# nt_symbol" table — 12 commodities, DAP excluded (no live feed / no
# liquid futures contract, per that doc §1.2). These bare tickers match
# nt_symbol.symbol exactly (registered 2026-09-12, active=false —
# migration register_commodity_symbols_inactive) — NOT
# nt_symbol.tradingview_symbol's "EXCHANGE:SYMBOL" form, matching every
# other per-symbol table in this schema (nt_signal_sim, nt_sweep_result,
# nt_macro_regime, ...), all of which key on the bare ticker too.
SYMBOLS: tuple[str, ...] = (
    "B", "RB", "HO", "JX", "VGT", "FCPO", "CT", "SB", "RR", "ZC", "ZW", "SI",
)

_FORECAST_DOC_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "LLM" / "commodity_12m_forecast_model_2027.md"
)

_TREND_LABELS = frozenset({
    "trending_up", "trending_down", "reversal_up", "reversal_down", "consolidating",
})
_MACRO_LABELS = frozenset({"bull", "bear", "neutral"})

_MAX_TOKENS = 400

_SYSTEM_PROMPT = """You are a macro-context classifier for a Renko-brick \
signal-generation and backtesting research platform (NOT a live \
trading system). You are given a 12-month commodity forecast model \
(quarterly price ranges + named risk drivers per commodity) and \
today's date. For the ONE commodity symbol named in the user message, \
determine which forecast quarter today falls in and classify the \
current outlook.

Respond with ONLY a single JSON object, no other text, no markdown \
fencing, in exactly this shape:
{"narrative": "1-2 sentence summary", "trend": "TREND_LABEL", "direction": "DIRECTION_LABEL", "strength": 0.0}

TREND_LABEL must be exactly one of: trending_up, trending_down, \
reversal_up, reversal_down, consolidating.
DIRECTION_LABEL must be exactly one of: bull, bear, neutral.
strength is a number from 0.0 to 1.0 (confidence / position within \
the forecast range — 1.0 = at the extreme bullish/bearish edge of the \
quarter's range, 0.0 = dead center / no conviction).
If you are not confident, use "consolidating" / "neutral" / a low \
strength rather than guessing.
"""


@lru_cache(maxsize=1)
def _load_forecast_doc() -> str:
    return _FORECAST_DOC_PATH.read_text(encoding="utf-8")


def _build_prompt(symbol: str, doc_text: str, today: datetime) -> str:
    return (
        f"Today's date: {today.date().isoformat()}\n"
        f"Commodity symbol: {symbol}\n\n"
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

    return {
        "narrative": narrative.strip(),
        "current_trend_label": trend,
        "current_macro_label": direction,
        "current_macro_strength": strength_f,
    }


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


async def run_macro_forecast(symbols: tuple[str, ...] = SYMBOLS) -> dict[str, Any]:
    """Classify every symbol in ``symbols`` against the 12-month
    forecast model and upsert the results into ``nt_macro_forecast``.
    Best-effort per symbol — one failure doesn't block the rest."""
    summary = {"symbols_seen": len(symbols), "classified": 0, "skipped": 0, "errors": 0}

    try:
        doc_text = _load_forecast_doc()
    except Exception as exc:
        logger.error("macro_forecast_agent: failed to load forecast doc: %s: %r", type(exc).__name__, exc)
        summary["errors"] = len(symbols)
        return summary

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            result = await classify_symbol(symbol, doc_text, now)
        except Exception as exc:
            logger.error(
                "macro_forecast_agent: %s failed: %s: %r", symbol, type(exc).__name__, exc,
            )
            summary["errors"] += 1
            continue

        if result is None:
            summary["skipped"] += 1
            continue

        rows.append({"symbol": symbol, "updated_at": now.isoformat(), **result})
        summary["classified"] += 1

    if rows:
        await upsert_macro_forecast(rows)

    return summary
