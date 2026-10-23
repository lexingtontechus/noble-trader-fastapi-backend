"""
kairos_mr_cohort_walkforward.py — B1: walk-forward validation of the
context-alignment "false-cohort" mean-reversion edge, per symbol and per class.

Method: docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE.md §B1
  The ID45 inversion (2026-10-10) showed XAUUSD buys that FAIL the context-
  alignment gate (macro+trend both oppose) win ~6x more than buys that pass it.
  B1 tests whether that inverted edge holds out-of-sample.

Cohort definition
  For each (symbol, signal) cell, resolved era-clean rows are split by
  context_aligned_ok:
    false-cohort = context_aligned_ok is False  (gate would REJECT)
    pass-cohort  = context_aligned_ok is True   (gate would PASS)
  The edge = WR(false) / WR(pass). Bar (per docs): WR(false) >= 1.5x WR(pass)
  on the HELD-OUT (later) half, with n >= 40 false-cohort rows in that half.

Era
  post-cutover (eod_resolver_basis intrabar, 2026-10-02), contaminated=false,
  outcome in (tp_hit, sl_hit) — expired excluded from both numerator and
  denominator (a timeout is neither a win nor a loss).

Usage
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_mr_cohort_walkforward.py
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_mr_cohort_walkforward.py --class forex
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_mr_cohort_walkforward.py --symbols XAUUSD XAUAUD
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_mr_cohort_walkforward.py --class forex --split 2026-10-06
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load credentials from .env.local (standalone-safe; no dotenv dependency).
_env = ROOT / ".env.local"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}

CUTOVER = "2026-10-02T00:00:00+00:00"
BAR_RATIO = 1.5
BAR_N = 40
PAGE = 1000


def fetch_resolved(symbols: list[str]) -> list[dict]:
    """All era-clean resolved (tp_hit/sl_hit) rows for the given symbols, paginated."""
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(
                f"{U}/rest/v1/nt_signal_sim",
                headers=H,
                params={
                    "select": "symbol,signal,ts,outcome,context_aligned_ok,qualified",
                    "symbol": f"in.({','.join(symbols)})",
                    "outcome": "in.(tp_hit,sl_hit)",
                    "contaminated": "eq.false",
                    "ts": f"gte.{CUTOVER}",
                    "order": "ts.asc",
                    "limit": str(PAGE),
                    "offset": str(off),
                },
            )
            r.raise_for_status()
            batch = r.json()
            rows.extend(batch)
            if len(batch) < PAGE:
                break
            off += PAGE
    return rows


def win_rate(rows: list[dict]) -> tuple[float, int]:
    if not rows:
        return 0.0, 0
    wins = sum(1 for r in rows if r["outcome"] == "tp_hit")
    return wins / len(rows), len(rows)


def split_walkforward(rows: list[dict], split_ts: str | None) -> tuple[str, list[dict], list[dict]]:
    """Return (split_ts, in-sample-half, held-out-half). Default split = median ts."""
    ordered = sorted(rows, key=lambda r: r["ts"])
    if not ordered:
        return CUTOVER, [], []
    if split_ts is None:
        split_ts = ordered[len(ordered) // 2]["ts"]
    h1 = [r for r in ordered if r["ts"] < split_ts]
    h2 = [r for r in ordered if r["ts"] >= split_ts]
    return split_ts, h1, h2


def evaluate_cell(name: str, rows: list[dict], split_ts: str | None, out: list[str]) -> dict:
    """Evaluate one (symbol[, signal]) cell: false vs pass cohort, walk-forward."""
    false_rows = [r for r in rows if r.get("context_aligned_ok") is False]
    pass_rows = [r for r in rows if r.get("context_aligned_ok") is True]

    wr_f_all, n_f_all = win_rate(false_rows)
    wr_p_all, n_p_all = win_rate(pass_rows)
    ratio_all = wr_f_all / wr_p_all if wr_p_all > 0 else (float("inf") if wr_f_all > 0 else 0.0)

    sp, _, _ = split_walkforward(rows, split_ts)
    _, f_h1, f_h2 = split_walkforward(false_rows, sp)
    _, p_h1, p_h2 = split_walkforward(pass_rows, sp)

    wr_fh2, n_fh2 = win_rate(f_h2)
    wr_ph2, n_ph2 = win_rate(p_h2)
    wr_fh1, n_fh1 = win_rate(f_h1)
    wr_ph1, n_ph1 = win_rate(p_h1)
    ratio_h2 = wr_fh2 / wr_ph2 if wr_ph2 > 0 else (float("inf") if wr_fh2 > 0 else 0.0)

    n_ok = n_fh2 >= BAR_N
    ratio_ok = ratio_h2 >= BAR_RATIO
    passed = n_ok and ratio_ok
    if n_f_all == 0:
        verdict = "NO_DATA"  # gate never opposes on this cohort — nothing to validate
    elif passed:
        verdict = "PASS"
    else:
        verdict = "FAIL"

    out.append(f"\n### {name}")
    out.append(f"  false-cohort: n={n_f_all:4d}  WR={wr_f_all:.3f}")
    out.append(f"  pass-cohort:  n={n_p_all:4d}  WR={wr_p_all:.3f}")
    out.append(f"  overall ratio: {ratio_all:.2f}x")
    out.append(f"  split @ {sp}")
    out.append(f"    H1 (in-sample): false n={n_fh1:3d} WR={wr_fh1:.3f} | pass n={n_ph1:3d} WR={wr_ph1:.3f}")
    out.append(
        f"    H2 (held-out):  false n={n_fh2:3d} WR={wr_fh2:.3f} | pass n={n_ph2:3d} WR={wr_ph2:.3f}"
        f"  ratio={ratio_h2:.2f}x"
    )
    if verdict == "NO_DATA":
        out.append(
            "    VERDICT: NO_DATA — context_alignment gate never opposes this cohort "
            "(0 false-cohort rows). No MR inversion exists to validate."
        )
    else:
        out.append(
            f"    BAR: n>=40 {'PASS' if n_ok else 'FAIL'} ({n_fh2})  "
            f"ratio>=1.5 {'PASS' if ratio_ok else 'FAIL'} ({ratio_h2:.2f}x)  => {verdict}"
        )

    return {
        "name": name,
        "n_false": n_f_all,
        "wr_false": wr_f_all,
        "n_pass": n_p_all,
        "wr_pass": wr_p_all,
        "ratio_all": ratio_all,
        "split": sp,
        "n_false_h2": n_fh2,
        "wr_false_h2": wr_fh2,
        "n_pass_h2": n_ph2,
        "wr_pass_h2": wr_ph2,
        "ratio_h2": ratio_h2,
        "n_ok": n_ok,
        "ratio_ok": ratio_ok,
        "passed": passed,
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="B1 false-cohort MR walk-forward validation")
    ap.add_argument(
        "--class", dest="asset_class", default=None, help="asset class from nt_symbol (e.g. forex, commodities)"
    )
    ap.add_argument("--symbols", nargs="*", default=None, help="explicit symbol list")
    ap.add_argument("--split", default=None, help="ISO split timestamp; default = median of pooled cohort ts")
    ap.add_argument("--direction", choices=["buy", "sell", "all"], default="all")
    args = ap.parse_args()

    # Resolve symbol set
    if args.symbols:
        symbols = args.symbols
    else:
        cls = args.asset_class or "forex"
        r = httpx.get(
            f"{U}/rest/v1/nt_symbol",
            headers=H,
            params={"select": "symbol,asset_class", "active": "eq.true", "limit": "200"},
            timeout=20,
        )
        r.raise_for_status()
        symbols = [row["symbol"] for row in r.json() if row.get("asset_class") == cls]
        if not symbols:
            print(f"No active symbols found for class '{cls}'", file=sys.stderr)
            return 1

    now = datetime.now(timezone.utc)
    out: list[str] = []
    out.append("# B1 — context-alignment false-cohort walk-forward")
    out.append(f"Generated: {now.isoformat()}")
    out.append(f"Symbols: {symbols}")
    out.append(f"Direction: {args.direction} | Era: post {CUTOVER[:10]}, contaminated=false, tp_hit/sl_hit only")
    out.append(f"Bar: WR(false) >= {BAR_RATIO}x WR(pass) on held-out half, n >= {BAR_N}")

    all_rows = fetch_resolved(symbols)
    if args.direction != "all":
        all_rows = [r for r in all_rows if r["signal"] == args.direction]
    out.append(f"\nResolved era-clean rows: {len(all_rows)}")
    if not all_rows:
        print("\n".join(out))
        return 1

    # Per-class pooled (split at the pooled median unless --split given)
    class_name = args.asset_class or ("explicit" if args.symbols else "forex")
    out.append("\n## Per-class (pooled)")
    class_res = evaluate_cell(f"{class_name} (pooled)", all_rows, args.split, out)

    # Per-symbol, using the class split timestamp so halves are comparable
    out.append("\n## Per-symbol")
    per_symbol: list[dict] = []
    for sym in symbols:
        sym_rows = [r for r in all_rows if r["symbol"] == sym]
        if not sym_rows:
            out.append(f"\n### {sym}\n  (no era-clean resolved rows)")
            continue
        per_symbol.append(evaluate_cell(sym, sym_rows, class_res["split"], out))

    # Per (symbol, direction)
    out.append("\n## Per (symbol, direction)")
    for sym in symbols:
        for direction in ("buy", "sell"):
            cell_rows = [r for r in all_rows if r["symbol"] == sym and r["signal"] == direction]
            if not cell_rows:
                continue
            evaluate_cell(f"{sym} {direction}", cell_rows, class_res["split"], out)

    # Summary table
    out.append("\n## Summary")
    out.append(
        f"{'cell':<22} {'n_false':>8} {'WR_false':>9} {'WR_pass':>8} {'ratio_all':>10} "
        f"{'n_false_h2':>11} {'ratio_h2':>9} {'verdict':>8}"
    )
    for res in [class_res] + per_symbol:
        out.append(
            f"{res['name']:<22} {res['n_false']:>8} {res['wr_false']:>9.3f} "
            f"{res['wr_pass']:>8.3f} {res['ratio_all']:>10.2f} "
            f"{res['n_false_h2']:>11} {res['ratio_h2']:>9.2f} "
            f"{res['verdict']:>8}"
        )

    text = "\n".join(out)
    print(text)

    # Persist to worklog reports (repo convention). Slug the cohort so runs
    # for different classes/symbol-sets don't clobber each other.
    slug = args.asset_class or ("-".join(args.symbols) if args.symbols else "forex")
    slug = slug.replace(",", "-").replace(" ", "")
    report_dir = ROOT / "worklog" / "kairos" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{now.strftime('%Y%m%d')}_b1_mr_cohort_walkforward_{slug}.md"
    report_path.write_text(text + "\n", encoding="utf-8")
    print(f"\n[saved] {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
