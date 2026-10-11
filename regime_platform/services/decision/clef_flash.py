"""
Clef-Flash caller — structured-decision model on Cloudflare Workers AI.

Scope: docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md and
docs/scopes/20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md.

Clef-Flash returns **one logit per option per question, softmax-normalised**
— no free text, no ``extract_json()`` fragility. This module is the shadow
research caller: it asks the model to classify a signal's OWN context
(symbol/direction/regime/bracket) into ``buy`` / ``sell`` / ``wait`` and
returns the direction-relative probability for the shadow stamp.

RESEARCH ONLY. Nothing here is an EV source and nothing here gates a signal:
  * the returned probabilities are stamped on ``nt_signal_sim`` for offline
    discrimination measurement against ``p_win_dir``;
  * this module never imports ``ev_engine``, never touches ``qualified`` /
    ``kelly`` / SL / TP, and is never read back into a decision.

Fail-open by construction: every function returns ``None`` on any failure
(missing config, non-2xx, timeout, malformed body) so the caller's stamp
simply stays NULL. This mirrors ``knowledge_index._embed_texts_cf``.

Reaches the deployed Worker (``cloudflare/worker.js``) at ``{base}/decision``. Base URL is ``CF_DECISION_URL`` if set,
else ``CF_EMBEDDINGS_URL`` (the same Worker hosts both routes). Auth is the
optional ``x-worker-secret`` header (``CF_WORKER_SECRET``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("regime.services.decision.clef_flash")

_REQUEST_TIMEOUT = 10.0
_MODEL = "@cf/cloudflare/clef-flash"

_DIRECTION_OPTIONS = ("buy", "sell", "wait")

_DIRECTION_INSTRUCTIONS = (
    "Given this signal's own context (symbol, stated direction, regime, "
    "bracket geometry), does the setup favour the stated direction, the "
    "opposite direction, or neither? Answer 'wait' when the context does "
    "not support either side."
)
_DIRECTION_CRITERIA = {
    "buy": "The setup favours a long / upward resolution.",
    "sell": "The setup favours a short / downward resolution.",
    "wait": "The setup favours neither side (no directional edge).",
}

_warned_no_config = False


# ─── Config ────────────────────────────────────────────────────────────────


def _worker_base_url() -> str:
    """Worker base URL — ``CF_DECISION_URL`` overrides, else
    ``CF_EMBEDDINGS_URL`` (same Worker hosts /embeddings and /decision)."""
    raw = (
        os.getenv("CF_DECISION_URL", "").strip()
        or os.getenv("CF_EMBEDDINGS_URL", "").strip()
    )
    return raw.rstrip("/")


def _worker_secret() -> str:
    return os.getenv("CF_WORKER_SECRET", "").strip()


def _enabled() -> bool:
    return bool(_worker_base_url())


# ─── Pure helpers (no I/O — unit-testable) ─────────────────────────────────


def build_direction_state(
    *,
    symbol: str,
    signal: str,
    regime: str | None = None,
    regime_conf: float | None = None,
    entry_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    tp_distance_atr: float | None = None,
    sl_distance_atr: float | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    p_win_dir: float | None = None,
) -> dict[str, Any]:
    """The ``state`` block sent to Clef-Flash.

    Carries ONLY the signal's already-computed context — never raw candles
    or a renko series. The model is not asked to re-derive anything the
    deterministic engine already produced (scope §1 rule). ``None`` fields
    are dropped so the payload stays small and honest.
    """
    raw = {
        "symbol": symbol,
        "signal": signal,
        "regime": regime,
        "regime_conf": regime_conf,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "tp_distance_atr": tp_distance_atr,
        "sl_distance_atr": sl_distance_atr,
        "macro_label": macro_label,
        "trend_label": trend_label,
        "p_win_dir": p_win_dir,
    }
    return {k: v for k, v in raw.items() if v is not None}


def build_direction_questions() -> dict[str, Any]:
    """The ``questions`` block — a three-way ``choice`` on direction.

    Three-way (not binary agree/disagree) so the model can express "the
    opposite is favoured", the informative case; ``wait`` is a real answer,
    not a parse failure.
    """
    return {
        "direction": {
            "type": "choice",
            "instructions": _DIRECTION_INSTRUCTIONS,
            "criteria": dict(_DIRECTION_CRITERIA),
        },
    }


@dataclass(frozen=True)
class ClefDecision:
    """Direction-relative read for the shadow stamp."""

    p_clef_dir: float   # P(model agrees with the signal's own direction)
    p_clef_wait: float  # P(model says "wait")
    model: str

    def as_log(self) -> str:
        return (
            f"p_clef_dir={self.p_clef_dir:.4f} wait={self.p_clef_wait:.4f} "
            f"model={self.model}"
        )


def parse_direction_answer(
    answers: dict[str, Any] | None,
    *,
    signal: str,
    model: str = _MODEL,
) -> Optional[ClefDecision]:
    """Extract ``(p_clef_dir, p_clef_wait)`` from a ``/decision`` response.

    ``p_clef_dir`` is the probability mass on the signal's OWN side
    (buy→``buy``, sell→``sell``) so buy and sell rows are comparable.
    Returns ``None`` if the shape is missing or unusable.
    """
    if not isinstance(answers, dict):
        return None
    direction = answers.get("direction")
    if not isinstance(direction, dict):
        return None
    probs = direction.get("probabilities")
    if not isinstance(probs, dict) or not probs:
        return None

    def _f(key: str) -> float | None:
        v = probs.get(key)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f else None  # reject NaN

    side = "buy" if str(signal).lower() != "sell" else "sell"
    p_dir = _f(side)
    if p_dir is None:
        return None
    p_wait = _f("wait")
    if p_wait is None:
        p_wait = 0.0
    return ClefDecision(
        p_clef_dir=max(0.0, min(1.0, p_dir)),
        p_clef_wait=max(0.0, min(1.0, p_wait)),
        model=model or _MODEL,
    )


# ─── I/O ───────────────────────────────────────────────────────────────────


async def decide(
    state: dict[str, Any],
    questions: dict[str, Any],
    *,
    timeout: float = _REQUEST_TIMEOUT,
) -> Optional[dict[str, Any]]:
    """POST one ``{state, questions}`` record to the Worker ``/decision``.

    Returns the parsed ``answers`` dict, or ``None`` on any failure
    (no config, non-2xx, timeout, malformed body). Never raises.
    """
    global _warned_no_config
    base = _worker_base_url()
    if not base:
        if not _warned_no_config:
            logger.warning(
                "clef_flash: neither CF_DECISION_URL nor CF_EMBEDDINGS_URL is "
                "set — shadow stamp will be NULL (no HTTP call)."
            )
            _warned_no_config = True
        return None

    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = _worker_secret()
    if secret:
        headers["x-worker-secret"] = secret

    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/decision",
                headers=headers,
                json={"state": state, "questions": questions},
            )
        if not resp.is_success:
            logger.warning(
                "clef_flash: /decision HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        answers = data.get("answers")
        return answers if isinstance(answers, dict) else None
    except Exception as exc:
        logger.warning(
            "clef_flash: /decision failed (%s: %s)",
            type(exc).__name__, exc,
        )
        return None


async def decide_direction(
    *,
    symbol: str,
    signal: str,
    regime: str | None = None,
    regime_conf: float | None = None,
    entry_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    tp_distance_atr: float | None = None,
    sl_distance_atr: float | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    p_win_dir: float | None = None,
) -> Optional[ClefDecision]:
    """One-shot: build the direction question, call Clef-Flash, parse.

    Returns ``None`` on any failure. Convenience wrapper for the shadow
    stamp call site.
    """
    state = build_direction_state(
        symbol=symbol, signal=signal, regime=regime, regime_conf=regime_conf,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        tp_distance_atr=tp_distance_atr, sl_distance_atr=sl_distance_atr,
        macro_label=macro_label, trend_label=trend_label, p_win_dir=p_win_dir,
    )
    answers = await decide(state, build_direction_questions())
    if answers is None:
        return None
    return parse_direction_answer(answers, signal=signal)


async def classify_choice(
    *,
    instructions: str,
    options: dict[str, str],
    state: dict[str, Any],
    question_id: str = "answer",
) -> Optional[dict[str, float]]:
    """Generic structured-label classification over a fixed label set.

    ``options`` maps each allowed label to its one-line description (the
    Clef-Flash ``choice`` criteria). Returns ``{label: probability}`` — a
    proper distribution summing to ~1 — or ``None`` on any failure.

    This is the primitive that makes Clef-Flash a *categorizer*: unlike the
    free-text chat path (``llm_client``), there is no string to parse and the
    returned distribution is a usable confidence (``max(probs)``) that a
    caller can gate on. Used to replace the fragile chat classification at
    ``kairos_pivot.py::label_event`` and
    ``macro_forecast_agent.py::classify_symbol`` (scope §2.1 of
    docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md).

    Never raises. Missing/garbled labels are passed through as returned —
    the caller's own validator is the authority on the allowed vocab.
    """
    if not options:
        return None
    questions = {
        question_id: {
            "type": "choice",
            "instructions": instructions,
            "criteria": dict(options),
        },
    }
    answers = await decide(state, questions)
    if answers is None:
        return None
    answer = answers.get(question_id)
    if not isinstance(answer, dict):
        return None
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or not probs:
        return None
    out: dict[str, float] = {}
    for k, v in probs.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:  # reject NaN
            out[str(k)] = f
    return out or None


# ─── R4: archetype / routing classifier (Role A) ───────────────────────────
#
# 2026-10-10, docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §5
# Role A. Clef-Flash classifies the SETUP TYPE (not the direction) from the
# indicator set the deterministic router (R1) already computes, so R4 can be
# measured against R1's own choice: does the model's archetype read agree with
# (or beat) the deterministic router, on era-clean outcomes? This is the
# "when to apply which strategy" decision, weighing INTERACTION indicators a
# hand-coded predicate misses (regime×deviation, macro×trend, vol×direction).
#
# RESEARCH ONLY — same contract as the direction stamp: never an EV source,
# never a gate. Shadow-stamped for offline discrimination measurement (AUC /
# agreement vs the R1 router + realized outcome). Fail-open: None on any
# failure, so the caller's stamp simply stays NULL.

# The archetype vocab MUST match config/strategy_registry.yaml's archetype
# names (+ stand_by). Kept as module constants so the classifier and the
# router speak one vocabulary; a mismatch is a bug caught by the test that
# pins these against the registry.
ARCHETYPE_OPTIONS = {
    "momentum": "Trend-continue: the setup favours riding the aligned "
                "macro/trend direction (far target, no reversion stop).",
    "mean_reversion": "Contrarian: an exhausted counter-trend extreme that "
                      "should fade back to the mean (tight target, "
                      "overshoot stop).",
    "ma_trend": "Moving-average cross trend-follow: a lower-frequency trend "
                "confirmed by moving-average alignment.",
    "fx_carry": "FX carry / momentum+carry: a currency setup favoured by the "
                "interest-rate differential (carry), optionally conditioned on "
                "momentum.",
    "range_fade": "Chop-boundary: a confirmed range with price at a boundary "
                  "to be faded toward the middle.",
    "breakout": "Volatility-expansion breakout with confirmation in the "
                "direction of the expansion.",
    "stand_by": "No directional edge: the setup favours neither side, so do "
                "not trade.",
}

_ARCHETYPE_INSTRUCTIONS = (
    "Given this signal's indicator context — momentum, volatility, trend "
    "reads, deviation from the mean, and how they INTERACT (does the regime "
    "agree with the trend? is the deviation extreme relative to volatility? "
    "is the direction with or against the slow reads?) — which trading "
    "strategy archetype best fits this setup right now? Choose the setup "
    "type, NOT the trade direction. 'stand_by' when no archetype has an edge."
)


def build_archetype_state(
    *,
    symbol: str,
    signal: str,
    regime: str | None = None,
    regime_conf: float | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    momentum_24h: float | None = None,
    vol_ratio: float | None = None,
    deviation_z: float | None = None,
    p_win_dir: float | None = None,
) -> dict[str, Any]:
    """The ``state`` block for the archetype classifier.

    Carries the indicator set mapped in the scope doc §5: momentum,
    volatility (vol_ratio), trend (macro/trend labels), deviation (deviation_z
    proxy). The model weighs their INTERACTION — it is NOT asked to re-derive
    anything the deterministic engine already produced. ``None`` fields are
    dropped so the payload stays small and honest.
    """
    raw = {
        "symbol": symbol,
        "signal": signal,
        "regime": regime,
        "regime_conf": regime_conf,
        "macro_label": macro_label,
        "trend_label": trend_label,
        "momentum_24h": momentum_24h,
        "vol_ratio": vol_ratio,
        "deviation_z": deviation_z,
        "p_win_dir": p_win_dir,
    }
    return {k: v for k, v in raw.items() if v is not None}


@dataclass(frozen=True)
class ClefArchetype:
    """Role-A read: the model's archetype distribution for one signal."""

    probabilities: dict[str, float]   # {archetype: prob}, sums ~1
    top: str                          # argmax label
    confidence: float                 # max prob
    model: str

    def agrees_with(self, router_archetype: str | None) -> bool | None:
        """Does the model's top choice agree with the R1 router's verdict?

        None when the router produced nothing (router off/errored) — agreement
        is undefined, not False.
        """
        if not router_archetype:
            return None
        return self.top == router_archetype

    def prob_of(self, archetype: str) -> float:
        return self.probabilities.get(archetype, 0.0)


async def classify_archetype(
    *,
    symbol: str,
    signal: str,
    regime: str | None = None,
    regime_conf: float | None = None,
    macro_label: str | None = None,
    trend_label: str | None = None,
    momentum_24h: float | None = None,
    vol_ratio: float | None = None,
    deviation_z: float | None = None,
    p_win_dir: float | None = None,
    model: str = _MODEL,
) -> Optional[ClefArchetype]:
    """One-shot: build the archetype question, call Clef-Flash, parse.

    Returns ``None`` on any failure (fail-open). The caller stamps the
    distribution + top/confidence on nt_signal_sim for R4's discrimination
    measurement against the R1 router. Never an EV source, never a gate.
    """
    state = build_archetype_state(
        symbol=symbol, signal=signal, regime=regime, regime_conf=regime_conf,
        macro_label=macro_label, trend_label=trend_label,
        momentum_24h=momentum_24h, vol_ratio=vol_ratio,
        deviation_z=deviation_z, p_win_dir=p_win_dir,
    )
    probs = await classify_choice(
        instructions=_ARCHETYPE_INSTRUCTIONS,
        options=ARCHETYPE_OPTIONS,
        state=state,
        question_id="archetype",
    )
    if not probs:
        return None
    top = max(probs, key=probs.get)
    return ClefArchetype(
        probabilities=probs,
        top=top,
        confidence=probs[top],
        model=model or _MODEL,
    )
