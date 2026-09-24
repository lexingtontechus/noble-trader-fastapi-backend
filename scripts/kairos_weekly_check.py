#!/usr/bin/env python3
"""
kairos_weekly_check.py — Kairos v3 G2 weekly check (live, geometry-free, read-only).

docs/scopes/20260924_KAIROS_V3_SCOPE.md §12. Runs on ALL light-sweep signals
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
W1_END = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)        # W1 = first full week after stamps went live 2026-09-24 08:00
H = (1, 4, 12, 24, 48)
FACTORS = ("rel_volume_1h", "clv_1h", "efficiency_24h", "atr_ratio_run", "spread_bps")
TIERS = ("all", "qualified", "suppressed", "gate_fail", "unknown")
EXTREME_P = 0.001
MATURE_H = 84          # 48h horizon + nightly backfill lag + margin
MIN_COVER = 0.95       # matured-cohort drift coverage required before any edge stat
RNG = np.random.default_rng(20260924)


# ─── data ──────────────────────────────────────────────────────────────


def _get(view: str, select: str, extra: dict | None = None) -> list[dict]:
    import httpx

    from regime_platform.core.settings import settings as s

    h = {"apikey": s.SUPABASE_KEY, "Authorization": f"Bearer {s.SUPABASE_KEY}"}
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
    end = week_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    ep = pd.DataFrame(_get("v_kairos_signal_episodes", cols,
                           {"hr": f"lt.{end}", "order": "symbol.asc,hr.asc,direction.asc"}))
    runs = pd.DataFrame(_get("v_kairos_regime_runs",
                             "symbol,run_id,cell,started_at,ended_at,length_h,exit_kind,"
                             "starts_after_gap,ended_by_gap,is_open",
                             {"started_at": f"lt.{end}", "order": "symbol.asc,started_at.asc"}))
    if not ep.empty:
        ep["hr"] = pd.to_datetime(ep["hr"], utc=True)
        ep["d"] = np.where(ep["direction"] == "buy", 1.0, -1.0)
        ep["day"] = ep["hr"].dt.strftime("%Y-%m-%d")
        ep["clu"] = ep["symbol"] + "|" + ep["day"]
        for c in ("m1", "m4", "m12", "m24", "m48", *FACTORS, "regime_age_h"):
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


# ─── report ────────────────────────────────────────────────────────────


def fmt(x, nd=3, sign=True):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-end", default="", help="UTC date (YYYY-MM-DD); default = now")
    ap.add_argument("--n-null", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--out", default=str(ROOT / "docs/cellpersist/weekly"))
    a = ap.parse_args()
    t0 = time.time()
    now = datetime.now(timezone.utc)
    week_end = (datetime.fromisoformat(a.week_end).replace(tzinfo=timezone.utc, hour=8)
                if a.week_end else now)
    week_start = week_end - timedelta(days=7)
    wk = max(0, int(np.ceil((week_end - W1_END).total_seconds() / (7 * 86400))) + 1)
    wlabel = f"W{wk}" if week_end >= W1_END - timedelta(days=7) else "W0"

    ep, runs = load(week_end)
    L: list[str] = []
    P = L.append
    P(f"# Kairos G2 weekly check — {wlabel} (week ending {week_end:%Y-%m-%d %H:%M} UTC)\n")
    P(f"Population: **all** light-sweep buy/sell signals since {GO_LIVE:%Y-%m-%d} (debounced), one episode per "
      f"symbol × direction × hour. Geometry-free: m_h = signed drift / sweep ATR (close-to-close SMA14 — "
      f"magnitudes are NOT comparable to P1's OHLC ATR; compare within live only). Interim looks flag only "
      f"p < {EXTREME_P}; the G2 decision is at W6.\n")
    if ep.empty:
        P("No episodes.")
        print("\n".join(L))
        return 1
    this_wk = ep[ep.hr >= week_start]
    flags: list[str] = []

    # A. data health ----------------------------------------------------
    P("## A. Data health (pass/fail every week)\n")
    stamped = this_wk[this_wk.hr >= datetime(2026, 9, 24, 8, tzinfo=timezone.utc)]
    cov = {f: float(stamped[f].notna().mean()) if len(stamped) else np.nan for f in FACTORS}
    tier_cov = float((stamped.tier != "unknown").mean()) if len(stamped) else np.nan
    # Matured cohort: the resolver + nightly kairos backfill (49–199h window)
    # have both had a pass. Younger rows only carry drift if they resolved
    # early (TP/SL touch) — that subset is geometry-selected and must never
    # be read as edge.
    mat = ep[ep.hr <= now - timedelta(hours=MATURE_H)]
    dcov = {k: float(mat[k].notna().mean()) if len(mat) else np.nan for k in ("m12", "m24", "m48")}
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
        P(f"| drift `{k}` on matured episodes | {fmt(v, 3, False)} | ≥ 0.95 | {'✅' if ok else '❌'} |")
        if not ok:
            flags.append(f"drift coverage {k}")
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
        P(f"⏳ **Not evaluable yet.** Edge statistics use only episodes ≥ {MATURE_H}h old with m₂₄ coverage "
          f"≥ {MIN_COVER:.0%} (matured: {len(mat)}, m₂₄ coverage {fmt(dcov['m24'], 3, False)}). Drift on younger "
          f"rows exists only where TP/SL settled early — geometry-selected, not read.\n")
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
    P("")

    # F. factors (V7 preview) --------------------------------------------
    P("## F. Factors vs forward edge: all signals (V7 live preview, descriptive)\n")
    fx = ep[ep.hr >= datetime(2026, 9, 24, 8, tzinfo=timezone.utc)]
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
    P(f"- Next: {'W' + str(wk + 1) if wk < 6 else 'G2 decision (pre-registered family, Holm)'}")
    P(f"\n_generated {now:%Y-%m-%d %H:%M} UTC · {a.n_null} nulls · {a.n_boot} bootstraps · {time.time() - t0:.0f}s_")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f_md = out / f"{week_end:%Y%m%d}_{wlabel}.md"
    f_md.write_text("\n".join(L), encoding="utf-8")
    (out / f"{week_end:%Y%m%d}_{wlabel}.json").write_text(json.dumps(
        {"week": wlabel, "week_end": week_end.isoformat(), "n_episodes": len(ep), "flags": flags,
         "stamp_coverage": cov, "drift_coverage": dcov}, indent=1, default=float))
    print("\n".join(L))
    print(f"\nreport: {f_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
