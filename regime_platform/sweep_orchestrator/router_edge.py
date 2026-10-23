"""Router Edge Model (R2) — the strategy router as a MEASURED next-candle edge
predictor.

══════════════════════════════════════════════════════════════════════════
Scope: docs/scopes/20261023_TSFM_VS_REASONING_LLM_AND_ROUTER_EDGE.md,
roadmap-ref worklog/kairos/20261023_ROUTER_EDGE_MODEL_BUILD.md. R2 of the
router+registry split (20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §4):
R1 (strategy_router.route) stamps the archetype; THIS module turns the router's
verdict into a measured

    p_router = P(next candle up | symbol, archetype, direction)

— a decay-weighted Beta posterior per cell, anchored on the symbol's own
next-candle base rate. The same stamp/resolve loop scores TimesFM's p_timesfm
on the IDENTICAL outcome stream, so "TSFM vs router as the next-candle edge
source" becomes a measured competition (skill vs the majority baseline, the
regime_edge W-X2 convention), not an argument.

WHY THIS SHAPE (the TSFM rethink, compressed — full matrix in the scope doc):
  * p_timesfm (TimesFM-2.5 zero-shot point forecast → z-map) is the ONLY live
    TSFM consumption and its 50% blend weight has never been skill-measured.
    Univariate, no covariates, network-dependent, 15-min cached.
  * A reasoning LLM zero-shot on the number is the documented mirage
    (STRIDE/TFRBench: frontier LLMs ~2x worse MASE than a 120M TSFM, which
    itself loses to HAR on RV). Never an LLM probability into p_win/EV —
    standing doctrine, externally re-confirmed 2026-10-23.
  * The router state (archetype x direction, from regime/macro/trend/class
    predicates) is a categorical conditioning the repo already computes every
    sweep tick. Conditioning is exactly where priors pay (P(Y|X,R)). The
    empirical conditional frequency of next-candle direction given that state
    is the honest, zero-network, self-calibrating edge estimate — the PBM
    pattern (stamp → backfill → score → refit) applied to the next-candle
    target with the PBM shrinkage convention (k0 pseudo-counts anchored on
    the base rate, exponential decay 2^(-age/tau), raw n tracked for
    diagnostics).

MECHANICS (per light-sweep tick, per symbol — see router_edge_tick):
  1. RESOLVE the previous stamp: if a bar newer than the stamped bar exists
     in the freshly fetched prices, the stamped prediction's outcome is
     close-vs-close (up/dn; ties skipped — no information). Update the
     cell (decay-then-add), the symbol base cell, and BOTH skill
     accumulators (router + timesfm) on the same outcome.
  2. STAMP the current tick: route() on the CURRENT signal/regime/macro/
     trend (every tick — NOT only qualified signals; conditioning on the
     qualified sub-population would select on the very gates we want to
     measure past). Read the cell, emit
     p_router = (k0*p_base + s_up) / (k0 + s_up + s_dn)
     and persist the stamp (bar ts + close + verdict + p_router + p_timesfm)
     for the next tick's resolution.

STORAGE: param_store tiers (local-SQLite authority, Supabase mirror via the
existing sync machinery) — the JSON-artifact-first shape the vol-recalib
assessment §1.7.2 prescribed; ZERO schema change, restart-safe:
  tier_type "router_edge", symbol SYMBOL:
    "<archetype>:<direction>" → EdgeCell (s_up/s_dn decayed, n_up/n_dn/n_tie raw)
    "_base"                   → EdgeCell for the symbol's pooled base rate
    "_router"/"_timesfm"      → SkillCell (s_hit/s_miss decayed, n_hit/n_miss)
    "_last"                   → the pending stamp (bar identity + predictions)

FLAGS (feature_flags.yaml): router_edge_mode off|shadow|live (default shadow —
measurement only, blend untouched, byte-identical EV); router_edge_weight /
_k0 /_tau_days /_min_n_eff knobs; timesfm_blend_mode off|shadow|live for the
TSFM exit ladder (live = today's behaviour byte-identical; shadow = fetched +
scored but blend-excluded — the A/B measurement; off = not fetched at all).

Fail-open: NOTHING in this module may raise into a sweep tick — every entry
point wraps and degrades (skip the tick / return None).
══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from regime_platform.core.param_store import read_dict, write_params

logger = logging.getLogger("regime.sweep.router_edge")

TIER = "router_edge"

# Reserved tier_keys (cells use "<archetype>:<direction>"; these never collide
# because archetype names come from the registry catalog which has no ":" in
# its keys, and direction is "buy"/"sell"/"none").
KEY_BASE = "_base"
KEY_ROUTER_SKILL = "_router"
KEY_TIMESFM_SKILL = "_timesfm"
KEY_LAST = "_last"


# ─── Dataclasses ────────────────────────────────────────────────────────────


@dataclass
class EdgeCell:
    """One cell's decay-weighted Beta state on P(next candle up).

    ``s_up``/``s_dn`` are DECAYED effective counts (2^(-age/tau) at each
    update, PBM convention); ``n_*`` are raw diagnostic counters (never
    decayed). Ties are skipped outright — a flat candle carries no
    directional information and must not dilute either side.
    """

    s_up: float = 0.0
    s_dn: float = 0.0
    n_up: int = 0
    n_dn: int = 0
    n_tie: int = 0
    updated_at: str | None = None

    @property
    def n_eff(self) -> float:
        return self.s_up + self.s_dn

    def decayed(self, now_iso: str, tau_days: float) -> "EdgeCell":
        """Return a copy with counts decayed to ``now_iso`` (2^(-age/tau))."""
        age_days = _age_days(self.updated_at, now_iso)
        f = 1.0 if age_days <= 0 else 2.0 ** (-age_days / max(1e-9, tau_days))
        return EdgeCell(
            s_up=self.s_up * f, s_dn=self.s_dn * f,
            n_up=self.n_up, n_dn=self.n_dn, n_tie=self.n_tie,
            updated_at=self.updated_at,
        )

    def add(self, up: bool, now_iso: str) -> "EdgeCell":
        return EdgeCell(
            s_up=self.s_up + (1.0 if up else 0.0),
            s_dn=self.s_dn + (0.0 if up else 1.0),
            n_up=self.n_up + (1 if up else 0),
            n_dn=self.n_dn + (0 if up else 1),
            n_tie=self.n_tie,
            updated_at=now_iso,
        )

    def as_value(self) -> dict[str, Any]:
        return {
            "s_up": self.s_up, "s_dn": self.s_dn,
            "n_up": self.n_up, "n_dn": self.n_dn, "n_tie": self.n_tie,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_value(cls, v: dict[str, Any] | None) -> "EdgeCell":
        if not isinstance(v, dict):
            return cls()
        try:
            return cls(
                s_up=float(v.get("s_up") or 0.0),
                s_dn=float(v.get("s_dn") or 0.0),
                n_up=int(v.get("n_up") or 0),
                n_dn=int(v.get("n_dn") or 0),
                n_tie=int(v.get("n_tie") or 0),
                updated_at=v.get("updated_at"),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass
class SkillCell:
    """Directional-call accuracy accumulator (skill = acc − majority).

    Scored ONLY on definite calls (p > 0.5 predicts up, p < 0.5 predicts
    down; p == 0.5 / None skipped — a coin has no call to score).
    """

    s_hit: float = 0.0
    s_miss: float = 0.0
    n_hit: int = 0
    n_miss: int = 0
    updated_at: str | None = None

    @property
    def n_eff(self) -> float:
        return self.s_hit + self.s_miss

    def decayed(self, now_iso: str, tau_days: float) -> "SkillCell":
        age_days = _age_days(self.updated_at, now_iso)
        f = 1.0 if age_days <= 0 else 2.0 ** (-age_days / max(1e-9, tau_days))
        return SkillCell(
            s_hit=self.s_hit * f, s_miss=self.s_miss * f,
            n_hit=self.n_hit, n_miss=self.n_miss,
            updated_at=self.updated_at,
        )

    def add(self, hit: bool, now_iso: str) -> "SkillCell":
        return SkillCell(
            s_hit=self.s_hit + (1.0 if hit else 0.0),
            s_miss=self.s_miss + (0.0 if hit else 1.0),
            n_hit=self.n_hit + (1 if hit else 0),
            n_miss=self.n_miss + (0 if hit else 1),
            updated_at=now_iso,
        )

    @property
    def acc(self) -> float | None:
        n = self.s_hit + self.s_miss
        return (self.s_hit / n) if n > 0 else None

    def as_value(self) -> dict[str, Any]:
        return {
            "s_hit": self.s_hit, "s_miss": self.s_miss,
            "n_hit": self.n_hit, "n_miss": self.n_miss,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_value(cls, v: dict[str, Any] | None) -> "SkillCell":
        if not isinstance(v, dict):
            return cls()
        try:
            return cls(
                s_hit=float(v.get("s_hit") or 0.0),
                s_miss=float(v.get("s_miss") or 0.0),
                n_hit=int(v.get("n_hit") or 0),
                n_miss=int(v.get("n_miss") or 0),
                updated_at=v.get("updated_at"),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass
class RouterEdgeTick:
    """What one router_edge_tick observed/did (for the log line + blend)."""

    symbol: str
    archetype: str
    direction: str | None
    p_router: float | None          # P(next candle up | cell); None = no cell read
    cell_n_eff: float
    base_rate: float | None
    resolved: bool                   # a previous stamp was resolved this tick
    resolved_outcome: str | None     # "up" | "dn" | "tie" | None
    stamped: bool                    # a new stamp was persisted
    p_router_blend: float | None     # P(win for THIS trade direction) — the blend input


# ─── Helpers ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_days(from_iso: str | None, to_iso: str) -> float:
    if not from_iso:
        return 0.0
    try:
        a = datetime.fromisoformat(str(from_iso))
        b = datetime.fromisoformat(str(to_iso))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return max(0.0, (b - a).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def cell_key(archetype: str, direction: str | None) -> str:
    """(archetype, direction) → tier_key. 'none' keeps neutral ticks in their
    own stand_by cell — the no-setup population is itself a measurable
    conditioning (if P(up|momentum:buy) ≠ P(up|stand_by:none), the router's
    verdict carries information)."""
    d = (direction or "none").lower()
    return f"{str(archetype)}:{d}"


def predict_p_up(cell: EdgeCell, base_rate: float | None, k0: float) -> float | None:
    """Posterior mean P(up | cell) with the base rate as shrinkage anchor.

    (k0*p_base + s_up) / (k0 + s_up + s_dn) — PBM's anchor-on-null shape with
    the symbol's own pooled base rate standing in for the analytic null (the
    next-candle target has no closed-form null; the empirical base IS the
    honest anchor). k0 pseudo-counts keep sparse cells at the base rate
    instead of overstating. An EMPTY cell with no base emits 0.5 — the
    uninformative prior, which the skill scorer correctly treats as "no
    call" (p == 0.5 is skipped). None only in the degenerate k0<=0-with-
    empty-cell config (no honest number exists then)."""
    p_base = 0.5 if base_rate is None else float(base_rate)
    denom = k0 + cell.s_up + cell.s_dn
    if denom <= 0:
        return None
    return (k0 * p_base + cell.s_up) / denom


def _base_rate_of(base_cell: EdgeCell, k0: float) -> float | None:
    """Symbol base rate = the _base cell's own posterior with a 0.5 prior."""
    if base_cell.n_eff <= 0:
        return None
    return predict_p_up(base_cell, 0.5, k0)


def _first_new_bar(
    prices: Sequence[float], timestamps: Sequence[float | None] | None,
    stamp_bar_ts: float | None,
) -> tuple[int, float] | None:
    """First bar STRICTLY newer than the stamped bar (by timestamp).

    Index-aligned per the orchestrator's fetch contract (valid_timestamps
    may contain Nones; only ts-valid entries participate). Returns
    (index, close) or None when no newer bar exists yet (same bar still
    forming / no new data — the stamp stays pending, not double-counted)."""
    if stamp_bar_ts is None or timestamps is None:
        return None
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        if float(ts) > float(stamp_bar_ts):
            if i < len(prices):
                return i, float(prices[i])
    return None


def _side_of_prediction(p: float | None, outcome_up: bool) -> bool | None:
    """Definite-call scoring: hit iff (p>0.5 and up) or (p<0.5 and not up).
    None when p is None or exactly 0.5 (no call to score)."""
    if p is None:
        return None
    p = float(p)
    if p > 0.5:
        return outcome_up
    if p < 0.5:
        return not outcome_up
    return None


# ─── Store I/O (param_store tiers) ─────────────────────────────────────────


async def _read_cell(symbol: str, key: str) -> EdgeCell:
    return EdgeCell.from_value(read_dict(symbol, TIER, key))


async def _read_skill(symbol: str, key: str) -> SkillCell:
    return SkillCell.from_value(read_dict(symbol, TIER, key))


def _read_cell_sync(symbol: str, key: str) -> EdgeCell:
    return EdgeCell.from_value(read_dict(symbol, TIER, key))


def _read_skill_sync(symbol: str, key: str) -> SkillCell:
    return SkillCell.from_value(read_dict(symbol, TIER, key))


async def _write_rows(rows: list[dict[str, Any]]) -> None:
    if rows:
        await write_params([
            {**r, "tier_type": TIER, "source": "router_edge"} for r in rows
        ])


# ─── The tick ───────────────────────────────────────────────────────────────


async def router_edge_tick(
    *,
    symbol: str,
    signal: str | None,
    regime_label: str | None,
    asset_class: str | None,
    macro_label: str | None,
    trend_label: str | None,
    prices: Sequence[float],
    timestamps: Sequence[float | None] | None,
    p_timesfm: float | None = None,
    mode: str = "shadow",
    k0: float = 20.0,
    tau_days: float = 90.0,
    min_n_eff: float = 30.0,
) -> RouterEdgeTick | None:
    """One sweep tick's router-edge update: resolve → route → read → stamp.

    Called from the light sweep for EVERY symbol (not only qualified
    signals — see the module docstring's selection-bias note). Pure-local
    I/O (param_store); never raises (fail-open to None on any internal
    error — a measurement instrument must not break the tick it measures).

    ``mode``: off returns None immediately (zero cost); shadow measures +
    stamps; live additionally exposes ``p_router_blend`` for the EV blend
    (still gated by ``min_n_eff`` per cell — a sparse cell ships its shrunk
    posterior only in shadow, where being wrong costs nothing).
    """
    if mode == "off":
        return None
    try:
        return await _tick_impl(
            symbol=symbol, signal=signal, regime_label=regime_label,
            asset_class=asset_class, macro_label=macro_label,
            trend_label=trend_label, prices=prices, timestamps=timestamps,
            p_timesfm=p_timesfm, mode=mode, k0=k0, tau_days=tau_days,
            min_n_eff=min_n_eff,
        )
    except Exception as exc:  # noqa: BLE001 — never break a sweep tick
        logger.warning(
            "[router:edge] %s tick failed (%s: %s) — skipped",
            symbol, type(exc).__name__, exc,
        )
        return None


async def _tick_impl(
    *,
    symbol: str,
    signal: str | None,
    regime_label: str | None,
    asset_class: str | None,
    macro_label: str | None,
    trend_label: str | None,
    prices: Sequence[float],
    timestamps: Sequence[float | None] | None,
    p_timesfm: float | None,
    mode: str,
    k0: float,
    tau_days: float,
    min_n_eff: float,
) -> RouterEdgeTick | None:
    now_iso = _now_iso()
    rows: list[dict[str, Any]] = []

    # Need a bar identity to stamp at all. Without timestamps the window
    # slides and there is no honest "next candle" — skip (documented
    # boundary; the orchestrator passes fetch timestamps when available).
    last_ts = _last_valid_ts(timestamps)
    if last_ts is None or not prices:
        return None
    last_close = float(prices[-1])

    # ── 1. Resolve the pending stamp ────────────────────────────────────
    resolved = False
    resolved_outcome: str | None = None
    last = read_dict(symbol, TIER, KEY_LAST)
    if isinstance(last, dict) and last.get("bar_ts") is not None:
        nb = _first_new_bar(prices, timestamps, float(last["bar_ts"]))
        if nb is not None:
            _idx, new_close = nb
            stamp_close = float(last.get("close") or 0.0)
            if new_close > stamp_close:
                resolved_outcome = "up"
            elif new_close < stamp_close:
                resolved_outcome = "dn"
            else:
                resolved_outcome = "tie"
            resolved = True

            if resolved_outcome != "tie":
                up = resolved_outcome == "up"
                # cell + base (decay-then-add, PBM convention)
                key = str(last.get("cell_key") or cell_key("stand_by", None))
                cell = _read_cell_sync(symbol, key).decayed(now_iso, tau_days).add(up, now_iso)
                base = _read_cell_sync(symbol, KEY_BASE).decayed(now_iso, tau_days).add(up, now_iso)
                rows.append({"symbol": symbol, "tier_key": key, "tier_value": cell.as_value(), "n": int(cell.n_eff)})
                rows.append({"symbol": symbol, "tier_key": KEY_BASE, "tier_value": base.as_value(), "n": int(base.n_eff)})
                # skill accumulators — BOTH predictors scored on the SAME outcome
                for skill_key, p_pred in (
                    (KEY_ROUTER_SKILL, last.get("p_router")),
                    (KEY_TIMESFM_SKILL, last.get("p_timesfm")),
                ):
                    hit = _side_of_prediction(
                        float(p_pred) if p_pred is not None else None, up,
                    )
                    if hit is not None:
                        sk = _read_skill_sync(symbol, skill_key).decayed(now_iso, tau_days).add(hit, now_iso)
                        rows.append({"symbol": symbol, "tier_key": skill_key, "tier_value": sk.as_value(), "n": int(sk.n_eff)})
            # tie or not: the pending stamp is consumed either way
            rows.append({"symbol": symbol, "tier_key": KEY_LAST, "tier_value": {}, "n": 0})

    # ── 2. Route the CURRENT tick + read the cell ───────────────────────
    from regime_platform.sweep_orchestrator.strategy_router import route
    routed = route(
        symbol=symbol, signal=signal, regime_label=regime_label,
        asset_class=asset_class, macro_label=macro_label, trend_label=trend_label,
    )
    key = cell_key(routed.archetype, _direction_word(signal))
    base_cell = _read_cell_sync(symbol, KEY_BASE)
    base_rate = _base_rate_of(base_cell, k0)
    cell = _read_cell_sync(symbol, key)
    p_router = predict_p_up(cell, base_rate, k0)

    # ── 3. Stamp for the next tick's resolution ─────────────────────────
    stamp = {
        "stamped_at": now_iso,
        "bar_ts": float(last_ts),
        "close": last_close,
        "cell_key": key,
        "archetype": routed.archetype,
        "direction": _direction_word(signal),
        "p_router": p_router,
        "p_timesfm": (float(p_timesfm) if p_timesfm is not None else None),
    }
    rows.append({"symbol": symbol, "tier_key": KEY_LAST, "tier_value": stamp, "n": 1})

    await _write_rows(rows)

    # ── 4. Blend exposure (live mode only, evidence-gated per cell) ─────
    p_router_blend: float | None = None
    if mode == "live" and p_router is not None and cell.n_eff >= min_n_eff:
        d = _direction_word(signal)
        if d == "buy":
            p_router_blend = p_router
        elif d == "sell":
            p_router_blend = 1.0 - p_router

    return RouterEdgeTick(
        symbol=symbol, archetype=routed.archetype,
        direction=_direction_word(signal),
        p_router=p_router, cell_n_eff=cell.n_eff, base_rate=base_rate,
        resolved=resolved, resolved_outcome=resolved_outcome,
        stamped=True, p_router_blend=p_router_blend,
    )


def _direction_word(signal: str | None) -> str | None:
    if not signal:
        return None
    s = str(signal).lower()
    return s if s in ("buy", "sell") else None


def _last_valid_ts(timestamps: Sequence[float | None] | None) -> float | None:
    if not timestamps:
        return None
    for ts in reversed(timestamps):
        if ts is not None:
            return float(ts)
    return None


# ─── Report (the W-X2 measurement, from the live store) ────────────────────


def router_edge_report(symbol: str | None = None) -> dict[str, Any]:
    """Skill table for the router-edge model + the TimesFM comparison.

    Per cell: n_eff, posterior p_up, raw counts, acc, and skill =
    acc − majority (majority = the symbol base rate's dominant-side
    frequency — the W-X2 convention from regime_edge/hmm_ref.py). Aggregate
    skill for the router's own directional calls AND for p_timesfm's, on
    the identical outcome stream. Pure read; safe to call from a REPL,
    a script, or an endpoint."""
    try:
        from regime_platform.core.feature_flags import (
            get_router_edge_k0,
        )

        k0 = get_router_edge_k0()
        out: dict[str, Any] = {"generated_at": _now_iso(), "symbols": {}}
        tiers = _read_tier_all(symbol)
        for sym, cells in sorted(tiers.items()):
            base = EdgeCell.from_value(cells.get(KEY_BASE))
            base_rate = _base_rate_of(base, k0)
            if base_rate is None:
                majority = 0.5
                base_rate_r = None
            else:
                majority = max(base_rate, 1.0 - base_rate)
                base_rate_r = round(base_rate, 4)
            sym_block: dict[str, Any] = {
                "base_rate": base_rate_r,
                "base_n_eff": round(base.n_eff, 1),
                "majority": round(majority, 4),
                "cells": {},
            }
            for key, val in sorted(cells.items()):
                if key.startswith("_"):
                    continue
                c = EdgeCell.from_value(val)
                if c.n_eff <= 0 and c.n_tie == 0:
                    continue
                acc = (c.s_up / c.n_eff) if c.n_eff > 0 else None
                sym_block["cells"][key] = {
                    "n_eff": round(c.n_eff, 1),
                    "p_up": round(predict_p_up(c, base_rate, k0), 4) if c.n_eff > 0 or base_rate is not None else None,
                    "raw": [c.n_up, c.n_dn, c.n_tie],
                    "acc": round(acc, 4) if acc is not None else None,
                    "skill_vs_majority": (
                        round(acc - majority, 4) if acc is not None else None
                    ),
                }
            for skill_key, label in (
                (KEY_ROUTER_SKILL, "router_skill"),
                (KEY_TIMESFM_SKILL, "timesfm_skill"),
            ):
                sk = SkillCell.from_value(cells.get(skill_key))
                acc_s = sk.acc
                sym_block[label] = {
                    "n_eff": round(sk.n_eff, 1),
                    "acc": round(acc_s, 4) if acc_s is not None else None,
                    "skill_vs_majority": (
                        round(acc_s - majority, 4) if acc_s is not None else None
                    ),
                }
            out["symbols"][sym] = sym_block
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "generated_at": _now_iso()}


def _read_tier_all(symbol: str | None = None) -> dict[str, dict[str, Any]]:
    """{symbol: {tier_key: parsed_value}} for the router_edge tier.
    Optional symbol filter; parses tier_value JSON strings once, here."""
    from regime_platform.core.param_store import read_all

    out: dict[str, dict[str, Any]] = {}
    for row in read_all():
        if str(row.get("tier_type")) != TIER:
            continue
        sym = str(row.get("symbol"))
        if symbol is not None and sym != str(symbol):
            continue
        key = str(row.get("tier_key"))
        val = row.get("tier_value")
        if isinstance(val, str):
            try:
                import json
                val = json.loads(val)
            except (ValueError, TypeError):
                val = {}
        out.setdefault(sym, {})[key] = val if isinstance(val, dict) else {}
    return out


def format_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of router_edge_report() for logs/scripts."""
    lines = ["router-edge skill report", "=" * 60]
    if "error" in report:
        lines.append(f"ERROR: {report['error']}")
        return "\n".join(lines)
    if not report.get("symbols"):
        lines.append("(no cells yet — accumulate stamps first)")
        return "\n".join(lines)
    for sym, blk in report["symbols"].items():
        lines.append(
            f"\n{sym}  base={blk.get('base_rate')}  majority={blk.get('majority')}  "
            f"base_n_eff={blk.get('base_n_eff')}"
        )
        for key, c in (blk.get("cells") or {}).items():
            lines.append(
                f"  {key:28s} n_eff={c.get('n_eff'):>7} p_up={c.get('p_up')} "
                f"acc={c.get('acc')} skill={c.get('skill_vs_majority')}"
            )
        for label in ("router_skill", "timesfm_skill"):
            sk = blk.get(label) or {}
            lines.append(
                f"  {label:28s} n_eff={sk.get('n_eff')} acc={sk.get('acc')} "
                f"skill={sk.get('skill_vs_majority')}"
            )
    return "\n".join(lines)
