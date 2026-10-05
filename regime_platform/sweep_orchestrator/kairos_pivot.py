"""
kairos_pivot.py — P2 typed pivot labels (T-Layer). Pure helpers only.

Scope: worklog/kairos/20260927_P0_P4_PROGRAMME.md §4.
Labels: major_pivot | incremental | no_change
  (reaction-function / tool / trajectory change → major_pivot)

Product mapping is a research overlay — never a live gate.
"""

from __future__ import annotations

import re
from typing import Any

PIVOT_LABELS = frozenset({"major_pivot", "incremental", "no_change"})

# Prior: over-detection suppressor (literature: 0–1 major pivots / year for policy).
# Data releases map to a much higher allowed rate — separate unit.
POLICY_PIVOTS_PER_YEAR = 1.0
DATA_MAJOR_MAX_RATE = 0.05  # ≤5% of data prints may be major_pivot

_PRODUCT_MAP = {
    "major_pivot": "dir_flip_candidate",
    "incremental": "none",
    "no_change": "none",
}


def validate_pivot(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate LLM JSON → {label, axis, intensity, surprise, summary} or None."""
    if not isinstance(parsed, dict):
        return None
    label = parsed.get("label")
    if label not in PIVOT_LABELS:
        return None
    axis = parsed.get("axis") or "none"
    if axis not in ("reaction_function", "tool", "trajectory", "none"):
        axis = "none"
    try:
        intensity = max(0.0, min(1.0, float(parsed.get("intensity", 0.0))))
        surprise = max(0.0, min(1.0, float(parsed.get("surprise", 0.0))))
    except (TypeError, ValueError):
        intensity, surprise = 0.0, 0.0
    summary = (parsed.get("summary") or "").strip()
    return {
        "label": label,
        "axis": axis,
        "intensity": intensity,
        "surprise": surprise,
        "summary": summary[:240],
        "product_type": _PRODUCT_MAP.get(label, "none"),
    }


def map_to_product_type(label: str | None) -> str:
    return _PRODUCT_MAP.get(label or "", "none")


def pivot_prior_ok(major_count: int, n_events: int, *, kind: str = "data") -> bool:
    """True if the major_pivot share respects the over-detection prior."""
    if n_events <= 0:
        return True
    rate = major_count / n_events
    if kind == "policy":
        # 0–1 majors per year ≈ very sparse; as a *share* of policy events
        # in a multi-year sample, still require ≤ 0.25 majors per event.
        return rate <= 0.25
    return rate <= DATA_MAJOR_MAX_RATE


def is_policy_event(title: str | None, country: str | None = None) -> bool:
    t = (title or "").lower()
    keys = (
        "fomc", "fed ", "federal reserve", "interest rate", "rate decision",
        "ecb", "boj", "boe", "rba", "boc", "snb", "central bank",
    )
    return any(k in t for k in keys)


def event_brief(ev: dict[str, Any]) -> str:
    """One-line compact event for the LLM prompt."""
    title = ev.get("title") or ev.get("indicator") or "event"
    country = ev.get("country") or "?"
    date = (ev.get("date") or "")[:10]
    bits = [f"{date}", f"{country}", str(title)]
    for k in ("actual", "forecast", "previous"):
        v = ev.get(k)
        if v is not None:
            bits.append(f"{k}={v}")
    ctx = (ev.get("context") or "").strip()
    if ctx:
        bits.append(f"note={ctx}")
    return " | ".join(bits)


def build_pivot_prompt(
    events: list[dict[str, Any]],
    *,
    kind: str = "data",
    structure_note: str | None = None,
) -> str:
    lines = [
        f"Event class: {kind}",
        "For EACH event line, classify whether it is a major_pivot "
        "(reaction function / dominant policy tool / trajectory change), "
        f"incremental, or no_change. Major pivots must be rare (~0-1 per year for policy).",
    ]
    if structure_note:
        lines.append(f"Short-term market structure (context only): {structure_note}")
        lines.append(
            "If structure is calm and the print is routine, prefer no_change/incremental. "
            "Do not call major_pivot from price TA alone."
        )
    lines += [
        'Respond with ONLY one JSON object: {"events":[...]} with one object per input line, in order:',
        'Each: {"label":"major_pivot|incremental|no_change",'
        '"axis":"reaction_function|tool|trajectory|none",'
        '"intensity":0.0,"surprise":0.0,"summary":"<=20 words"}',
        "Events:",
    ]
    for i, ev in enumerate(events):
        lines.append(f"{i}. {event_brief(ev)}")
    return "\n".join(lines)


_PIVOT_SYSTEM = (
    "You are a monetary-policy / macro event classifier for a research "
    "trading platform (not a live system). Emit strict JSON only. "
    "Do not invent events. Prefer no_change or incremental unless the "
    "print clearly changes a reaction function, policy tool, or trajectory."
)


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Pull the first JSON array from an LLM reply."""
    if not text:
        return None
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        return None
    import json

    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return arr if isinstance(arr, list) else None


def attach_pivot_fields(row: dict[str, Any], pivot: dict[str, Any] | None) -> dict[str, Any]:
    """Merge pivot_* onto a nt_macro_forecast row (same table, no second store)."""
    if not pivot:
        return row
    out = dict(row)
    out["pivot_label"] = pivot.get("label")
    out["pivot_axis"] = pivot.get("axis") or "none"
    out["pivot_intensity"] = pivot.get("intensity")
    out["pivot_surprise"] = pivot.get("surprise")
    out["pivot_as_of"] = pivot.get("as_of")
    out["pivot_event_title"] = pivot.get("event_title")
    out["pivot_summary"] = pivot.get("summary")
    return out


def select_pivot_candidates(
    events: list[dict[str, Any]],
    *,
    max_age_days: float = 30.0,
    now_ts: float | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Policy + high-surprise importance-1 events, newest first."""
    import time
    from datetime import datetime, timezone

    now = now_ts if now_ts is not None else time.time()
    cut = now - max_age_days * 86400.0
    scored: list[tuple[float, dict[str, Any]]] = []
    for ev in events or []:
        if (ev.get("importance") or 0) < 1:
            continue
        raw = ev.get("date")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < cut:
            continue
        title = ev.get("title") or ""
        rank = 2.0 if is_policy_event(title, ev.get("country")) else 1.0
        act, fc = ev.get("actual"), ev.get("forecast")
        if act is not None and fc is not None:
            try:
                a, f = float(act), float(fc)
                if f != 0 and abs(a - f) / abs(f) >= 0.05:
                    rank += 0.5
            except (TypeError, ValueError):
                pass
        scored.append((ts * rank, ev))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in scored[:limit]]


async def label_event(
    event: dict[str, Any],
    *,
    structure_note: str | None = None,
) -> dict[str, Any] | None:
    """LLM-label one calendar event → pivot dict (with as_of / event_title).

    When ``clef_flash_mode == "live"`` the structured-logit path is tried
    first; any failure (None, missing config, unknown label) falls through to
    the free-text chat path unchanged. The shadow stamp (``clef_flash_mode ==
    "shadow"``) does NOT affect this — the classifier cutover is the ``live``
    value only. See docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md.
    """
    from regime_platform.core.feature_flags import get_clef_flash_mode

    if get_clef_flash_mode() == "live":
        try:
            v = await _label_event_clef(event, structure_note=structure_note)
            if v is not None:
                return v
        except Exception as exc:  # noqa: BLE001 — fail open to the chat path
            logger.debug("kairos_pivot: clef label_event failed: %s", exc)

    from regime_platform.services.self_learning.llm_client import call_llm_with_provider

    prompt = build_pivot_prompt(
        [event],
        kind="policy" if is_policy_event(event.get("title")) else "data",
        structure_note=structure_note,
    )
    result = await call_llm_with_provider(_PIVOT_SYSTEM, prompt, max_tokens=300)
    if result is None:
        return None
    parsed, _provider = result
    if not isinstance(parsed, dict):
        return None
    v = validate_pivot(parsed)
    if v is None and isinstance(parsed.get("events"), list) and parsed["events"]:
        v = validate_pivot(parsed["events"][0])
    if v is None:
        return None
    v["as_of"] = event.get("date")
    v["event_title"] = event.get("title")
    return v


_PIVOT_CHOICE_CRITERIA = {
    "major_pivot": "The print clearly changes a reaction function, the dominant policy tool, or the trajectory.",
    "incremental": "A meaningful move in an existing direction, but not a regime change.",
    "no_change": "Routine print — consistent with the prevailing reaction function.",
}


async def _label_event_clef(
    event: dict[str, Any],
    *,
    structure_note: str | None = None,
) -> dict[str, Any] | None:
    """Structured-logit pivot label via Clef-Flash → same ``validate_pivot`` shape.

    The label is the arg-max of the returned distribution; ``intensity`` is its
    probability (a native confidence the free-text path cannot supply). Returns
    ``None`` on any failure so the caller falls back to the chat path.
    """
    from regime_platform.services.decision.clef_flash import classify_choice

    kind = "policy" if is_policy_event(event.get("title")) else "data"
    state = {
        "event_class": kind,
        "event": event_brief(event),
    }
    if structure_note:
        state["structure_note"] = structure_note
    instructions = (
        "Classify whether this macro event is a major_pivot (reaction function / "
        "dominant policy tool / trajectory change), incremental, or no_change. "
        "Major pivots are rare (~0-1 per year for policy). Prefer no_change or "
        "incremental unless the print clearly changes a reaction function. "
        "Do not call major_pivot from price TA alone."
    )
    probs = await classify_choice(
        instructions=instructions, options=_PIVOT_CHOICE_CRITERIA, state=state,
    )
    if not probs:
        return None
    label = max(probs, key=probs.get)
    v = validate_pivot({
        "label": label,
        "axis": "none",
        "intensity": float(probs.get(label, 0.0)),
        "surprise": 0.0,
        "summary": "",
    })
    if v is None:
        return None
    v["as_of"] = event.get("date")
    v["event_title"] = event.get("title")
    return v


async def latest_pivot_for_rows(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Label the top candidate events once; return the strongest pivot (or None)."""
    cands = select_pivot_candidates(events)
    best: dict[str, Any] | None = None
    for ev in cands[:4]:
        v = await label_event(ev)
        if v is None:
            continue
        if best is None:
            best = v
        elif (v.get("label") == "major_pivot") and best.get("label") != "major_pivot":
            best = v
        elif v.get("label") == best.get("label") and (v.get("intensity") or 0) > (best.get("intensity") or 0):
            best = v
        if best.get("label") == "major_pivot":
            break
    return best
