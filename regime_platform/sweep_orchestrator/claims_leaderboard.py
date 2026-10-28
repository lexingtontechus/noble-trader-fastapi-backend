"""kairos-claims Phase E: the "arm 11" leaderboard + the claims-evidence gate.

worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4 row E + §6: the claims
ledger (``nt_signal_claims``) is scored by the same pinball/coverage kernels
that grade the lab's model arms, so kairos can stand on the lab leaderboard
as "arm 11" — measured, not argued. Two surfaces live here:

1. :func:`build_leaderboard` — aggregates RESOLVED claim rows into
   per-symbol/per-kind calibration cells (pooled coverage + Wilson bands,
   mean CRPS/pinball/Brier), runs the PAIRED Diebold–Mariano test between
   the two sibling claims every qualified signal carries (``bracket`` vs
   ``vol_fan`` — same signal, same bars, same single settlement resolver),
   and joins the committed lab arm table
   (``config/claims_lab_arm_reference.json``) for symbol-level context.
   Exposed read-only at ``GET /claims/leaderboard`` (routers/claims.py).

2. :func:`fetch_symbol_claims_evidence` — the evidence gate behind the
   optional ``claims_prior_mode`` flag (consumed by
   ``calibration_enforcement``): when a symbol's resolved claims prove the
   house distribution calibrated (pooled coverage80 in the design doc's
   [0.80, 0.95] acceptance band, Wilson-accepted, n ≥ floor), the Bayesian
   shrink prior may trust the raw p_win a little more (×0.75); proven
   miscalibration shrinks a little harder (×1.5). OFF by default — the
   flag check itself lives in the enforcement module, so the default path
   never touches this code.

House style: stdlib-only aggregation (no pandas), httpx REST reads through
the shared ``_headers``/``_url`` helpers, mtime-cached artifact loader
(claims_vol_fan.py convention), fail-open everywhere.

Consistency contract (design doc §1): this module only READS the ledger —
it never writes claims, never touches geometry/EV/gates, and adds no second
resolver (claim outcomes are whatever the one settlement resolver wrote).
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any, Optional

import httpx

from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.claims_leaderboard")

# ─── Gates and bands (all documented in the design doc / build worklog) ─────

# Lab-style verdict floor: below this many resolved claims a cell is
# UNPROVEN, no matter how good the point estimates look.
LEADERBOARD_MIN_N = 8

# The evidence-gate floor for claims_prior_mode — deliberately equal to
# calibration_enforcement.CELL_MIN_N (the R3 hierarchy's own "enough
# episodes" bar). Kept as a literal (not imported) to preserve the
# enforcement module's zero-import-from-claims-layer property.
CLAIMS_EVIDENCE_MIN_N = 10

# Two-sided 95% z for Wilson intervals.
_WILSON_Z = 1.959963984540054

# Design doc §6 acceptance bar: vol-fan claim coverage80 in [0.80, 0.95].
COV80_ACCEPT_LO = 0.80
COV80_ACCEPT_HI = 0.95
# Clear-miscalibration bands outside which the verdict is not merely
# "off nominal" but actionable (drives the ×1.5 shrink penalty).
COV80_BREAK_LO = 0.55
COV80_BREAK_HI = 0.98
# The Wilson lower bound a "calibrated" verdict must clear, so thin
# ledgers cannot buy the trust bonus with a lucky point estimate.
COV80_WILSON_LO_FLOOR = 0.55

# Gentle prior-count modulation bounds (calibration_enforcement applies
# them to BAYES_PRIOR_COUNT). Claims measure the FAN's coverage — an
# adjacent, not identical, object to directional p_win — so the
# modulation is deliberately narrow, and the dangerous direction (the
# trust bonus) is the one that requires Wilson evidence.
PRIOR_MULT_CALIBRATED = 0.75
PRIOR_MULT_MISCALIBRATED = 1.5
PRIOR_MULT_NEUTRAL = 1.0

EVIDENCE_CACHE_TTL_S = 300.0
EVIDENCE_FETCH_LIMIT = 200

_KINDS = ("vol_fan", "bracket", "thesis")


# ─── Pure kernels ────────────────────────────────────────────────────────────


def wilson_interval(prop: float, n: float, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score interval on a proportion (stdlib only).

    Accepts a float numerator via ``prop * n`` so pooled (weighted)
    proportions work unchanged. Returns ``(lo, hi)``; ``(0.0, 1.0)`` when
    ``n <= 0`` (nothing observed — the maximally honest interval).
    """
    if n <= 0:
        return (0.0, 1.0)
    p = min(max(float(prop), 0.0), 1.0)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p)) / n + (z * z) / (4.0 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def coverage_verdict(cov80: float, wilson_lo: float, n_claims: int) -> str:
    """Lab-style verdict for one cell's pooled coverage80.

    ``unproven``      — n < LEADERBOARD_MIN_N (the ledger is young; say so).
    ``calibrated``    — inside the §6 acceptance band AND Wilson-clear.
    ``miscalibrated`` — clearly broken (cov80 < 0.55 or > 0.98): actionable
                        both by the lab's standards and for the prior gate.
    ``off_nominal``   — everything between: not acceptance-grade, not broken.
    """
    if n_claims < LEADERBOARD_MIN_N:
        return "unproven"
    if not (math.isfinite(cov80) and 0.0 < cov80 <= 1.0):
        return "unproven"
    if COV80_ACCEPT_LO <= cov80 <= COV80_ACCEPT_HI and wilson_lo >= COV80_WILSON_LO_FLOOR:
        return "calibrated"
    if cov80 < COV80_BREAK_LO or cov80 > COV80_BREAK_HI:
        return "miscalibrated"
    return "off_nominal"


def prior_mult_for_verdict(verdict: str) -> float:
    """Map a claims verdict to the shrink-prior multiplier (the §1 semantics).

    ``calibrated``    → ×0.75 (proven-honest distribution ⇒ trust the raw
                        p_win a little more, shrink less).
    ``miscalibrated`` → ×1.5 (proven-over/underconfident fan ⇒ distrust,
                        shrink more). Penalizing is the conservative
                        direction, so it needs only the point estimate.
    anything else     → ×1.0 (no modulation).
    """
    if verdict == "calibrated":
        return PRIOR_MULT_CALIBRATED
    if verdict == "miscalibrated":
        return PRIOR_MULT_MISCALIBRATED
    return PRIOR_MULT_NEUTRAL


def _outcome_of(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validated outcome JSONB of one claim row (None when unusable)."""
    out = row.get("outcome")
    if not isinstance(out, dict):
        return None
    return out


def _num(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def aggregate_claim_cells(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group resolved claim rows into per-(symbol, kind) calibration cells.

    Coverage is POOLED across claims by scored bars (Σ cov·n / Σ n) — a
    claim's coverage80 is already a proportion over its ``n_scored_h``
    bars, so pooling is the bar-weighted mean, not a mean of proportions
    (both are reported; the pooled one drives verdicts). The Wilson band
    treats pooled bars as Bernoulli: within-claim correlation makes the
    true interval wider, so the band is an optimistic LOWER bound on
    uncertainty — which is why gates also require the plain n_claims
    floor on top.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        kind = str(row.get("kind") or "").lower()
        if not symbol or kind not in _KINDS:
            continue
        out = _outcome_of(row)
        if out is None:
            continue
        cell = buckets.setdefault(
            (symbol, kind),
            {
                "symbol": symbol,
                "kind": kind,
                "source": None,
                "n_claims": 0,
                "n_scored": 0.0,
                "_k50": 0.0, "_k80": 0.0, "_k90": 0.0,
                "_cov_plain": [],  # per-claim proportions, for the plain mean
                "_crps": [], "_pinball": [], "_brier_resolve": [],
                "_arms": {}, "_calib_levels": {},
            },
        )
        cell["n_claims"] += 1
        src = row.get("source")
        if src and not cell["source"]:
            cell["source"] = str(src)
        arm = row.get("arm")
        if arm:
            cell["_arms"][str(arm)] = cell["_arms"].get(str(arm), 0) + 1
        level = row.get("calib_level")
        if level:
            cell["_calib_levels"][str(level)] = cell["_calib_levels"].get(str(level), 0) + 1
        n_scored = _num(out.get("n_scored_h")) or 0.0
        cell["n_scored"] += n_scored
        for key, acc in (("coverage50", "_k50"), ("coverage80", "_k80"), ("coverage90", "_k90")):
            cov = _num(out.get(key))
            if cov is not None:
                cell[acc] += cov * n_scored
                if key == "coverage80":
                    cell["_cov_plain"].append(cov)
        for key, acc in (("crps", "_crps"), ("pinball", "_pinball"), ("brier_resolve", "_brier_resolve")):
            v = _num(out.get(key))
            if v is not None:
                cell[acc].append(v)

    cells: list[dict[str, Any]] = []
    for (symbol, kind), c in buckets.items():
        n_scored = c["n_scored"]
        pooled80 = (c["_k80"] / n_scored) if n_scored > 0 else (
            mean(c["_cov_plain"]) if c["_cov_plain"] else None
        )
        wilson_lo = wilson_hi = None
        if pooled80 is not None and n_scored > 0:
            wilson_lo, wilson_hi = wilson_interval(pooled80, n_scored)
        verdict = (
            coverage_verdict(pooled80, wilson_lo or 0.0, c["n_claims"])
            if pooled80 is not None else "unproven"
        )
        cells.append(
            {
                "symbol": symbol,
                "kind": kind,
                "source": c["source"],
                "n_claims": c["n_claims"],
                "n_scored_bars": int(n_scored),
                "coverage80_pooled": round(pooled80, 4) if pooled80 is not None else None,
                "coverage80_plain_mean": round(mean(c["_cov_plain"]), 4) if c["_cov_plain"] else None,
                "coverage80_wilson95": (
                    [round(wilson_lo, 4), round(wilson_hi, 4)]
                    if wilson_lo is not None and wilson_hi is not None else None
                ),
                "coverage50_pooled": round(c["_k50"] / n_scored, 4) if n_scored > 0 else None,
                "coverage90_pooled": round(c["_k90"] / n_scored, 4) if n_scored > 0 else None,
                "mean_crps": round(mean(c["_crps"]), 6) if c["_crps"] else None,
                "mean_pinball": round(mean(c["_pinball"]), 6) if c["_pinball"] else None,
                "mean_brier_resolve": round(mean(c["_brier_resolve"]), 6) if c["_brier_resolve"] else None,
                "verdict": verdict,
                "prior_mult": prior_mult_for_verdict(verdict),
                "arms": dict(sorted(c["_arms"].items())) or None,
                "calib_levels": dict(sorted(c["_calib_levels"].items())) or None,
            }
        )
    cells.sort(key=lambda c: (c["symbol"], c["kind"]))
    return cells


def paired_pinball_dm(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """PAIRED Diebold–Mariano between sibling claims on the SAME signal.

    Every qualified signal may carry two claims — the ``bracket`` control
    (ATR normal-approx fan) and the ``vol_fan`` arm (recalibrated GARCH
    family). Both are scored by the ONE settlement resolver over the SAME
    bars, so pinball differences per signal are paired observations: the
    strongest in-ledger statistic and the honest form of the §6 question
    ("does the registered distribution beat the baseline arm?").

    d_i = pinball_vol_fan − pinball_bracket  (pinball is a LOSS: d < 0
    means the vol_fan fan scores better on that signal). DM z = mean(d) /
    (sd(d)/√n), two-sided NormalDist p — the lab's DM shape with the
    simple variance (per-signal differences are near-iid by construction
    of the 15-min sweep spacing; no NW lag term needed at this n).
    """
    by_signal: dict[str, dict[str, float]] = {}
    for row in rows:
        kind = str(row.get("kind") or "").lower()
        if kind not in ("bracket", "vol_fan"):
            continue
        out = _outcome_of(row)
        if out is None:
            continue
        pin = _num(out.get("pinball"))
        crps = _num(out.get("crps"))
        if pin is None:
            continue
        sig = str(row.get("signal_id") or "")
        if not sig:
            continue
        by_signal.setdefault(sig, {})[kind] = pin
        if crps is not None:
            by_signal.setdefault(sig, {}).setdefault("_crps_" + kind, crps)

    diffs: list[float] = []
    crps_v: list[float] = []
    crps_b: list[float] = []
    for fields in by_signal.values():
        if "bracket" in fields and "vol_fan" in fields:
            diffs.append(fields["vol_fan"] - fields["bracket"])
            if "_crps_vol_fan" in fields:
                crps_v.append(fields["_crps_vol_fan"])
            if "_crps_bracket" in fields:
                crps_b.append(fields["_crps_bracket"])

    n = len(diffs)
    base: dict[str, Any] = {
        "n_pairs": n,
        "d_definition": "pinball_vol_fan - pinball_bracket (loss units; d < 0 favors vol_fan)",
        "mean_d": round(mean(diffs), 6) if diffs else None,
        "mean_crps_vol_fan": round(mean(crps_v), 6) if crps_v else None,
        "mean_crps_bracket": round(mean(crps_b), 6) if crps_b else None,
    }
    if n < 2:
        return {**base, "verdict": "insufficient_pairs"}
    sd_d = stdev(diffs)  # n-1 ddof
    if sd_d <= 0:
        return {**base, "sd_d": 0.0, "z": None, "p_two_sided": None, "verdict": "degenerate"}
    se = sd_d / math.sqrt(n)
    z = mean(diffs) / se
    p = 2.0 * (1.0 - NormalDist().cdf(abs(z)))
    if n < LEADERBOARD_MIN_N:
        verdict = "unproven"
    elif p < 0.05 and z < 0:
        verdict = "vol_fan_better"
    elif p < 0.05 and z > 0:
        verdict = "bracket_better"
    else:
        verdict = "equivalent"
    return {
        **base,
        "sd_d": round(sd_d, 6),
        "z": round(z, 4),
        "p_two_sided": round(p, 6),
        "alpha": 0.05,
        "verdict": verdict,
    }


# ─── Lab arm reference (mtime-cached loader, claims_vol_fan convention) ──────

_LAB_REF_CACHE: dict[str, Any] = {"mtime": None, "checked": 0.0, "doc": {}}


def lab_reference_path() -> Path:
    """Repo-root-relative lab arm table (``parents[2]`` = the repo root)."""
    return Path(__file__).resolve().parents[2] / "config" / "claims_lab_arm_reference.json"


def load_lab_reference(max_age_s: float = 30.0) -> dict[str, Any]:
    """Read the committed lab arm table (mtime-cached, fail-open to {})."""
    now = time.monotonic()
    if now - float(_LAB_REF_CACHE["checked"]) < max_age_s:
        return _LAB_REF_CACHE["doc"]
    _LAB_REF_CACHE["checked"] = now
    p = lab_reference_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _LAB_REF_CACHE.update({"mtime": None, "doc": {}})
        return {}
    if mtime == _LAB_REF_CACHE["mtime"] and _LAB_REF_CACHE["doc"]:
        return _LAB_REF_CACHE["doc"]
    try:
        doc = json.loads(p.read_text())
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        _LAB_REF_CACHE.update({"mtime": mtime, "doc": doc})
        return doc
    except Exception as exc:
        logger.debug("claims leaderboard: lab reference unreadable (%s): %s", p, exc)
        _LAB_REF_CACHE.update({"mtime": mtime, "doc": {}})
        return {}


def lab_cells_for_symbol(lab_doc: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Lab cells matching ``symbol`` (case-insensitive) + the lane-level '*' rows."""
    cells = lab_doc.get("cells") if isinstance(lab_doc.get("cells"), list) else []
    sym = symbol.upper()
    return [
        c for c in cells
        if isinstance(c, dict) and (str(c.get("symbol") or "").upper() in (sym, "*"))
    ]


# ─── DB reads (the ensure_claims_index SELECT template) ──────────────────────

_CLAIMS_SELECT = (
    "claim_id,signal_id,symbol,kind,source,arm,calib_level,ts,deadline_ts,status,outcome"
)


async def fetch_resolved_claims(limit: int = 5000) -> list[dict[str, Any]]:
    """SELECT resolved claims (newest first). Raises on REST errors — the
    router maps them to 502/504; the evidence gate wraps its own call."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _url("nt_signal_claims"),
            headers=_headers(),
            params={
                "select": _CLAIMS_SELECT,
                "status": "eq.resolved",
                "order": "ts.desc",
                "limit": str(max(1, int(limit))),
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows if isinstance(rows, list) else []


# ─── The leaderboard assembly ────────────────────────────────────────────────

_LEADERBOARD_NOTES = [
    "Primary statistic: the PAIRED bracket-vs-vol_fan DM test — same signal, same bars, same "
    "single settlement resolver; no resolver fork exists to drift.",
    "Lab cells are joined by SYMBOL as context/provenance (ratio tables, mixed horizons); claims "
    "resolve at horizon 48h only (settlement contract) — never compare numbers across horizons.",
    "Coverage is pooled across claims by scored bars; the Wilson band treats bars as Bernoulli, so "
    "it is an optimistic lower bound on uncertainty (within-claim correlation) — verdict gates "
    "therefore also require the plain n_claims floor.",
    "thesis-kind claims aggregate automatically when a registrant lands (the 218 CHECK already "
    "allows kind='thesis'); today only bracket (always) and vol_fan (Phase D flag) register.",
    "Everything here is read-only over the shadow-first ledger; flipping claims_vol_fan_mode / "
    "claims_publish changes what lands in it, not what this endpoint computes.",
]


def build_leaderboard(rows: list[dict[str, Any]], *, limit: int = 5000) -> dict[str, Any]:
    """Assemble the arm-11 leaderboard document from resolved claim rows."""
    cells = aggregate_claim_cells(rows)
    ts_values = sorted(str(r.get("ts")) for r in rows if r.get("ts"))
    lab_doc = load_lab_reference()
    lab_cells = lab_doc.get("cells") if isinstance(lab_doc.get("cells"), list) else []
    lab_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for c in cells:
        matches = lab_cells_for_symbol(lab_doc, c["symbol"])
        if matches:
            lab_by_symbol[c["symbol"]] = matches
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_resolved_total": len(rows),
        "fetch_limit": limit,
        "window": {"ts_min": ts_values[0] if ts_values else None,
                   "ts_max": ts_values[-1] if ts_values else None},
        "cells": cells,
        "paired_dm": paired_pinball_dm(rows),
        "lab_reference": {
            "path": "config/claims_lab_arm_reference.json",
            "loaded": bool(lab_doc),
            "schema_version": lab_doc.get("schema_version"),
            "measured_date": (lab_doc.get("provenance") or {}).get("measured_date"),
            "n_cells": len(lab_cells),
            "cells_for_symbols_on_the_board": lab_by_symbol,
        },
        "gates": {
            "leaderboard_min_n": LEADERBOARD_MIN_N,
            "evidence_min_n": CLAIMS_EVIDENCE_MIN_N,
            "cov80_accept_band": [COV80_ACCEPT_LO, COV80_ACCEPT_HI],
            "cov80_break_band": [COV80_BREAK_LO, COV80_BREAK_HI],
            "cov80_wilson_lo_floor": COV80_WILSON_LO_FLOOR,
            "prior_mult": {"calibrated": PRIOR_MULT_CALIBRATED,
                           "miscalibrated": PRIOR_MULT_MISCALIBRATED},
        },
        "notes": _LEADERBOARD_NOTES,
    }


# ─── The claims-evidence gate (claims_prior_mode's data source) ──────────────

_EVIDENCE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def fetch_symbol_claims_evidence(
    symbol: str, *, ttl_s: float = EVIDENCE_CACHE_TTL_S
) -> Optional[dict[str, Any]]:
    """Coverage evidence for ONE symbol, gated to the Phase E semantics.

    Prefers ``vol_fan`` rows once they clear CLAIMS_EVIDENCE_MIN_N (the §6
    acceptance bar is written against the recalibrated arm); falls back to
    the ``bracket`` control during the all-bracket era. Any REST/parse
    failure ⇒ ``None`` (the caller fails open to the standard shrink).
    Cached per symbol for ``ttl_s`` — apply_calibration_adjustment is
    called per signal, and the evidence moves at resolver cadence.
    """
    sym = str(symbol or "").upper()
    if not sym:
        return None
    now = time.monotonic()
    cached = _EVIDENCE_CACHE.get(sym)
    if cached and now - cached[0] < ttl_s:
        return {**cached[1], "cached": True}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_signal_claims"),
                headers=_headers(),
                params={
                    "select": "symbol,kind,source,arm,ts,status,outcome",
                    "symbol": f"eq.{sym}",
                    "status": "eq.resolved",
                    "order": "ts.desc",
                    "limit": str(EVIDENCE_FETCH_LIMIT),
                },
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        logger.debug("claims evidence %s: fetch failed (fail-open): %s", sym, exc)
        return None
    if not isinstance(rows, list):
        return None

    def _usable(kind: str) -> list[dict[str, Any]]:
        return [r for r in rows if str(r.get("kind") or "").lower() == kind and _outcome_of(r)]

    vol_rows, bracket_rows = _usable("vol_fan"), _usable("bracket")
    if len(vol_rows) >= CLAIMS_EVIDENCE_MIN_N:
        chosen, kind = vol_rows, "vol_fan"
    elif len(bracket_rows) >= CLAIMS_EVIDENCE_MIN_N:
        chosen, kind = bracket_rows, "bracket"
    elif vol_rows or bracket_rows:
        chosen = vol_rows if len(vol_rows) >= len(bracket_rows) else bracket_rows
        kind = str(chosen[0].get("kind") or "").lower() if chosen else None
    else:
        evidence = {
            "symbol": sym, "kind": None, "n_claims": 0, "n_scored": 0,
            "coverage80_pooled": None, "wilson95": None,
            "verdict": "unproven", "prior_mult": PRIOR_MULT_NEUTRAL,
            "reason": "no_resolved_claims",
        }
        _EVIDENCE_CACHE[sym] = (now, evidence)
        return evidence

    cells = aggregate_claim_cells(chosen)
    if not cells:
        return None
    cell = cells[0]
    evidence = {
        "symbol": sym,
        "kind": kind,
        "source": cell.get("source"),
        "n_claims": cell["n_claims"],
        "n_scored": cell["n_scored_bars"],
        "coverage80_pooled": cell["coverage80_pooled"],
        "wilson95": cell["coverage80_wilson95"],
        "verdict": cell["verdict"],
        "prior_mult": cell["prior_mult"],
        "reason": (
            f"kind={kind}; pooled coverage80 vs [0.80, 0.95] acceptance band; "
            f"n_claims={cell['n_claims']} vs floor {CLAIMS_EVIDENCE_MIN_N}"
        ),
    }
    _EVIDENCE_CACHE[sym] = (now, evidence)
    return evidence
