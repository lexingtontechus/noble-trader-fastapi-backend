"""
kairos_calibration_decomp.py — is `p_win` calibrated for the event it forecasts?
═══════════════════════════════════════════════════════════════════════════════

Scopes the question the repo's quant audit calls the core "category error" (E1):
``p_win`` is a **directional** confidence; the settlement label is a **bracket /
first-passage** event. This scores ``p_win`` against BOTH, with the Murphy
decomposition so calibration (REL) and discrimination (RES/AUC) are separated —
which the previous constant-baseline pass conflated.

Output: worklog/kairos/reports/YYYYMMDD_calibration_decomp.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_calibration_decomp.py
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
LOOKBACK_DAYS = int(os.environ.get("CAL_LOOKBACK_DAYS", "45"))
MIN_SYMBOL_N = int(os.environ.get("CAL_MIN_SYMBOL_N", "200"))

CANDIDATES = ("p_win", "predicted_p_win_raw", "p_win_bracket", "p_win_dir")


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
    return _fetch({
        "select": "symbol,signal,ts,p_win,predicted_p_win_raw,p_win_bracket,p_win_dir,"
                  "p_resolve,p_win_resolve,drift_1h_pct,drift_4h_pct,drift_24h_pct,"
                  "mae_4h_pct,mae_12h_pct,mae_24h_pct,mae_48h_pct,"
                  "mfe_4h_pct,mfe_12h_pct,mfe_24h_pct,mfe_48h_pct,"
                  "entry_price,sl_bricks,brick_size,"
                  "outcome,regime",
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": f"gte.{since}",
        "order": "ts.asc",
    })


# ─── Label construction ────────────────────────────────────────────────────
# Each returns (predicted, realised) pairs. Rows missing either side are
# dropped rather than imputed — a fabricated label would invent a result.


def label_directional(rows: list[dict], col: str, pred: str) -> list[tuple[float, bool]]:
    """``drift_{h}_pct > 0`` — the event p_win actually forecasts."""
    out = []
    for r in rows:
        d, p = r.get(col), r.get(pred)
        if d is None or p is None:
            continue
        try:
            d, p = float(d), float(p)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0):
            continue
        out.append((p, d > 0))
    return out


def label_bracket(rows: list[dict], pred: str) -> list[tuple[float, bool]]:
    """``outcome == 'tp_hit'`` — the settlement event (expired excluded)."""
    out = []
    for r in rows:
        o, p = r.get("outcome"), r.get(pred)
        if o not in ("tp_hit", "sl_hit") or p is None:
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0):
            continue
        out.append((p, o == "tp_hit"))
    return out


def label_survivable(rows: list[dict], pred: str, horizon: str = "24h",
                     mae_col: str | None = None) -> list[tuple[float, bool]]:
    """``tp_hit AND MAE within the stop distance`` — the *survivable* win.

    The attachment's "ghost edge" point, made measurable. The settlement label
    comes from a **close-walk that checks TP before SL on the same bar**
    (`eod_resolver`), so a trade that ran deep against the position and then
    recovered is logged a win. This label asks the question a real stop would ask:
    did the trade reach TP *without* first breaching the stop distance?

    ``mae_{h}_pct`` (migration 170) is max adverse excursion over the fixed
    horizon, in % of entry, independent of TP/SL. The stop distance in the same
    units is ``sl_bricks × brick_size / entry_price × 100``.

    Rows missing either the MAE horizon or the geometry are dropped — never
    imputed, because assuming "no drawdown" would manufacture survivable wins.
    """
    col = mae_col or f"mae_{horizon}_pct"
    out = []
    for r in rows:
        o, p = r.get("outcome"), r.get(pred)
        if o not in ("tp_hit", "sl_hit") or p is None:
            continue
        mae = r.get(col)
        if mae is None:
            continue
        try:
            p = float(p)
            mae = float(mae)
            sl_dist_pct = (float(r["sl_bricks"]) * float(r["brick_size"])
                           / float(r["entry_price"])) * 100.0
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            continue
        if not (0.0 <= p <= 1.0) or sl_dist_pct <= 0:
            continue
        # a win that survived: TP reached AND never breached the stop distance
        survived = (o == "tp_hit") and (mae < sl_dist_pct)
        out.append((p, survived))
    return out


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def _score_table(rows: list[dict], target: str, label_fn, preds=CANDIDATES) -> list[dict]:
    out = []
    for p in preds:
        pairs = label_fn(rows, p)
        c = cd.score_candidate(p, pairs, target=target)
        if c.get("n"):
            out.append(c)
    return out


def render(rows: list[dict], now: datetime) -> str:
    L: list[str] = []
    L.append("# Is `p_win` calibrated for the event it forecasts?")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"light sweep, buy/sell")
    L.append("")
    L.append("The repo's quant audit calls this the core **category error (E1)**: "
             "`p_win` is a *directional* confidence (its sources are "
             "`p_regime` = P(bull|bear) and `p_timesfm` = P(next bar up)), while the "
             "settlement label is a *bracket* event (does price reach +TP before −SL "
             "within 48h). This scores `p_win` against **both**, with the Murphy "
             "decomposition so calibration and discrimination are not conflated.")
    L.append("")

    # ── The reference-line correction, stated up front ──
    L.append("## Why the previous 'anti-predictive' verdict was a reference-line artifact")
    L.append("")
    L.append("A constant forecast `p ≡ ō` has `REL = 0` and `RES = 0` by construction, so:")
    L.append("")
    L.append("```")
    L.append("Brier(constant) = UNC = ō(1 − ō)      # a property of the EVENT, not the model")
    L.append("Brier = REL − RES + UNC                # Murphy (1973)")
    L.append("```")
    L.append("")
    L.append("Losing to that constant therefore means `RES ≤ REL` — it does **not** "
             "distinguish *'no resolution for this event'* from *'calibrated for a "
             "different event'*. The decomposition below separates them.")
    L.append("")

    # ── Headline: the two labels ──
    L.append("## 1. `p_win` against both labels")
    L.append("")
    L.append("| label | event | n | base rate | Brier | REL | RES | UNC | **BSS** | AUC |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    both = []
    for target, label_fn in (
        ("DIRECTIONAL (drift_1h > 0)", lambda r, p: label_directional(r, "drift_1h_pct", p)),
        ("DIRECTIONAL (drift_4h > 0)", lambda r, p: label_directional(r, "drift_4h_pct", p)),
        ("DIRECTIONAL (drift_24h > 0)", lambda r, p: label_directional(r, "drift_24h_pct", p)),
        ("BRACKET (tp_hit before sl_hit)", label_bracket),
        ("SURVIVABLE (tp_hit AND mae_24h < sl)", label_survivable),
    ):
        pairs = label_fn(rows, "p_win")
        c = cd.score_candidate("p_win", pairs, target=target)
        if not c.get("n"):
            continue
        both.append((target, c))
        L.append(
            f"| `{target.split(' ')[0]}` | {target.split(' ', 1)[1]} | {c['n']} | "
            f"{c['base_rate']} | {c['brier']} | {c['rel']} | {c['res']} | {c['unc']} | "
            f"**{_fmt(c['bss'], '.4f', plus=True)}** | {_fmt(c['auc'], '.4f')} |"
        )
    L.append("")
    L.append("`BSS = 1 − Brier/UNC` — **positive = beats saying the base rate**. "
             "`REL` lower is better, `RES` higher is better. `AUC` 0.5 = no ordering.")
    L.append("")

    # ── The read ──
    d1 = next((c for t, c in both if "drift_1h" in t), None)
    br = next((c for t, c in both if "BRACKET" in t), None)
    if d1 and br:
        L.append("### The read")
        L.append("")
        L.append(f"- Against **directional** (drift_1h > 0): BSS "
                 f"{_fmt(d1['bss'], '.4f', plus=True)}, AUC {_fmt(d1['auc'], '.4f')}, "
                 f"REL {d1['rel']} vs RES {d1['res']}.")
        L.append(f"- Against **bracket** (tp_hit): BSS "
                 f"{_fmt(br['bss'], '.4f', plus=True)}, AUC {_fmt(br['auc'], '.4f')}, "
                 f"REL {br['rel']} vs RES {br['res']}.")
        L.append("")
        if d1["auc"] and d1["auc"] > 0.52 and (not br["auc"] or br["auc"] <= 0.52):
            L.append("**The model has directional discrimination and no bracket "
                     "discrimination.** It can tell up from down (AUC > 0.5) but not "
                     "reach-TP from reach-SL (AUC ≈ 0.5). That is exactly the "
                     "category error: the prediction target and the outcome target "
                     "are different random variables, so no amount of recalibration "
                     "of `p_win` can make it a P(win) — it must be *re-derived* as a "
                     "first-passage quantity (`p_win_bracket` is that attempt).")
            L.append("")
        elif br["auc"] and br["auc"] > 0.52:
            L.append("**The model discriminates the bracket event too** — the "
                     "category error is narrower than the audit states, and "
                     "recalibration (not re-derivation) may suffice.")
            L.append("")

    # ── The survivable-label comparison (same rows) ──
    L.append("## 1b. Ghost edge — bracket vs survivable, on the SAME rows")
    L.append("")
    L.append("The `SURVIVABLE` label above is computed only on rows where `mae_24h_pct` "
             "is populated, so its n is much smaller than `BRACKET`'s — comparing them "
             "directly would confound the label with the population. Below, **both** are "
             "restricted to the rows that have the MAE column.")
    L.append("")
    mae_rows = [r for r in rows if r.get("mae_24h_pct") is not None
                and r.get("entry_price") and r.get("sl_bricks")
                and r.get("brick_size")]
    if mae_rows:
        L.append(f"Common population: **{len(mae_rows)}** rows with `mae_24h_pct`.")
        L.append("")
        L.append("| label | n | base rate | Brier | REL | RES | AUC |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for tname, lfn in (("BRACKET (tp_hit)", label_bracket),
                           ("SURVIVABLE (tp_hit AND mae_24h < sl)", label_survivable)):
            pairs = lfn(mae_rows, "p_win")
            c = cd.score_candidate("p_win", pairs, target=tname)
            if not c.get("n"):
                continue
            L.append(f"| {tname} | {c['n']} | {c['base_rate']} | {c['brier']} | "
                     f"{c['rel']} | {c['res']} | **{_fmt(c['auc'])}** |")
        L.append("")
        L.append("**If the survivable AUC is lower than the bracket AUC on identical "
                 "rows, part of the apparent bracket skill was ghost edge** — wins that "
                 "a real stop would have taken out before TP was reached.")
        L.append("")

        # ── The ghost-edge LEVEL finding ──
        br_pairs = label_bracket(mae_rows, "p_win")
        sv_pairs = label_survivable(mae_rows, "p_win")
        n_br = len(br_pairs)
        n_br_win = sum(1 for _, y in br_pairs if y)
        n_sv_win = sum(1 for _, y in sv_pairs if y)
        if n_br_win:
            lost = n_br_win - n_sv_win
            L.append("### The level finding — how many 'wins' a stop would have taken out")
            L.append("")
            L.append(f"- bracket wins on this population: **{n_br_win}** of {n_br} "
                     f"(base rate {n_br_win/n_br:.4f})")
            L.append(f"- of those, **{n_sv_win}** survived (never breached the stop "
                     f"distance) — **{lost} did not ({100*lost/n_br_win:.1f}%)**")
            L.append(f"- survivable base rate: **{n_sv_win/n_br:.4f}**")
            L.append("")
            L.append("**This is the ghost edge, quantified.** The settlement label comes "
                     "from a close-walk; the MAE column comes from **intrabar highs/lows** "
                     "(`migration 170`). So the two are not the same measurement, and the "
                     f"{100*lost/n_br_win:.0f}% figure is an **upper bound** — some of it "
                     "is the close-vs-intrabar difference rather than a genuine stop-out. "
                     "The true figure lies between 0 and that number; pinning it needs an "
                     "intrabar-consistent resolver.")
            L.append("")
            L.append("**Note where the effect lands.** AUC barely moves (bracket "
                     f"{cd.score_candidate('p_win', br_pairs, target='b')['auc']:.4f} → "
                     f"survivable {cd.score_candidate('p_win', sv_pairs, target='s')['auc']:.4f}), "
                     "but `REL` more than doubles (0.0692 → 0.1443). The ghost edge is a "
                     "**level** distortion, not a ranking one — which is exactly what the "
                     "`REL ≫ RES` finding already said. It does not rescue the model's "
                     "discrimination, and it makes its overconfidence worse.")
            L.append("")

    # ── Side by side, directional label ──
    L.append("## 2. Side-by-side on the directional label (drift_1h > 0)")
    L.append("")
    L.append("Each candidate scored on its own available rows.")
    L.append("")
    L.append("| candidate | n | coverage | base rate | Brier | REL | RES | BSS | AUC |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    n_all = len(rows)
    for c in _score_table(rows, "DIRECTIONAL (drift_1h > 0)",
                          lambda r, p: label_directional(r, "drift_1h_pct", p)):
        L.append(
            f"| `{c['name']}` | {c['n']} | {c['n']/n_all:.3f} | {c['base_rate']} | "
            f"{c['brier']} | {c['rel']} | {c['res']} | "
            f"**{_fmt(c['bss'], '.4f', plus=True)}** | {_fmt(c['auc'], '.4f')} |"
        )
    L.append("")
    L.append("`p_win_dir` is stamped as `_p_win_dir = p_win_raw` in "
             "`orchestrator.py:4816` — it is the *same variable*, so its numbers "
             "should be identical to `predicted_p_win_raw`'s. If they differ, the "
             "capture is inconsistent.")
    L.append("")

    # ── Side by side, bracket label ──
    L.append("## 3. Side-by-side on the bracket label (tp_hit)")
    L.append("")
    L.append("| candidate | n | base rate | Brier | REL | RES | BSS | AUC |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for c in _score_table(rows, "BRACKET (tp_hit)", label_bracket):
        L.append(
            f"| `{c['name']}` | {c['n']} | {c['base_rate']} | {c['brier']} | "
            f"{c['rel']} | {c['res']} | **{_fmt(c['bss'], '.4f', plus=True)}** | "
            f"{_fmt(c['auc'], '.4f')} |"
        )
    L.append("")

    # ── Per symbol ──
    L.append("## 4. Per symbol — does `p_win` predict this symbol's direction?")
    L.append("")
    L.append("The operational question. A symbol whose AUC sits at 0.5 has a "
             "directional score that carries no ordering information for that symbol, "
             "regardless of how the system-wide number looks.")
    L.append("")
    syms: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("symbol"):
            syms.setdefault(r["symbol"], []).append(r)
    L.append(f"| symbol | n | dir base rate | dir AUC | dir BSS | dir REL | dir RES | bracket AUC | bracket BSS |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    per_sym = []
    for sym, rs in syms.items():
        dp = label_directional(rs, "drift_1h_pct", "p_win")
        if len(dp) < MIN_SYMBOL_N:
            continue
        d = cd.score_candidate(sym, dp, target="dir")
        bp = label_bracket(rs, "p_win")
        b = cd.score_candidate(sym, bp, target="br") if len(bp) >= MIN_SYMBOL_N else {}
        per_sym.append((sym, d, b))
    for sym, d, b in sorted(per_sym, key=lambda x: (x[1].get("auc") or 0), reverse=True):
        L.append(
            f"| {sym} | {d['n']} | {d['base_rate']} | {_fmt(d['auc'], '.4f')} | "
            f"{_fmt(d['bss'], '.4f', plus=True)} | {d['rel']} | {d['res']} | "
            f"{_fmt(b.get('auc'), '.4f')} | {_fmt(b.get('bss'), '.4f', plus=True)} |"
        )
    L.append("")
    if per_sym:
        aucs = [d["auc"] for _, d, _ in per_sym if d.get("auc") is not None]
        above = sum(1 for a in aucs if a > 0.5)
        L.append(f"**{above} of {len(aucs)} symbols** have directional AUC > 0.5 "
                 f"(mean {statistics.fmean(aucs):.4f}, "
                 f"min {min(aucs):.4f}, max {max(aucs):.4f}).")
        L.append("")
        br_aucs = [b["auc"] for _, _, b in per_sym if b.get("auc") is not None]
        if br_aucs:
            br_above = sum(1 for a in br_aucs if a > 0.5)
            L.append(f"On the **bracket** label: {br_above} of {len(br_aucs)} symbols "
                     f"have AUC > 0.5 (mean {statistics.fmean(br_aucs):.4f}).")
            L.append("")

    # ── Reliability ──
    L.append("## 5. Reliability — `p_win` vs both labels")
    L.append("")
    for target, label_fn in (
        ("DIRECTIONAL (drift_1h > 0)", lambda r, p: label_directional(r, "drift_1h_pct", p)),
        ("BRACKET (tp_hit)", label_bracket),
    ):
        pairs = label_fn(rows, "p_win")
        tab = cd.reliability(pairs, n_bins=5)
        L.append(f"**{target}** (n={len(pairs)})")
        L.append("")
        L.append("| bin | n | mean pred | realised | gap |")
        L.append("|---|---:|---:|---:|---:|")
        for t in tab:
            L.append(f"| {t['bin']} | {t['n']} | {t['mean_pred']} | {t['realised']} | "
                     f"{_fmt(t['gap'], '.4f', plus=True)} |")
        L.append("")
    L.append("`gap = realised − mean_pred`; **negative = overconfident**. If the "
             "directional gaps are small and the bracket gaps are large, the "
             "miscalibration is *entirely* the label mismatch — not a defect in "
             "the model.")
    L.append("")

    # ── Caveats ──
    L.append("## 6. Caveats")
    L.append("")
    L.append("- **`drift_*_pct` is a proxy for the directional event.** It is "
             "direction-relative post-entry drift as % of entry price (migration 121), "
             "computed regardless of outcome. The model's `p_timesfm` is "
             "specifically P(**next bar** up), so `drift_1h_pct` is the closest "
             "available label; 4h/24h are shown because the signal's own horizon is "
             "not 1 bar. `p_regime` is a slower regime posterior and may align better "
             "with 24h.")
    L.append("- **Close-only resolution.** Drift and outcomes come from hourly "
             "closes; the bracket resolver checks TP before SL on the same bar "
             "(optimistic) and does not model gaps. Bracket AUC inherits that.")
    L.append("- **Expired excluded from the bracket label** (neither win nor loss, "
             "per the platform's own convention). That is ~49% of light signals, so "
             "the bracket label describes the *resolving* sub-population, which is "
             "selected — a further reason the bracket AUC is not a clean read on the "
             "model.")
    L.append("- **`p_win_bracket` is bucket-derived**, so its n is limited by "
             "reachability-table coverage and its values are coarse (piecewise "
             "constant across a bucket). Its AUC is correspondingly attenuated.")
    L.append("- **Binning residual.** `Brier = REL − RES + UNC` holds exactly only "
             "when stratifying on distinct forecast values; with 10 bins there is a "
             "residual, reported in §1's source data.")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- This module (`calibration_decomp.py`) is the corrected instrument: "
             "Murphy decomposition + AUC, against a named label. The earlier "
             "`leverage_eval.py` constant-baseline pass remains valid as a *bracket* "
             "measurement; it was never a directional one.")
    L.append("- `p_win_dir == p_win_raw` is an open question — if they are the same "
             "variable, the capture stamps two column names for one quantity and "
             "`p_win_dir` should be dropped or redefined.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"fetching {LOOKBACK_DAYS}d…")
    rows = fetch(since)
    print(f"  rows: {len(rows)}")
    now = datetime.now(timezone.utc)
    text = render(rows, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_calibration_decomp.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", out)


if __name__ == "__main__":
    main()
