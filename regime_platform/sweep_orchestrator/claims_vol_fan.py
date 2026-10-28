"""Signal claims layer — Phase D: the recalibrated vol fan (``vol_fan`` claims).

Worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4 Phase D + migration 220.

Phase A registered the mandatory BRACKET claim with the house ATR
normal-approximation fan (``source='atr_normal'``) — deliberately a
**baseline/control arm**: zero drift, ``√h`` scaling, one σ from ATR. Phase D
adds the arm that baseline must beat: a second claim per qualified signal,
``kind='vol_fan'`` + ``source='vol_backtest_recalib'``, whose fan comes from
the vol_backtest GARCH family (GARCH(1,1)-N / GJR-t / EGARCH-t, commit
a64a624) fitted on the qualification window, then recalibrated with the
recalib.py MZ+k evidence map.

Same contract as Phase A/B, unchanged:

  * additive + flag-gated — ``claims_vol_fan_mode: off`` (default; the design
    doc's "research-only, as today") AND ``claims_mode != off``;
  * price space at the boundary — log-space stays inside the fan builder,
    the row's ``fan`` is prices, q05..q95 on the same 7 lab levels, hourly
    ``h = 1..48`` = the settlement window (scored by claims_resolver with
    ZERO resolver changes — the kernels are kind-agnostic);
  * one settlement resolver — the vol_fan claim is scored by the same
    ``score_claims_for_signal`` hook on the same bars as its signal;
  * immutable claims — one POST at registration, ``unique (signal_id, kind)``
    keeps the bracket and vol_fan rows siblings, never replacements;
  * MT5 excluded — the optional broadcast re-publish follows the Phase B
    ``claims_publish`` gate and the ``p_clef_wait`` convention verbatim.

The fan math (the MZ+k transfer, stated honestly)
────────────────────────────────────────────────
The GARCH arm's ``_fit_forecast_variance`` yields the per-horizon conditional
variance path ``var_path[h-1]`` (original return units, unit-variance rescale
convention). The cumulative vol at horizon h is
``σ_raw(h) = sqrt(Σ_{i≤h} var_path[i-1])`` — which at the recalib cell
horizons is EXACTLY the ``rv`` aggregate the cells were fitted on
(``rv_H = sqrt(Σ var)``), so the MZ center applies to the same quantity:

    F   = σ_raw(H)                    (the arm's own cumulative forecast)
    F'  = exp(a) · F^b                (MZ center, recalib.py step 1)
    m(H) = k · F'/F = k · exp(a) · F^(b-1)

The k transfer is a modeling choice, documented as one: recalib.py fitted k
against the arm's claimed RV-scale interval; the price fan's log-spread at
horizon h IS ``σ(h)``, and the same normal-approx understatement the k factor
measures (t-innovation tails + parameter uncertainty, both horizon-growing)
hits the fan's z-quantile spread. The claims ledger is the instrument that
MEASURES whether the transfer worked: coverage50/80/90 + CRPS of the vol_fan
fan vs realized closes is exactly the design doc §6 acceptance criterion
("[0.80, 0.95] Wilson band"). No stacked corrections: normal z-quantiles +
one multiplicative m — never t-quantiles AND k.

Between cell horizons (cells exist only at the backtest's discrete H),
``log m`` interpolates linearly in h; below the smallest cell horizon m = 1
(no evidence below 6h — identity, never extrapolated). The row's
``calib_level`` stamps the evidence ladder level that produced m at the
terminal horizon (cell | pooled_symbol_arm | global_arm | none), so a fan
built without a calibration artifact registers honestly as ``none``.

Determinism: GARCH/GJR forecasts are analytic; EGARCH's is the mean of SEEDED
simulations (seed=7) — the same window always produces the same fan, which is
what claim immutability requires.

Fail-open everywhere (Phase A convention): arch not installed, too few bars,
fetch failure, unfittable window, missing artifact, insert error ⇒ no claim,
never an exception, never a sweep impact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.claims import (
    _Z,
    DEFAULT_REF_H,
    FAN_QUANTILES,
    NT_SIGNAL_CLAIMS_COLUMNS,
    _claims_on,
    _flag,
    _shift_iso_hours,
    build_bracket_ref,
)
from regime_platform.sweep_orchestrator.settlement_contract import EXPIRY_BARS
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.claims")

# ─── Columns (module-owned superset: Phase A whitelist + Phase D stamps) ─────

VOL_FAN_CLAIM_COLUMNS: frozenset[str] = NT_SIGNAL_CLAIMS_COLUMNS | {
    "arm",        # which GARCH-family arm produced the fan (migration 220)
    "calib_level",  # evidence-ladder level of the terminal-horizon cell (220)
}

# ─── Fan arm plumbing (lazy: arch/numpy stay optional for the sweep) ────────

# The vol_backtest package is deliberately OUTSIDE regime_platform (research
# tooling); Phase D bridges it into the claims layer by design-doc decree —
# through a lazy import so the sweep path never pays for it unless the flag
# is on, and an absent `arch` degrades to a no-op claim (never an exception).
VOL_FAN_MIN_BARS = 720          # _ArchArmBase floor; the arm refuses less
VOL_FAN_FETCH_HOURS = 2160      # the heavy sweep's own 90d hourly window

_ARM_NAMES = ("garch", "gjr", "egarch")


def claims_vol_fan_mode() -> str:
    """``off`` | ``shadow`` — Phase D vol_fan claim registration.

    AND-gated on top of ``claims_mode``: the bracket claim (Phase A) and this
    flag together decide whether a vol_fan claim registers. ``off`` (shipped,
    the design doc's "research-only, as today") = full no-op. A ``live``
    value behaves as ``shadow`` until a later phase gives it meaning.
    """
    return str(_flag("claims_vol_fan_mode", "off")).lower()


def claims_vol_fan_arm() -> str:
    """``garch`` | ``gjr`` | ``egarch`` — the PRE-REGISTERED fan arm.

    Defaults to ``egarch`` on the lab's measured prior (EGARCH-t
    best-in-family, skillRatio 0.944 vs Log-HAR; GARCH/GJR worse at long
    horizons on metals). Setting this flag IS the pre-registration: the
    claims ledger records ``arm`` per row, so arm switching is auditable,
    never silent cherry-picking.
    """
    arm = str(_flag("claims_vol_fan_arm", "egarch")).lower()
    return arm if arm in _ARM_NAMES else "egarch"


def claims_vol_fan_calib_path() -> Path:
    """Repo-relative path of the recalib.json artifact the fan consumes.

    ``parents[2]`` = the repo root (``regime_platform/sweep_orchestrator/``
    sits two directories deep); absolute paths pass through untouched.
    """
    raw = str(_flag("claims_vol_fan_calib_path", "config/claims_vol_fan_recalib.json"))
    p = Path(raw)
    return p if p.is_absolute() else Path(__file__).resolve().parents[2] / raw


# ─── Calibration map loader (mtime-cached, fail-open to identity) ───────────

# {(symbol, horizon, arm) -> {"a", "b", "k", "level"}} — the recalib.json
# cells, thinned to what the fan builder needs. Empty dict = identity
# calibration (honest level='none'), never an error.
_CALIB_CACHE: dict[str, Any] = {"mtime": None, "checked": 0.0, "cells": {}}


def _cell_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        level = str(row.get("level") or "none")
        a = float(row.get("mz_a") or 0.0)
        b = float(row.get("mz_b") or 1.0)
        k = row.get("spread_k")
        k = 1.0 if k is None else float(k)
        horizon = int(row.get("horizon_bars"))
        symbol = str(row.get("symbol") or "").upper()
        arm = str(row.get("arm") or "").lower()
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(k) and k > 0):
        return None
    return {
        "symbol": symbol, "horizon": horizon, "arm": arm,
        "a": a, "b": b, "k": k, "level": level,
    }


def load_calib_cells(
    path: Path | None = None, *, max_age_s: float = 30.0
) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Read the recalib.json artifact into {(symbol, horizon, arm) -> cell}.

    Mtime-cached on the default path (the feature_flags hot-reload
    convention): an edited or re-minted artifact is picked up within
    ``max_age_s`` without a restart. An explicit ``path`` is a one-shot read
    (tests/tools). Missing/unreadable/corrupt artifact ⇒ ``{}`` — the fan
    registers with identity calibration (``calib_level='none'``), it never
    blocks.
    """
    if path is not None:
        return _read_calib_cells(Path(path))
    now = time.monotonic()
    if now - float(_CALIB_CACHE["checked"]) < max_age_s:
        return _CALIB_CACHE["cells"]
    _CALIB_CACHE["checked"] = now
    p = claims_vol_fan_calib_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _CALIB_CACHE.update({"mtime": None, "cells": {}})
        return {}
    if mtime == _CALIB_CACHE["mtime"] and _CALIB_CACHE["cells"]:
        return _CALIB_CACHE["cells"]
    cells = _read_calib_cells(p)
    _CALIB_CACHE.update({"mtime": mtime, "cells": cells})
    return cells


def _read_calib_cells(p: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Parse one recalib.json artifact (any error ⇒ {})."""
    try:
        doc = json.loads(p.read_text())
        rows = doc.get("cells") or []
        cells: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            cell = _cell_from_row(row)
            if cell is None or cell["level"] == "none":
                continue
            cells[(cell["symbol"], cell["horizon"], cell["arm"])] = cell
        return cells
    except Exception as exc:
        logger.debug("claims vol_fan: calib artifact unreadable (%s): %s", p, exc)
        return {}


# ─── The per-horizon multiplier (pure) ──────────────────────────────────────


def _level_rank(level: str) -> int:
    return {"cell": 3, "pooled_symbol_arm": 2, "global_arm": 1}.get(level, 0)


def _cell_multiplier(cell: dict[str, Any], sigma_raw_at_h: float) -> float:
    """m = k · exp(a) · F^(b−1) at one cell horizon (see module docstring)."""
    f = float(sigma_raw_at_h)
    if not (math.isfinite(f) and f > 0):
        return 1.0
    try:
        m = cell["k"] * math.exp(cell["a"]) * (f ** (cell["b"] - 1.0))
    except (OverflowError, ValueError):
        return 1.0
    if not (math.isfinite(m) and m > 0):
        return 1.0
    return min(max(m, 0.05), 50.0)  # safety clamp, recalib.py's guard spirit


def vol_fan_multiplier(
    cells: dict[tuple[str, int, str], dict[str, Any]],
    symbol: str,
    arm: str,
    h: int,
    sigma_raw_at_h: float,
) -> tuple[float, str]:
    """The multiplicative m for fan row h, with its evidence level.

    Exact cell at h → cell multiplier. Between two cell horizons → log m
    linear in h (interpolated; level = the WEAKER bracketing cell, honest).
    Below the smallest / above the largest cell horizon → identity below (no
    evidence under 6h, never extrapolated downward), clamped-to-nearest
    above (the terminal horizon is a cell in practice).
    """
    sym = str(symbol or "").upper()
    arm = str(arm or "").lower()
    hi = max(int(h), 1)
    mine = {H: c for (s, H, a), c in cells.items() if s == sym and a == arm}
    if not mine:
        return 1.0, "none"
    if hi in mine:
        c = mine[hi]
        return _cell_multiplier(c, sigma_raw_at_h), c["level"]
    horizons = sorted(mine)
    if hi < horizons[0]:
        return 1.0, "none"
    if hi > horizons[-1]:
        c = mine[horizons[-1]]
        return _cell_multiplier(c, sigma_raw_at_h), c["level"]
    lo_h, hi_h = horizons[0], horizons[-1]
    for a, b in zip(horizons, horizons[1:]):
        if a <= hi <= b:
            lo_h, hi_h = a, b
            break
    c_lo, c_hi = mine[lo_h], mine[hi_h]
    if hi_h == lo_h:  # pragma: no cover — guarded by the sorted walk above
        return _cell_multiplier(c_lo, sigma_raw_at_h), c_lo["level"]
    m_lo = _cell_multiplier(c_lo, sigma_raw_at_h)
    m_hi = _cell_multiplier(c_hi, sigma_raw_at_h)
    w = (hi - lo_h) / (hi_h - lo_h)
    try:
        m = math.exp((1.0 - w) * math.log(m_lo) + w * math.log(m_hi))
    except (OverflowError, ValueError):
        m = 1.0
    level = min((c_lo["level"], c_hi["level"]), key=_level_rank)
    return (m if math.isfinite(m) and m > 0 else 1.0), level


# ─── Fan construction (sync, CPU-bound — call via asyncio.to_thread) ────────


def _arm_instance(arm_name: str):
    """Construct the pre-registered arm; None when arch is unavailable."""
    try:
        from vol_backtest.arms import EGARCH11T, GARCH11, GJRGARCH11T
    except Exception as exc:
        logger.debug("claims vol_fan: vol_backtest.arms import failed: %s", exc)
        return None
    cls = {"garch": GARCH11, "gjr": GJRGARCH11T, "egarch": EGARCH11T}[arm_name]
    try:
        arm = cls()
    except Exception as exc:
        logger.debug("claims vol_fan: arm %s construction failed: %s", arm_name, exc)
        return None
    if not getattr(arm, "available", False):
        logger.debug("claims vol_fan: arm %s unavailable: %s", arm_name, getattr(arm, "unavailable_reason", ""))
        return None
    return arm


def _calendar_filter(prices: list[float], timestamps: list[Any] | None, symbol: str) -> list[float]:
    """Apply the vol_backtest calendar mask when timestamps are supplied.

    The recalib cells were fitted on calendar-filtered series (the harness's
    ``respect_calendar=True``), so the live fit filters the same closed bars
    when it can. No timestamps (the heavy path's in-scope window) ⇒ fit
    as-is: the TDVA hourly feed already omits most closed-session bars, and
    a wrong-window filter would be worse than an honest unfiltered fit.
    """
    if not timestamps or len(timestamps) != len(prices):
        return list(prices)
    try:
        import numpy as np

        from vol_backtest.realized import calendar_mask

        ts = np.asarray(list(timestamps), dtype="datetime64[s]")
        mask = calendar_mask(ts, symbol)
        kept = [p for p, keep in zip(prices, mask) if bool(keep)]
        return kept if len(kept) >= VOL_FAN_MIN_BARS + 1 else list(prices)
    except Exception as exc:
        logger.debug("claims vol_fan: calendar filter skipped (%s): %s", symbol, exc)
        return list(prices)


def build_garch_price_fan(
    entry_price: float,
    prices: list[float],
    *,
    symbol: str,
    arm_name: str,
    cells: dict[tuple[str, int, str], dict[str, Any]] | None = None,
    timestamps: list[Any] | None = None,
    horizon_h: int = EXPIRY_BARS,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """The price-space vol fan from the GARCH-family arm (sync, CPU-bound).

    Returns ``(fan_rows, meta)`` — ``fan_rows`` exactly the Phase A schema
    (``{h, q05..q95}``, price space, 6dp) so claims_resolver scores it with
    zero changes; ``meta`` = ``{arm, fit_bars, sigma_h48, m_h48, calib_level,
    horizon_h}`` for the row stamps. None when the arm is unavailable, the
    window too short, or the forecast degenerate — never a partial fan.
    """
    try:
        entry = float(entry_price)
        px = [float(p) for p in (prices or [])]
    except (TypeError, ValueError):
        return None
    if not (entry > 0) or len(px) < VOL_FAN_MIN_BARS + 1:
        return None
    horizon_h = int(horizon_h)
    if horizon_h < 1:
        return None

    arm = _arm_instance(arm_name)
    if arm is None:
        return None

    px = _calendar_filter(px, timestamps, symbol)

    try:
        import numpy as np

        from vol_backtest.realized import log_returns
    except Exception as exc:
        logger.debug("claims vol_fan: numpy/vol_backtest import failed: %s", exc)
        return None

    r = log_returns(np.asarray(px, dtype=float))
    if len(r) < VOL_FAN_MIN_BARS:
        return None
    var_path = arm.variance_path(px, horizon_h)
    if var_path is None or len(var_path) < horizon_h:
        return None
    var_path = np.asarray(var_path, dtype=float)
    # Degenerate-fit guards: the arms' research aggregate only checks
    # finiteness/positivity, but a blown EGARCH fit (e.g. beta≈1 on an
    # ARCH-free window) can stay "finite" while its simulated variance path
    # explodes past any market scale — exp(z·σ) then overflows into garbage
    # quantiles. Absurd-value thresholds, not tuning: 50% per-bar vol and a
    # terminal cumulative σ of 10 (a e^10 ≈ 22,000× move) are impossible in
    # this universe; a fan that would assert them is no fan. Fail-open None.
    if not np.isfinite(var_path).all() or np.any(var_path <= 0):
        return None
    if float(np.sqrt(var_path).max()) > 0.5:
        return None
    cum_var = np.cumsum(var_path[:horizon_h])
    if not np.isfinite(cum_var).all() or np.any(cum_var <= 0):
        return None
    if float(math.sqrt(float(cum_var[-1]))) > 10.0:
        return None

    cells = cells if cells is not None else load_calib_cells()
    fan: list[dict[str, Any]] = []
    terminal_level = "none"
    m_terminal = 1.0
    sigma_terminal = float(math.sqrt(float(cum_var[-1])))
    for h in range(1, horizon_h + 1):
        sigma_raw = math.sqrt(float(cum_var[h - 1]))
        m, level = vol_fan_multiplier(cells, symbol, arm_name, h, sigma_raw)
        if h == horizon_h:
            terminal_level, m_terminal = level, m
        scale = sigma_raw * m
        if not (math.isfinite(scale) and 0.0 < scale <= 15.0):
            # scale beyond e^15 territory asserts absurd price moves even
            # after the m clamp — reject the whole fan, never truncate it
            return None
        row: dict[str, Any] = {"h": h}
        for name, _q in FAN_QUANTILES:
            row[name] = round(entry * math.exp(_Z[name] * scale), 6)
        fan.append(row)

    meta = {
        "arm": arm_name,
        "fit_bars": int(len(px)),
        "sigma_h48": round(sigma_terminal, 8),
        "m_h48": round(m_terminal, 4),
        "calib_level": terminal_level,
        "horizon_h": horizon_h,
    }
    return fan, meta


# ─── Registration (Phase A write pattern + Phase B publish pattern) ─────────


async def register_vol_fan_claim(
    *,
    signal_id: str,
    symbol: str,
    ts_iso: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    atr: float | None,
    source: str | None,
    sym: dict[str, Any] | None = None,
    prices: list[float] | None = None,
    timestamps: list[Any] | None = None,
    p_resolve: float | None = None,
    p_win_given_resolve: float | None = None,
    p_win_bracket: float | None = None,
    expected_resolution_hours: float | None = None,
    expiry_bars: int = EXPIRY_BARS,
    broadcast_payload: dict[str, Any] | None = None,
    plan_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Register the Phase D ``vol_fan`` claim for one qualified signal.

    Fits the pre-registered GARCH-family arm on the qualification window
    (the sweep's in-scope ``prices`` when long enough — the exact window the
    signal saw; otherwise a fresh 2160-bar TDVA fetch inside this task, which
    never blocks the sweep), builds the recalibrated price fan, and POSTs one
    ``kind='vol_fan'`` row next to its bracket sibling. When
    ``claims_publish`` is live and the row persisted, re-publishes the signal
    broadcast with THIS claim attached — the vol fan supersedes the bracket
    fan's event (same base payload, last-wins per symbol; the matview's
    kind-priority in migration 220 agrees). Fail-open everywhere; never
    raises; a lost fan is a lost claim, not a lost signal.
    """
    if not _claims_on() or claims_vol_fan_mode() == "off" or not settings.SUPABASE_URL:
        return None
    try:
        entry, sl, tp = float(entry_price), float(stop_loss), float(take_profit)
    except (TypeError, ValueError):
        return None
    if not (entry > 0 and sl > 0 and tp > 0) or entry in (sl, tp):
        return None

    horizon_h = int(expiry_bars)
    arm_name = claims_vol_fan_arm()
    cells = load_calib_cells()

    # Data: the sweep's own window when it is long enough (heavy path: the
    # exact 2160-bar window the signal was computed on); a fresh fetch
    # otherwise (light path: ~200 in-scope bars are far below the arm floor).
    px: list[float] | None = (
        list(prices) if prices and len(prices) >= VOL_FAN_MIN_BARS + 1 else None
    )
    ts: list[Any] | None = (
        timestamps
        if px is not None and timestamps and len(timestamps) == len(prices or [])
        else None
    )
    if px is None:
        px, ts = await _fetch_window(sym, symbol)
        if px is None:
            logger.debug("claims vol_fan %s: no fittable window — no claim", symbol)
            return None

    try:
        built = await asyncio.to_thread(
            build_garch_price_fan, entry, px,
            symbol=symbol, arm_name=arm_name, cells=cells,
            timestamps=ts, horizon_h=horizon_h,
        )
    except Exception as exc:
        logger.debug("claims vol_fan %s: fan build failed: %s", symbol, exc)
        return None
    if built is None:
        return None
    fan, meta = built

    try:
        h_ref = int(round(float(expected_resolution_hours))) if expected_resolution_hours else DEFAULT_REF_H
    except (TypeError, ValueError):
        h_ref = DEFAULT_REF_H
    h_ref = min(max(h_ref, 1), horizon_h)

    bracket_ref = build_bracket_ref(
        entry_price=entry, stop_loss=sl, take_profit=tp,
        direction=direction, fan=fan, h_ref=h_ref,
    )

    # Same claimed_probs evidence set as the bracket sibling (the
    # probabilities are qualification-time facts, fan-independent); the
    # barrier null is the same closed-form triple the PBM anchor uses.
    claimed_probs: dict[str, Any] = {}
    if p_resolve is not None:
        claimed_probs["p_resolve"] = round(float(p_resolve), 6)
    if p_win_given_resolve is not None:
        claimed_probs["p_win_given_resolve"] = round(float(p_win_given_resolve), 6)
    if p_win_bracket is not None:
        claimed_probs["p_win_bracket"] = round(float(p_win_bracket), 6)
    if atr and float(atr) > 0:
        try:
            from regime_platform.sweep_orchestrator.barrier import (
                barrier_null_from_prices,
            )
            from regime_platform.sweep_orchestrator.claims import _sigma_atr

            _null = barrier_null_from_prices(
                entry, tp, sl, atr=float(atr), t_bars=horizon_h,
                sigma=_sigma_atr(), direction=direction or "buy",
            )
            claimed_probs["pi_win"] = round(float(_null.p_win), 6)
            claimed_probs["pi_loss"] = round(float(_null.p_loss), 6)
            claimed_probs["pi_expire"] = round(float(_null.p_expire), 6)
        except Exception as exc:
            logger.debug("claims vol_fan %s: barrier null unavailable: %s", symbol, exc)

    row: dict[str, Any] = {
        "signal_id": str(signal_id),
        "symbol": str(symbol),
        "ts": str(ts_iso),
        "deadline_ts": _shift_iso_hours(ts_iso, horizon_h),
        "kind": "vol_fan",
        "source": "vol_backtest_recalib",
        "space": "price",
        "horizon_h": float(horizon_h),
        "fan": fan,
        "bracket_ref": bracket_ref,
        "claimed_probs": claimed_probs,
        "arm": meta["arm"],
        "calib_level": meta["calib_level"],
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    row = {k: v for k, v in row.items() if k in VOL_FAN_CLAIM_COLUMNS and v is not None}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_signal_claims"),
                headers={**_headers(), "Content-Type": "application/json"},
                json=[row],
            )
            if not resp.is_success:
                logger.debug(
                    "claims vol_fan stamp %s HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
                return None
    except Exception as exc:
        logger.debug("claims vol_fan stamp %s failed: %s", symbol, exc)
        return None

    # Phase B transport convention, verbatim: publish only after the ledger
    # write persisted, only when the wire flag is live. The vol_fan row
    # carries the GARCH fan — the design doc §2 end state (ENTRY/SL/TP drawn
    # on the honest distribution); the bracket sibling's earlier event (same
    # base payload) is superseded last-wins per symbol.
    if broadcast_payload is not None:
        try:
            from regime_platform.sweep_orchestrator.claims import claims_publish

            if claims_publish() == "live":
                from regime_platform.sweep_orchestrator.realtime_publisher import (
                    broadcast_signal,
                )
                broadcast_signal(
                    {**broadcast_payload, "claim": row},
                    plan_ids=plan_ids,
                )
        except Exception as exc:
            logger.debug("claims vol_fan publish %s: rebroadcast failed: %s", symbol, exc)
    return row


async def _fetch_window(
    sym: dict[str, Any] | None, symbol: str
) -> tuple[list[float] | None, list[Any] | None]:
    """Fresh 2160-bar hourly window for ``symbol`` (the heavy sweep's own
    fetcher, timestamps included so the calendar filter can apply). Fail-open
    to ``(None, None)``."""
    try:
        from regime_platform.sweep_orchestrator.price_fetcher import (
            fetch_candles_for_symbols,
        )

        target = sym if (sym and sym.get("tradingview_symbol")) else {"symbol": str(symbol)}
        batch, batch_ts = await fetch_candles_for_symbols(
            [target], n_bars=VOL_FAN_FETCH_HOURS, include_timestamps=True,
        )
        px = batch.get(str(symbol))
        if not px or len(px) < VOL_FAN_MIN_BARS + 1:
            return None, None
        ts = (batch_ts or {}).get(str(symbol))
        return list(px), list(ts) if ts and len(ts) == len(px) else None
    except Exception as exc:
        logger.debug("claims vol_fan %s: window fetch failed: %s", symbol, exc)
        return None, None
