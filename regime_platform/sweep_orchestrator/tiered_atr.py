"""
Dynamic ATR — TP/SL picked by realised expectancy on the platform's own signals
═══════════════════════════════════════════════════════════════════════════════

worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md; evidence: scope §14.5
(worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md) and
worklog/kairos/reports/20260926_geometry_replay.md.

``dynamic_atr_mode`` (off | shadow | live) is the platform's geometry lever.
On each heavy sweep it chooses the bracket (TP and SL distance in ATR units)
for the symbol's asset class:

  1. Load the class's signals from the last ``lookback_days`` whose 48-bar
     window has finished (nt_signal_sim, one per symbol x direction x hour).
  2. Re-walk each one under every TP x SL candidate with the live resolver's
     rule (1h closes, first touch, 48-bar expiry, TP first on a tie).
  3. Keep candidates with TP/SL >= min_rr, expiry share <= ``max_expiry`` and
     positive expectancy (net of ``cost_atr``) in BOTH halves of the window.
  4. Pick the highest expectancy. Nothing qualifies -> keep the heavy pick.

The pick is expressed in bricks with ``sl_bricks`` fixed, so
brick = sl_atr x ATR / sl_bricks and tp_bricks = round(tp_atr / sl_atr x sl_bricks).

Rolling walk-forward (fit on the trailing 10 days, apply the next day,
08-26 -> 09-24, 4,647 signals, cost 0.1 ATR): recorded geometry -1.55 ATR per
signal, this selector +0.82; the old band-limited pick (TP <= 2 ATR) -1.50.

Also here: the ATR-anchored evidence promote helpers used by the heavy
sweep's candidate pool (``should_promote_atr_candidates``,
``compose_deadline_candidate_pool``, ``fetch_bucket_long_lag``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

from regime_platform.sweep_orchestrator.geometry_risk import GEOMETRY_RISK_CAP

logger = logging.getLogger("regime.sweep.tiered_atr")

EXPIRY_BARS = 48
DEFAULT_RISK_CAP = GEOMETRY_RISK_CAP


def asset_class_key(asset_class: str | None) -> str:
    if not asset_class:
        return "default"
    a = str(asset_class).strip().lower()
    if "crypto" in a or a in ("btc", "coin"):
        return "crypto"
    if "forex" in a or "fx" in a or a in ("currency",):
        return "forex"
    if "metal" in a or a in ("gold", "silver", "xau", "xag"):
        return "metal"
    if "commod" in a:
        return "commodity"
    if "etf" in a:
        return "etf"
    if "index" in a or a in ("us30", "us500", "ndx"):
        return "index"
    if "equity" in a or "stock" in a or "share" in a:
        return "equity"
    return "default"


# ─── Replay walk (same rule as the live resolver) ──────────────────────────
#
# 2026-10-08 (worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md): this module's
# walk IS the geometry picker, and it used to resolve on closes-only with a
# TP-first tie-break while the live resolver settles on high/low with SL-first.
# The picker therefore scored +0.61 ATR/signal on a bracket the resolver
# settled at −0.24 — the gap was measurement, not market. The walk now defers
# to settlement_contract's single `resolve_bracket` rule and takes an explicit
# ``basis`` (intrabar | close_walk) and ``same_bar_policy`` (sl_first |
# tp_first). The live basis is the default for every picker call site.

from regime_platform.sweep_orchestrator.settlement_contract import (
    BASIS_CLOSE,
    BASIS_INTRABAR,
    OUTCOME_EXPIRED,
    OUTCOME_SL,
    OUTCOME_TP,
    SAME_BAR_SL_FIRST,
    SAME_BAR_TP_FIRST,
    resolve_bracket_from_envelope,
)

# The basis/policy the LIVE resolver settles on. Kept as module constants so
# a picker can state the assumption explicitly; callers may override for
# parity replays of pre-cutover history.
LIVE_BASIS = BASIS_INTRABAR
LIVE_SAME_BAR_POLICY = SAME_BAR_SL_FIRST


def live_basis() -> str:
    """Basis the live resolver currently settles on (``eod_resolver_basis``)."""
    try:
        from regime_platform.core.feature_flags import get_eod_resolver_basis
        return str(get_eod_resolver_basis() or LIVE_BASIS).strip().lower()
    except Exception:
        return LIVE_BASIS


def live_same_bar_policy() -> str:
    """Same-bar tie-break the live resolver uses (``settlement_same_bar_policy``)."""
    try:
        from regime_platform.core.feature_flags import get_settlement_same_bar_policy
        return str(get_settlement_same_bar_policy() or LIVE_SAME_BAR_POLICY).strip().lower()
    except Exception:
        return LIVE_SAME_BAR_POLICY


def walk(
    paths: np.ndarray,
    tp_atr: float,
    sl_atr: float,
    *,
    same_bar_policy: str = SAME_BAR_TP_FIRST,
) -> tuple[np.ndarray, np.ndarray]:
    """First-passage over direction-signed, ATR-scaled **close** paths.

    ``paths`` is (n, 48): (close - entry) * dir / atr. Returns
    (realised pnl in ATR at the triggering close, expired mask).

    ``same_bar_policy`` defaults to ``tp_first`` (the historical close-walk
    convention) so a close-path replay is byte-stable; pass ``sl_first`` for
    the conservative OCO rule. Prefer ``walk_envelopes`` on the live basis —
    a close path cannot see an intrabar wick, so it under-counts stop-outs.
    """
    n = len(paths)
    hit_tp = paths >= tp_atr
    hit_sl = paths <= -sl_atr
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    if str(same_bar_policy).strip().lower() == SAME_BAR_SL_FIRST:
        sl_first = (si < EXPIRY_BARS) & (si <= ti)
        tp_first = ~sl_first & (ti < EXPIRY_BARS)
    else:
        tp_first = (ti < EXPIRY_BARS) & (ti <= si)
        sl_first = ~tp_first & (si < EXPIRY_BARS)
    exit_bar = np.where(tp_first, ti, np.where(sl_first, si, EXPIRY_BARS - 1))
    pnl = paths[np.arange(n), exit_bar]
    return pnl, ~(tp_first | sl_first)


def build_envelopes(
    signals: list[dict],
    bars: dict[str, list[dict]],
    *,
    basis: str = LIVE_BASIS,
    return_vol: bool = False,
    vol_short: int = 24,
    vol_long: int = 168,
):
    """(n, EXPIRY_BARS, 2) favourable/adverse envelope, ATR-scaled, time-ordered.

    ``[:, :, 0]`` is the favourable excursion per bar (max profit reachable),
    ``[:, :, 1]`` the adverse excursion (max drawdown reachable). Both are in
    ATR units and direction-signed. This is the shape the picker must use on
    the **intrabar** basis: it carries the wick a close path cannot see.

    ``basis="close_walk"`` degrades each bar to its close (identical shape),
    so a grid search can switch basis without changing its arithmetic.
    """
    bas = str(basis or LIVE_BASIS).strip().lower()
    use_ohlc = bas != BASIS_CLOSE
    tcache = {s: np.array([float(b["t"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    ccache = {s: np.array([float(b["c"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    if use_ohlc:
        hcache = {s: np.array([float(b["h"]) if b.get("h") is not None else float(b["c"])
                               for b in rows], dtype="float64") for s, rows in bars.items()}
        lcache = {s: np.array([float(b["l"]) if b.get("l") is not None else float(b["c"])
                               for b in rows], dtype="float64") for s, rows in bars.items()}
    out, vols = [], []
    for r in sorted(signals, key=lambda x: x["_ts"]):
        t = tcache.get(r["symbol"])
        if t is None or not len(t):
            continue
        j = int(np.searchsorted(t, r["_ts"], side="right")) - 1   # bar in progress at entry
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue
        d = 1.0 if r["signal"] == "buy" else -1.0
        atr = float(r["atr"]); entry = float(r["entry_price"])
        cl = ccache[r["symbol"]][j:j + EXPIRY_BARS]
        if use_ohlc:
            hi = hcache[r["symbol"]][j:j + EXPIRY_BARS]
            lo = lcache[r["symbol"]][j:j + EXPIRY_BARS]
        else:
            hi = lo = cl
        up = (hi - entry) * d / atr
        dn = (lo - entry) * d / atr
        fav = np.maximum(up, dn)
        adv = np.minimum(up, dn)
        out.append(np.stack([fav, adv], axis=1))
        vols.append(compute_vol_ratio(ccache[r["symbol"]], j, vol_short, vol_long))
    env = np.stack(out) if out else np.empty((0, EXPIRY_BARS, 2))
    if return_vol:
        return env, vols
    return env


def walk_envelopes(
    env: np.ndarray,
    tp_atr: float,
    sl_atr: float,
    *,
    same_bar_policy: str = LIVE_SAME_BAR_POLICY,
) -> tuple[np.ndarray, np.ndarray]:
    """First-passage over an (n, 48, 2) favourable/adverse envelope.

    Same rule as ``settlement_contract.resolve_bracket_from_envelope`` (which
    it defers to per row), vectorised for a grid search. Returns
    (realised pnl in ATR, expired mask). A same-bar both-touch resolves by
    ``same_bar_policy`` — default ``sl_first``, matching the live resolver.
    """
    n = len(env)
    if n == 0:
        return np.zeros(0), np.ones(0, dtype=bool)
    fav = env[:, :, 0]
    adv = env[:, :, 1]
    hit_tp = fav >= tp_atr
    hit_sl = adv <= -sl_atr
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    if str(same_bar_policy).strip().lower() == SAME_BAR_SL_FIRST:
        sl_first = (si < EXPIRY_BARS) & (si <= ti)
        tp_first = ~sl_first & (ti < EXPIRY_BARS)
    else:
        tp_first = (ti < EXPIRY_BARS) & (ti <= si)
        sl_first = ~tp_first & (si < EXPIRY_BARS)
    expired = ~(tp_first | sl_first)
    pnl = np.where(tp_first, tp_atr, np.where(sl_first, -sl_atr, 0.0))
    return pnl, expired


def walk_forward_validate(
    env: np.ndarray,
    tp_atr: float,
    sl_atr: float,
    *,
    same_bar_policy: str = LIVE_SAME_BAR_POLICY,
    cost_atr: float = 0.0,
    min_fold_n: int = 20,
    min_oos_expectancy: float = 0.0,
) -> dict[str, Any]:
    """Held-out (walk-forward) check that a chosen bracket generalises.

    ``env`` is the chronologically-ordered (n, 48, 2) envelope
    ``build_envelopes`` produces (it sorts by ``_ts``; ``_load_class_signals``
    is ``order: ts.asc``), so a half-split is a time split. The bracket is
    measured on each half independently; the half it was NOT chosen on is the
    out-of-sample test.

    Worklog 20261009_WALKFORWARD_SPLIT.md: ``select_tp_sl``'s only robustness
    check is an in-sample time split on the same data the bracket was
    optimised over, so a RR=6 pick passes trivially. This adds a true held-out
    fold.

    Returns ``{is_exp, oos_exp, is_n, oos_n, ok, reason}``:
      * ``is_exp`` / ``oos_exp`` — net expectancy (ATR, net of ``cost_atr``) on
        the first / second half.
      * ``ok`` — True when both folds have >= ``min_fold_n`` DECIDED signals
        AND **both** folds' expectancy >= ``min_oos_expectancy``. The pick was
        chosen on the FULL sample, so both halves are partly in-sample; the
        held-out test is that it does not collapse on EITHER half. A pick that
        earns only because one half carried it is the overfit signature.
      * Fail-open: if either fold is too thin, ``ok=True`` with
        ``reason="insufficient_data"`` — a thin sample defers to prior
        behaviour, it does not silently reject the pick.
    """
    n = len(env)
    if n < 2:
        return {"is_exp": None, "oos_exp": None, "is_n": 0, "oos_n": 0,
                "ok": True, "reason": "insufficient_data"}
    mid = n // 2
    folds = (env[:mid], env[mid:])
    exps: list[float | None] = []
    ns: list[int] = []
    for fold in folds:
        if len(fold) == 0:
            exps.append(None); ns.append(0); continue
        pnl, expired = walk_envelopes(fold, tp_atr, sl_atr, same_bar_policy=same_bar_policy)
        decided = int((~expired).sum())
        ns.append(decided)
        exps.append(float((pnl - cost_atr).mean()) if decided else None)

    is_exp, oos_exp = exps[0], exps[1]
    is_n, oos_n = ns[0], ns[1]

    if is_n < min_fold_n or oos_n < min_fold_n:
        return {"is_exp": is_exp, "oos_exp": oos_exp, "is_n": is_n, "oos_n": oos_n,
                "ok": True, "reason": "insufficient_data"}
    # Both folds must clear the floor: the pick was chosen on the full sample,
    # so a fold that collapses means the pick only earned on the other half.
    for label, val in (("is", is_exp), ("oos", oos_exp)):
        if val is None or val < min_oos_expectancy:
            return {"is_exp": is_exp, "oos_exp": oos_exp, "is_n": is_n, "oos_n": oos_n,
                    "ok": False, "reason": f"{label}_expectancy_below_floor"}
    return {"is_exp": is_exp, "oos_exp": oos_exp, "is_n": is_n, "oos_n": oos_n,
            "ok": True, "reason": "ok"}


@dataclass(frozen=True)
class DynamicAtrPick:
    tp_atr: float
    sl_atr: float
    exp_atr: float          # mean realised pnl per signal, ATR, net of cost
    exp_h1: float
    exp_h2: float
    expiry: float           # share of signals expired at 48 bars
    n: int

    def to_bricks(self, atr: float, sl_bricks: int) -> tuple[float, int, int]:
        """Renko display bricks (integer counts). Lossy vs exact ATR."""
        brick = self.sl_atr * float(atr) / sl_bricks
        tp_bricks = max(1, int(round(self.tp_atr / self.sl_atr * sl_bricks)))
        return brick, int(sl_bricks), tp_bricks

    def to_price_levels(
        self, entry: float, atr: float, signal: str
    ) -> tuple[float, float]:
        """Exact TP/SL **prices** from ATR multiples (pips/points), not bricks.

        2026-09-30: brick snap (integer sl/tp counts) quantised ATR tiers —
        e.g. wanted 14 ATR, delivered 13.33. Signal brackets are now pure
        price distance: entry ± tp_atr * atr.
        """
        a = float(atr)
        e = float(entry)
        tp_d = self.tp_atr * a
        sl_d = self.sl_atr * a
        side = (signal or "").lower()
        if side in ("sell", "bear") or "bear" in side:
            return e - tp_d, e + sl_d
        return e + tp_d, e - sl_d

    def as_log(self) -> str:
        return (f"tp={self.tp_atr:g}ATR sl={self.sl_atr:g}ATR exp={self.exp_atr:+.3f} "
                f"(h1 {self.exp_h1:+.3f} h2 {self.exp_h2:+.3f}) expiry={self.expiry:.0%} n={self.n}")

    def to_json(self) -> dict[str, Any]:
        """The pick as the param_store tier value (JSON object).

        2026-10-09 (worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): the
        ``dynamic_atr`` tier carries the USABLE geometry, not a bucket
        label — so both sweeps can build a bracket from one stored value.
        """
        return {
            "tp_atr": float(self.tp_atr),
            "sl_atr": float(self.sl_atr),
            "exp_atr": float(self.exp_atr),
            "exp_h1": float(self.exp_h1),
            "exp_h2": float(self.exp_h2),
            "expiry": float(self.expiry),
            "n": int(self.n),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "DynamicAtrPick | None":
        """Rebuild a pick from a stored tier value; ``None`` when malformed."""
        if not isinstance(d, dict):
            return None
        try:
            return cls(
                tp_atr=float(d["tp_atr"]),
                sl_atr=float(d["sl_atr"]),
                exp_atr=float(d.get("exp_atr", 0.0)),
                exp_h1=float(d.get("exp_h1", 0.0)),
                exp_h2=float(d.get("exp_h2", 0.0)),
                expiry=float(d.get("expiry", 0.0)),
                n=int(d.get("n", 0) or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ─── Store-backed pick (single source of truth, 2026-10-09) ───────────────
#
# One computation (`dynamic_atr_pick`) owns the ATR geometry; the
# ``dynamic_atr`` tier in config/param_store.sqlite carries it so the light
# and heavy sweeps read the SAME value instead of each deriving its own.
# Key is ``latest`` — the pick is a whole-symbol value, not per-bucket.

_DYNAMIC_ATR_KEY = "latest"


def read_pick_from_store(
    symbol: str, *, max_age_hours: float | None = None,
) -> Optional["DynamicAtrPick"]:
    """Read the symbol's ATR pick from the LOCAL param store (no network).

    Returns ``None`` when absent, malformed, or older than ``max_age_hours``
    — callers then compute (or keep the brick geometry), fail-open.
    """
    try:
        from datetime import datetime, timezone

        from regime_platform.core import param_store

        parsed = param_store.read_dict(symbol, param_store.TIER_DYNAMIC_ATR, _DYNAMIC_ATR_KEY)
        if not parsed:
            return None
        if max_age_hours is not None:
            rows = param_store.read_tier(symbol, param_store.TIER_DYNAMIC_ATR)
            row = rows.get(_DYNAMIC_ATR_KEY) or {}
            ts = row.get("computed_at")
            if ts:
                try:
                    computed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if computed.tzinfo is None:
                        computed = computed.replace(tzinfo=timezone.utc)
                    age_h = (datetime.now(timezone.utc) - computed).total_seconds() / 3600.0
                    if age_h > float(max_age_hours):
                        return None
                except (TypeError, ValueError):
                    pass  # unparseable stamp → treat as fresh (fail-open)
        return DynamicAtrPick.from_json(parsed)
    except Exception as exc:
        logger.debug("tiered_atr read_pick_from_store %s: %s", symbol, exc)
        return None


async def write_pick_to_store(
    symbol: str, pick: "DynamicAtrPick", *, source: str = "tiered_atr",
) -> bool:
    """Write the pick's usable geometry to the LOCAL param store.

    This is what makes the ``dynamic_atr`` tier a live read path rather than
    the dead bucket-label write it was before. Best-effort: returns False on
    failure, never raises.
    """
    try:
        from datetime import datetime, timezone

        from regime_platform.core import param_store

        await param_store.write_params([{
            "symbol": symbol,
            "tier_type": param_store.TIER_DYNAMIC_ATR,
            "tier_key": _DYNAMIC_ATR_KEY,
            "tier_value": pick.to_json(),
            "n": int(pick.n),
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        }])
        # Prune the pre-2026-10-09 per-bucket rows (keys like "4+") — the tier
        # now holds exactly one row (key "latest"). They are unreachable and
        # would otherwise linger as permanent parity drift.
        param_store.delete_keys(
            symbol, param_store.TIER_DYNAMIC_ATR, keep_keys=(_DYNAMIC_ATR_KEY,),
        )
        return True
    except Exception as exc:
        logger.warning("tiered_atr write_pick_to_store %s: %s", symbol, exc)
        return False


def select_tp_sl(
    paths: np.ndarray | None = None,
    *,
    tp_grid: list[float],
    sl_grid: list[float],
    min_rr: float,
    max_expiry: float,
    cost_atr: float,
    min_signals: int,
    tp_atr_min: float | None = None,
    tp_atr_max: float | None = None,
    sl_atr_min: float | None = None,
    sl_atr_max: float | None = None,
    envelopes: np.ndarray | None = None,
    same_bar_policy: str = LIVE_SAME_BAR_POLICY,
    allow_best_effort: bool = True,
) -> Optional[DynamicAtrPick]:
    """Best TP/SL by expectancy; must be positive in both halves (rows are time-ordered).

    Two input shapes (pass exactly one):
      * ``paths`` (n, 48) close paths → resolved with ``walk`` (close basis,
        TP-first) — the historical call shape, kept for parity replays.
      * ``envelopes`` (n, 48, 2) favourable/adverse envelope → resolved with
        ``walk_envelopes`` on the LIVE basis (intrabar, sl_first). **This is
        the shape a live pick must use** — a close path cannot see the wick
        that trips a tight stop (worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md).

    Optional band clamps (2026-09-29 geometry_rework): drop candidates outside
    [tp_atr_min, tp_atr_max] x [sl_atr_min, sl_atr_max] even if the grid lists
    them — the 10d walk otherwise prefers a wide TP outside the replay's
    best region (TP 9-17 / SL 0.5-2).
    """
    if envelopes is not None:
        n = len(envelopes)
    elif paths is not None:
        n = len(paths)
    else:
        return None
    if n < min_signals:
        return None
    mid = n // 2
    best: Optional[DynamicAtrPick] = None          # clears the strict bar
    best_effort: Optional[DynamicAtrPick] = None   # in-band, max expectancy
    for tp in tp_grid:
        for sl in sl_grid:
            tp_f, sl_f = float(tp), float(sl)
            if sl_f <= 0 or tp_f / sl_f < min_rr:
                continue
            if tp_atr_min is not None and tp_f < tp_atr_min:
                continue
            if tp_atr_max is not None and tp_f > tp_atr_max:
                continue
            if sl_atr_min is not None and sl_f < sl_atr_min:
                continue
            if sl_atr_max is not None and sl_f > sl_atr_max:
                continue
            if envelopes is not None:
                pnl, expired = walk_envelopes(envelopes, tp_f, sl_f,
                                              same_bar_policy=same_bar_policy)
            else:
                pnl, expired = walk(paths, tp_f, sl_f)
            net = pnl - cost_atr
            e1, e2 = float(net[:mid].mean()), float(net[mid:].mean())
            e = float(net.mean())
            exp_m = float(expired.mean())
            cand = DynamicAtrPick(tp_f, sl_f, e, e1, e2, exp_m, n)
            # Best-effort track: the in-band bracket with the highest net
            # expectancy, ignoring the strict bar. Consulted only when nothing
            # clears it (allow_best_effort below) — G2, see the return comment.
            if best_effort is None or e > best_effort.exp_atr:
                best_effort = cand
            if exp_m > max_expiry or e1 <= 0 or e2 <= 0:
                continue
            if best is None or e > best.exp_atr:
                best = cand
    # Belt-and-braces: never return a pick outside the published clamps.
    if best is not None:
        if tp_atr_min is not None and best.tp_atr < tp_atr_min:
            best = None
        elif tp_atr_max is not None and best.tp_atr > tp_atr_max:
            best = None
        elif sl_atr_min is not None and best.sl_atr < sl_atr_min:
            best = None
        elif sl_atr_max is not None and best.sl_atr > sl_atr_max:
            best = None
    if best is not None:
        return best
    # G2 (2026-10-12, docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md
    # §3.2): the solve ALWAYS returns geometry. When no in-band candidate
    # clears the strict both-halves bar, return the best-expectancy in-band
    # bracket computed from the CURRENT window — a real, current, in-band
    # geometry. Never None, so nothing downstream can fall back to brick. The
    # caller sees the weak/negative exp_atr and decides; qualification (not
    # geometry) is the gate.
    return best_effort if allow_best_effort else None


def compute_vol_ratio(
    closes: np.ndarray, j: int, short: int = 24, long: int = 168
) -> float:
    """Entry-time short/long realized-vol ratio (NOT volume).

    ``rv_short / rv_long`` of 1h log returns ending at bar ``j`` (inclusive).
    Past bars only — safe as a live gate. NaN if either window is missing.
    """
    c = np.asarray(closes, dtype="float64")
    if j < short or j < long or j + 1 > len(c):
        return float("nan")
    rs = np.diff(np.log(c[j - short:j + 1]))
    rl = np.diff(np.log(c[j - long:j + 1]))
    if rs.size < 2 or rl.size < 2:
        return float("nan")
    rv_s, rv_l = float(rs.std()), float(rl.std())
    if not np.isfinite(rv_s) or not np.isfinite(rv_l) or rv_l <= 0:
        return float("nan")
    return rv_s / rv_l


def build_paths(
    signals: list[dict],
    bars: dict[str, list[dict]],
    *,
    return_vol: bool = False,
    vol_short: int = 24,
    vol_long: int = 168,
):
    """(n, 48) ATR-scaled paths for signals with a full 48-bar window, time-ordered.

    ``return_vol=True`` also returns a parallel list of entry-time
    ``vol_ratio`` values (see ``compute_vol_ratio``).
    """
    tcache = {s: np.array([float(b["t"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    ccache = {s: np.array([float(b["c"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    out, vols = [], []
    for r in sorted(signals, key=lambda x: x["_ts"]):
        t = tcache.get(r["symbol"])
        if t is None or not len(t):
            continue
        j = int(np.searchsorted(t, r["_ts"], side="right")) - 1   # bar in progress at entry
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue
        d = 1.0 if r["signal"] == "buy" else -1.0
        out.append((ccache[r["symbol"]][j:j + EXPIRY_BARS] - r["entry_price"]) * d / r["atr"])
        vols.append(compute_vol_ratio(ccache[r["symbol"]], j, vol_short, vol_long))
    paths = np.vstack(out) if out else np.empty((0, EXPIRY_BARS))
    if return_vol:
        return paths, vols
    return paths


# ─── Stage 5: pick ↔ reachability consistency gate (2026-10-08) ────────────
#
# The picker scores a candidate bracket by re-walking prices; the platform
# ALSO measures every bracket that actually traded, per (symbol, vol_tier,
# distance_atr_bucket), in nt_bracket_reachability. Those two must agree. A
# pick whose walk implies a healthy expectancy while the bucket it lands in
# shows a near-zero TP rate is the optimizer disagreeing with its own
# platform's measurements — the exact failure the basis fix addresses, kept
# as a standing guard so a future drift is caught rather than traded.

# Tolerance: the walk's implied TP rate may differ from the measured bucket
# rate by up to this before the pick is rejected. Generous on purpose — the
# measured bucket pools many brackets, so a candidate need only be in the
# same regime of plausibility, not identical.
REACHABILITY_AGREE_TOL = 0.20
# Minimum measured-bucket sample before the gate is willing to reject
# (below this, the bucket is noise — fail-open).
REACHABILITY_MIN_N = 30


def check_pick_reachability_consistency(
    walk_tp_rate: float | None,
    measured_pct_hit_tp: float | None,
    measured_n: int | None,
    *,
    tolerance: float = REACHABILITY_AGREE_TOL,
    min_n: int = REACHABILITY_MIN_N,
) -> tuple[bool, str]:
    """Does a candidate's walked TP rate agree with its measured bucket?

    Returns ``(consistent, reason)``. Fail-open: ``True`` when either side is
    missing or the bucket is below ``min_n`` (no evidence to contradict the
    walk). Only a bucket with enough samples that DISAGREES by more than
    ``tolerance`` rejects the pick.
    """
    if walk_tp_rate is None or measured_pct_hit_tp is None:
        return True, "no_comparison"
    try:
        n = int(measured_n or 0)
    except (TypeError, ValueError):
        n = 0
    if n < min_n:
        return True, "bucket_below_min_n"
    diff = abs(float(walk_tp_rate) - float(measured_pct_hit_tp))
    if diff > tolerance:
        return False, (
            f"walk_tp={float(walk_tp_rate):.2f} vs measured_tp={float(measured_pct_hit_tp):.2f} "
            f"(Δ{diff:.2f} > {tolerance:.2f}, n={n})"
        )
    return True, "ok"


async def fetch_bucket_measured_tp(
    symbol: str, distance_atr_bucket: str,
) -> tuple[float | None, int | None]:
    """(pct_hit_tp, n) for one (symbol, distance_atr_bucket) from reachability.

    Reads the platform's OWN measurement of brackets that traded (the live
    resolver's verdicts) — the ground truth the walk must agree with. Pools
    across vol_tier (the picker is symbol-pooled; it has no live vol tier at
    pick time) by taking the highest-n row for the bucket.
    """
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    if not settings.SUPABASE_URL:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "n,pct_hit_tp,vol_tier,computed_at",
                    "symbol": f"eq.{symbol}",
                    "distance_atr_bucket": f"eq.{distance_atr_bucket}",
                    "signal": "eq.all",
                    "order": "n.desc",
                    "limit": "1",
                },
            )
        rows = resp.json() or []
        if not rows:
            return None, None
        r = rows[0]
        tp = r.get("pct_hit_tp")
        return (float(tp) if tp is not None else None, int(r.get("n") or 0))
    except Exception as exc:
        logger.debug("tiered_atr fetch_bucket_measured_tp %s: %s", symbol, exc)
        return None, None


# ─── Production entry point ────────────────────────────────────────────────

_PICK_CACHE: dict[tuple, tuple[float, Optional[DynamicAtrPick]]] = {}
_PICK_TTL_S = 6 * 3600


async def _load_class_signals(symbols: list[str], since: datetime, until: datetime) -> list[dict]:
    import httpx

    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    rows: list[dict] = []
    off = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(_url("nt_signal_sim"), headers=_headers(), params={
                "select": "symbol,signal,ts,entry_price,atr",
                "source": "eq.sweep-light",
                "signal": "in.(buy,sell)",
                "symbol": f"in.({','.join(symbols)})",
                "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                "order": "ts.asc",
                "limit": "1000",
                "offset": str(off),
            })
            resp.raise_for_status()
            batch = resp.json() or []
            rows += batch
            off += len(batch)
            if len(batch) < 1000:
                break
    out, seen = [], set()
    for r in rows:
        try:
            atr = float(r.get("atr") or 0)
            entry = float(r["entry_price"])
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        if atr <= 0:
            continue
        key = (r["symbol"], r["signal"], int(ts // 3600))   # 15-min sweeps repeat the call
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": r["symbol"], "signal": r["signal"], "entry_price": entry,
                    "atr": atr, "_ts": ts})
    return out


def _band_center_pick(cfg: dict[str, Any]) -> Optional[DynamicAtrPick]:
    """The operator's deliberate mid-band bracket — G2's last resort.

    Consulted when the solve has no usable evidence (thin/absent window, symbol
    not active, or a robustness gate declined the walked pick). It is a CURRENT,
    in-band, operator-set value — never a stored/stale/brick parameter, which is
    the whole point of the redesign: on a miss, resolve to something deliberate,
    not to old params. Returns None only if the band itself is unconfigured.
    """
    tps = [float(x) for x in (cfg.get("tp_atr") or [])]
    sls = [float(x) for x in (cfg.get("sl_atr") or [])]
    if not tps or not sls:
        return None
    tp, sl = tps[len(tps) // 2], sls[len(sls) // 2]
    logger.info(
        "[dynamic:atr] band-centre fallback tp=%.2f sl=%.2f (no usable walked evidence)", tp, sl,
    )
    return DynamicAtrPick(tp, sl, 0.0, 0.0, 0.0, 0.0, 0)


def _pick_in_band(pick: DynamicAtrPick, cfg: dict[str, Any]) -> bool:
    """G1 at the STORE boundary — is this persisted pick inside the CURRENT band?

    The store is a cache of *past* solves, and a symbol's band can change
    (retune, reopt, the 2026-10-12 reconciliation). A pick that is fresh by AGE
    can still carry a bracket from a *previous* band, and the light path would
    apply it verbatim. Observed live:

        BTCUSD   band tp=[7.8, 8.07, 8.4] clamps tp<=8.8   served tp=13ATR
        US30USD  band tp=[12.5,12.8,13.1] clamps sl<=2.8   served tp=8/sl=5

    A pick is always a grid cell of the band it was solved under, so the test is
    both: inside the clamps AND a member of the current grid.
    """
    def _in(v: float, lo, hi) -> bool:
        if lo is not None and v < float(lo) - 1e-9:
            return False
        if hi is not None and v > float(hi) + 1e-9:
            return False
        return True

    if not _in(pick.tp_atr, cfg.get("tp_atr_min"), cfg.get("tp_atr_max")):
        return False
    if not _in(pick.sl_atr, cfg.get("sl_atr_min"), cfg.get("sl_atr_max")):
        return False
    tp_grid = [float(x) for x in (cfg.get("tp_atr") or [])]
    sl_grid = [float(x) for x in (cfg.get("sl_atr") or [])]
    if tp_grid and not any(abs(pick.tp_atr - g) < 1e-6 for g in tp_grid):
        return False
    if sl_grid and not any(abs(pick.sl_atr - g) < 1e-6 for g in sl_grid):
        return False
    return True


async def dynamic_atr_pick(
    asset_class: str | None,
    *,
    cfg: dict[str, Any],
    min_rr: float,
    symbol: str | None = None,
    use_store: bool = True,
    allow_best_effort: bool = True,
) -> Optional[DynamicAtrPick]:
    """TP/SL from recent signals. ``symbol`` walks only that symbol (per-symbol
    solve); otherwise the whole asset class. Cached 6h per (symbol|class, grid).

    ``use_store`` (default True): return a fresh persisted pick without
    walking. The pre-sweep refresh passes ``False`` so it always recomputes
    (otherwise it would read back its own previous write and never refresh).
    """
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    ac = asset_class_key(asset_class)
    scope = symbol or ac
    logger.info(
        "[dynamic:atr] scope=%s class=%s grid tp=%s sl=%s clamps tp=[%s,%s] sl=[%s,%s] max_exp=%s",
        scope, ac,
        cfg.get("tp_atr"), cfg.get("sl_atr"),
        cfg.get("tp_atr_min"), cfg.get("tp_atr_max"),
        cfg.get("sl_atr_min"), cfg.get("sl_atr_max"),
        cfg.get("max_expiry"),
    )
    vol_min = cfg.get("vol_ratio_min")
    vol_max = cfg.get("vol_ratio_max")
    vol_short = int(cfg.get("vol_ratio_short") or 24)
    vol_long = int(cfg.get("vol_ratio_long") or 168)
    grid_fp = (
        scope,
        tuple(cfg.get("tp_atr") or ()),
        tuple(cfg.get("sl_atr") or ()),
        cfg.get("tp_atr_min"),
        cfg.get("tp_atr_max"),
        cfg.get("sl_atr_min"),
        cfg.get("sl_atr_max"),
        cfg.get("max_expiry"),
        vol_min,
        vol_max,
        vol_short,
        vol_long,
    )
    hit = _PICK_CACHE.get(grid_fp)
    if hit and time.time() - hit[0] < _PICK_TTL_S and use_store:
        return hit[1]

    # Single source of truth (2026-10-09, worklog
    # 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): the optimizer refreshes this
    # store every 15 min, so a fresh persisted pick is authoritative and we
    # skip the walk entirely. Stale/absent → compute below (fail-open).
    # ``use_store=False`` (the refresh path) always recomputes.
    if symbol and use_store:
        _stored = read_pick_from_store(symbol, max_age_hours=_PICK_TTL_S / 3600.0)
        if _stored is not None and _pick_in_band(_stored, cfg):
            logger.info(
                "[dynamic:atr] %s store hit — %s (no walk)", symbol, _stored.as_log(),
            )
            _PICK_CACHE[grid_fp] = (time.time(), _stored)
            return _stored
        if _stored is not None:
            # G1 at the store boundary (2026-10-13): a pick fresh by AGE can still
            # carry a bracket from a PREVIOUS band. Out of band is a miss, not a
            # hit — fall through and recompute (G3), never apply it verbatim.
            logger.warning(
                "[dynamic:atr] %s store hit REJECTED — %s is outside the current band "
                "tp=%s clamps[%s,%s] sl=%s clamps[%s,%s]; recomputing",
                symbol, _stored.as_log(),
                cfg.get("tp_atr"), cfg.get("tp_atr_min"), cfg.get("tp_atr_max"),
                cfg.get("sl_atr"), cfg.get("sl_atr_min"), cfg.get("sl_atr_max"),
            )

    peers = await fetch_active_symbols()
    if symbol:
        members = [p for p in peers if p.get("symbol") == symbol and p.get("tradingview_symbol")]
    else:
        members = [p for p in peers if asset_class_key(p.get("asset_class")) == ac and p.get("tradingview_symbol")]
    if not members:
        _fb = _band_center_pick(cfg) if allow_best_effort else None
        _PICK_CACHE[grid_fp] = (time.time(), _fb)
        return _fb
    lookback = int(cfg["lookback_days"])
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=lookback)
    signals = await _load_class_signals([m["symbol"] for m in members], since, until)
    if len(signals) < int(cfg["min_signals"]):
        _fb = _band_center_pick(cfg) if allow_best_effort else None
        _PICK_CACHE[grid_fp] = (time.time(), _fb)
        return _fb
    bars: dict[str, list[dict]] = {}
    await fetch_tradingview_batch(
        [{"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]} for m in members],
        hours=24 * (lookback + 5), interval="1h", ohlcv_out=bars,
    )
    bars = {s: [b for b in rows if b.get("t") is not None and b.get("c") is not None]
            for s, rows in bars.items()}

    # Live vol_ratio gate (2026-09-30): pick only applies when the *current*
    # entry-time ratio is inside the configured band. Outside → None (fail-closed
    # to the heavy pick). BTCUSD's balanced cell is low-vol only.
    if vol_min is not None or vol_max is not None:
        closes_by = {
            s: np.array([float(b["c"]) for b in rows], dtype="float64")
            for s, rows in bars.items()
        }
        _vr_now = float("nan")
        for s, c in closes_by.items():
            if len(c) > vol_long:
                _vr_now = compute_vol_ratio(c, len(c) - 1, vol_short, vol_long)
                if np.isfinite(_vr_now):
                    break
        _in = (
            np.isfinite(_vr_now)
            and (vol_min is None or _vr_now >= float(vol_min))
            and (vol_max is None or _vr_now <= float(vol_max))
        )
        if not _in:
            logger.info(
                "[dynamic:atr] scope=%s vol_ratio=%.4f outside [%s,%s] — walked "
                "pick not applicable now (band-centre fallback)",
                scope, _vr_now, vol_min, vol_max,
            )
            _fb = _band_center_pick(cfg) if allow_best_effort else None
            _PICK_CACHE[grid_fp] = (time.time(), _fb)
            return _fb
        logger.info(
            "[dynamic:atr] scope=%s vol_ratio=%.4f in band [%s,%s]",
            scope, _vr_now, vol_min, vol_max,
        )

    # 2026-10-08 (worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md): build the
    # picker's evidence on the LIVE basis (intrabar high/low + sl_first), so
    # the bracket this chooses is scored the same way the resolver settles it.
    # ``basis`` is read from the live flag; a "close_walk" basis reproduces
    # the historical close-path pick for parity replays only.
    _basis = live_basis()
    _policy = live_same_bar_policy()
    env, vols = build_envelopes(
        signals, bars, basis=_basis, return_vol=True,
        vol_short=vol_short, vol_long=vol_long,
    )
    if vol_min is not None or vol_max is not None:
        keep = []
        for i, v in enumerate(vols):
            if v is None or not np.isfinite(v):
                continue
            if vol_min is not None and v < float(vol_min):
                continue
            if vol_max is not None and v > float(vol_max):
                continue
            keep.append(i)
        env = env[keep] if keep else np.empty((0, EXPIRY_BARS, 2))
        logger.info(
            "[dynamic:atr] scope=%s basis=%s policy=%s vol-filtered %d → %d (band [%s,%s])",
            scope, _basis, _policy, len(vols), len(env), vol_min, vol_max,
        )
    else:
        logger.info(
            "[dynamic:atr] scope=%s basis=%s policy=%s n=%d",
            scope, _basis, _policy, len(env),
        )
    pick = select_tp_sl(
        envelopes=env,
        same_bar_policy=_policy,
        tp_grid=[float(x) for x in cfg["tp_atr"]],
        sl_grid=[float(x) for x in cfg["sl_atr"]],
        min_rr=min_rr,
        max_expiry=float(cfg["max_expiry"]),
        cost_atr=float(cfg["cost_atr"]),
        min_signals=int(cfg["min_signals"]),
        # 2026-09-29 band rework — clamp to replay best region even if the
        # grid is widened later (tp=24 used to win the 10d walk).
        tp_atr_min=cfg.get("tp_atr_min"),
        tp_atr_max=cfg.get("tp_atr_max"),
        sl_atr_min=cfg.get("sl_atr_min"),
        sl_atr_max=cfg.get("sl_atr_max"),
        allow_best_effort=allow_best_effort,
    )
    # G2 (2026-10-12): never hand back None while a band is configured.
    # `select_tp_sl` returns None only when NO cell clears the min_rr
    # pre-filter — a band whose entire grid sits below the floor (git's XAGUSD
    # 0.99-1.07 and XAUAUD 1.23-1.34 were exactly that). Fall back to the
    # operator's deliberate band centre instead, because the light path reads
    # None as "keep the brick bracket" — the precise fallback G2 exists to
    # remove. Without this the invariant holds only for bands whose grid
    # clears the floor.
    if pick is None and allow_best_effort:
        pick = _band_center_pick(cfg)
    # Stage 5 (2026-10-08): does the pick's walked TP rate agree with the
    # platform's own measured bucket? shadow = log; live = reject (fall back
    # to the heavy pick). Fail-open on a cold bucket.
    if pick is not None and symbol:
        try:
            from regime_platform.core.feature_flags import (
                get_dynamic_atr_consistency_mode,
                get_dynamic_atr_consistency_tol,
            )
            _cmode = get_dynamic_atr_consistency_mode()
            if _cmode != "off":
                # walked TP rate = TP / decided, from the SAME envelope the
                # pick was chosen on.
                _pnl, _exp = walk_envelopes(env, pick.tp_atr, pick.sl_atr,
                                            same_bar_policy=_policy)
                _dec = int((~_exp).sum())
                _walk_tp = float(((_pnl > 0) & (~_exp)).sum() / _dec) if _dec else None
                from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for
                _bkt = _bucket_for(float(pick.tp_atr)) or ""
                _meas_tp, _meas_n = await fetch_bucket_measured_tp(symbol, _bkt)
                _ok, _reason = check_pick_reachability_consistency(
                    _walk_tp, _meas_tp, _meas_n,
                    tolerance=get_dynamic_atr_consistency_tol(),
                )
                if not _ok:
                    logger.warning(
                        "[dynamic:atr] %s CONSISTENCY %s — pick tp=%.2f sl=%.2f "
                        "contradicts measured bucket (%s)",
                        symbol, "REJECT" if _cmode == "live" else "shadow",
                        pick.tp_atr, pick.sl_atr, _reason,
                    )
                    if _cmode == "live":
                        _fb = _band_center_pick(cfg) if allow_best_effort else None
                        _PICK_CACHE[grid_fp] = (time.time(), _fb)
                        return _fb
                else:
                    logger.info(
                        "[dynamic:atr] %s consistency ok (%s walk_tp=%s meas_tp=%s n=%s)",
                        symbol, _reason,
                        f"{_walk_tp:.2f}" if _walk_tp is not None else "—",
                        f"{_meas_tp:.2f}" if _meas_tp is not None else "—", _meas_n,
                    )
        except Exception as exc:
            logger.debug("[dynamic:atr] %s consistency check failed: %s", symbol, exc)

    # Stage 6 (2026-10-09, worklog 20261009_WALKFORWARD_SPLIT.md): held-out
    # check that the chosen bracket generalises. select_tp_sl's e1>0 AND e2>0
    # is an in-sample split on the same data the bracket was optimised over, so
    # a RR=6 pick passes trivially. This measures the pick on the half it was
    # NOT chosen on. shadow = log; live = reject the overfit pick (heavy kept);
    # off = skip. Fail-open on thin folds (handled inside walk_forward_validate).
    if pick is not None and symbol:
        try:
            from regime_platform.core.feature_flags import (
                get_dynamic_atr_walkforward_mode,
                get_dynamic_atr_walkforward_min_fold_n,
                get_dynamic_atr_walkforward_min_oos,
            )
            _wf_mode = get_dynamic_atr_walkforward_mode()
            if _wf_mode != "off":
                _wf = walk_forward_validate(
                    env, pick.tp_atr, pick.sl_atr,
                    same_bar_policy=_policy,
                    cost_atr=float(cfg["cost_atr"]),
                    min_fold_n=get_dynamic_atr_walkforward_min_fold_n(),
                    min_oos_expectancy=get_dynamic_atr_walkforward_min_oos(),
                )
                if not _wf["ok"]:
                    logger.warning(
                        "[dynamic:atr] %s walk-forward %s — pick tp=%.2f sl=%.2f "
                        "is=%s oos=%s (is_n=%d oos_n=%d)",
                        symbol, "REJECT" if _wf_mode == "live" else "WOULD-REJECT",
                        pick.tp_atr, pick.sl_atr,
                        f"{_wf['is_exp']:+.3f}" if _wf["is_exp"] is not None else "—",
                        f"{_wf['oos_exp']:+.3f}" if _wf["oos_exp"] is not None else "—",
                        _wf["is_n"], _wf["oos_n"],
                    )
                    if _wf_mode == "live":
                        _fb = _band_center_pick(cfg) if allow_best_effort else None
                        _PICK_CACHE[grid_fp] = (time.time(), _fb)
                        return _fb
                else:
                    logger.info(
                        "[dynamic:atr] %s walk-forward ok (%s is=%s oos=%s "
                        "is_n=%d oos_n=%d)",
                        symbol, _wf["reason"],
                        f"{_wf['is_exp']:+.3f}" if _wf["is_exp"] is not None else "—",
                        f"{_wf['oos_exp']:+.3f}" if _wf["oos_exp"] is not None else "—",
                        _wf["is_n"], _wf["oos_n"],
                    )
        except Exception as exc:
            logger.debug("[dynamic:atr] %s walk-forward check failed: %s", symbol, exc)

    # Persist the pick (single source of truth) so the light path can read the
    # same geometry the heavy path just chose — no re-derivation. Best-effort.
    if pick is not None and symbol:
        await write_pick_to_store(symbol, pick, source="tiered_atr")

    _PICK_CACHE[grid_fp] = (time.time(), pick)
    return pick


# ─── ATR-anchored evidence promote (heavy candidate pool) ─────────────────

def should_promote_atr_candidates(
    *,
    selection_changed: bool,
    baseline_bucket: str | None,
    widened_bucket: str | None,
    pct_expired_by_bucket: dict[str, float | None] | None,
    risk_cap: float = DEFAULT_RISK_CAP,
) -> bool:
    """P0 evidence rule: allow ATR-anchored candidates into the live pick pool.

    Promote only when shadow comparison says the pick **changed** and the
    widened bucket is strictly better on long-lag (lower pct_expired) than
    baseline — or baseline is 4+ and widened is a tighter band under cap.
    Missing metrics → no promote (fail-closed for evidence).
    """
    if not selection_changed:
        return False
    exp = pct_expired_by_bucket or {}
    b_lag = exp.get(baseline_bucket) if baseline_bucket else None
    w_lag = exp.get(widened_bucket) if widened_bucket else None
    tighter = {"<0.5", "0.5-1", "1-2", "2-4"}
    # 2026-09-29 control-symbol fix: when the pick "changed" but the bucket
    # stays the same (often stuck on 4+ for gold/USDCAD), still admit ATR
    # candidates so tighter SL bands can enter the pool — basket bucket alone
    # does not capture SL-atr profitability (see 20260929_CONTROL_GEOMETRY).
    if selection_changed and baseline_bucket is not None and baseline_bucket == widened_bucket:
        return True
    if baseline_bucket == "4+" and widened_bucket in tighter:
        if w_lag is not None and float(w_lag) <= risk_cap:
            return True
        if widened_bucket in ("0.5-1", "1-2", "<0.5") and w_lag is None:
            # Tighter ATR band with unknown lag — still prefer vs known-bad 4+
            return b_lag is None or float(b_lag) > risk_cap
    if b_lag is not None and w_lag is not None:
        return float(w_lag) < float(b_lag) and float(w_lag) <= risk_cap
    return False


def compose_deadline_candidate_pool(
    grid_results: list,
    original_brick_sizes: set,
    atr_anchored_mode: str,
    *,
    promote: bool,
) -> list:
    """P0.3: pool fed into deadline-aware scoring.

    * atr_anchored_mode == live → full grid
    * shadow + evidence promote → full grid (ATR candidates eligible)
    * shadow without promote → original brick sizes only (leak-safe)
    * off → original sizes only
    """
    if atr_anchored_mode == "live" or (atr_anchored_mode != "off" and promote):
        return list(grid_results)
    return [c for c in grid_results if getattr(c, "brick_size", None) in original_brick_sizes]


async def fetch_bucket_long_lag(symbol: str) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Load (pct_expired_by_bucket, p90_by_bucket) from reachability for risk scores."""
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    exp: dict[str, float | None] = {}
    p90: dict[str, float | None] = {}
    if not settings.SUPABASE_URL:
        return exp, p90
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "distance_atr_bucket,pct_expired,p90_hours,n_stored",
                    "symbol": f"eq.{symbol}",
                    "signal": "eq.all",
                    "limit": "20",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        for r in rows if isinstance(rows, list) else []:
            if int(r.get("n_stored") or 0) < 3:
                continue
            b = r.get("distance_atr_bucket")
            if not b:
                continue
            exp[b] = r.get("pct_expired")
            p90[b] = r.get("p90_hours")
    except Exception as exc:
        logger.warning("tiered_atr fetch_bucket_long_lag %s: %s", symbol, exc)
    return exp, p90
