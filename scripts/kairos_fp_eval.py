"""
kairos_fp_eval.py — Phase 2 CLI: first-passage vs base-rate control vs incumbent.

Method: worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md §4 (Phase 2)
Output: worklog/kairos/reports/YYYYMMDD_fp_eval.md

Read-only. Fetches decided nt_signal_sim rows + 1h bars, rebuilds each signal's
48-bar ATR-scaled path, runs the three arms through `fp_eval`, writes the report.

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_fp_eval.py
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator import fp_eval  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_quant import era_of  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}

LOOKBACK_DAYS = int(os.environ.get("FP_EVAL_LOOKBACK_DAYS", "45"))


def fetch_decided() -> list[dict]:
    """Decided light-sweep rows with a bracket and a legacy markov state."""
    cols = (
        "signal_id,symbol,signal,ts,outcome,regime,tp_distance_atr,sl_distance_atr,"
        "tp_bricks,sl_bricks,entry_price,atr,legacy_markov_state"
    )
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H, params={
                "select": cols,
                "source": "eq.sweep-light",
                "signal": "in.(buy,sell)",
                "outcome": "in.(tp_hit,sl_hit)",
                "ts": f"gte.{since}",
                "order": "ts.asc",
                "limit": "1000",
                "offset": str(off),
            })
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


async def build_paths_by_id(records: list[dict]) -> dict[str, np.ndarray]:
    """(n, 48) ATR-scaled path per signal_id — mirrors tiered_atr.build_paths.

    Only signals whose full 48-bar window has already closed are built (risk
    R1: no look-ahead). ``build_paths`` drops those rows entirely; here we
    build them keyed by ``signal_id`` so an arm can be scored per row.
    """
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    peers = await fetch_active_symbols()
    tv = {p["symbol"]: p.get("tradingview_symbol") for p in peers if p.get("tradingview_symbol")}
    symbols = sorted({x["symbol"] for x in records if x.get("symbol") in tv})
    if not symbols:
        return {}

    bars: dict[str, list[dict]] = {}
    await fetch_tradingview_batch(
        [{"symbol": s, "tradingview_symbol": tv[s]} for s in symbols],
        hours=24 * (LOOKBACK_DAYS + 8), interval="1h", ohlcv_out=bars,
    )
    bars = {s: [b for b in rows if b.get("t") is not None and b.get("c") is not None]
            for s, rows in bars.items()}

    tc = {s: np.array([float(b["t"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    cc = {s: np.array([float(b["c"]) for b in rows], dtype="float64") for s, rows in bars.items()}

    out: dict[str, np.ndarray] = {}
    for x in records:
        sid, sym = x.get("signal_id"), x.get("symbol")
        if not sid or sym not in tc:
            continue
        t = tc[sym]
        try:
            entry = float(x["entry_price"])
            atr = float(x["atr"])
            ts = datetime.fromisoformat(str(x["ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        if atr <= 0 or not len(t):
            continue
        j = int(np.searchsorted(t, ts, side="right")) - 1
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue   # window has not closed -> skip (no look-ahead)
        d = 1.0 if x["signal"] == "buy" else -1.0
        # 2-D (1, EXPIRY_BARS): first_passage() expects (n_paths, bars), and
        # one signal contributes exactly one path.
        out[sid] = ((cc[sym][j:j + EXPIRY_BARS] - entry) * d / atr).reshape(1, -1)
    return out


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def render(blocks: list[dict], meta: dict) -> str:
    L: list[str] = []
    L.append("# First-passage vs base-rate control vs incumbent HMM — Phase 2")
    L.append("")
    L.append(f"Generated {meta['now']:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"one 48-bar path per signal · base-rate k={fp_eval.BASE_RATE_K:g} · gate P{int(fp_eval.GATE_Q*100)}")
    L.append("")
    L.append("worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md §4 (Phase 2). "
             "Arms scored on the same decided population, episode-deduped "
             "(symbol × direction × hour), ΔD bootstrapped on **symbol-day clusters**.")
    L.append("")
    L.append("**Read `skill`, not `accuracy`.** Skill = accuracy − the symbol's own "
             "majority-direction base rate. Negative skill means worse than silence — "
             "the finding that retired the HMM.")
    L.append("")

    # ── Verdict (computed) ────────────────────────────────────────────────
    v = fp_eval.verdict(blocks)
    s = v["_summary"]
    L.append(f"## Verdict — {s['passed']}/{s['total']} bar items pass")
    L.append("")
    L.append("| bar | requirement | result | detail |")
    L.append("|---|---|---|---|")
    labels = {
        "B1_beat_base_rate": "B1 beat the base-rate control",
        "B2_coverage": "B2 coverage ≥ 0.30",
        "B3_consistency": "B3 positive skill ≥ 2/3 of symbols",
        "B4_era_halves": "B4 holds in both halves",
        "B5_overlay_increment": "B5 overlay adds skill",
    }
    for key, label in labels.items():
        item = v.get(key, {})
        mark = "**PASS**" if item.get("pass") else "**FAIL**"
        L.append(f"| {label} | | {mark} | {item.get('detail', '—')} |")
    L.append("")
    if s["passed"] < s["total"]:
        L.append("> **Not promotable.** The bar is pre-registered (scope §5); a partial "
                 "pass is a fail. Kill criteria (scope §5) apply — see the worklog's "
                 "decision section for the disposition.")
    else:
        L.append("> **All bar items pass.** B5 (incremental vs the current blend) still "
                 "requires the Phase-4 comparison before any live flip.")
    L.append("")

    for b in blocks:
        L.append(f"## {b['label']}")
        L.append("")
        if b["n_population"] == 0:
            L.append("No decided rows in this block.")
            L.append("")
            continue
        L.append(f"n={b['n_population']} decided episodes")
        L.append("")

        # Incumbent (the withdrawn source)
        inc = b["inc"]
        L.append("### Incumbent — legacy HMM (the withdrawn source)")
        L.append("")
        if inc["n_calls"]:
            L.append(f"- directional calls **{inc['n_calls']}** · accuracy "
                     f"**{_fmt(inc['accuracy'])}**")
        else:
            L.append("- no directional calls in this block")
        L.append("")

        # Arms
        L.append("### Arms")
        L.append("")
        L.append("| arm | calls | coverage | accuracy | base rate | **skill** | positive symbols |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for name in ("first_passage", "base_rate"):
            a = b["arms"][name]
            L.append(
                f"| **{name}** | {a['n_calls']} | {_fmt(a['coverage'], '.3f')} | "
                f"{_fmt(a['accuracy'])} | {_fmt(a['base_rate'])} | "
                f"**{_fmt(a['skill'], '.4f', plus=True)}** | "
                f"{a['positive_symbols']}/{a['n_symbols']} |"
            )
        L.append("")
        if b["br"]["population_tp"] is not None:
            L.append(f"Population TP share: **{b['br']['population_tp']:.4f}** "
                     f"(the shrunk control shrinks toward this).")
            L.append("")

        # Overlay increment (B5)
        L.append("### D5 overlay increment (bar B5)")
        L.append("")
        L.append("| arm | bare skill | +overlay skill | **Δ (overlay)** |")
        L.append("|---|---:|---:|---:|")
        for name in ("first_passage", "base_rate"):
            bare = b["arms"][name]["skill"]
            over = b["overlay"][name]["skill"]
            delta = (over - bare) if (bare is not None and over is not None) else None
            L.append(
                f"| {name} | {_fmt(bare, '.4f', plus=True)} | {_fmt(over, '.4f', plus=True)} | "
                f"**{_fmt(delta, '.4f', plus=True)}** |"
            )
        L.append("")

        # ΔD
        L.append("### ΔD — accuracy vs no-skill, gated vs rest")
        L.append("")
        L.append("| arm | gated n | rest n | clusters | ΔD | 95% CI |")
        L.append("|---|---:|---:|---:|---:|---|")
        for name in ("first_passage", "base_rate", "trailing_base_rate"):
            d = b["dd"].get(name) or {}
            dd, lo, hi = d.get("dd"), d.get("lo"), d.get("hi")
            ci = f"[{_fmt(lo, '.3f', plus=True)}, {_fmt(hi, '.3f', plus=True)}]" if lo is not None else "—"
            disp = f"*{name}*" if name == "trailing_base_rate" else name
            L.append(
                f"| {disp} | {d.get('n_gated', '—')} | {d.get('n_rest', '—')} | "
                f"{d.get('clusters_gated', '—')}/{d.get('clusters_rest', '—')} | "
                f"{_fmt(dd, '.3f', plus=True)} | {ci} |"
            )
        L.append("")
        L.append("*`trailing_base_rate` is the honest floor: a walk-forward per-symbol TP rate "
                 "that knows **nothing about the bracket**. A geometry-aware arm must clear it, "
                 "or its ΔD is just a momentum read, not geometry information.*")
        L.append("")

        # Coverage diagnostics for the first-passage arm
        fp = b["fp"]
        if fp.get("no_path") or fp.get("skipped"):
            L.append(f"First-passage path coverage: {fp.get('no_path', 0)} rows had no closed "
                     f"48-bar window (skipped, never imputed — risk R1); "
                     f"{fp.get('skipped', 0)} produced no readable first-passage.")
            L.append("")

    L.append("## Bar (pre-registered, scope §5)")
    L.append("")
    L.append("| # | requirement | threshold |")
    L.append("|---|---|---|")
    L.append("| B1 | accuracy vs the shrunk base-rate control | **must beat it** |")
    L.append("| B2 | coverage | ≥ 0.30 |")
    L.append("| B3 | consistency | positive skill on ≥ 2/3 of symbols |")
    L.append("| B4 | era split | holds in both halves |")
    L.append("| B5 | **incremental** | beats p_regime + p_timesfm alone on ΔD, CI > 0 |")
    L.append("| B6 | episode unit | symbol × direction × hour, deduped |")
    L.append("| B7 | cluster CIs | symbol-day clustered |")
    L.append("")
    L.append("**B5 is the real gate.** B1–B4/B6/B7 are computed here; B5 needs the "
             "blend comparison (Phase 4) and is not answerable from this report alone.")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- First-passage rows require a **closed 48-bar window** — signals still "
             "inside their window are skipped, never imputed (risk R1, no look-ahead).")
    L.append("- A probability arm \"calls\" when it leans past neutral (p ≠ 0.5), the "
             "same opinion-vs-silence rule the incumbent arm uses — so coverage and "
             "accuracy are comparable across arms.")
    L.append("- Episode dedupe: one decision per symbol × direction × hour (B6). "
             "Raw-row AUC is inflated by overlapping hours.")
    L.append("- Overlay uses the Kairos-proven primitives verbatim "
             "(`freshness_score` × `type_score`); surprise stays REJECTED (Spearman 0.004).")
    L.append("- Mixed-era blocks are discovery; promote only on post-ATR confirmation.")
    L.append("")
    return "\n".join(L) + "\n"


async def main() -> None:
    print("fetching decided rows…")
    raw = fetch_decided()
    print(f"  {len(raw)} decided rows (light, {LOOKBACK_DAYS}d)")

    records = fp_eval.build_records(raw)
    print(f"  {len(records)} with a usable bracket")

    print("building paths…")
    paths = await build_paths_by_id(records)
    print(f"  {len(paths)} closed 48-bar windows")

    # Keep only rows we can score (a path exists) so every arm sees one population.
    scorable = [x for x in records if x.get("signal_id") in paths]
    fp_eval.annotate_event_time(scorable)
    episodes = fp_eval.dedupe_episodes(scorable)
    print(f"  {len(episodes)} episodes after dedupe")

    blocks = [fp_eval.evaluate_block(episodes, paths, "1. All eras")]

    from datetime import datetime as _dt, timezone as _tz

    def parse(s):
        try:
            return _dt.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(_tz.utc)
        except (TypeError, ValueError):
            return None

    for x in episodes:
        x["ts_parsed"] = parse(x.get("ts"))
    post_atr = [x for x in episodes if era_of(x.get("ts_parsed")) in ("atr_to_resolver", "post_resolver")]
    post_debounce = [x for x in episodes
                     if era_of(x.get("ts_parsed")) in ("debounce_to_atr", "atr_to_resolver", "post_resolver")]

    blocks.append(fp_eval.evaluate_block(post_debounce, paths, "2. Post-debounce"))
    blocks.append(fp_eval.evaluate_block(post_atr, paths, "3. Post-ATR only"))

    # era halves for B4
    half = len(episodes) // 2
    by_ts = sorted(episodes, key=lambda x: str(x.get("ts") or ""))
    blocks.append(fp_eval.evaluate_block(by_ts[:half], paths, "4. First half"))
    blocks.append(fp_eval.evaluate_block(by_ts[half:], paths, "5. Second half"))

    now = datetime.now(timezone.utc)
    text = render(blocks, {"now": now})
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_fp_eval.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", out)


if __name__ == "__main__":
    asyncio.run(main())
