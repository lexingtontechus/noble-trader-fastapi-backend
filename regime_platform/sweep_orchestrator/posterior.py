"""
posterior.py — the empirical posterior + one price, one gate.
═══════════════════════════════════════════════════════════════════════════════

Posterior Barrier Model, Layers 2-3 (`worklog/20261014_BARRIER_MODEL_INTEGRATION_
MAP_AND_PHASES.md`; proposal §3-§4).

Layer 2 — every resolved outcome (``tp_hit`` / ``sl_hit`` / **``expired``** —
all three, survivorship closed by construction) increments a decay-weighted
Dirichlet on one cell key:

    cell = (symbol, side, vol_tier, distance_atr_bucket, deadline_class, era)

ONE key, everywhere — solver, pricing, and measurement read the same rows
(retires the ``signal=eq.all`` pooling and finally consumes migration 159's
side columns). The base measure of the Dirichlet is the analytic null
(``barrier.barrier_null``) at the cell's geometry, so a thin cell rests on
physics and a heavy cell speaks for itself — evidence-proportional
behaviour, no floors, no fail-open/fail-closed cliffs.

Layer 3 — the price and the gate:

    EV   = p_W*a - p_L*b + p_E*m - c              (barrier.ev_atr)
    gate = E[EV] - z_q * sd[EV] > 0               (ev_lower_bound, Dirichlet moments)

Plus the conservative Kelly sizing (Phase D): scale the platform's Kelly by
the EV lower bound's share of the EV mean — thin evidence → wide interval →
small size → tends to zero as evidence → zero.

Era discipline: cells keyed ``era='live'`` only accumulate outcomes resolved
after the resolver-basis cutover (``nt_calibration_cutover`` — the same
marker ``bracket_reachability`` / ``atr_reopt`` use). Pre-cutover rows are
SKIPPED at update time, so the era leak closes by construction rather than
by a read-time filter that can be forgotten.

All I/O is best-effort and never raises into the sweep tick or the resolver
(same contract as ``p_win_bracket.lookup_bracket_probs``). Pure math is
separated from I/O so tests can exercise the conjugacy without a database.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.barrier import (
    BarrierNull,
    barrier_null,
    barrier_null_from_prices,
    ev_lower_bound_z,
)
from regime_platform.sweep_orchestrator.bracket_reachability import (
    ATR_BUCKETS,
    _bucket_for,
    _fetch_cutover_at,
    _vol_tier,
)
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger(__name__)

# ─── Column whitelists (module-owned; one service, one schema) ──────────────

NT_CELL_POSTERIOR_COLUMNS: frozenset[str] = frozenset({
    "symbol", "side", "vol_tier", "distance_atr_bucket", "deadline_class",
    "era", "s_w", "s_l", "s_e", "n_w", "n_l", "n_e", "n_eff",
    "p_w", "p_l", "p_e", "p_w_null", "gap", "m_mtm",
    "alpha_w", "alpha_l", "alpha_e", "updated_at",
})

NT_PBM_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "id", "symbol", "ts", "signal", "source",
    "tp_atr", "sl_atr", "t_bars", "sigma",
    "p_w", "p_l", "p_e", "p_w_null", "gap", "n_eff",
    "ev_atr", "ev_lb_atr", "ev_lb_pass",
    "alpha_w", "alpha_l", "alpha_e",
    "bracket_bucket", "vol_tier", "deadline_class", "era",
    "created_at",
})

_PK = "symbol,side,vol_tier,distance_atr_bucket,deadline_class,era"

# Bucket centroid (geometric mid of the ATR bucket) for geometry-anchored
# reads that lack an explicit bracket.
_BUCKET_CENTROID: dict[str, float] = {
    label: (math.sqrt(lo * hi) if hi is not None else lo * 1.5)
    for lo, hi, label in ATR_BUCKETS
}

# Sentinel for "no stored row" — distinct from a stored zero.
_NO_ROW = object()


# ─── Flags (lazy import; defaults live in feature_flags._DEFAULTS) ──────────


def _flag(name: str, default: Any) -> Any:
    try:
        from regime_platform.core.feature_flags import _load
        return _load().get(name, default)
    except Exception:
        return default


def pbm_mode() -> str:
    """``off`` | ``shadow`` — master switch for stamps + posterior writes."""
    return str(_flag("pbm_mode", "shadow")).lower()


def pbm_k0() -> float:
    """Prior pseudo-counts: how much evidence moves a cell off physics."""
    try:
        return max(1.0, float(_flag("pbm_posterior_k0", 30.0)))
    except (TypeError, ValueError):
        return 30.0


def pbm_tau_days() -> float:
    """Exponential decay half-life basis for outcome weights (days)."""
    try:
        return max(1.0, float(_flag("pbm_posterior_tau_days", 14.0)))
    except (TypeError, ValueError):
        return 14.0


def pbm_sigma_atr() -> float:
    """Per-bar log-vol in ATR units for the analytic anchor (1.0 = the
    proposal's sigma = ATR_true_range / price, by construction)."""
    try:
        return max(0.05, float(_flag("pbm_sigma_atr", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def pbm_ev_bound_q() -> float:
    try:
        return min(max(float(_flag("pbm_ev_bound_q", 0.05)), 0.005), 0.25)
    except (TypeError, ValueError):
        return 0.05


def pbm_cost_atr(symbol: str | None, default_cost: float) -> float:
    """Per-symbol cost (proposal §4) with honest fallback to the band's
    ``cost_atr`` until the operator tunes the spread table."""
    table = _flag("pbm_cost_atr_by_symbol", {}) or {}
    if symbol and symbol in table:
        try:
            return max(0.0, float(table[symbol]))
        except (TypeError, ValueError):
            pass
    return float(default_cost)


def pbm_true_range_atr() -> bool:
    return bool(_flag("pbm_true_range_atr", False))


# ─── Cell keying ────────────────────────────────────────────────────────────


def _side_of(sig_or_signal: Any) -> str:
    s = str(sig_or_signal or "").lower()
    return "sell" if s.startswith("s") else "buy"


def deadline_class_of(sig: dict[str, Any]) -> str:
    """Coarse deadline class from the signal's dynamic-deadline stamps.

    ``soft_deadline_hours`` is stamped on nt_signal_sim by the light/heavy
    sweeps (``_build_signal_sim_payload``); absent/standard expiry rows
    key ``'standard'`` (EXPIRY_BARS = 48h on the 1h bar basis).
    """
    try:
        soft = sig.get("soft_deadline_hours")
        if soft is None:
            hard = sig.get("hard_deadline_hours")
            soft = hard
        if soft is None:
            return "standard"
        h = float(soft)
        if h <= 0:
            return "standard"
        if h <= 24.0:
            return "short"
        if h <= 72.0:
            return "medium"
        return "long"
    except (TypeError, ValueError):
        return "standard"


async def era_of(ts_iso: str | None) -> str:
    """``'live'`` for rows after the resolver-basis cutover, else ``'legacy'``.

    The cutover marker is read through ``bracket_reachability._fetch_cutover_at``
    (same single-row source every other era-aware consumer uses — the
    coherence point). Fail-open to ``'live'`` when the marker is unreadable:
    the update path then simply accumulates, exactly like today.
    """
    if not ts_iso:
        return "live"
    try:
        cut = await _fetch_cutover_at()
        if not cut:
            return "live"
        t = str(ts_iso).replace("Z", "+00:00")
        return "live" if t >= str(cut).replace("Z", "+00:00") else "legacy"
    except Exception:
        return "live"


# ─── Pure math: decay-weighted Dirichlet ────────────────────────────────────


def decay_lambda(age_days: float, tau_days: float) -> float:
    """Exponential decay factor ``2^(-age/tau)`` — weight of one outcome."""
    if age_days <= 0:
        return 1.0
    return 2.0 ** (-float(age_days) / max(1e-9, float(tau_days)))


def dirichlet_moments(alpha: Sequence[float]) -> tuple[tuple[float, float, float], float]:
    """Dirichlet means and total concentration from ``alpha = (aw, al, ae)``."""
    a = [max(0.0, float(x)) for x in alpha]
    a0 = sum(a)
    if a0 <= 0:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0), 0.0
    return ((a[0] / a0, a[1] / a0, a[2] / a0), a0)


def dirichlet_p_w_sd(alpha: Sequence[float]) -> float:
    """Normal-approx sd of the p_W marginal ``Beta(aw, a0-aw)``."""
    a = [max(0.0, float(x)) for x in alpha]
    a0 = sum(a)
    if a0 <= 1.0:
        return 0.5
    var = a[0] * (a0 - a[0]) / (a0 * a0 * (a0 + 1.0))
    return math.sqrt(max(0.0, var))


def ev_moments(
    alpha: Sequence[float],
    tp_atr: float,
    sl_atr: float,
    *,
    cost_atr: float,
    m_mtm: float = 0.0,
) -> tuple[float, float]:
    """Posterior mean and sd of the EV under the Dirichlet on (W, L, E).

    EV = p_W*a - p_L*b + p_E*m - c is LINEAR in the probability vector, so
    E[EV] and Var[EV] are exact Dirichlet moments — no simulation, no
    approximation beyond the normal quantile used for the bound itself.
    """
    a = [max(0.0, float(x)) for x in alpha]
    a0 = sum(a)
    if a0 <= 0:
        return 0.0, 0.0
    means = (a[0] / a0, a[1] / a0, a[2] / a0)
    v = (float(tp_atr), -float(sl_atr), float(m_mtm))
    ev_mean = (
        means[0] * v[0] + means[1] * v[1] + means[2] * v[2] - float(cost_atr)
    )
    var = 0.0
    for c in range(3):
        for d in range(3):
            cov = (
                (a[c] if c == d else 0.0) - a[c] * a[d] / a0
            ) / (a0 * (a0 + 1.0))
            var += v[c] * v[d] * cov
    return ev_mean, math.sqrt(max(0.0, var))


def ev_lower_bound(
    alpha: Sequence[float],
    tp_atr: float,
    sl_atr: float,
    *,
    cost_atr: float,
    m_mtm: float = 0.0,
    q: float | None = None,
) -> float:
    """THE gate: the q-quantile lower bound of the posterior EV.

    ``> 0`` qualifies; ``<= 0`` abstains. Uncertainty enters the decision
    directly — thin cells fail it not because a floor rejected them but
    because their interval still straddles zero. That is the entire point.
    """
    if q is None:
        q = pbm_ev_bound_q()
    ev_mean, ev_sd = ev_moments(
        alpha, tp_atr, sl_atr, cost_atr=cost_atr, m_mtm=m_mtm,
    )
    return ev_mean - ev_lower_bound_z(q) * ev_sd


def conservative_kelly(kelly_f: float, ev: float, ev_lb: float) -> float:
    """Phase D sizing: scale the platform Kelly by the bound's share of the
    mean. Monotone in evidence: as the interval tightens toward the mean the
    factor -> 1; as evidence thins the bound collapses and so does the size.

    Target form (documented, for the sizing worklog): full Kelly on the
    bound, f* = (p_w_lb*RR - (1 - p_w_lb)) / RR among resolved mass. This
    build ships the scaling form — it is smaller than either and composes
    with the existing variable-Kelly machinery without a second sizing
    authority.
    """
    if ev is None or ev <= 0:
        return 0.0
    if ev_lb is None or not math.isfinite(ev_lb):
        return 0.0
    return max(0.0, min(float(kelly_f), float(kelly_f) * (float(ev_lb) / float(ev))))


# ─── The posterior object ───────────────────────────────────────────────────


@dataclass
class CellPosterior:
    """One cell's Dirichlet state, anchored on the analytic null."""

    symbol: str
    side: str
    vol_tier: str
    bucket: str
    deadline_class: str
    era: str
    s_w: float = 0.0
    s_l: float = 0.0
    s_e: float = 0.0
    n_w: int = 0
    n_l: int = 0
    n_e: int = 0
    p_w_null: float | None = None   # analytic anchor at last write's geometry
    m_mtm: float = 0.0              # decayed E[MTM | expire], ATR units
    updated_at: str | None = None
    source: str = "stored"          # stored | null_prior
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def n_eff(self) -> float:
        return self.s_w + self.s_l + self.s_e

    def anchor(self, null: BarrierNull | None = None, k0: float | None = None) -> tuple[float, float, float]:
        """Dirichlet alpha = k0 * pi(geometry) + s, as (aw, al, ae).

        Re-anchoring at the CURRENT geometry keeps the prior's k0
        pseudo-counts consistent when pricing a bracket different from the
        ones that accumulated the evidence. Without an explicit null the
        stored pi cannot be decomposed into its (loss, expire) split, so the
        anchor degrades to uniform — diagnostics only, never pricing.
        """
        if null is None:
            return self._alpha_uniform(k0)
        k = pbm_k0() if k0 is None else k0
        return (
            k * null.p_win + self.s_w,
            k * null.p_loss + self.s_l,
            k * null.p_expire + self.s_e,
        )

    def _alpha_uniform(self, k0: float | None) -> tuple[float, float, float]:
        k = pbm_k0() if k0 is None else k0
        return (k / 3.0 + self.s_w, k / 3.0 + self.s_l, k / 3.0 + self.s_e)

    def means(self, null: BarrierNull | None = None) -> tuple[float, float, float]:
        m, _ = dirichlet_moments(self.anchor(null))
        return m

    def ci_p_w(self, null: BarrierNull | None = None) -> tuple[float, float]:
        aw, al, ae = self.anchor(null)
        mu, _ = dirichlet_moments((aw, al, ae))
        sd = dirichlet_p_w_sd((aw, al, ae))
        z = ev_lower_bound_z(0.05)
        return (max(0.0, mu[0] - z * sd), min(1.0, mu[0] + z * sd))

    def gap(self, null: BarrierNull | None = None) -> float:
        """selection gap = posterior p_W - null pi_W (entry-timing KPI, §8)."""
        if null is None:
            return 0.0
        mu, _ = dirichlet_moments(self.anchor(null))
        return mu[0] - null.p_win

    def as_log(self) -> str:
        return (
            f"cell=({self.symbol},{self.side},{self.vol_tier},{self.bucket},"
            f"{self.deadline_class},{self.era}) n_eff={self.n_eff:.1f} "
            f"raw=({self.n_w}/{self.n_l}/{self.n_e})"
        )


# ─── I/O: read / update / shadow-stamp ──────────────────────────────────────


async def read_cell(
    symbol: str,
    side: str,
    vol_tier: str,
    bucket: str,
    *,
    deadline_class: str = "standard",
    era: str = "live",
) -> CellPosterior | None:
    """Fetch one cell's posterior row. Best-effort; None when absent/off."""
    if not settings.SUPABASE_URL or pbm_mode() == "off":
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_cell_posterior"),
                headers=_headers(),
                params={
                    "select": ",".join(sorted(NT_CELL_POSTERIOR_COLUMNS)),
                    "symbol": f"eq.{symbol}",
                    "side": f"eq.{side}",
                    "vol_tier": f"eq.{vol_tier}",
                    "distance_atr_bucket": f"eq.{bucket}",
                    "deadline_class": f"eq.{deadline_class}",
                    "era": f"eq.{era}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                return None
            r = data[0]
            return CellPosterior(
                symbol=symbol, side=side, vol_tier=vol_tier, bucket=bucket,
                deadline_class=deadline_class, era=era,
                s_w=float(r.get("s_w") or 0.0),
                s_l=float(r.get("s_l") or 0.0),
                s_e=float(r.get("s_e") or 0.0),
                n_w=int(r.get("n_w") or 0),
                n_l=int(r.get("n_l") or 0),
                n_e=int(r.get("n_e") or 0),
                p_w_null=(float(r["p_w_null"]) if r.get("p_w_null") is not None else None),
                m_mtm=float(r.get("m_mtm") or 0.0),
                updated_at=r.get("updated_at"),
                source="stored",
            )
    except Exception as exc:
        logger.debug("posterior.read_cell %s failed: %s", symbol, exc)
        return None


async def cells_for_symbol(symbol: str) -> list[CellPosterior]:
    """All live-era cells for a symbol (geometry solve pools over sides)."""
    if not settings.SUPABASE_URL or pbm_mode() == "off":
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_cell_posterior"),
                headers=_headers(),
                params={
                    "select": ",".join(sorted(NT_CELL_POSTERIOR_COLUMNS)),
                    "symbol": f"eq.{symbol}",
                    "era": "eq.live",
                    "limit": "500",
                },
            )
            resp.raise_for_status()
            data = resp.json() or []
        out = []
        for r in data:
            out.append(CellPosterior(
                symbol=symbol,
                side=str(r.get("side") or "buy"),
                vol_tier=str(r.get("vol_tier") or "unknown"),
                bucket=str(r.get("distance_atr_bucket") or ""),
                deadline_class=str(r.get("deadline_class") or "standard"),
                era=str(r.get("era") or "live"),
                s_w=float(r.get("s_w") or 0.0),
                s_l=float(r.get("s_l") or 0.0),
                s_e=float(r.get("s_e") or 0.0),
                n_w=int(r.get("n_w") or 0),
                n_l=int(r.get("n_l") or 0),
                n_e=int(r.get("n_e") or 0),
                p_w_null=(float(r["p_w_null"]) if r.get("p_w_null") is not None else None),
                m_mtm=float(r.get("m_mtm") or 0.0),
                updated_at=r.get("updated_at"),
            ))
        return out
    except Exception as exc:
        logger.debug("posterior.cells_for_symbol %s failed: %s", symbol, exc)
        return []


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _row_to_cell(r: dict[str, Any]) -> CellPosterior:
    return CellPosterior(
        symbol=str(r.get("symbol") or ""),
        side=str(r.get("side") or "buy"),
        vol_tier=str(r.get("vol_tier") or "unknown"),
        bucket=str(r.get("distance_atr_bucket") or ""),
        deadline_class=str(r.get("deadline_class") or "standard"),
        era=str(r.get("era") or "live"),
        s_w=float(r.get("s_w") or 0.0),
        s_l=float(r.get("s_l") or 0.0),
        s_e=float(r.get("s_e") or 0.0),
        n_w=int(r.get("n_w") or 0),
        n_l=int(r.get("n_l") or 0),
        n_e=int(r.get("n_e") or 0),
        p_w_null=(float(r["p_w_null"]) if r.get("p_w_null") is not None else None),
        m_mtm=float(r.get("m_mtm") or 0.0),
        updated_at=r.get("updated_at"),
    )


async def update_cell(sig: dict[str, Any], resolved: dict[str, Any]) -> None:
    """The resolver hook — ONE outcome, ALL outcome classes, into the cell.

    Called from ``eod_resolver._resolve_one`` / ``_resolve_one_bars`` for
    ``tp_hit`` / ``sl_hit`` / ``expired`` alike (expired marked-to-market
    via its exit price — the resolver already settles it that way). This is
    where survivorship ends: the third outcome becomes data.

    Era-clean by construction: rows resolved before the cutover are skipped
    entirely (their cells would poison the live-basis posterior with
    close-walk-era outcomes).
    """
    if pbm_mode() == "off" or not settings.SUPABASE_URL:
        return
    symbol = sig.get("symbol")
    if not symbol:
        return
    outcome = str(resolved.get("outcome") or "")
    if outcome not in ("tp_hit", "sl_hit", "expired"):
        return
    try:
        entry = float(sig.get("entry_price"))
        tp = float(sig.get("take_profit"))
        sl = float(sig.get("stop_loss"))
        atr = float(sig.get("atr"))
    except (TypeError, ValueError):
        return
    if atr <= 0 or entry <= 0 or tp <= 0 or sl <= 0:
        return

    side = _side_of(sig.get("signal"))
    vol_tier = str(sig.get("bracket_vol_tier") or _vol_tier(sig.get("regime")))
    tp_atr = abs(tp - entry) / atr
    bucket = _bucket_for(tp_atr)
    if not bucket:
        return
    dclass = deadline_class_of(sig)
    era = await era_of(sig.get("ts"))
    if era != "live":
        return  # pre-cutover era: skip, don't contaminate

    # Analytic anchor at THIS signal's geometry (the covariate-aware prior).
    direction = "sell" if side == "sell" else "buy"
    null = barrier_null_from_prices(
        entry, tp, sl, atr=atr, direction=direction, sigma=pbm_sigma_atr(),
    )
    k0 = pbm_k0()
    tau = pbm_tau_days()

    onehot = (1.0, 0.0, 0.0) if outcome == "tp_hit" else (
        (0.0, 1.0, 0.0) if outcome == "sl_hit" else (0.0, 0.0, 1.0)
    )
    # MTM at expiry, ATR units, direction-signed (0 for decided outcomes).
    m = 0.0
    if outcome == "expired":
        exit_px = resolved.get("exit_price")
        if exit_px is not None:
            try:
                d = -1.0 if side == "sell" else 1.0
                m = (float(exit_px) - entry) * d / atr
            except (TypeError, ValueError):
                m = 0.0

    now = datetime.now(timezone.utc)

    # Fetch-then-upsert (same shape as record_trend_probs_gate_shadow).
    prev: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_cell_posterior"),
                headers=_headers(),
                params={
                    "select": ",".join(sorted(NT_CELL_POSTERIOR_COLUMNS)),
                    "symbol": f"eq.{symbol}",
                    "side": f"eq.{side}",
                    "vol_tier": f"eq.{vol_tier}",
                    "distance_atr_bucket": f"eq.{bucket}",
                    "deadline_class": f"eq.{dclass}",
                    "era": "eq.live",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                prev = data[0]
    except Exception as exc:
        logger.debug("posterior.update_cell fetch %s failed: %s", symbol, exc)
        return

    updated_prev = _parse_ts(prev.get("updated_at"))
    age_days = max(0.0, (now - updated_prev).total_seconds() / 86400.0) if updated_prev else 0.0
    lam = decay_lambda(age_days, tau)

    s = (
        float(prev.get("s_w") or 0.0) * lam + onehot[0],
        float(prev.get("s_l") or 0.0) * lam + onehot[1],
        float(prev.get("s_e") or 0.0) * lam + onehot[2],
    )
    n = (
        int(prev.get("n_w") or 0) + (1 if outcome == "tp_hit" else 0),
        int(prev.get("n_l") or 0) + (1 if outcome == "sl_hit" else 0),
        int(prev.get("n_e") or 0) + (1 if outcome == "expired" else 0),
    )
    # decayed MTM accumulator: s_e * m_prev * lam + m  (weighted mean below)
    m_s = float(prev.get("s_e") or 0.0) * lam * float(prev.get("m_mtm") or 0.0) + (
        m if outcome == "expired" else 0.0
    )
    m_mtm = m_s / s[2] if s[2] > 1e-9 else 0.0

    alpha = (k0 * null.p_win + s[0], k0 * null.p_loss + s[1], k0 * null.p_expire + s[2])
    means, _ = dirichlet_moments(alpha)

    row = {
        "symbol": symbol, "side": side, "vol_tier": vol_tier,
        "distance_atr_bucket": bucket, "deadline_class": dclass, "era": "live",
        "s_w": round(s[0], 6), "s_l": round(s[1], 6), "s_e": round(s[2], 6),
        "n_w": n[0], "n_l": n[1], "n_e": n[2],
        "n_eff": round(s[0] + s[1] + s[2], 6),
        "p_w": round(means[0], 6), "p_l": round(means[1], 6), "p_e": round(means[2], 6),
        "p_w_null": round(null.p_win, 6),
        "gap": round(means[0] - null.p_win, 6),
        "m_mtm": round(m_mtm, 6),
        "alpha_w": round(alpha[0], 6), "alpha_l": round(alpha[1], 6),
        "alpha_e": round(alpha[2], 6),
        "updated_at": now.isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_cell_posterior')}?on_conflict={_PK}",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=[row],
            )
            if not resp.is_success:
                logger.error(
                    "posterior.update_cell %s upsert HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        logger.error("posterior.update_cell %s upsert failed: %s", symbol, exc)


async def record_pbm_shadow(
    symbol: str,
    ts_iso: str,
    signal: str,
    *,
    source: str | None,
    null: BarrierNull,
    alpha: tuple[float, float, float],
    tp_atr: float,
    sl_atr: float,
    cost_atr: float,
    m_mtm: float,
    bucket: str | None,
    vol_tier: str | None,
    deadline_class: str | None,
    era: str | None,
) -> dict[str, Any] | None:
    """Phase A shadow stamp — the prediction ledger, one row per emitted
    signal (the same population ``p_win_bracket`` stamps cover). Returns the
    stamp dict so callers can log it; never raises."""
    if pbm_mode() == "off" or not settings.SUPABASE_URL:
        return None
    means, a0 = dirichlet_moments(alpha)
    gap = means[0] - null.p_win
    ev_mean, ev_sd = ev_moments(
        alpha, tp_atr, sl_atr, cost_atr=cost_atr, m_mtm=m_mtm,
    )
    ev_lb = ev_mean - ev_lower_bound_z(pbm_ev_bound_q()) * ev_sd
    row = {
        "symbol": symbol,
        "ts": ts_iso,
        "signal": _side_of(signal),
        "source": source,
        "tp_atr": round(float(tp_atr), 6),
        "sl_atr": round(float(sl_atr), 6),
        "t_bars": int(null.t_bars),
        "sigma": round(float(null.sigma), 6),
        "p_w": round(means[0], 6),
        "p_l": round(means[1], 6),
        "p_e": round(means[2], 6),
        "p_w_null": round(null.p_win, 6),
        "gap": round(gap, 6),
        "n_eff": round(float(a0), 6),
        "ev_atr": round(ev_mean, 6),
        "ev_lb_atr": round(ev_lb, 6),
        "ev_lb_pass": bool(ev_lb > 0.0),
        "alpha_w": round(alpha[0], 6),
        "alpha_l": round(alpha[1], 6),
        "alpha_e": round(alpha[2], 6),
        "bracket_bucket": bucket,
        "vol_tier": vol_tier,
        "deadline_class": deadline_class,
        "era": era,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_pbm_shadow"),
                headers={**_headers(), "Content-Type": "application/json"},
                json=[row],
            )
            if not resp.is_success:
                logger.debug(
                    "pbm shadow stamp %s HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
                return None
        return row
    except Exception as exc:
        logger.debug("pbm shadow stamp %s failed: %s", symbol, exc)
        return None


# ─── Geometry tilt (Phase C): cells -> posterior probability field ──────────


def bucket_tilt(
    cells: Sequence[CellPosterior],
    *,
    t_bars: int = 48,
    sigma: float | None = None,
    k_pool: float | None = None,
) -> Callable[[float, float], tuple[float, float, float]]:
    """Build the (a, b) -> (p_W, p_L, p_E) field for the continuous solve.

    The analytic null carries the fine geometry structure; the pooled cell
    evidence supplies two per-bucket multipliers, both clamped to sane
    ranges and shrunk toward 1 with the same k0 discipline:

      f_hat  — win-tilt within resolved mass (logit shift, preserves
               normalization for ANY f_hat — the honest way to multiply an
               odds ratio onto a probability field)
      r_hat  — resolve-rate multiplier vs the null (p_E is data too: the
               platform's measured resolve mass is the expiry KPI)

    Cold book (no cells): the field IS the null — physics, not prior.
    """
    sig = pbm_sigma_atr() if sigma is None else float(sigma)
    kp = pbm_k0() if k_pool is None else float(k_pool)

    # pool per bucket across sides/deadline classes (geometry is shared)
    per_bucket: dict[str, dict[str, float]] = {}
    for c in cells:
        if not c.bucket or c.era != "live":
            continue
        b = per_bucket.setdefault(
            c.bucket, {"s_w": 0.0, "s_l": 0.0, "s_e": 0.0, "m": 0.0, "s_e_m": 0.0},
        )
        b["s_w"] += c.s_w
        b["s_l"] += c.s_l
        b["s_e"] += c.s_e
        b["s_e_m"] += c.s_e * c.m_mtm

    def _bucket_stats(a: float) -> tuple[float, float, float] | None:
        label = _bucket_for(a) or ""
        b = per_bucket.get(label)
        if not b or label not in _BUCKET_CENTROID:
            return None
        s_w, s_l, s_e = b["s_w"], b["s_l"], b["s_e"]
        s_tot = s_w + s_l + s_e
        if s_tot <= 0:
            return None
        centroid = _BUCKET_CENTROID[label]
        # a representative sl shape for the anchor: the bucket's own
        # realized RR is unknown at solve time; anchor on a 2:1 shape,
        # degraded honestly by the k0 shrinkage.
        anchor = barrier_null(centroid, max(0.25, centroid / 2.0), t_bars=t_bars, sigma=sig)
        # win tilt f_raw (posterior vs null win share, resolved mass).
        # NOTE: ``is None`` checks, never ``or`` — a MEASURED 0.0 (dead
        # bucket, 0 wins in n resolved) is the strongest signal this
        # machinery can carry; it must not be flattened to "no evidence".
        emp_w = (s_w / (s_w + s_l)) if (s_w + s_l) > 0 else None
        f_raw = (emp_w / anchor.p_win) if emp_w is not None and anchor.p_win > 1e-6 else None
        # resolve tilt r_raw
        emp_r = (s_w + s_l) / s_tot
        r_raw = emp_r / (anchor.p_win + anchor.p_loss) if (anchor.p_win + anchor.p_loss) > 1e-6 else None
        m = (b["s_e_m"] / s_e) if s_e > 1e-9 else 0.0
        return (
            1.0 if f_raw is None else f_raw,
            1.0 if r_raw is None else r_raw,
            m,
        )

    def tilt(a: float, b: float) -> tuple[float, float, float]:
        n = barrier_null(a, b, t_bars=t_bars, sigma=sig)
        stats = _bucket_stats(a)
        if stats is None:
            return (n.p_win, n.p_loss, n.p_expire)
        f_raw, r_raw, m = stats
        # shrink toward 1 with the cell's effective sample: thin evidence
        # cannot tilt physics far. n_eff proxy = s_w+s_l (resolved evidence).
        n_res = per_bucket.get(_bucket_for(a) or "")
        if n_res is None:
            return (n.p_win, n.p_loss, n.p_expire)
        n_eff = n_res["s_w"] + n_res["s_l"]
        w = n_eff / (n_eff + kp)
        f_hat = 1.0 + w * (min(3.0, max(1.0 / 3.0, f_raw)) - 1.0)
        r_hat = 1.0 + w * (min(2.0, max(0.5, r_raw)) - 1.0)
        # apply: resolve mass scales, win odds shift by f_hat (logit tilt —
        # preserves normalization for ANY f_hat, unlike a raw multiplier)
        resolve = min(0.999, (n.p_win + n.p_loss) * r_hat)
        if n.p_loss > 1e-9:
            odds = (n.p_win / n.p_loss) * f_hat
            p_w_r = odds / (1.0 + odds)
        else:
            p_w_r = min(1.0, f_hat)
        p_w = resolve * p_w_r
        p_l = resolve * (1.0 - p_w_r)
        p_e = 1.0 - resolve
        # stash m for diagnostics callers (EV solve currently prices m=0;
        # bucket-specific MTM enters with the Phase C acceptance replay)
        tilt.last_m = m  # type: ignore[attr-defined]
        return (p_w, p_l, p_e)

    tilt.last_m = 0.0  # type: ignore[attr-defined]
    return tilt


# ─── Rebuild (decay + hierarchical pooling) ─────────────────────────────────


async def rebuild_posteriors(symbols: Sequence[str] | None = None) -> dict[str, int]:
    """Decay rebuild + hierarchical pooling pass.

    Decays every cell's sufficient stats to NOW (so reads between outcomes
    see honest effective sample sizes), recomputes means/gap, and pools the
    resolved win share per symbol — a thin cell's derived posterior inherits
    its symbol's behaviour, a heavy cell speaks for itself, both through the
    same ``k0`` shrinkage weight (partial pooling). Pooling is applied to
    the DERIVED alphas only: the stored sufficient stats stay raw+decayed,
    so repeated rebuilds are idempotent. Called by the weekly
    bracket-reachability job (Phase A); cadence becomes event-driven in
    Phase C.
    """
    if not settings.SUPABASE_URL or pbm_mode() == "off":
        return {"rows": 0}
    now = datetime.now(timezone.utc)
    tau = pbm_tau_days()
    k0 = pbm_k0()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            params: dict[str, Any] = {
                "select": ",".join(sorted(NT_CELL_POSTERIOR_COLUMNS)),
                "era": "eq.live",
                "limit": "5000",
            }
            if symbols:
                params["symbol"] = f"in.({','.join(symbols)})"
            resp = await client.get(
                _url("nt_cell_posterior"), headers=_headers(), params=params,
            )
            resp.raise_for_status()
            rows = resp.json() or []
    except Exception as exc:
        logger.error("posterior.rebuild fetch failed: %s", exc)
        return {"rows": 0}

    cells = [_row_to_cell(r) for r in rows]
    # per-symbol pooled resolved win share: sum(s_w) / sum(s_w + s_l)
    win_acc: dict[str, float] = {}
    res_acc: dict[str, float] = {}
    for c in cells:
        s_res = c.s_w + c.s_l
        if s_res > 0:
            win_acc[c.symbol] = win_acc.get(c.symbol, 0.0) + c.s_w
            res_acc[c.symbol] = res_acc.get(c.symbol, 0.0) + s_res
    w_sym = {
        sym: win_acc[sym] / res_acc[sym]
        for sym in res_acc
        if res_acc[sym] > 0
    }
    tot_win = sum(win_acc.values())
    tot_res = sum(res_acc.values())
    w_global = (tot_win / tot_res) if tot_res > 0 else None

    out_rows = []
    for c in cells:
        updated_prev = _parse_ts(c.updated_at)
        age_days = (
            max(0.0, (now - updated_prev).total_seconds() / 86400.0)
            if updated_prev else 0.0
        )
        lam = decay_lambda(age_days, tau)
        s = (c.s_w * lam, c.s_l * lam, c.s_e * lam)
        m_s = c.s_e * lam * c.m_mtm
        m_mtm = m_s / s[2] if s[2] > 1e-9 else 0.0
        # anchor from stored null win share; the loss/expire split of the
        # anchor uses the canonical 70/30 convention (update_cell stores
        # exact alphas from the true geometry; the rebuild's decay refresh
        # only needs a stable, honest anchor).
        pw_null = c.p_w_null if c.p_w_null is not None else 1.0 / 3.0
        # pooled resolved win share — READ-SIDE ONLY (derived alpha), so the
        # rebuild stays idempotent. Thin cell -> symbol behaviour; heavy cell
        # -> its own evidence; no resolved evidence -> anchor carries it.
        s_res = s[0] + s[1]
        w_pool = w_sym.get(c.symbol, w_global)
        if s_res > 1e-9 and w_pool is not None:
            w_hat = (s_res * (s[0] / s_res) + k0 * float(w_pool)) / (s_res + k0)
            alpha = (
                k0 * pw_null + s_res * w_hat,
                k0 * (1.0 - pw_null) * 0.7 + s_res * (1.0 - w_hat),
                k0 * (1.0 - pw_null) * 0.3 + s[2],
            )
        else:
            alpha = (
                k0 * pw_null + s[0],
                k0 * (1.0 - pw_null) * 0.7 + s[1],
                k0 * (1.0 - pw_null) * 0.3 + s[2],
            )
        means, _ = dirichlet_moments(alpha)
        out_rows.append({
            "symbol": c.symbol, "side": c.side, "vol_tier": c.vol_tier,
            "distance_atr_bucket": c.bucket, "deadline_class": c.deadline_class,
            "era": "live",
            "s_w": round(s[0], 6), "s_l": round(s[1], 6), "s_e": round(s[2], 6),
            "n_w": c.n_w, "n_l": c.n_l, "n_e": c.n_e,
            "n_eff": round(sum(s), 6),
            "p_w": round(means[0], 6), "p_l": round(means[1], 6),
            "p_e": round(means[2], 6),
            "p_w_null": round(pw_null, 6),
            "gap": round(means[0] - pw_null, 6),
            "m_mtm": round(m_mtm, 6),
            "alpha_w": round(alpha[0], 6), "alpha_l": round(alpha[1], 6),
            "alpha_e": round(alpha[2], 6),
            "updated_at": now.isoformat(),
        })
    if out_rows:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{_url('nt_cell_posterior')}?on_conflict={_PK}",
                    headers={
                        **_headers(),
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=out_rows,
                )
                if not resp.is_success:
                    logger.error(
                        "posterior.rebuild upsert HTTP %d — %s",
                        resp.status_code, resp.text[:200],
                    )
        except Exception as exc:
            logger.error("posterior.rebuild upsert failed: %s", exc)
    return {"rows": len(out_rows)}
