"""
Phase 3 MVP — LLM-assisted triage (worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md,
self_learning_phase_3_4_forecasting_rescope, RM-50).

Minimum-effort version: reads whatever Phase 2's detectors haven't
triaged yet (``nt_self_learning_findings.triaged_at IS NULL``), grounds
each in a handful of related code/worklog chunks via Phase 1's existing
``semantic_search`` (no new retrieval infra), and asks an LLM for a
short narrative plus an OPTIONAL, bounded Tier D parameter-change
proposal. Output lands in ``nt_self_learning_reports`` for a human to
read — nothing here writes to ``tuning_registry`` or any other live
parameter. Structurally enforced, not just by convention: this module
never imports ``tuning_registry.write_param``.

No new embedding model, no forecasting integration, no SDK dependency
for the LLM call (raw ``httpx``, same pattern ``orchestrator.py`` uses
for ``TIMESFM_API_KEYS``) — see the worklog for what was deliberately
left out of this first cut.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

from regime_platform.core import tuning_registry
from regime_platform.services.self_learning.knowledge_index import semantic_search
from regime_platform.sweep_orchestrator.auto_tuner import TIER_D_PILOT_PARAMS
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_untriaged_findings,
    insert_self_learning_report,
    mark_finding_triaged,
)

logger = logging.getLogger("regime.self_learning.triage")

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
# Cheapest available model — MVP cost discipline, not a quality decision.
_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 512
_REQUEST_TIMEOUT = 30.0
_CONTEXT_K = 3

_SYSTEM_PROMPT = """You are a diagnostic assistant for a Renko-brick \
signal-generation and backtesting research platform (NOT a live \
trading system). You are given one finding from an automated \
statistical detector, plus a few related code/worklog excerpts from \
this repository for context.

Write a short (2-4 sentence) root-cause narrative for the finding. \
You may ALSO propose changing exactly one tunable parameter, but ONLY \
if you choose it from the "Allowed parameters" list below — never any \
other name. If no parameter change is warranted, or you are not \
confident, set proposed_param and proposed_value to null.

Nothing you output is applied automatically. A human reads every \
report before anything changes.

Respond with ONLY a single JSON object, no other text, no markdown \
fencing, in exactly this shape:
{"narrative": "...", "proposed_param": "NAME_OR_NULL", "proposed_value": 0.0_OR_NULL}
"""

_warned_missing_key = False


def _anthropic_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


async def _allowed_params_context() -> dict[str, dict[str, float]]:
    """Current value + bounds for every Tier D pilot param, for the
    prompt. A param with no file/DB entry yet is included with
    ``current`` omitted rather than dropped — the model can still
    propose an initial value within bounds."""
    out: dict[str, dict[str, float]] = {}
    for name in TIER_D_PILOT_PARAMS:
        lo, hi = tuning_registry.PARAM_BOUNDS[name]
        entry: dict[str, float] = {"min": lo, "max": hi}
        try:
            entry["current"] = await tuning_registry.read_param_async(name)
        except KeyError:
            pass
        out[name] = entry
    return out


def _build_prompt(
    finding: dict[str, Any],
    context_chunks: list[str],
    allowed_params: dict[str, dict[str, float]],
) -> str:
    lines = [
        f"Finding (detector={finding.get('detector')!r}, symbol={finding.get('symbol')!r}):",
        str(finding.get("summary", "")),
        f"Details: {json.dumps(finding.get('details') or {}, default=str)}",
        "",
        "Related code/worklog context:",
    ]
    if context_chunks:
        for i, chunk in enumerate(context_chunks, 1):
            lines.append(f"--- excerpt {i} ---")
            lines.append(chunk[:1500])
    else:
        lines.append("(none found)")
    lines.append("")
    lines.append("Allowed parameters (name: min/max/current):")
    for name, bounds in allowed_params.items():
        current = bounds.get("current", "unset")
        lines.append(f"- {name}: {bounds['min']}/{bounds['max']}/{current}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


async def _call_llm(prompt: str) -> Optional[dict[str, Any]]:
    global _warned_missing_key
    api_key = _anthropic_api_key()
    if not api_key:
        if not _warned_missing_key:
            logger.warning(
                "ANTHROPIC_API_KEY env var not set — Phase 3 triage will "
                "no-op for every finding until it's added as a Studio secret."
            )
            _warned_missing_key = True
        return None

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                _ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "max_tokens": _MAX_TOKENS,
                    "system": _SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if not resp.is_success:
                logger.warning(
                    "triage: Anthropic API HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
                return None
            data = resp.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            return _extract_json(text)
    except Exception as exc:
        logger.warning("triage: LLM call failed: %s: %r", type(exc).__name__, exc)
        return None


def _validate_proposal(
    proposed_param: Any,
    proposed_value: Any,
) -> tuple[Optional[str], Optional[float]]:
    """Discard anything outside the Tier D allowlist; clamp anything
    inside it. Belt-and-suspenders — nothing reads this table to make
    a live decision, but a stored proposal should still be honest
    about what's actually in bounds."""
    if not isinstance(proposed_param, str) or proposed_param not in TIER_D_PILOT_PARAMS:
        if proposed_param not in (None, "null"):
            logger.warning(
                "triage: LLM proposed out-of-scope param %r — discarded", proposed_param,
            )
        return None, None
    try:
        value = float(proposed_value)
    except (TypeError, ValueError):
        return None, None
    return proposed_param, tuning_registry.clamp(proposed_param, value)


async def triage_finding(finding: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One finding in, one report dict out — or ``None`` if the LLM
    call failed/was skipped (best-effort, matches
    ``run_detectors.py``'s per-item pattern)."""
    query = str(finding.get("summary", ""))
    try:
        results = semantic_search(query, k=_CONTEXT_K)
        context_chunks = [r.text for r in results]
    except Exception as exc:
        logger.warning("triage: semantic_search failed: %s: %r", type(exc).__name__, exc)
        context_chunks = []

    allowed_params = await _allowed_params_context()
    prompt = _build_prompt(finding, context_chunks, allowed_params)
    parsed = await _call_llm(prompt)
    if parsed is None:
        return None

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        logger.warning("triage: LLM response missing a usable narrative — skipping")
        return None

    proposed_param, proposed_value = _validate_proposal(
        parsed.get("proposed_param"), parsed.get("proposed_value"),
    )
    current_value = allowed_params.get(proposed_param, {}).get("current") if proposed_param else None

    return {
        "narrative": narrative.strip(),
        "proposed_param": proposed_param,
        "proposed_value": proposed_value,
        "current_value": current_value,
    }


async def run_triage(limit: int = 20) -> dict[str, Any]:
    """Fetch untriaged findings, triage each (best-effort — one
    failure doesn't block the rest), write reports, mark findings
    triaged. Returns a summary dict."""
    summary = {"findings_seen": 0, "reports_written": 0, "skipped": 0, "errors": 0}

    try:
        findings = await fetch_untriaged_findings(limit=limit)
    except Exception as exc:
        logger.error("triage: fetch_untriaged_findings failed: %s: %r", type(exc).__name__, exc)
        summary["errors"] += 1
        return summary

    summary["findings_seen"] = len(findings)
    for finding in findings:
        finding_id = finding.get("id")
        try:
            report = await triage_finding(finding)
        except Exception as exc:
            logger.error(
                "triage: finding %s failed: %s: %r", finding_id, type(exc).__name__, exc,
            )
            summary["errors"] += 1
            continue

        if report is None:
            summary["skipped"] += 1
            continue

        await insert_self_learning_report(
            finding_id=finding_id,
            narrative=report["narrative"],
            proposed_param=report["proposed_param"],
            proposed_value=report["proposed_value"],
            current_value=report["current_value"],
        )
        await mark_finding_triaged(finding_id)
        summary["reports_written"] += 1

    return summary
