"""
kairos_postflip_rescore.py — live era-split scorecard + G2.2 power (triage D3–D5).

Read-only. Uses kairos_quant for era bounds and no-skill maths.
Outputs worklog/kairos/reports/20260926_postflip_rescore.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_postflip_rescore.py
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.kairos_quant import (  # noqa: E402
    ATR_SCALE_LIVE,
    ERA_ORDER,
    accuracy_vs_no_skill,
    calibration_error,
    claimed_edge,
    era_of,
    no_skill_line,
    symbol_day_clusters,
)

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260926_postflip_rescore.md"


def fetch(table: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            p = {**params, "limit": "1000", "offset": str(off)}
            r = c.get(f"{U}/rest/v1/{table}", headers=H, params=p)
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def reconstruct_tier(row: dict) -> str:
    go = row.get("gate_outcome")
    if go in ("qualified", "suppressed", "gate_fail"):
        return go
    if row.get("qualified") is True:
        return "qualified"
    try:
        conf = float(row.get("regime_conf") or 0)
        kelly = float(row.get("effective_kelly") or 0)
        ev = float(row.get("ev") or 0)
        tp = float(row.get("tp_bricks") or 0)
        sl = float(row.get("sl_bricks") or 0)
        pwin = row.get("p_win_dir")
        if pwin is None:
            pwin = row.get("p_win")
        pwin = float(pwin or 0)
    except (TypeError, ValueError):
        return "gate_fail"
    if conf >= 0.60 and kelly >= 0.02 and ev > 0 and sl > 0 and (tp / sl) >= 1.5 and pwin >= 0.50:
        return "suppressed"
    return "gate_fail"


def main() -> None:
    now = datetime.now(timezone.utc)
    cols = (
        "signal_id,symbol,signal,ts,exit_ts,outcome,qualified,p_win,predicted_p_win_raw,"
        "entry_price,stop_loss,take_profit,regime,regime_conf,effective_kelly,ev,"
        "p_win_dir,tp_bricks,sl_bricks,gate_outcome,drift_24h_pct,atr"
    )
    print("fetching nt_signal_sim…")
    raw = fetch("nt_signal_sim", {
        "select": cols,
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "order": "ts.asc",
    })
    print(f"  {len(raw)} rows")

    recs = []
    for r in raw:
        ts = parse_ts(r.get("ts"))
        exit_ts = parse_ts(r.get("exit_ts"))
        outcome = r.get("outcome")
        p_win = r.get("p_win")
        try:
            p_win = float(p_win) if p_win is not None else None
        except (TypeError, ValueError):
            p_win = None
        tp_b = r.get("tp_bricks")
        sl_b = r.get("sl_bricks")
        try:
            tp_b = float(tp_b) if tp_b is not None else None
            sl_b = float(sl_b) if sl_b is not None else None
        except (TypeError, ValueError):
            tp_b = sl_b = None
        # distances: prefer brick columns; fall back to absolute SL/TP distance in price
        if tp_b and sl_b and tp_b > 0 and sl_b > 0:
            dist_tp, dist_sl = tp_b, sl_b
        else:
            try:
                ep = float(r.get("entry_price") or 0)
                dist_tp = abs(float(r.get("take_profit") or 0) - ep) or None
                dist_sl = abs(float(r.get("stop_loss") or 0) - ep) or None
            except (TypeError, ValueError):
                dist_tp = dist_sl = None

        d24 = r.get("drift_24h_pct")
        try:
            d24 = float(d24) if d24 is not None else None
        except (TypeError, ValueError):
            d24 = None
        atr = r.get("atr")
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            atr = None
        entry = r.get("entry_price")
        try:
            entry = float(entry) if entry is not None else None
        except (TypeError, ValueError):
            entry = None

        m24 = None
        if d24 is not None and atr and atr > 0 and entry:
            # drift_24h_pct is % of entry; convert to ATR units
            m24 = (d24 / 100.0) * entry / atr

        recs.append({
            "symbol": r.get("symbol"),
            "signal": r.get("signal"),
            "ts": ts,
            "exit_ts": exit_ts,
            "outcome": outcome,
            "qualified": r.get("qualified") is True,
            "p_win": p_win,
            "tier": reconstruct_tier(r),
            "dist_tp": dist_tp,
            "dist_sl": dist_sl,
            "m24": m24,
            "era": era_of(ts),
            "era_exit": era_of(exit_ts) if exit_ts else None,
        })

    decided = [x for x in recs if x["outcome"] in ("tp_hit", "sl_hit") and x["p_win"] is not None]
    for x in decided:
        tp_first = x["outcome"] == "tp_hit"
        x["err"] = calibration_error(x["p_win"], tp_first)
        ns = no_skill_line(x["dist_tp"], x["dist_sl"]) if x["dist_tp"] and x["dist_sl"] else None
        x["no_skill"] = ns
        x["acc"] = accuracy_vs_no_skill(tp_first, x["dist_tp"], x["dist_sl"]) if ns is not None else None
        x["claimed"] = claimed_edge(x["p_win"], x["dist_tp"], x["dist_sl"]) if ns is not None else None
        x["tp_first"] = tp_first

    # ── Scorecard by era × tier ──────────────────────────────────────────
    def block(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0}
        errs = [x["err"] for x in rows if x.get("err") is not None]
        accs = [x["acc"] for x in rows if x.get("acc") is not None]
        claims = [x["claimed"] for x in rows if x.get("claimed") is not None]
        tps = [x["tp_first"] for x in rows]
        nss = [x["no_skill"] for x in rows if x.get("no_skill") is not None]
        return {
            "n": len(rows),
            "tp_rate": sum(tps) / len(tps) if tps else None,
            "no_skill": statistics.fmean(nss) if nss else None,
            "mean_err": statistics.fmean(errs) if errs else None,
            "mean_acc": statistics.fmean(accs) if accs else None,
            "mean_claimed": statistics.fmean(claims) if claims else None,
            "mean_pwin": statistics.fmean([x["p_win"] for x in rows if x["p_win"] is not None]),
        }

    lines: list[str] = []
    lines.append("# Post-flip rescore (2026-09-26) — era-split scorecard + G2.2 power")
    lines.append("")
    lines.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · read-only · `kairos_quant` eras · ATR scale `{ATR_SCALE_LIVE}`.")
    lines.append("Unit: light buy/sell rows with a decided outcome (TP/SL) for scorecard; episodes for G2.2.")
    lines.append("")
    lines.append("## 0. Population by signal era (ts)")
    lines.append("")
    lines.append("| era | n signals | decided | qualified | suppressed | gate_fail |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for era in ERA_ORDER:
        rows = [x for x in recs if x["era"] == era]
        dec = [x for x in rows if x["outcome"] in ("tp_hit", "sl_hit")]
        lines.append(
            f"| {era} | {len(rows)} | {len(dec)} | "
            f"{sum(1 for x in rows if x['tier']=='qualified')} | "
            f"{sum(1 for x in rows if x['tier']=='suppressed')} | "
            f"{sum(1 for x in rows if x['tier']=='gate_fail')} |"
        )
    lines.append("")
    n_post = sum(1 for x in decided if x["era"] == "post_resolver" or x["era_exit"] == "post_resolver")
    lines.append(
        f"**Post-resolver decided (ts or exit in era):** {n_post}. "
        "Promotion bar for evidence pool (ΔBrier ≤ −0.05 on S2) **cannot be met on this era alone** until "
        "qualified S2 volume exists here."
    )
    lines.append("")

    lines.append("## 1. Scorecard D–E by era × tier (decided only)")
    lines.append("")
    lines.append("| era | tier | n | TP rate | no-skill | mean err | mean acc | mean claimed | mean p_win |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")

    def fmt(v, nd=3):
        return "—" if v is None else f"{v:.{nd}f}"

    for era in ERA_ORDER:
        for tier in ("qualified", "suppressed", "gate_fail", "all"):
            rows = decided if tier == "all" else [x for x in decided if x["tier"] == tier and x["era"] == era]
            if tier != "all" and era:
                rows = [x for x in decided if x["tier"] == tier and x["era"] == era]
            if tier == "all":
                rows = [x for x in decided if x["era"] == era]
            b = block(rows)
            if b["n"] == 0:
                continue
            lines.append(
                f"| {era} | {tier} | {b['n']} | {fmt(b['tp_rate'])} | {fmt(b['no_skill'])} | "
                f"{fmt(b['mean_err'])} | {fmt(b['mean_acc'])} | {fmt(b['mean_claimed'])} | {fmt(b['mean_pwin'])} |"
            )
    # post-resolver special: by exit era
    post_exit = [x for x in decided if x["era_exit"] == "post_resolver"]
    b = block(post_exit)
    lines.append("")
    lines.append(f"**Exit-era post_resolver (incl. earlier entries):** n={b.get('n',0)} "
                 f"TP={fmt(b.get('tp_rate'))} err={fmt(b.get('mean_err'))} acc={fmt(b.get('mean_acc'))}")
    lines.append("")

    # ── Evidence-pool Brier walk-forward (production-like vs q+s) ────────
    lines.append("## 2. Evidence-pool ΔBrier walk-forward (all eras + post-ATR)")
    lines.append("")
    # Use decided rows as both evidence and targets (next qualified as S2 proxy:
    # next decided qualified signal same symbol after an earlier decided signal).
    q_dec = [x for x in decided if x["tier"] == "qualified"]
    q_dec.sort(key=lambda x: (x["symbol"], x["ts"] or now))

    def walk(pool_tiers: set[str], half_life_h: float | None, min_ev: int, use_hourly_latency: bool) -> dict:
        """ΔBrier on qualified S2 targets: p' = p_win - bias, bias from prior evidence."""
        by_sym: dict[str, list[dict]] = defaultdict(list)
        for x in decided:
            if x["tier"] in pool_tiers and x["exit_ts"] is not None:
                by_sym[x["symbol"]].append(x)
        for v in by_sym.values():
            v.sort(key=lambda x: x["exit_ts"])
        scored = 0
        d_brier = []
        uncor = []
        cor = []
        for t in q_dec:
            if t["ts"] is None or t["p_win"] is None:
                continue
            ev = [e for e in by_sym.get(t["symbol"], []) if e["exit_ts"] < t["ts"]]
            if len(ev) < min_ev:
                continue
            errs = []
            wts = []
            for e in ev:
                age_h = (t["ts"] - e["exit_ts"]).total_seconds() / 3600.0
                if use_hourly_latency and age_h < 0.5:
                    continue  # evidence would not be written yet under nightly-only
                if half_life_h:
                    w = 0.5 ** (age_h / half_life_h)
                else:
                    # production-like: 3-day half-life on entry age, 7d window
                    entry_age_d = (t["ts"] - e["ts"]).total_seconds() / 86400.0
                    if entry_age_d < 0 or entry_age_d > 7:
                        continue
                    w = 0.5 ** (entry_age_d / 3.0)
                errs.append(e["err"])
                wts.append(w)
            if not errs or sum(wts) <= 0:
                continue
            bias = sum(e * w for e, w in zip(errs, wts)) / sum(wts)
            p = min(1.0, max(0.0, t["p_win"] - bias))
            y = 1.0 if t["tp_first"] else 0.0
            uncor.append((t["p_win"] - y) ** 2)
            cor.append((p - y) ** 2)
            scored += 1
        if not scored:
            return {"n": 0}
        return {
            "n": scored,
            "brier0": statistics.fmean(uncor),
            "brier1": statistics.fmean(cor),
            "d_brier": statistics.fmean(cor) - statistics.fmean(uncor),
        }

    configs = [
        ("all tiers, prod 3d HL, nightly", {"qualified", "suppressed", "gate_fail"}, 72.0, 5, True),
        ("q+s, prod 3d HL, nightly", {"qualified", "suppressed"}, 72.0, 5, True),
        ("q+s, 3h HL, hourly", {"qualified", "suppressed"}, 3.0, 3, False),
        ("q+s, 3h HL, nightly", {"qualified", "suppressed"}, 3.0, 3, True),
        ("gate_fail, 3h HL, hourly", {"gate_fail"}, 3.0, 5, False),
    ]
    lines.append("| rule | S2 scored | Brier uncor | Brier cor | ΔBrier |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, pool, hl, min_ev, nightly in configs:
        r = walk(pool, hl, min_ev, nightly)
        if r.get("n", 0) == 0:
            lines.append(f"| {name} | 0 | — | — | — |")
        else:
            lines.append(
                f"| {name} | {r['n']} | {r['brier0']:.3f} | {r['brier1']:.3f} | {r['d_brier']:+.3f} |"
            )
    lines.append("")
    lines.append(
        "Note: walk-forward uses prior **decided** same-symbol evidence only (exit_ts < S2 ts). "
        "This is a scorecard proxy, not a byte-identical `fetch_calibration_bias` replay. "
        "Post-flip S2 volume is still too small for the −0.05 promote bar."
    )
    lines.append("")

    # ── G2.2: gates select direction (Δm24) ─────────────────────────────
    lines.append("## 3. G2.2 — do gates select direction (Δm₂₄)?")
    lines.append("")
    # episodes: one per symbol×direction×hour from m24-available rows
    eps = [x for x in recs if x["m24"] is not None]
    # dedupe symbol×signal×hour
    seen = set()
    episodes = []
    for x in eps:
        if x["ts"] is None:
            continue
        key = (x["symbol"], x["signal"], x["ts"].strftime("%Y%m%d%H"))
        if key in seen:
            continue
        seen.add(key)
        episodes.append(x)

    def demeaned_m24(rows: list[dict]) -> dict[tuple, float]:
        """Within-symbol demeaned m24 keyed by episode id (pre-registration)."""
        by: dict[str, list[float]] = defaultdict(list)
        for x in rows:
            by[x["symbol"]].append(x["m24"])
        out = {}
        for i, x in enumerate(rows):
            vals = by[x["symbol"]]
            mu = statistics.fmean(vals)
            out[id(x)] = x["m24"] - mu
        return out

    dm = demeaned_m24(episodes)
    for x in episodes:
        x["m24_d"] = dm[id(x)]

    def cluster_boot_diff(a: list[dict], b: list[dict], n_boot: int = 500, key: str = "m24_d") -> tuple[float, float, float]:
        """Δ mean(demeaned m24) a-b with symbol-day cluster bootstrap CI. Returns (delta, lo, hi)."""
        if not a or not b:
            return (float("nan"), float("nan"), float("nan"))

        def delta(rows_a, rows_b):
            ma = statistics.fmean([x[key] for x in rows_a])
            mb = statistics.fmean([x[key] for x in rows_b])
            return ma - mb

        point = delta(a, b)
        # cluster ids
        def clusters(rows):
            d = defaultdict(list)
            for x in rows:
                if x["ts"] is None:
                    continue
                d[(x["symbol"], x["ts"].date())].append(x)
            return list(d.values())

        ca, cb = clusters(a), clusters(b)
        if not ca or not cb:
            return (point, float("nan"), float("nan"))
        rng = np.random.default_rng(20260926)
        stats_ = []
        for _ in range(n_boot):
            sa = [x for _i in range(len(ca)) for x in ca[int(rng.integers(0, len(ca)))]]
            sb = [x for _i in range(len(cb)) for x in cb[int(rng.integers(0, len(cb)))]]
            if not sa or not sb:
                continue
            stats_.append(delta(sa, sb))
        if len(stats_) < 20:
            return (point, float("nan"), float("nan"))
        lo, hi = float(np.percentile(stats_, 2.5)), float(np.percentile(stats_, 97.5))
        return (point, lo, hi)

    qsup = [x for x in episodes if x["tier"] in ("qualified", "suppressed")]
    gfail = [x for x in episodes if x["tier"] == "gate_fail"]
    d, lo, hi = cluster_boot_diff(qsup, gfail)
    n_clusters = symbol_day_clusters([(x["ts"], x["symbol"]) for x in episodes if x["ts"]])
    lines.append(f"Episodes with matured m₂₄: **{len(episodes)}** · symbol-day clusters: **{n_clusters}**")
    lines.append(f"q∪s n={len(qsup)} mean m₂₄ (raw)={statistics.fmean([x['m24'] for x in qsup]) if qsup else float('nan'):.3f} · demeaned={statistics.fmean([x['m24_d'] for x in qsup]) if qsup else float('nan'):.3f}")
    lines.append(f"gate_fail n={len(gfail)} mean m₂₄ (raw)={statistics.fmean([x['m24'] for x in gfail]) if gfail else float('nan'):.3f} · demeaned={statistics.fmean([x['m24_d'] for x in gfail]) if gfail else float('nan'):.3f}")
    lines.append(f"**Δm₂₄ demeaned (q∪s − gate_fail) = {d:+.3f} [{lo:+.3f}, {hi:+.3f}]**")
    lines.append("")
    # power on demeaned
    m24s = [x["m24_d"] for x in episodes]
    sd = statistics.pstdev(m24s) if len(m24s) > 1 else float("nan")
    half = (hi - lo) / 2 if math.isfinite(lo) and math.isfinite(hi) else float("nan")
    lines.append(f"### H. Power criterion (demeaned m₂₄)")
    lines.append(f"- m₂₄ SD = {sd:.3f} · 0.2·SD = {0.2 * sd:.3f}")
    lines.append(f"- Δm₂₄ 95% CI half-width = {half:.3f} → **{'PASS' if half <= 0.2 * sd else 'keep collecting'}**")
    if half > 0.2 * sd and sd > 0:
        lines.append(f"- Required cluster inflation ≈ {(half / (0.2 * sd)) ** 2:.2f}× current clusters")
    else:
        lines.append("- At the power bar — G2.2 can be decided (go/no-go), not merely monitored.")
    lines.append("")
    if math.isfinite(d) and math.isfinite(lo):
        if hi < 0:
            verdict = "G2.2 **FAILS** (Δ < 0, CI excludes 0): gates select *worse* geometry-free direction than gate_fail."
        elif lo > 0:
            verdict = "G2.2 **PASSES** (Δ > 0, CI excludes 0)."
        elif d < 0:
            verdict = "G2.2 **NO PASS** (point estimate negative, CI spans 0): no evidence gates select direction."
        else:
            verdict = "G2.2 **NO PASS** (point estimate positive, CI spans 0): underpowered or no effect."
        lines.append(f"**Decision (pre-registered):** {verdict}")
        lines.append("")

    # era-split Δ
    lines.append("### Δm₂₄ (demeaned) by signal era")
    lines.append("")
    lines.append("| era | n eps | q∪s n | gate_fail n | Δm₂₄ | CI lo | CI hi |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for era in ERA_ORDER:
        rows = [x for x in episodes if x["era"] == era]
        if not rows:
            continue
        a = [x for x in rows if x["tier"] in ("qualified", "suppressed")]
        b = [x for x in rows if x["tier"] == "gate_fail"]
        dd, lo_e, hi_e = cluster_boot_diff(a, b, n_boot=200)
        lines.append(f"| {era} | {len(rows)} | {len(a)} | {len(b)} | {dd:+.3f} | {lo_e:+.3f} | {hi_e:+.3f} |")
    lines.append("")

    # ── Evidence completeness proxy (A) ─────────────────────────────────
    lines.append("## 4. Scorecard A–B proxy")
    lines.append("")
    cal = fetch("nt_calibration_records", {"select": "signal_id,closed_at,entered_at,outcome", "order": "closed_at.asc"})
    closed = [x for x in cal if x.get("closed_at")]
    post_cal = [x for x in closed if parse_ts(x["closed_at"]) and parse_ts(x["closed_at"]) >= datetime(2026, 9, 26, tzinfo=timezone.utc)]
    delays = []
    for x in closed[-2000:]:
        c, e = parse_ts(x.get("closed_at")), parse_ts(x.get("entered_at"))
        if c and e:
            delays.append((c - e).total_seconds() / 3600.0)
    lines.append(f"- `nt_calibration_records` rows: {len(cal)} · closed: {len(closed)} · closed since 09-26: {len(post_cal)}")
    if delays:
        delays.sort()
        lines.append(f"- entered→closed delay (recent {len(delays)}): median {statistics.median(delays):.1f}h · p90 {delays[int(0.9 * len(delays))]:.1f}h")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
