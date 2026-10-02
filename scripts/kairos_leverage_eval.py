"""
kairos_leverage_eval.py — Track A CLI: measure the withdrawal's calibration +
leverage effect on live data.

Scope: docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md §3 Track A.
Output: worklog/kairos/reports/YYYYMMDD_leverage_eval.md

Read-only. For every decided signal, reconstructs what the EV blend would have
produced WITH `p_markov_hold_n` present, and compares calibration (Brier) and
sizing (ev_scale) against what actually happened.

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_leverage_eval.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator import leverage_eval as le  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
LOOKBACK_DAYS = int(os.environ.get("LEV_LOOKBACK_DAYS", "45"))
JOIN_TOL_SEC = 600          # ±10 min between a sim row and its sweep_result row


def _fetch(table: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{U}/rest/v1/{table}", headers=H,
                      params={**params, "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def _ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def fetch_sims(since: str) -> list[dict]:
    return _fetch("nt_signal_sim", {
        "select": "signal_id,symbol,signal,ts,outcome,predicted_p_win_raw,"
                  "tp_bricks,sl_bricks,brick_size,legacy_markov_state",
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "outcome": "in.(tp_hit,sl_hit)",
        "predicted_p_win_raw": "not.is.null",
        "ts": f"gte.{since}",
        "order": "ts.asc",
    })


def fetch_sweeps(since: str) -> list[dict]:
    return _fetch("nt_sweep_result", {
        "select": "symbol,signal,sweep_timestamp,markov_p_up,markov_p_dn,p_timesfm",
        "source": "eq.sweep-light",
        "markov_p_up": "not.is.null",
        "sweep_timestamp": f"gte.{since}",
        "order": "sweep_timestamp.asc",
    })


def join(sims: list[dict], sweeps: list[dict]) -> tuple[list[dict], dict]:
    """Nearest-(symbol, ts) join, sim -> sweep. Reports its own match rate."""
    by_sym: dict[str, list[tuple[float, dict]]] = {}
    for s in sweeps:
        t = _ts(s.get("sweep_timestamp"))
        if t is None or not s.get("symbol"):
            continue
        by_sym.setdefault(s["symbol"], []).append((t, s))
    for v in by_sym.values():
        v.sort(key=lambda x: x[0])

    out: list[dict] = []
    no_sweep = no_dir = stale = 0
    for sim in sims:
        t = _ts(sim.get("ts"))
        sym = sim.get("symbol")
        cand = by_sym.get(sym)
        if t is None or not cand:
            no_sweep += 1
            continue
        # nearest by timestamp
        best = min(cand, key=lambda x: abs(x[0] - t))
        if abs(best[0] - t) > JOIN_TOL_SEC:
            stale += 1
            continue
        sw = best[1]
        # p_m = the mass the legacy model put on THIS trade's direction
        p_up, p_dn = sw.get("markov_p_up"), sw.get("markov_p_dn")
        if sim.get("signal") == "buy":
            p_m = p_up
        elif sim.get("signal") == "sell":
            p_m = p_dn
        else:
            p_m = None
        if p_m is None:
            no_dir += 1
            continue
        out.append({
            "symbol": sym,
            "p_win_on": sim.get("predicted_p_win_raw"),
            "p_m": p_m,
            "realised": sim.get("outcome") == "tp_hit",
            "tp_bricks": sim.get("tp_bricks"),
            "sl_bricks": sim.get("sl_bricks"),
            "brick_size": sim.get("brick_size"),
            "ts": sim.get("ts"),
            "legacy_state": sim.get("legacy_markov_state"),
        })
    return out, {
        "sims": len(sims), "sweeps": len(sweeps), "joined": len(out),
        "no_sweep": no_sweep, "stale": stale, "no_direction": no_dir,
    }


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def render(rows: list[le.LeverageRow], diag: dict, now: datetime) -> str:
    s = le.summarize(rows)
    v = le.leverage_verdict(s)
    L: list[str] = []
    L.append("# Track A — calibration + leverage effect of withdrawing `p_markov_hold_n`")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"join tolerance ±{JOIN_TOL_SEC // 60} min")
    L.append("")
    L.append("`docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md` §3 Track A. "
             "Every decided signal is replayed with the withdrawn source present, via the "
             "exact identity `L_off = (L_on − w_m·logit(p_m)) / (1 − w_m)`.")
    L.append("")

    # ── Verdict ──
    L.append(f"## Verdict — {v['read']}")
    L.append("")
    L.append("| question | answer |")
    L.append("|---|---|")
    L.append(f"| Did calibration improve? | **{'YES' if v['calibration_better'] else 'NO'}** "
             f"(Brier Δ {_fmt(s.get('brier_delta'), '.4f', plus=True)}; negative = better) |")
    L.append(f"| Did sizing rise? | **{'YES' if v['size_rose'] else 'NO'}** "
             f"(mean ev_scale Δ {_fmt(s.get('mean_d_scale'), '.4f', plus=True)}) |")
    L.append(f"| Signals going 0 → sized | **{s.get('pct_zero_to_size')}%** "
             f"({s.get('n_zero_to_size')} of {s.get('n')}) |")
    L.append("")

    # ── Headline numbers ──
    L.append("## 1. Headline")
    L.append("")
    L.append(f"n = **{s.get('n')}** decided signals with a reconstructable blend")
    L.append("")
    L.append("| metric | with source (actual) | without source (counterfactual) | Δ |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| **Brier** | {_fmt(s.get('brier_on'))} | {_fmt(s.get('brier_off'))} | "
             f"**{_fmt(s.get('brier_delta'), '.4f', plus=True)}** |")
    L.append(f"| mean `p_win` | {_fmt(s.get('mean_p_win_on'))} | {_fmt(s.get('mean_p_win_off'))} | "
             f"{_fmt(s.get('mean_d_p_win'), '.4f', plus=True)} |")
    L.append(f"| mean `ev_scale` | {_fmt(s.get('mean_scale_on'))} | {_fmt(s.get('mean_scale_off'))} | "
             f"**{_fmt(s.get('mean_d_scale'), '.4f', plus=True)}** |")
    L.append("")
    L.append(f"- Population base rate (TP share): **{s.get('base_rate')}**")
    L.append(f"- **Constant-prediction Brier (p = base rate): {s.get('brier_constant')}** — "
             f"the reference line. Actual blend: {s.get('brier_on')}.")
    L.append(f"- Withdrawn source `p_m`: mean **{s.get('mean_p_m')}**, median **{s.get('median_p_m')}** "
             f"(vs 0.5 neutral — a source below 0.5 drags the pool down)")
    L.append(f"- `p_win` shift: median **{_fmt(s.get('median_d_p_win'), '.4f', plus=True)}**, "
             f"max **{_fmt(s.get('max_d_p_win'), '.4f', plus=True)}**")
    L.append(f"- `ev_scale` shift: median **{_fmt(s.get('median_d_scale'), '.4f', plus=True)}**")
    L.append("")

    # ── The reference-line read ──
    bc, bo = s.get("brier_constant"), s.get("brier_on")
    if bc is not None and bo is not None:
        L.append("### Reference line — does the blend beat a constant?")
        L.append("")
        if bo > bc:
            L.append(f"**NO.** The blend's Brier ({bo}) is *worse* than predicting the "
                     f"population base rate ({s.get('base_rate')}) for every signal "
                     f"(Brier {bc}). The blend is adding **negative** information about the "
                     f"outcome. This is a property of the blend itself, independent of which "
                     f"sources are in it — re-weighting cannot fix it.")
        else:
            L.append(f"**YES.** The blend ({bo}) beats the constant ({bc}) by "
                     f"{bc - bo:.4f} Brier.")
        L.append("")

    # ── Sizing transitions ──
    L.append("## 2. Sizing transitions (the risk view)")
    L.append("")
    L.append("| transition | n | % of population |")
    L.append("|---|---:|---:|")
    L.append(f"| unsized → sized (**0 → >0**) | {s.get('n_zero_to_size')} | "
             f"**{s.get('pct_zero_to_size')}%** |")
    L.append(f"| partially sized → **full size** | {s.get('n_to_full_size')} | "
             f"{s.get('pct_to_full_size')}% |")
    L.append(f"| unsized, before | {s.get('n_unsized_on')} | — |")
    L.append(f"| unsized, after | {s.get('n_unsized_off')} | — |")
    L.append("")

    # ── Reliability ──
    L.append("## 3. Reliability (shape, not just level)")
    L.append("")
    pairs_on = [(r.p_win_on, r.realised) for r in rows]
    pairs_off = [(r.p_win_off, r.realised) for r in rows]
    tab_on = le.reliability_table(pairs_on)
    tab_off = {t["bin"]: t for t in le.reliability_table(pairs_off)}
    L.append("| bin | n | mean pred (actual) | realised | mean pred (counterfactual) |")
    L.append("|---|---:|---:|---:|---:|")
    for t in tab_on:
        o = tab_off.get(t["bin"], {})
        L.append(f"| {t['bin']} | {t['n']} | {t['mean_pred']} | {t['realised']} | "
                 f"{o.get('mean_pred', '—')} |")
    L.append("")
    L.append("A pure level shift moves every bin's `mean pred` the same way while `realised` "
             "stays put. A slope change is a calibration change. Compare the two pred columns "
             "against the realised column.")
    L.append("")

    # ── Per symbol ──
    L.append("## 4. Per symbol")
    L.append("")
    L.append("| symbol | n | mean p_m | Brier on | Brier off | Δ Brier | mean ev_scale on | off | Δ | 0→size |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for d in le.by_symbol(rows):
        L.append(
            f"| {d['symbol']} | {d['n']} | {d['mean_p_m']} | {d['brier_on']} | {d['brier_off']} | "
            f"{_fmt(d['brier_delta'], '.4f', plus=True)} | {d['mean_scale_on']} | "
            f"{d['mean_scale_off']} | {_fmt(d['mean_d_scale'], '.4f', plus=True)} | "
            f"{d['pct_zero_to_size']}% |"
        )
    L.append("")

    # ── Caveats ──
    L.append("## 5. Caveats (read before acting)")
    L.append("")
    L.append(f"- **`p_m` is a proxy.** The engine consumed `p_markov_hold_n = T^N[current][target]`; "
             f"what is stored is the single-step `T[current][target]` "
             f"(`markov_p_up`/`markov_p_dn`). The single-step value is *lower* than any "
             f"multi-step hold probability, so this **understates** the withdrawal's effect "
             f"rather than overstating it.")
    L.append(f"- **`L_on` is the pre-enforcement blend output** (`predicted_p_win_raw`), which is "
             f"what the engine produced before `apply_calibration_adjustment`. The counterfactual "
             f"is therefore of the *blend*, not of the final enforced `p_win`.")
    L.append(f"- **Storage rounding.** Stored values are 4-decimal; the reconstruction inherits "
             f"that (~1e-3 worst case in logit space). Measured effects are ~1e-1.")
    L.append(f"- **`ev_scale` is one factor of size.** `effective_kelly = kelly_f × ev_scale`, and "
             f"`kelly_f` is unchanged by the withdrawal. The Δ here is the *leverage* component "
             f"only.")
    L.append(f"- **Join coverage:** {diag['joined']} of {diag['sims']} sims joined "
             f"({diag['no_sweep']} no sweep row, {diag['stale']} outside ±{JOIN_TOL_SEC // 60} min, "
             f"{diag['no_direction']} no directional p_m).")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- Removing a source renormalises the surviving weights by `1/(1−w_m)`, so the "
             "withdrawal changes the blend **even where the source said nothing** (p_m = 0.5).")
    L.append("- `p_markov_hold_n_mode: \"off\"` is live as of 2026-10-01; this measures the change "
             "it made.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"fetching {LOOKBACK_DAYS}d…")
    sims = fetch_sims(since)
    print(f"  sims: {len(sims)}")
    sweeps = fetch_sweeps(since)
    print(f"  sweeps: {len(sweeps)}")

    joined, diag = join(sims, sweeps)
    print(f"  joined: {len(joined)}  {diag}")
    rows = le.build_rows(joined)
    print(f"  replayable: {len(rows)}")

    now = datetime.now(timezone.utc)
    text = render(rows, diag, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_leverage_eval.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", out)


if __name__ == "__main__":
    main()
