"""tv_corpus.py — the TDVA-basis research corpus generator (Path B, 2026-11-16).

Generates ``CRT_CONT_RESEARCH_<SYMBOL>.csv`` files in the EXACT
``CRT_EV_Research_Export.mq4`` v1.10 compact-path schema — but from
TradingView (TDVA) candles instead of an MT4 chart, so the CRT-0 lab
(``scripts/run_crt_lab.py --data-dir data/crt/tv``) can mint
``config/crt_cont.json`` without MT4 access.

WHY THIS IS THE HONEST PATH B (worklog Task 74): the corpus is produced by
the SAME ports the live engine runs — ``crt.setup.detect_setups`` (setup
detection), ``crt.atr`` (MT4-seeded Wilder ATR), ``crt.sessions`` (ET
session windows), ``crt.candles`` (closed-bar + M30 = 2x15m aggregation) —
on the SAME TDVA feed the engine trades. The evidence basis equals the
trading basis; the MT4-vs-TV data-basis mismatch disappears. The
``#meta`` line stamps ``basis=tdva_tv`` and the feed, and the minted
artifact carries the basis block (``report.build_artifact(metas=...)``).

Port discipline (EA ``VariantRow`` / ``ExportTF`` verbatim, with the
engine-parity decisions recorded here):

- models: CONT only (the v5 main trade; the lab runs models "CONT");
- per setup, one row per (sl_buf, entry_buf) — the EA's loop order;
- SL = C2 opposite extreme + sb*ATR; entry stop = C2 sweep extreme +/-
  eb*ATR in the SWEEP direction (trade opposite the setup);
- fill check: buy ``h3 + spread >= entry`` / sell ``l3 <= entry``
  (ask vs bid, the EA's asymmetry);
- costs encoded as the EA encodes them: ``entry_cost_r``/``exit_slip_r``
  in R (the optimizer applies them per barrier; the labeler's
  exit-price math agrees numerically — verified in crt_tv_verify.py);
- the C3 bar is checked FIRST for stop models — SL before TP (same-bar
  tie -> SL, conservative), then held=1..max_hold with SL before TP on
  each bar; when SL hits at ``held``, the R targets of THAT SAME bar are
  still recorded (the EA's loop does; the optimizer resolves the tie
  SL-first);
- ``bars_avail`` = closed bars after C3 in the fetched window (the
  right-edge honesty: near-edge rows carry empty ``c_<H>`` / ``-1``);
- ``c3_dir_ok``/``c3_body`` from the C3 bar; ``chop_pass`` from the
  engine-computed chop metrics with the EA thresholds;
- ``swept_pd``: prior TRADING day's high/low sweep, derived from the TF's
  own bars (the EA used broker D1 bars; UTC trading-day stand-in — the
  column is inert for the optimizer, divergence documented).

ENGINE-PARITY DIVERGENCES from the MT4 export (each is the engine's own
rule, kept so the corpus describes exactly what the engine trades):

- C3 is matched by EXACT open time ``c2_open + tf`` (the engine's
  ``iBarShift(..., true)`` rule); a session/weekend gap where no bar
  opened at the boundary SKIPS the setup (counted in ``gap_skipped``).
  The MT4 export was positional (next bar whatever its time);
- session is frozen at C2 close from the C3 boundary time (clock
  arithmetic), via ``crt.sessions`` on the UTC-native clock;
- M30 bars are the engine's 2x15m aggregation (both halves must exist).

No ``regime_platform`` imports and no network here: this module is pure
(corpus math on closed-bar arrays); the fetch lives in
``scripts/run_crt_tv_export.py`` (the composition root).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from crt.config_loader import TF_SECONDS
from crt.sessions import SessionWindows
from crt.setup import SetupRecord, detect_setups

MODEL_CONT = "CONT"


def _fmt_time(epoch: float) -> str:
    """UTC '%Y.%m.%d %H:%M' (the export/loader time format)."""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
        "%Y.%m.%d %H:%M")


@dataclass(frozen=True)
class ExportGrid:
    """The research grid (EA EntryBufGrid/SLBufGrid/RGridResearch/HoldGrid)."""

    eb_grid: tuple[float, ...] = (0.0, 0.05, 0.10)
    sb_grid: tuple[float, ...] = (0.0, 0.10, 0.20)
    r_grid: tuple[float, ...] = (1.0, 1.5, 2.0)
    hold_grid: tuple[int, ...] = (12, 24, 48)

    @property
    def max_hold(self) -> int:
        return max(self.hold_grid) if self.hold_grid else 1

    def validate(self) -> None:
        if not self.eb_grid or not self.sb_grid or not self.r_grid or not self.hold_grid:
            raise ValueError("empty research grid (eb/sb/r/hold)")


@dataclass(frozen=True)
class CoreParams:
    """EA core inputs — same defaults as crt_engine.yaml / the toolkit."""

    atr_period: int = 14
    chop_lookback: int = 6
    liquidity_lookback: int = 10
    vol_lookback: int = 500
    max_wick_ratio: float = 0.70
    max_overlap_ratio: float = 0.75
    max_direction_changes: int = 4

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "CoreParams":
        cfg = cfg or {}
        return cls(
            atr_period=int(cfg.get("atr_period", 14)),
            chop_lookback=int(cfg.get("chop_lookback", 6)),
            liquidity_lookback=int(cfg.get("liquidity_lookback", 10)),
            vol_lookback=int(cfg.get("vol_lookback", 500)),
            max_wick_ratio=float(cfg.get("max_wick_ratio", 0.70)),
            max_overlap_ratio=float(cfg.get("max_overlap_ratio", 0.75)),
            max_direction_changes=int(cfg.get("max_direction_changes", 4)),
        )

    def chop_pass(self, wick: float, overlap: float, dir_changes: int) -> int:
        """EA CfgFilterPass chop verdict on the computed metrics."""
        return 1 if (wick <= self.max_wick_ratio and overlap <= self.max_overlap_ratio
                     and dir_changes <= self.max_direction_changes) else 0


def variant_tail(setup: SetupRecord, t: np.ndarray, h: np.ndarray, low: np.ndarray,
                 c: np.ndarray, c3_idx: int, grid: ExportGrid, buy: bool,
                 eb: float, sb: float, spread: float, slip: float,
                 point: float) -> str:
    """The compact-path tail for one CONT (eb, sb) variant — EA ``VariantRow``
    port for stop models (model 2), path scanned C3 -> max_hold.

    Returns the CSV fields from ``filled`` through ``c_<H>`` (the caller
    prepends the common feature columns). Unfilled/risk<=0 rows carry the
    EA's inert tail (``0,0,0,0,0,-1`` + ``-1`` b-grid + empty c-grid).
    """
    grid.validate()
    atr, sp, slp = setup.atr, spread, slip
    h3, l3 = float(h[c3_idx]), float(low[c3_idx])
    sl = (setup.c2_low - sb * atr) if buy else (setup.c2_high + sb * atr)
    entry_ref = (setup.c2_high + eb * atr) if buy else (setup.c2_low - eb * atr)
    filled = (h3 + sp >= entry_ref) if buy else (l3 <= entry_ref)
    risk = (entry_ref - sl) if buy else (sl - entry_ref)

    if not filled or risk <= 0:
        return ("0,0,0,0,0,-1" + ",-1" * len(grid.r_grid)
                + "," * len(grid.hold_grid))

    fill = (entry_ref + slp) if buy else (entry_ref - slp)
    entry_cost_r = abs(fill - entry_ref) / risk
    exit_slip_r = slp / risk
    last_idx = t.size - 1
    avail = last_idx - c3_idx          # closed bars after C3 (right-edge truth)

    bars_sl = -1
    bars_r = [-1] * len(grid.r_grid)

    def _tp(k: int) -> float:
        rr = grid.r_grid[k]
        return (entry_ref + rr * risk) if buy else (entry_ref - rr * risk)

    # C3 bar (stop models): entry happens inside C3 — SL before TP, the
    # conservative same-bar tie; R targets of the SAME bar are still recorded.
    fav0 = h3 if buy else (l3 + sp)
    adv0 = l3 if buy else (h3 + sp)
    if (adv0 <= sl) if buy else (adv0 >= sl):
        bars_sl = 0
    for k in range(len(grid.r_grid)):
        tp = _tp(k)
        if (fav0 >= tp) if buy else (fav0 <= tp):
            bars_r[k] = 0

    held = 1
    while held <= grid.max_hold and bars_sl < 0:
        j = c3_idx + held
        if j > last_idx:
            break
        hh, ll = float(h[j]), float(low[j])
        fav = hh if buy else (ll + sp)
        adv = ll if buy else (hh + sp)
        if (adv <= sl) if buy else (adv >= sl):
            bars_sl = held
        for k in range(len(grid.r_grid)):
            if bars_r[k] >= 0:
                continue
            tp = _tp(k)
            if (fav >= tp) if buy else (fav <= tp):
                bars_r[k] = held
        held += 1

    c_vals: list[str] = []
    for hh_int in grid.hold_grid:
        jj = c3_idx + hh_int
        if jj > last_idx:
            c_vals.append("")
            continue
        cc = float(c[jj])
        exit_px = cc if buy else (cc + sp)
        r_mult = ((exit_px - entry_ref) if buy else (entry_ref - exit_px)) / risk
        c_vals.append(f"{r_mult:.4f}")

    return (f"1,{risk / point:.1f},{entry_cost_r:.4f},{exit_slip_r:.4f},"
            f"{avail},{bars_sl},"
            + ",".join(str(b) for b in bars_r)
            + "," + ",".join(c_vals))


def _trading_days(t: np.ndarray, h: np.ndarray, low: np.ndarray) -> dict:
    """UTC trading-day -> (high, low) from the TF's own bars (D1 stand-in)."""
    days: dict[str, tuple[float, float]] = {}
    for i in range(t.size):
        d = datetime.fromtimestamp(float(t[i]), tz=timezone.utc).strftime("%Y-%m-%d")
        hi, lo = days.get(d, (float(h[i]), float(low[i])))
        days[d] = (max(hi, float(h[i])), min(lo, float(low[i])))
    return days


def _prior_day_sweep(days: dict, t: np.ndarray, c2_idx: int,
                     c2_high: float, c2_low: float, bull: bool) -> int:
    """swept_pd: C2 swept the PRIOR TRADING day's extreme (EA: broker D1)."""
    keys = sorted(days.keys())
    if len(keys) < 2:
        return 0
    d2 = datetime.fromtimestamp(float(t[c2_idx]), tz=timezone.utc).strftime("%Y-%m-%d")
    prior = None
    for d in reversed(keys):
        if d < d2:
            prior = d
            break
    if prior is None:
        return 0
    pdh, pdl = days[prior]
    if pdh <= pdl:
        return 0
    return 1 if (bull and c2_low < pdl) or ((not bull) and c2_high > pdh) else 0


def export_tf(symbol: str, tf: str, bars: list[dict[str, Any]],
              grid: ExportGrid, windows: SessionWindows, core: CoreParams,
              spread: float, slip: float, point: float) -> tuple[list[str], dict]:
    """All CONT variant rows for one (symbol, TF) closed-bar list + stats.

    ``bars`` are CLOSED, ASCENDING, open-time stamped (crt.candles shape).
    Returns (rows, stats) — stats carries the honesty counters.
    """
    if tf not in TF_SECONDS:
        raise ValueError(f"unsupported tf {tf!r}")
    tf_seconds = TF_SECONDS[tf]
    t = np.asarray([b["t"] for b in bars], dtype=float)
    o = np.asarray([b["o"] for b in bars], dtype=float)
    h = np.asarray([b["h"] for b in bars], dtype=float)
    low = np.asarray([b["l"] for b in bars], dtype=float)
    c = np.asarray([b["c"] for b in bars], dtype=float)

    stats = dict(tf=tf, bars=int(t.size), rows=0, setups=0, gap_skipped=0,
                 edge_skipped=0,
                 t_min=(_fmt_time(t[0]) if t.size else ""),
                 t_max=(_fmt_time(t[-1]) if t.size else ""))
    if t.size < 3:
        return [], stats

    days = _trading_days(t, h, low)
    setups = detect_setups(t, o, h, low, c, tf_seconds, windows,
                           chop_lookback=core.chop_lookback,
                           liquidity_lookback=core.liquidity_lookback,
                           atr_period=core.atr_period,
                           vol_lookback=core.vol_lookback)
    rows: list[str] = []
    for setup in setups:
        stats["setups"] += 1
        c2_idx = setup.c2_idx
        c3_idx = c2_idx + 1
        if c3_idx > t.size - 1:
            stats["edge_skipped"] += 1      # C3 not closed yet (right edge)
            continue
        # engine rule: C3 matched by EXACT open time (iBarShift exact=True)
        expected = float(t[c2_idx]) + tf_seconds
        if abs(float(t[c3_idx]) - expected) > 1e-6:
            stats["gap_skipped"] += 1       # weekend/session gap — engine PENDING
            continue
        o3, h3, l3, c3 = (float(o[c3_idx]), float(h[c3_idx]),
                          float(low[c3_idx]), float(c[c3_idx]))
        c3_body = (abs(c3 - o3) / (h3 - l3)) if (h3 > l3) else 0.0
        c3_dir_ok = 1 if ((c3 > o3) if setup.dir == 1 else (c3 < o3)) else 0
        swept = _prior_day_sweep(days, t, c2_idx, setup.c2_high, setup.c2_low,
                                 setup.dir == 1)
        common = (f"{symbol},{tf},{'BUY' if setup.dir == 1 else 'SELL'},"
                  f"{_fmt_time(setup.c2_open)},{_fmt_time(setup.c3_open)},"
                  f"{setup.session_id},"
                  f"{setup.sweep_atr:.3f},{setup.chop_wick:.3f},"
                  f"{setup.chop_overlap:.3f},{setup.dir_changes},"
                  f"{core.chop_pass(setup.chop_wick, setup.chop_overlap, setup.dir_changes)},"
                  f"{setup.atr / point:.1f},{setup.vol_pct:.3f},"
                  f"{1 if setup.c1_swing else 0},{swept},"
                  f"{c3_body:.3f},{c3_dir_ok}")
        buy = setup.trade_buy           # CONT trades OPPOSITE the setup
        for sb in grid.sb_grid:          # EA loop order: sb outer, eb inner
            for eb in grid.eb_grid:
                tail = variant_tail(setup, t, h, low, c, c3_idx, grid, buy,
                                    eb, sb, spread, slip, point)
                rows.append(f"{common},{MODEL_CONT},{eb:.3f},{sb:.3f},{tail}")
    stats["rows"] = len(rows)
    return rows, stats


def export_symbol(symbol: str, tv_symbol: str, bars_by_tf: dict[str, list[dict]],
                  grid: ExportGrid, windows: SessionWindows, core: CoreParams,
                  spread: float, slip: float, point: float, digits: int,
                  out_path: str) -> dict:
    """Write one CRT_CONT_RESEARCH_<SYMBOL>.csv (header + #meta + CRLF rows).

    ``bars_by_tf``: {"M15": closed bars, "M30": closed bars, "H1": closed bars}
    — M30 may be pre-derived via ``crt.candles.aggregate_m30`` (engine parity).
    The ``#meta`` line stamps the TV basis (extra keys are inert to the
    loader's parser and travel into the artifact's basis block).
    """
    grid.validate()
    spread_pts = int(round(spread / point)) if point > 0 else 0
    slip_pts = int(round(slip / point)) if point > 0 else 0
    header = ("symbol,tf,dir,c2_time,c3_time,session,sweep_atr,chop_wick,"
              "chop_overlap,dir_changes,chop_pass,atr_pts,atr_pct,c1_swing,"
              "swept_pd,c3_body,c3_dir_ok,model,entry_buf,sl_buf,filled,"
              "risk_pts,entry_cost_r,exit_slip_r,bars_avail,bars_sl,"
              + ",".join(f"b_{r:.2f}" for r in grid.r_grid) + ","
              + ",".join(f"c_{hh}" for hh in grid.hold_grid))
    meta = (f"#meta,point={point},digits={digits},spread_pts={spread_pts},"
            f"slip_pts={slip_pts},clock=utc,gmt_off=0,nyclose=0,"
            f"basis=tdva_tv,feed={tv_symbol}")

    lines = [header, meta]
    tf_stats = []
    for tf in ("H1", "M30", "M15"):
        bars = bars_by_tf.get(tf) or []
        if not bars:
            continue
        rows, stats = export_tf(symbol, tf, bars, grid, windows, core,
                                spread, slip, point)
        lines.extend(rows)
        tf_stats.append(stats)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="latin-1", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")

    return dict(symbol=symbol, tv_symbol=tv_symbol, path=out_path,
                rows=sum(s["rows"] for s in tf_stats),
                setups=sum(s["setups"] for s in tf_stats),
                gap_skipped=sum(s["gap_skipped"] for s in tf_stats),
                edge_skipped=sum(s["edge_skipped"] for s in tf_stats),
                tfs=tf_stats)
