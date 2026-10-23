#!/usr/bin/env python3
"""
kairos_weekly_check.py — Kairos v3 G2 weekly check (live, geometry-free, read-only).

worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §12. Runs on ALL light-sweep signals
(v_kairos_signal_episodes: one row per symbol × direction × hour since the
2026-09-22 debounce go-live), not just qualified=true — cooldown/dedup
suppression is split out via gate_outcome (migration 168).

Never uses SL/TP, R, W/L, P/L or outcome. Edge = m_h, signed drift in ATR
units at h ∈ {1,4,12,24,48} bars from the hour's first tick.

Interim rule (Haybittle–Peto): weekly looks are monitoring. A weekly result
is only flagged as "extreme" at p < 0.001; the pre-registered decision is at
W6 (G2). Data-health checks are pass/fail every week.

Usage:
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_weekly_check.py \
      [--week-end 2026-10-01] [--n-null 1000] [--n-boot 1000] [--out .cache/kairos/weekly]
"""
# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GO_LIVE = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)      # debounce live (runs view start)
W0_END = datetime(2026, 9, 24, 8, tzinfo=timezone.utc)        # §12.2: W0 ends Thu 2026-09-24 08:00; Wk ends W0_END + 7k days
STAMPS_LIVE = datetime(2026, 9, 24, 8, tzinfo=timezone.utc)
H = (1, 4, 12, 24, 48)
FACTORS = ("rel_volume_1h", "clv_1h", "efficiency_24h", "atr_ratio_run", "spread_bps")
TIERS = ("all", "qualified", "suppressed", "gate_fail", "unknown")
EXTREME_P = 0.001
BACKFILL_LAG_H = 2     # hourly kairos-daily backfill (:20) + margin; m_h is mature at hr + h + lag
MIN_COVER = 0.95       # matured-cohort drift coverage required before any edge stat
HEARTBEAT_MAX_AGE_H = 2.0   # kairos-daily runs hourly; > 2h without a heartbeat = missed run
POWER_SD_FRAC = 0.2    # §12.2 W5 rule, now checked every look: stop when CI half-width ≤ 0.2·SD
# Gate reconstruction for pre-168 rows (validated 2026-09-25 against 910
# stamped rows: tier agreement 1.000; worklog/kairos/20260925_*_ACCELERATED_G2.md).
RECON = {"min_regime_conf": 0.60, "min_kelly": 0.02, "min_rr": 1.5, "min_p_win": 0.50}
RNG = np.random.default_rng(20260924)


def schedule_week(week_end: datetime) -> tuple[datetime, str]:
    """Snap to the most recent pre-registered Thu 08:00 boundary ≤ week_end
    and name it (W0 = 2026-09-24). Fixes the 09-24 23:22 run being called W1."""
    k = int(np.floor((week_end - W0_END).total_seconds() / (7 * 86400)))
    snapped = W0_END + timedelta(days=7 * k)
    return snapped, (f"W{k}" if k >= 0 else "pre-W0")


def mature_mask(hr: pd.Series, h: int, now: datetime) -> pd.Series:
    """Per-horizon maturity: the h-th bar after the entry hour has closed and
    one hourly backfill has had a pass (replaces the blanket 84h rule)."""
    return hr <= pd.Timestamp(now) - pd.Timedelta(hours=h + 1 + BACKFILL_LAG_H)


def reconstruct_tier(ticks: pd.DataFrame) -> pd.Series:
    """Per-tick tier for rows without gate_outcome, from stored columns."""
    rr = ticks.tp_bricks / ticks.sl_bricks
    pw = ticks.p_win_dir.fillna(ticks.p_win)
    ok = ((ticks.regime_conf >= RECON["min_regime_conf"]) & (ticks.effective_kelly >= RECON["min_kelly"])
          & (ticks.ev > 0) & (rr >= RECON["min_rr"]) & (pw >= RECON["min_p_win"]))
    return pd.Series(np.where(ticks.qualified.astype(bool), "qualified",
                              np.where(ok, "suppressed", "gate_fail")), index=ticks.index)


def fill_unknown_tiers(ep: pd.DataFrame, end: datetime) -> pd.DataFrame:
    """Replace tier 'unknown' (pre-168) with the best reconstructed per-tick
    tier in the hour. Adds tier_src ∈ {stamp, recon}."""
    ep = ep.copy()
    ep["tier_src"] = np.where(ep.tier == "unknown", "recon", "stamp")
    if not (ep.tier == "unknown").any():
        return ep
    cols = "ts,symbol,signal,qualified,regime_conf,effective_kelly,ev,p_win,p_win_dir,sl_bricks,tp_bricks"
    t = pd.DataFrame(_get("nt_signal_sim", cols, {
        "source": "eq.sweep-light", "signal": "in.(buy,sell)", "gate_outcome": "is.null",
        "ts": f"gte.{GO_LIVE:%Y-%m-%dT%H:%M:%SZ}", "order": "ts.asc"}))
    if t.empty:
        return ep
    t = t[pd.to_datetime(t.ts, utc=True, format="ISO8601") < pd.Timestamp(end)]
    for c in ("regime_conf", "effective_kelly", "ev", "p_win", "p_win_dir", "sl_bricks", "tp_bricks"):
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t["hr"] = pd.to_datetime(t.ts, utc=True, format="ISO8601").dt.floor("h")
    t["rank"] = reconstruct_tier(t).map({"qualified": 1, "suppressed": 2, "gate_fail": 3})
    best = t.groupby(["symbol", "signal", "hr"])["rank"].min().map({1: "qualified", 2: "suppressed", 3: "gate_fail"})
    key = pd.MultiIndex.from_arrays([ep.symbol, ep.direction, ep.hr])
    rec = pd.Series(best.reindex(key).to_numpy(), index=ep.index)
    m = (ep.tier == "unknown") & rec.notna()
    ep.loc[m, "tier"] = rec[m]
    return ep


def fetch_heartbeat(mechanism: str = "kairos_daily_job") -> tuple[datetime | None, str]:
    try:
        rows = _get("nt_roadmap", "current_state,updated_at", {"mechanism": f"eq.{mechanism}"})
    except Exception as exc:  # noqa: BLE001
        return None, f"query failed: {type(exc).__name__}"
    if not rows or not rows[0].get("current_state", "").startswith("last_run "):
        return None, "no heartbeat yet (needs the 2026-09-25 hourly job deployed)"
    ts = datetime.fromisoformat(rows[0]["current_state"].split()[1].replace("Z", "+00:00"))
    return ts, rows[0]["current_state"][:160]


def projected_halfwidth(a: pd.DataFrame, b: pd.DataFrame, col: str, n_boot: int) -> tuple[float, float]:
    """(CI half-width of Δ now, pooled SD of col) — §12.2 power criterion."""
    d = boot_diff(a, b, col, n_boot)
    sd = float(pd.concat([a[col], b[col]]).std())
    return (d[2] - d[1]) / 2 if np.isfinite(d[1]) else np.nan, sd


# ─── data ──────────────────────────────────────────────────────────────


def _get(view: str, select: str, extra: dict | None = None) -> list[dict]:
    import httpx

    from regime_platform.core.settings import settings as s

    h = {"apikey": s.SUPABASE_SECRET_KEY, "Authorization": f"Bearer {s.SUPABASE_SECRET_KEY}"}
    url = f"{s.SUPABASE_URL}/rest/v1/{view}"
    rows, off = [], 0
    with httpx.Client(timeout=120) as c:
        while True:
            params = {"select": select, "limit": "1000", "offset": str(off), **(extra or {})}
            r = c.get(url, headers=h, params=params)
            r.raise_for_status()
            page = r.json()
            rows += page
            if len(page) < 1000:
                return rows
            off += 1000


def load(week_end: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    cols = ("symbol,direction,hr,tier,suppress_reason,any_cooldown,failed_gates,cell,"
            "m1,m4,m12,m24,m48," + ",".join(FACTORS) + ",regime_age_h,age_left_censored,run_id")
    path_cols = ",".join(f"{k}{h}" for k in ("mae", "mfe") for h in PATH_H)
    end = week_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {"hr": f"lt.{end}", "order": "symbol.asc,hr.asc,direction.asc"}
    try:
        ep = pd.DataFrame(_get("v_kairos_signal_episodes", cols + "," + path_cols, q))
    except Exception as exc:  # noqa: BLE001 — migration 170 not applied yet
        if "400" not in str(exc):
            raise
        ep = pd.DataFrame(_get("v_kairos_signal_episodes", cols, q))
        for c in path_cols.split(","):
            ep[c] = np.nan
    runs = pd.DataFrame(_get("v_kairos_regime_runs",
                             "symbol,run_id,cell,started_at,ended_at,length_h,exit_kind,"
                             "starts_after_gap,ended_by_gap,is_open",
                             {"started_at": f"lt.{end}", "order": "symbol.asc,started_at.asc"}))
    if not ep.empty:
        ep["hr"] = pd.to_datetime(ep["hr"], utc=True)
        ep["d"] = np.where(ep["direction"] == "buy", 1.0, -1.0)
        ep["day"] = ep["hr"].dt.strftime("%Y-%m-%d")
        ep["clu"] = ep["symbol"] + "|" + ep["day"]
        for c in ("m1", "m4", "m12", "m24", "m48", *FACTORS, "regime_age_h",
                  *(f"{k}{h}" for k in ("mae", "mfe") for h in PATH_H)):
            ep[c] = pd.to_numeric(ep[c], errors="coerce")
    if not runs.empty:
        for c in ("started_at", "ended_at"):
            runs[c] = pd.to_datetime(runs[c], utc=True)
        runs["length_h"] = pd.to_numeric(runs["length_h"], errors="coerce")
    return ep, runs


# ─── stats ─────────────────────────────────────────────────────────────


def boot_mean(v: np.ndarray, clu: np.ndarray, n_boot: int) -> tuple[float, float, float, float]:
    """Mean with symbol|day cluster-bootstrap 95% CI and P(mean ≤ 0)."""
    ok = np.isfinite(v)
    v, clu = v[ok], clu[ok]
    if len(v) < 5:
        return np.nan, np.nan, np.nan, np.nan
    keys, inv = np.unique(clu, return_inverse=True)
    s = np.bincount(inv, weights=v)
    n = np.bincount(inv).astype(float)
    k = len(keys)
    draws = RNG.integers(0, k, size=(n_boot, k))
    bm = s[draws].sum(1) / n[draws].sum(1)
    return float(v.mean()), float(np.percentile(bm, 2.5)), float(np.percentile(bm, 97.5)), float((bm <= 0).mean())


def boot_diff(a: pd.DataFrame, b: pd.DataFrame, col: str, n_boot: int) -> tuple[float, float, float, float]:
    """Mean(a) − mean(b) of a within-symbol-demeaned column, cluster bootstrap over the union of clusters."""
    a, b = a[np.isfinite(a[col])], b[np.isfinite(b[col])]
    if len(a) < 5 or len(b) < 5:
        return np.nan, np.nan, np.nan, np.nan
    obs = a[col].mean() - b[col].mean()
    keys = np.unique(np.r_[a.clu.to_numpy(), b.clu.to_numpy()])
    idx = {k: i for i, k in enumerate(keys)}
    ia, ib = a.clu.map(idx).to_numpy(), b.clu.map(idx).to_numpy()
    sa, na = np.bincount(ia, a[col].to_numpy(), len(keys)), np.bincount(ia, minlength=len(keys)).astype(float)
    sb, nb = np.bincount(ib, b[col].to_numpy(), len(keys)), np.bincount(ib, minlength=len(keys)).astype(float)
    d = RNG.integers(0, len(keys), size=(n_boot, len(keys)))
    with np.errstate(invalid="ignore", divide="ignore"):
        bd = sa[d].sum(1) / na[d].sum(1) - sb[d].sum(1) / nb[d].sum(1)
    bd = bd[np.isfinite(bd)]
    return float(obs), float(np.percentile(bd, 2.5)), float(np.percentile(bd, 97.5)), float(min(1.0, 2 * min((bd <= 0).mean(), (bd >= 0).mean())))


def circ_null(ep: pd.DataFrame, col: str, n_null: int, min_shift: int = 24) -> tuple[float, float]:
    """Pooled mean of d·u under per-symbol circular rotation of the direction
    series vs the unsigned drift u = m·d (keeps both autocorrelations, breaks
    their alignment). Returns (observed, two-sided p)."""
    parts = []
    for _, g in ep[np.isfinite(ep[col])].sort_values("hr").groupby("symbol"):
        if len(g) > 2 * min_shift:
            d = g["d"].to_numpy()
            parts.append((d, g[col].to_numpy() * d))
    if not parts:
        return np.nan, np.nan
    obs = np.concatenate([d * u for d, u in parts]).mean()
    null = np.empty(n_null)
    for i in range(n_null):
        acc, n = 0.0, 0
        for d, u in parts:
            k = RNG.integers(min_shift, len(d) - min_shift)
            acc += (np.roll(d, k) * u).sum()
            n += len(d)
        null[i] = acc / n
    p = (1 + (np.abs(null - null.mean()) >= abs(obs - null.mean())).sum()) / (n_null + 1)
    return float(obs), float(p)


def band(age: pd.Series) -> pd.Series:
    return pd.cut(age, [-0.01, 6, 12, 24, 48, np.inf], labels=["0–6h", "6–12h", "12–24h", "24–48h", "≥48h"])


# ─── survival + path risk (2026-09-25, worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md) ──

PATH_H = (4, 12, 24, 48)
STOP_K = (1.0, 2.0, 3.0)     # fixed stop distances in sweep-ATR units (reporting grid, not a knob)


def kaplan_meier(durations, events) -> pd.DataFrame:
    """Product-limit estimator with Greenwood 95% CI (log-log). ``durations``
    in hours (ties at the same t are handled together); ``events`` True =
    the run ended by a regime change, False = right-censored (still open, or
    ended by a data gap). Returns one row per distinct event time:
    t, n_risk, d, S, lo, hi."""
    t = np.asarray(durations, dtype=float)
    e = np.asarray(events, dtype=bool)
    ok = np.isfinite(t) & (t >= 0)
    t, e = t[ok], e[ok]
    out, S, gw = [], 1.0, 0.0
    for u in np.unique(t[e]):
        n = int((t >= u).sum())
        d = int(((t == u) & e).sum())
        if n == 0:
            break
        S *= 1.0 - d / n
        gw += d / (n * (n - d)) if n > d else np.inf
        if 0 < S < 1 and np.isfinite(gw):
            se = np.sqrt(gw) / abs(np.log(S))
            lo, hi = S ** np.exp(1.96 * se), S ** np.exp(-1.96 * se)
        else:
            lo = hi = S
        out.append((u, n, d, S, lo, hi))
    return pd.DataFrame(out, columns=["t", "n_risk", "d", "S", "lo", "hi"])


def km_at(km: pd.DataFrame, x: float) -> float:
    """S(x): the step function evaluated at x (1.0 before the first event;
    NaN past the last observed time when the curve hasn't reached 0)."""
    if km.empty:
        return np.nan
    past = km[km.t <= x]
    return 1.0 if past.empty else float(past.S.iloc[-1])


def km_median(km: pd.DataFrame) -> float:
    hit = km[km.S <= 0.5]
    return float(hit.t.iloc[0]) if len(hit) else np.nan


def conditional_survival(km: pd.DataFrame, age: float, delta: float) -> float:
    """P(run still alive at age+Δ | alive at age) = S(age+Δ)/S(age). The
    'remaining regime clock' a later signal (S2) in the same run inherits."""
    s0 = km_at(km, age)
    s1 = km_at(km, age + delta)
    return s1 / s0 if s0 and np.isfinite(s0) and np.isfinite(s1) and s0 > 0 else np.nan


def geometric_survival(durations, events, x: float) -> float:
    """Memoryless reference: constant hazard λ = events / hours at risk."""
    t = np.asarray(durations, dtype=float)
    e = np.asarray(events, dtype=bool)
    at = np.nansum(t)
    return float(np.exp(-(e.sum() / at) * x)) if at > 0 else np.nan


def stop_truncated(m: np.ndarray, mae: np.ndarray, k: float) -> np.ndarray:
    """Endpoint drift with a k-ATR stop and no target: −k if the adverse
    excursion reached k before h, else m_h. NaN where either is missing.
    (Intrabar order and gaps are ignored — a stop that gaps through k reads
    as −k, so this is optimistic by the gap size.)"""
    m, mae = np.asarray(m, dtype=float), np.asarray(mae, dtype=float)
    out = np.where(mae >= k, -k, m)
    out[~(np.isfinite(m) & np.isfinite(mae))] = np.nan
    return out


# ─── report ────────────────────────────────────────────────────────────


def fmt(x, nd=3, sign=True):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-end", default="", help="UTC date (YYYY-MM-DD); default = now (snapped to Thu 08:00)")
    ap.add_argument("--accel", action="store_true",
                    help="accelerated look: rolling --window-h window ending now, no weekly snap (2026-09-25)")
    ap.add_argument("--window-h", type=int, default=48)
    ap.add_argument("--n-null", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--out", default=str(ROOT / "worklog/kairos/reports"))
    a = ap.parse_args()
    t0 = time.time()
    now = datetime.now(timezone.utc)
    if a.accel:
        week_end = now.replace(minute=0, second=0, microsecond=0)
        week_start = week_end - timedelta(hours=a.window_h)
        wlabel = f"A{week_end:%m%d%H}"
        wk = None
    else:
        raw_end = (datetime.fromisoformat(a.week_end).replace(tzinfo=timezone.utc, hour=8)
                   if a.week_end else now)
        week_end, wlabel = schedule_week(raw_end)
        week_start = week_end - timedelta(days=7)
        wk = int(wlabel[1:]) if wlabel.startswith("W") else -1

    ep, runs = load(week_end)
    if not ep.empty:
        ep = fill_unknown_tiers(ep, week_end)
    L: list[str] = []
    P = L.append
    kind = f"accelerated look {wlabel} (last {a.window_h}h, ending {week_end:%Y-%m-%d %H:%M} UTC)" if a.accel \
        else f"{wlabel} (week ending {week_end:%Y-%m-%d %H:%M} UTC)"
    P(f"# Kairos G2 check — {kind}\n")
    P(f"Population: **all** light-sweep buy/sell signals since {GO_LIVE:%Y-%m-%d} (debounced), one episode per "
      f"symbol × direction × hour. Geometry-free: m_h = signed drift / sweep ATR (close-to-close SMA14 — "
      f"magnitudes are NOT comparable to P1's OHLC ATR; compare within live only). Interim looks flag only "
      f"p < {EXTREME_P}; the G2 decision fires when the power criterion (§H) is met, not on a calendar date. "
      f"m_h counts as matured once bar h has closed + {BACKFILL_LAG_H}h (hourly backfill).\n")
    if ep.empty:
        P("No episodes.")
        print("\n".join(L))
        return 1
    this_wk = ep[ep.hr >= week_start]
    flags: list[str] = []

    # A. data health ----------------------------------------------------
    P("## A. Data health (pass/fail every week)\n")
    stamped = this_wk[this_wk.hr >= STAMPS_LIVE]
    cov = {f: float(stamped[f].notna().mean()) if len(stamped) else np.nan for f in FACTORS}
    tier_cov = float((stamped.tier_src == "stamp").mean()) if len(stamped) else np.nan
    # Per-horizon maturity (2026-09-25): m_h is read only once bar h has
    # closed and one hourly backfill ran. Before that, drift exists only
    # where TP/SL settled early — geometry-selected, never read as edge.
    dcov, mats = {}, {}
    for k, h in (("m4", 4), ("m12", 12), ("m24", 24), ("m48", 48)):
        mats[k] = ep[mature_mask(ep.hr, h, now)]
        dcov[k] = float(mats[k][k].notna().mean()) if len(mats[k]) else np.nan
    mat = mats["m24"]
    P("| Check | Value | Target | Status |\n|---|---|---|---|")
    for f, v in cov.items():
        ok = np.isfinite(v) and v >= (0.90 if f == "spread_bps" else 0.95)
        P(f"| stamp `{f}` (this week) | {fmt(v, 3, False)} | ≥ {'0.90' if f == 'spread_bps' else '0.95'} | {'✅' if ok else '❌'} |")
        if not ok:
            flags.append(f"stamp coverage {f} {fmt(v, 3, False)}")
    ok = np.isfinite(tier_cov) and tier_cov >= 0.95
    P(f"| `gate_outcome` stamped (this week) | {fmt(tier_cov, 3, False)} | ≥ 0.95 | {'✅' if ok else '❌ (needs migration-168 code deployed)'} |")
    if not ok:
        flags.append("gate_outcome coverage")
    for k, v in dcov.items():
        if not np.isfinite(v):
            P(f"| drift `{k}` on matured episodes | – | ≥ 0.95 | ⏳ none matured yet |")
            continue
        ok = v >= 0.95
        P(f"| drift `{k}` on matured episodes (n {len(mats[k])}) | {fmt(v, 3, False)} | ≥ 0.95 | {'✅' if ok else '❌'} |")
        if not ok:
            flags.append(f"drift coverage {k}")
    hb_ts, hb_txt = fetch_heartbeat()
    hb_age = (now - hb_ts).total_seconds() / 3600 if hb_ts else np.nan
    ok = np.isfinite(hb_age) and hb_age <= HEARTBEAT_MAX_AGE_H
    P(f"| `kairos-daily` heartbeat (hourly) | {fmt(hb_age, 1, False)}h ago | ≤ {HEARTBEAT_MAX_AGE_H:.0f}h | "
      f"{'✅' if ok else '❌ ' + hb_txt} |")
    if not ok:
        flags.append("kairos-daily heartbeat coverage")
    n_rec = int((ep.tier_src == "recon").sum())
    P(f"| pre-168 episodes with reconstructed tier | {n_rec} | – | – |")
    m24c = mats["m24"]
    pcov = float(m24c.mae24.notna().mean()) if len(m24c) else np.nan
    if not (np.isfinite(pcov) and pcov > 0):
        P("| path `mae24` on matured episodes | – | ≥ 0.95 | ⏳ migration 170 / backfill pending |")
    else:
        ok = pcov >= MIN_COVER
        P(f"| path `mae24` on matured episodes (n {len(m24c)}) | {fmt(pcov, 3, False)} | ≥ 0.95 | {'✅' if ok else '❌'} |")
        if not ok:
            flags.append("path coverage mae24")
    if not runs.empty:
        done = runs[~runs.is_open.astype(bool) & ~runs.ended_by_gap.astype(bool)]
        per_cell = done.groupby(["symbol", "cell"]).size()
        gap_wk = int(runs[runs.started_at >= week_start].starts_after_gap.astype(bool).sum())
        P(f"| completed runs (pooled) | {len(done)} | – | – |")
        P(f"| median run length (h) | {fmt(float(done.length_h.median()), 1, False)} | – | – |")
        P(f"| (symbol, cell) pairs with ≥ 30 completed runs | {(per_cell >= 30).sum()} / {len(per_cell)} | pooling required below 30 | – |")
        P(f"| runs starting after a data gap (restart/weekend) this week | {gap_wk} | – | – |")
    # Sweep cadence: light sweeps fire every 15 min ON the hour (:00/:15/:30/:45).
    # A slot is "covered" when it has ≥1 light-sweep row; coverage is measured
    # per 15-min slot, never from gaps between consecutive rows (a 15-min gap
    # is the normal cadence, not a miss).
    try:
        sl = _get("nt_signal_sim", "ts,symbol", {
            "source": "eq.sweep-light",
            "ts": f"gte.{week_start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "order": "ts.asc"})
        sdf = pd.DataFrame(sl)
    except Exception as exc:  # noqa: BLE001
        sdf = pd.DataFrame()
        P(f"| sweep cadence | – | – | ⚠️ query failed: {type(exc).__name__} |")
    if not sdf.empty:
        st = pd.to_datetime(sdf.ts, utc=True, format="ISO8601")
        got = set(st.dt.floor("15min"))
        per_slot = sdf.assign(slot=st.dt.floor("15min")).groupby("slot").symbol.nunique()
        lo = max(pd.Timestamp(week_start), st.min().floor("15min"))
        hi = (pd.Timestamp(week_end) - pd.Timedelta(minutes=15)).floor("15min")
        # Weekday slots only (Fri 21:00 → Sun 21:00 UTC excluded: FX/indices
        # closed, so weekend coverage is not a health signal).
        exp = [s for s in pd.date_range(lo, hi, freq="15min")
               if not (s.dayofweek == 5 or (s.dayofweek == 4 and s.hour >= 21)
                       or (s.dayofweek == 6 and s.hour < 21))]
        miss = [s for s in exp if s not in got]
        runs_m, cur = [], []
        for s in miss:
            if cur and s - cur[-1] != pd.Timedelta(minutes=15):
                runs_m.append(cur)
                cur = []
            cur.append(s)
        if cur:
            runs_m.append(cur)
        txt = "; ".join(f"{r[0]:%m-%d %H:%M}–{r[-1] + pd.Timedelta(minutes=15):%H:%M} ({len(r)})" for r in runs_m) or "none"
        P(f"| light-sweep 15-min slots covered (weekday) | {len(exp) - len(miss)} / {len(exp)} | 100% | "
          f"{'✅' if not miss else '⚠️ uncovered: ' + txt} |")
        partial = int((per_slot < per_slot.max()).sum())
        P(f"| slots with fewer symbols than the max ({int(per_slot.max())}) | {partial} | – | – |")
        if miss:
            flags.append(f"sweep slots uncovered {len(miss)}")
    P("")

    # B. population -----------------------------------------------------
    P("## B. Population: all signals, split by gate outcome\n")
    P("| Tier | Episodes (cum.) | This week | Share |\n|---|---|---|---|")
    for t in TIERS[1:]:
        n, nw = int((ep.tier == t).sum()), int((this_wk.tier == t).sum())
        P(f"| {t} | {n} | {nw} | {n / len(ep):.1%} |")
    P(f"| **all** | {len(ep)} | {len(this_wk)} | 100% |")
    sup = ep[ep.tier == "suppressed"]
    if len(sup):
        # v_kairos_signal_episodes carries suppress_reason from the hour's FIRST
        # tick; when a later tick was the suppressed one it is NULL there — use
        # any_cooldown (bool_or over the hour) to name it.
        rsn = sup.suppress_reason.where(sup.suppress_reason.notna(), np.where(
            sup.any_cooldown.astype(bool), "cooldown (later tick in hour)", "other (later tick in hour)"))
        P("\nSuppression reasons: " + " · ".join(f"{k} {v}" for k, v in rsn.value_counts().items()))
    gf = ep[ep.tier == "gate_fail"].failed_gates.dropna().str.split(",").explode()
    if len(gf):
        P("\nFailed gates (gate_fail episodes; one episode can fail several): " + " · ".join(f"`{k}` {v}" for k, v in gf.value_counts().items()))
    P("\n`unknown` = captured before migration-168 stamps (not qualified; suppressed vs gate_fail not recorded).\n")

    # C. edge by population ----------------------------------------------
    P("## C. Geometry-free edge by population (cumulative, matured cohort only)\n")
    cover_ok = len(mat) > 0 and np.isfinite(dcov["m24"]) and dcov["m24"] >= MIN_COVER
    if not cover_ok:
        P(f"⏳ **Not evaluable yet.** Edge statistics use only episodes whose m₂₄ bar has closed "
          f"(+{BACKFILL_LAG_H}h) with m₂₄ coverage ≥ {MIN_COVER:.0%} (matured: {len(mat)}, m₂₄ coverage "
          f"{fmt(dcov['m24'], 3, False)}). Drift on younger rows exists only where TP/SL settled early — "
          f"geometry-selected, not read.\n")
    ep_all = ep
    ep = mat.copy() if cover_ok else mat.iloc[0:0].copy()
    for c in ("m4", "m24"):
        ep[f"{c}_dm"] = ep[c] - ep.groupby("symbol")[c].transform("mean")
    P("| Population | n (m24) | m₄ mean [95% CI] | m₂₄ mean [95% CI] | hit rate m₂₄ | P(m₂₄ ≤ 0) |\n|---|---|---|---|---|---|")
    for t in TIERS:
        g = ep if t == "all" else ep[ep.tier == t]
        if len(g) < 5:
            P(f"| {t} | {int(g.m24.notna().sum())} | – | – | – | – |")
            continue
        m4 = boot_mean(g.m4.to_numpy(), g.clu.to_numpy(), a.n_boot)
        m24 = boot_mean(g.m24.to_numpy(), g.clu.to_numpy(), a.n_boot)
        hr = float((g.m24.dropna() > 0).mean()) if g.m24.notna().any() else np.nan
        P(f"| {t} | {int(g.m24.notna().sum())} | {fmt(m4[0])} [{fmt(m4[1])}, {fmt(m4[2])}] | "
          f"{fmt(m24[0])} [{fmt(m24[1])}, {fmt(m24[2])}] | {fmt(hr, 3, False)} | {fmt(m24[3], 3, False)} |")
    P("")
    for c in ("m4", "m24"):
        obs, p = circ_null(ep, c, a.n_null)
        ext = np.isfinite(p) and p < EXTREME_P
        P(f"- All signals {c} vs circular-shift null (direction rotated within symbol): observed {fmt(obs)} ATR · p {fmt(p, 3, False)}{' **EXTREME**' if ext else ''}")
        if ext:
            flags.append(f"all-signal {c} extreme vs null (p {p:.4f})")
    P("\nComparisons (within-symbol demeaned, symbol|day cluster bootstrap):\n")
    P("| Comparison | Δ m₄ [95% CI] | Δ m₂₄ [95% CI] | p (m₂₄) | Reads as |\n|---|---|---|---|---|")
    comps = [("qualified − suppressed", "qualified", "suppressed",
              "does cooldown/dedup throw away signals as good as the published ones?"),
             ("suppressed − gate_fail", "suppressed", "gate_fail", "do the gates select edge (among non-published)?"),
             ("(qualified+suppressed) − gate_fail", ("qualified", "suppressed"), "gate_fail", "gates as a whole")]
    for name, ta, tb, meaning in comps:
        A_ = ep[ep.tier.isin(ta if isinstance(ta, tuple) else (ta,))]
        B_ = ep[ep.tier == tb]
        d4 = boot_diff(A_, B_, "m4_dm", a.n_boot)
        d24 = boot_diff(A_, B_, "m24_dm", a.n_boot)
        P(f"| {name} | {fmt(d4[0])} [{fmt(d4[1])}, {fmt(d4[2])}] | {fmt(d24[0])} [{fmt(d24[1])}, {fmt(d24[2])}] | {fmt(d24[3], 3, False)} | {meaning} |")
        if np.isfinite(d24[3]) and d24[3] < EXTREME_P:
            flags.append(f"{name} extreme (p {d24[3]:.4f})")
    P("")

    # D. edge vs regime age (V2 preview) ---------------------------------
    P("## D. Edge vs regime age: all signals (V2 live preview, matured cohort)\n")
    P(f"Regime age at signal, all {len(ep_all)} episodes (descriptive): "
      + " · ".join(f"{k} {v}" for k, v in band(ep_all.regime_age_h.dropna()).value_counts(sort=False).items()) + "\n")
    s2 = ep[~ep.age_left_censored.fillna(True).astype(bool) & ep.regime_age_h.notna()].copy()
    if len(s2) >= 20:
        s2["band"] = band(s2.regime_age_h)
        t = s2.groupby("band", observed=True).agg(n=("m24", "size"), m4=("m4", "mean"), m24=("m24", "mean"), m48=("m48", "mean"))
        P("| Age band | n | m₄ | m₂₄ | m₄₈ |\n|---|---|---|---|---|")
        for b, r in t.iterrows():
            P(f"| {b} | {int(r.n)} | {fmt(r.m4)} | {fmt(r.m24)} | {fmt(r.m48)} |")
    else:
        P("Too few uncensored episodes with a regime age.")
    P("")

    # E. regime-exit hazard (V4 preview) -----------------------------------
    P("## E. Regime-exit hazard: live runs (V4 live preview)\n")
    if not runs.empty:
        r = runs[~runs.starts_after_gap.astype(bool)].copy()
        r["event"] = ~r.is_open.astype(bool) & ~r.ended_by_gap.astype(bool)
        L_ = np.ceil(r.length_h.to_numpy()).astype(int)
        ev = r.event.to_numpy()
        rows = []
        for lo, hi, lab in ((0, 6, "0–6h"), (6, 12, "6–12h"), (12, 24, "12–24h"), (24, 48, "24–48h"), (48, 10**6, "≥48h")):
            at = np.clip(np.minimum(L_, hi) - lo, 0, None).sum()
            e = ((L_ > lo) & (L_ <= hi) & ev).sum()
            rows.append((lab, int(e), int(at), e / at if at else np.nan))
        P("| Age band | exits | hours at risk | hazard / h |\n|---|---|---|---|")
        for lab, e, at, hz in rows:
            P(f"| {lab} | {e} | {at} | {fmt(hz, 3, False)} |")
        ek = r[r.event].exit_kind.value_counts(normalize=True)
        P("\nExit compass (completed runs): " + " · ".join(f"{k} {v:.0%}" for k, v in ek.items()))
        # Kaplan-Meier (non-parametric, censoring-aware) vs the memoryless
        # geometric reference, and the conditional "remaining regime clock".
        km = kaplan_meier(r.length_h.to_numpy(), ev)
        if len(km):
            P(f"\nKaplan-Meier survival of regime runs (n {len(r)}, {int(ev.sum())} exits, "
              f"{int((~ev).sum())} censored; median {fmt(km_median(km), 1, False)}h):\n")
            P("| Age x | S(x) KM [95% CI] | S(x) geometric (memoryless) | at risk |\n|---|---|---|---|")
            for x in (2, 6, 12, 24, 48):
                row = km[km.t <= x]
                lo_, hi_ = (row.lo.iloc[-1], row.hi.iloc[-1]) if len(row) else (1.0, 1.0)
                n_at = int((r.length_h.to_numpy() >= x).sum())
                P(f"| {x}h | {fmt(km_at(km, x), 3, False)} [{fmt(lo_, 3, False)}, {fmt(hi_, 3, False)}] | "
                  f"{fmt(geometric_survival(r.length_h.to_numpy(), ev, x), 3, False)} | {n_at} |")
            P("\nRemaining regime clock — P(same run still alive Δ later | a signal fires at run age a) "
              "(KM; '–' = beyond observed run lengths):\n")
            P("| Signal at age a | Δ 4h | Δ 12h | Δ 24h | Δ 48h |\n|---|---|---|---|---|")
            for a0 in (0, 2, 6, 12, 24):
                P(f"| {a0}h | " + " | ".join(fmt(conditional_survival(km, a0, dd), 3, False)
                                           for dd in (4, 12, 24, 48)) + " |")
    P("")

    # E2. fixed-horizon path risk (geometry-free MAE/MFE, migration 170) ---
    P("## E2. Path risk: fixed-horizon adverse excursion (all signals, matured per horizon)\n")
    P("MAE_h / MFE_h = worst / best move against / with the signal over bars 1..h, sweep-ATR units, "
      "walked the full h bars (independent of TP/SL). `m_h | stop k` = endpoint drift with a k-ATR "
      "stop and no target (stopped → −k). Reporting grid only — no knob reads it.\n")
    have_path = ep_all.mae24.notna().any()
    if not have_path:
        P("⏳ No path columns yet (migration 170 + the hourly backfill).")
    else:
        P("| h | n | median MAE | median MFE | P(MAE ≥ 1) | P(MAE ≥ 2) | P(MAE ≥ 3) | m_h mean / median | m_h \\| stop 2 mean [95% CI] / median | ghost share† |")
        P("|---|---|---|---|---|---|---|---|---|---|")
        for h in PATH_H:
            g = ep_all[mature_mask(ep_all.hr, h, now)]
            g = g[g[f"mae{h}"].notna() & g[f"m{h}"].notna()]
            if len(g) < 5:
                P(f"| {h}h | {len(g)} | – | – | – | – | – | – | – | – |")
                continue
            mae, m = g[f"mae{h}"].to_numpy(), g[f"m{h}"].to_numpy()
            stv = stop_truncated(m, mae, 2.0)
            st = boot_mean(stv, g.clu.to_numpy(), a.n_boot)
            pos = m > 0
            ghost = float((mae[pos] >= 2.0).mean()) if pos.any() else np.nan
            P(f"| {h}h | {len(g)} | {fmt(np.median(mae), 2, False)} | {fmt(np.median(g[f'mfe{h}']), 2, False)} | "
              + " | ".join(fmt(float((mae >= k).mean()), 3, False) for k in STOP_K)
              + f" | {fmt(float(np.mean(m)))} / {fmt(float(np.median(m)))} | {fmt(st[0])} [{fmt(st[1])}, {fmt(st[2])}] / "
                f"{fmt(float(np.nanmedian(stv)))} | {fmt(ghost, 3, False)} |")
        P("\n† share of episodes with m_h > 0 whose MAE_h reached 2 ATR first (the 'ghost edge' case). "
          "Read medians first: means are dominated by a few large-ATR moves, and the stop caps exactly those, "
          "so a stopped mean above the endpoint mean is tail-capping, not edge. Both the stop fill (−k, no gap) "
          "and the MAE window (starts at the first full bar after entry) are optimistic.\n")
        g = ep_all[mature_mask(ep_all.hr, 24, now) & ep_all.mae24.notna() & ep_all.m24.notna()]
        P("By tier (h = 24):\n")
        P("| Tier | n | P(MAE₂₄ ≥ 1) | P(MAE₂₄ ≥ 2) | m₂₄ | m₂₄ \\| stop 2 [95% CI] |\n|---|---|---|---|---|---|")
        for t in ("qualified", "suppressed", "gate_fail"):
            gt = g[g.tier == t]
            if len(gt) < 5:
                P(f"| {t} | {len(gt)} | – | – | – | – |")
                continue
            st = boot_mean(stop_truncated(gt.m24.to_numpy(), gt.mae24.to_numpy(), 2.0), gt.clu.to_numpy(), a.n_boot)
            P(f"| {t} | {len(gt)} | {fmt(float((gt.mae24 >= 1).mean()), 3, False)} | "
              f"{fmt(float((gt.mae24 >= 2).mean()), 3, False)} | {fmt(float(gt.m24.mean()))} | "
              f"{fmt(st[0])} [{fmt(st[1])}, {fmt(st[2])}] |")
        viol = int(((g.m24 < -g.mae24 - 1e-6) | (g.m24 > g.mfe24 + 1e-6)).sum())
        P(f"\nInvariant m₂₄ ∈ [−MAE₂₄, +MFE₂₄]: {viol} violations of {len(g)}"
          + (" ❌" if viol else " ✅"))
        if viol:
            flags.append(f"path coverage invariant: {viol} m24 outside excursion band")
    P("")

    # F. factors (V7 preview) --------------------------------------------
    P("## F. Factors vs forward edge: all signals (V7 live preview, descriptive)\n")
    fx = ep[ep.hr >= STAMPS_LIVE]
    P("| Factor | n | Spearman ρ(factor, m₂₄), within-symbol ranks |\n|---|---|---|")
    for f in FACTORS:
        g = fx[[f, "m24", "symbol"]].dropna()
        if len(g) < 30:
            P(f"| `{f}` | {len(g)} | – (n < 30) |")
            continue
        rf = g.groupby("symbol")[f].rank(pct=True)
        rm = g.groupby("symbol")["m24"].rank(pct=True)
        P(f"| `{f}` | {len(g)} | {fmt(float(np.corrcoef(rf, rm)[0, 1]))} |")
    P("")

    # G. summary ---------------------------------------------------------
    P("## G. This week's read\n")
    P("- Data health: " + ("✅ all checks pass" if not any("coverage" in f for f in flags) else "❌ " + "; ".join(f for f in flags if "coverage" in f)))
    slot_f = [f for f in flags if f.startswith("sweep slots")]
    if slot_f:
        P("- Sweep cadence: ⚠️ " + slot_f[0] + " (see A — check on-start logs for restarts)")
    ext = [f for f in flags if "extreme" in f]
    P("- Extreme interim results (p < 0.001): " + ("; ".join(ext) if ext else "none"))
    # H. power / decision readiness (replaces the W5 calendar check) ---------
    P("\n## H. Decision readiness (power criterion, checked every look)\n")
    ga = ep[ep.tier.isin(("qualified", "suppressed"))]
    gb = ep[ep.tier == "gate_fail"]
    hw, sd = projected_halfwidth(ga, gb, "m24_dm", a.n_boot) if len(ep) else (np.nan, np.nan)
    ready = np.isfinite(hw) and np.isfinite(sd) and sd > 0 and hw <= POWER_SD_FRAC * sd
    unc = ep[~ep.age_left_censored.fillna(True).astype(bool)] if len(ep) else ep
    n_old = int((unc.regime_age_h >= 12).sum()) if len(unc) else 0
    P(f"- gates-vs-rest Δm₂₄ 95% CI half-width {fmt(hw, 3, False)} vs {POWER_SD_FRAC}·SD {fmt(POWER_SD_FRAC * sd, 3, False) if np.isfinite(sd) else '–'} "
      f"→ {'✅ **G2.1–G2.3 decidable now**' if ready else '⏳ keep collecting'} (decided on CI width only, never on the effect)")
    P(f"- G2.4/G2.5 need uncensored episodes aged ≥ 12h: {n_old} (target ≥ 200; restart-safe state from 2026-09-25 stops restarts censoring runs)")
    P(f"- Next: {'G2 decision (pre-registered family, Holm)' if ready else ('next accelerated look in 24h' if a.accel else 'W' + str((wk or 0) + 1))}")
    P(f"\n_generated {now:%Y-%m-%d %H:%M} UTC · {a.n_null} nulls · {a.n_boot} bootstraps · {time.time() - t0:.0f}s_")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f_md = out / f"{week_end:%Y%m%d}_{wlabel}.md"
    f_md.write_text("\n".join(L), encoding="utf-8")
    (out / f"{week_end:%Y%m%d}_{wlabel}.json").write_text(json.dumps(
        {"week": wlabel, "week_end": week_end.isoformat(), "n_episodes": len(ep), "flags": flags,
         "stamp_coverage": cov, "drift_coverage": dcov, "ci_halfwidth_m24": hw, "sd_m24": sd,
         "decidable": bool(ready), "n_uncensored_age_ge_12h": n_old}, indent=1, default=float))
    print("\n".join(L))
    print(f"\nreport: {f_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
