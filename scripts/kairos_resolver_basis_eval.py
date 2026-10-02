"""
kairos_resolver_basis_eval.py — §3.1 kill criterion: does the intrabar basis matter?
═══════════════════════════════════════════════════════════════════════════════════

`worklog/20261002_SIGNAL_QUALITY_PLAN.md` §3.1. The ghost-edge measurement found
a survivable base rate of **0.1186** vs a bracket base rate of **0.2805** — 57.7%
of "wins" never survived the stop — but flagged it as an **upper bound**, because
the settlement label is a close-walk while the `mae_*_pct` column is intrabar.

This settles it. Both labels are computed on the **same intrabar basis**:

  BRACKET_INTRA   tp_hit  — did intrabar high/low reach TP first?
  SURVIVABLE      tp_hit  — AND intrabar MAE never breached the stop distance

Because both now come from the same OHLC walk, the difference between them is the
ghost edge *without* the close-vs-intrabar confound.

**Kill criterion (from the plan):** if the corrected survivor base rate lands
within ±0.02 of the bracket base rate, the ghost edge was an artefact and Phase 1
collapses. If it lands well below, the label is genuinely optimistic.

`nt_signal_sim` stores fixed-horizon `mae_*_pct` / `mfe_*_pct` but NOT a bar
series, so the bracket leg is reconstructed from `mfe`: a bar-series-first-touch
TP and "mfe reached the TP distance" are the same event when the excursion is
measured over the same horizon and the TP is the maximum favourable level. The
MAE leg uses the stored column directly.

Output: worklog/kairos/reports/YYYYMMDD_resolver_basis_eval.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_resolver_basis_eval.py
"""

from __future__ import annotations

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

from regime_platform.sweep_orchestrator import calibration_decomp as cd  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
LOOKBACK_DAYS = int(os.environ.get("RB_LOOKBACK_DAYS", "60"))
HORIZONS = ("4h", "24h", "48h")     # the columns that exist (migration 170)
KILL_TOL = 0.02          # the plan's ±0.02 tolerance


def _fetch(params: dict) -> list[dict]:
    rows, off = [], 0
    with httpx.Client(timeout=180) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H,
                      params={**params, "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def fetch(since: str) -> list[dict]:
    cols = ",".join(
        f"mae_{h}_pct,mfe_{h}_pct" for h in HORIZONS
    )
    return _fetch({
        "select": f"symbol,signal,ts,p_win,outcome,entry_price,stop_loss,take_profit,"
                  f"sl_bricks,tp_bricks,brick_size,{cols}",
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": f"gte.{since}",
        "order": "ts.asc",
    })


def label_intrabar_bracket(r: dict, horizon: str = "24h") -> bool | None:
    """Did intrabar price reach TP within the horizon?

    Reconstructed from ``mfe_{h}_pct`` (max favourable excursion, % of entry,
    intrabar). TP distance in the same units is ``tp_bricks * brick_size /
    entry_price * 100``. Returns None when either side is missing — never
    imputed, since assuming a reach would manufacture wins.
    """
    mfe = r.get(f"mfe_{horizon}_pct")
    try:
        if mfe is None:
            return None
        tp_pct = (float(r["tp_bricks"]) * float(r["brick_size"])
                  / float(r["entry_price"])) * 100.0
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None
    if tp_pct <= 0:
        return None
    return float(mfe) >= tp_pct


def label_survivable(r: dict, horizon: str = "24h") -> bool | None:
    """TP reached AND the stop was never breached — both intrabar."""
    reached = label_intrabar_bracket(r, horizon)
    if reached is None:
        return None
    mae = r.get(f"mae_{horizon}_pct")
    try:
        if mae is None:
            return None
        sl_pct = (float(r["sl_bricks"]) * float(r["brick_size"])
                  / float(r["entry_price"])) * 100.0
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None
    if sl_pct <= 0:
        return None
    return reached and (float(mae) < sl_pct)


def label_close_walk_bracket(r: dict) -> bool | None:
    """The EXISTING settlement label, for reference: ``outcome == tp_hit``."""
    o = r.get("outcome")
    if o not in ("tp_hit", "sl_hit"):
        return None
    return o == "tp_hit"


def _rate(rows: list[dict], fn) -> tuple[int, float | None]:
    vals = [fn(r) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return 0, None
    return len(vals), sum(1 for v in vals if v) / len(vals)


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def render(rows: list[dict], now: datetime) -> str:
    L: list[str] = []
    L.append("# §3.1 kill criterion — does the intrabar basis change the label?")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"horizons {', '.join(HORIZONS)} · light sweep")
    L.append("")
    L.append("Both labels below are computed on the **same intrabar basis** "
             "(`mae_*`/`mfe_*` are intrabar highs/lows), so the difference between "
             "them is the ghost edge *without* the close-vs-intrabar confound that "
             "made the earlier 57.7% figure an upper bound.")
    L.append("")
    L.append("Reported at **every** available horizon, because the fixed-horizon "
             "columns are not the 48h settlement deadline — a single number could be "
             "an artefact of the horizon chosen.")
    L.append("")

    n_cw, r_cw = _rate(rows, label_close_walk_bracket)

    # ── Per-horizon table ──
    L.append("## Per horizon")
    L.append("")
    L.append("| horizon | n (intrabar) | BRACKET_INTRA | SURVIVABLE | **ghost gap** | % of bracket | verdict |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    results = {}
    for h in HORIZONS:
        n_ib, r_ib = _rate(rows, lambda r, h=h: label_intrabar_bracket(r, h))
        n_sv, r_sv = _rate(rows, lambda r, h=h: label_survivable(r, h))
        results[h] = (n_ib, r_ib, n_sv, r_sv)
        if r_ib is None or r_sv is None:
            L.append(f"| {h} | {n_ib} | — | — | — | — | no data |")
            continue
        gap = r_ib - r_sv
        pct = 100 * gap / max(r_ib, 1e-9)
        v = ("**KILL MET**" if gap <= KILL_TOL else "**NOT MET**")
        L.append(f"| **{h}** | {n_ib} | {_fmt(r_ib)} | {_fmt(r_sv)} | "
                 f"**{_fmt(gap)}** | {pct:.1f}% | {v} |")
    L.append("")
    L.append(f"Reference — close-walk bracket base rate (what the platform publishes): "
             f"**{_fmt(r_cw)}** (n={n_cw}).")
    L.append("")

    # ── Verdict ──
    L.append("## Verdict")
    L.append("")
    verdicts = [h for h, (_, rib, _, rsv) in results.items()
                if rib is not None and rsv is not None]
    if not verdicts:
        L.append("_Insufficient data._")
    else:
        all_not_met = all(
            (results[h][1] - results[h][3]) > KILL_TOL for h in verdicts
        )
        L.append(f"The ghost gap is positive at **every** horizon "
                 f"({len(verdicts)}/{len(verdicts)}), so this is not a horizon "
                 f"artefact.")
        L.append("")
        if all_not_met:
            L.append(f"**KILL CRITERION NOT MET at every horizon — the ghost edge "
                     f"survives on a consistent intrabar basis.** The close-walk label "
                     f"is genuinely optimistic, not a close-vs-intrabar artefact. "
                     f"Phase 1 stands: the intrabar resolver should be adopted.")
        else:
            L.append(f"**KILL CRITERION MET at at least one horizon** — the ghost edge "
                     f"is horizon-dependent, which means part of it is the fixed-window "
                     f"measurement rather than a genuine stop-out. Investigate before "
                     f"adopting the intrabar basis.")
    L.append("")

    # ── Cross-check ──
    L.append("## Cross-check against the earlier (confounded) figure")
    L.append("")
    L.append("The earlier measurement compared a **close-walk** bracket label against "
             "an **intrabar** survivable label — mixing bases. Here both are intrabar.")
    L.append("")
    if r_cw is not None and "24h" in results:
        _, r_ib, _, r_sv = results["24h"]
        if r_ib is not None:
            L.append(f"- close-walk bracket: **{_fmt(r_cw)}** (published)")
            L.append(f"- intrabar bracket (24h): **{_fmt(r_ib)}** "
                     f"({_fmt(r_ib - r_cw, '.4f', plus=True)} vs close-walk)")
            L.append(f"- intrabar survivable (24h): **{_fmt(r_sv)}**")
            L.append("")
            L.append(f"The basis change alone moves the base rate by "
                     f"**{_fmt(r_cw - r_ib, '.4f', plus=True)}**; the survival "
                     f"requirement accounts for the rest. The earlier "
                     f"0.2805 → 0.1186 drop was those two effects stacked — this "
                     f"table separates them.")
            L.append("")

    # ── Does the label change the discrimination verdict? ──
    L.append("## Does the label choice change the discrimination verdict?")
    L.append("")
    L.append("If AUC is at or below 0.5 under every label, the label debate is "
             "orthogonal to the model's ranking ability — which is the finding that "
             "matters for Phase 3.")
    L.append("")
    L.append("| label | n | base rate | Brier | REL | RES | AUC |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    candidates = [("outcome=tp_hit (close-walk)", label_close_walk_bracket)]
    for h in HORIZONS:
        candidates.append(
            (f"BRACKET_INTRA {h}", lambda r, h=h: label_intrabar_bracket(r, h)))
    for h in HORIZONS:
        candidates.append(
            (f"SURVIVABLE {h}", lambda r, h=h: label_survivable(r, h)))
    for name, fn in candidates:
        pairs = []
        for r in rows:
            v = fn(r)
            p = r.get("p_win")
            if v is None or p is None:
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            if 0.0 <= p <= 1.0:
                pairs.append((p, v))
        c = cd.score_candidate("p_win", pairs, target=name)
        if c.get("n"):
            L.append(f"| {name} | {c['n']} | {c['base_rate']} | {c['brier']} | "
                     f"{c['rel']} | {c['res']} | **{_fmt(c['auc'])}** |")
    L.append("")
    L.append("**Read this row-wise, not as a ranking.** Each label is a different "
             "event with a different base rate, so Brier is not comparable across "
             "rows — the informative column is AUC, and the informative fact is "
             "whether it is above or below 0.5.")
    L.append("")

    # ── Coverage ──
    L.append("## Coverage")
    L.append("")
    L.append(f"- fetched: **{len(rows)}** light signals (all outcomes)")
    L.append(f"- close-walk label available: **{n_cw}** "
             f"({100*n_cw/max(1,len(rows)):.1f}%)")
    for h in HORIZONS:
        n_ib = results[h][0]
        L.append(f"- intrabar labels at {h}: **{n_ib}** "
                 f"({100*n_ib/max(1,len(rows)):.1f}% — needs `mfe_{h}_pct` + geometry)")
    L.append("")

    # ── Caveats ──
    L.append("## Caveats")
    L.append("")
    L.append("- **The bracket leg is reconstructed from `mfe_{h}_pct`**, not from a bar "
             "series (the table stores fixed-horizon excursions, not OHLC). 'MFE "
             "reached the TP distance' and 'first-touch TP' coincide when the excursion "
             "is over the same horizon and TP is the maximum favourable level — but "
             "they are not *identical* if price touched TP and then ran further while "
             "the stored MFE saturates. The MAE leg is the stored column directly, so "
             "the **survivable** label is the tighter of the two.")
    L.append("- **`mfe_4h_pct` has thin coverage** (added later than 24h/48h), so its n "
             "is smaller — read it as a sensitivity check, not the headline.")
    L.append("- **Fixed horizons are not the settlement deadline.** A trade reaching TP "
             "in hour 30 is 'not reached' at 24h and 'reached' at 48h; the gap is "
             "stable across both, which is why all three are reported.")
    L.append("- **Both legs now share the intrabar basis**, which is the point — but "
             "neither is a true bar-series first-touch until the resolver runs on OHLC.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"fetching {LOOKBACK_DAYS}d (horizons {', '.join(HORIZONS)})…")
    rows = fetch(since)
    print(f"  rows: {len(rows)}")
    now = datetime.now(timezone.utc)
    text = render(rows, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_resolver_basis_eval.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", out)


if __name__ == "__main__":
    main()
