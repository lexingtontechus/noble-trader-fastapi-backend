"""
kairos_r2_router_measure.py — R2: does the stamped strategy archetype predict
outcome better than the uniform engine?

Scope: docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md
  H1  routed (non-stand_by) expectancy vs stand_by
  H2  per-archetype WR/expectancy vs the uniform-engine baseline
  H3  stand_by-as-suppress (descriptive only)

Era: post-cutover (2026-10-02), contaminated=false. Expired excluded from
WR/expectancy (a timeout is neither a win nor a loss) but reported as its own
share. Verdict per archetype: HELPS / INERT / ANTI-PREDICTIVE / UNDECIDABLE.
Fail-open on a thin cohort -> UNDECIDABLE (never a manufactured pass).

Usage
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_r2_router_measure.py
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_r2_router_measure.py --min-routed 200 --min-cell 40
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
_env = ROOT / ".env.local"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
CUTOVER = "2026-10-02T00:00:00+00:00"
PAGE = 1000


def fetch_stamped() -> list[dict]:
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(
                f"{U}/rest/v1/nt_signal_sim",
                headers=H,
                params={
                    "select": "symbol,signal,ts,outcome,strategy_archetype,p_win_dir,pnl_bricks,qualified",
                    "strategy_archetype": "not.is.null",
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


def metrics(rows: list[dict]) -> dict:
    """WR / expectancy / expired share / calibration bias over a row set."""
    resolved = [r for r in rows if r["outcome"] in ("tp_hit", "sl_hit")]
    expired = [r for r in rows if r["outcome"] == "expired"]
    n = len(resolved)
    wins = sum(1 for r in resolved if r["outcome"] == "tp_hit")
    wr = wins / n if n else 0.0
    pnls = [r["pnl_bricks"] for r in resolved if r.get("pnl_bricks") is not None]
    exp = sum(pnls) / len(pnls) if pnls else None
    pwd = [r["p_win_dir"] for r in resolved if r.get("p_win_dir") is not None]
    apwd = sum(pwd) / len(pwd) if pwd else None
    bias = (apwd - wr) if apwd is not None else None
    return {
        "n_resolved": n,
        "wr": wr,
        "expectancy": exp,
        "avg_p_win_dir": apwd,
        "bias": bias,
        "n_expired": len(expired),
        "expired_share": (len(expired) / len(rows)) if rows else 0.0,
        "n_total": len(rows),
    }


def walkforward(rows: list[dict], split_ts: str | None) -> tuple[str | None, dict, dict]:
    ordered = sorted(rows, key=lambda r: r["ts"])
    if not ordered:
        return split_ts, metrics([]), metrics([])
    if split_ts is None:
        split_ts = ordered[len(ordered) // 2]["ts"]
    h1 = [r for r in ordered if r["ts"] < split_ts]
    h2 = [r for r in ordered if r["ts"] >= split_ts]
    return split_ts, metrics(h1), metrics(h2)


def verdict(cell: dict, baseline: dict, min_cell: int, half1: dict, half2: dict) -> str:
    if cell["n_resolved"] < min_cell:
        return "UNDECIDABLE"
    if cell["expectancy"] is None or baseline["expectancy"] is None:
        return "UNDECIDABLE"
    d1 = (half1["expectancy"] or 0.0) - (baseline["expectancy"] or 0.0)
    d2 = (half2["expectancy"] or 0.0) - (baseline["expectancy"] or 0.0)
    if d1 > 0 and d2 > 0:
        return "HELPS"
    if d1 < 0 and d2 < 0:
        return "ANTI-PREDICTIVE"
    return "INERT"


def main() -> int:
    ap = argparse.ArgumentParser(description="R2 strategy-router measurement")
    ap.add_argument("--min-routed", type=int, default=200, help="routed episodes needed to decide H1")
    ap.add_argument("--min-cell", type=int, default=40, help="resolved episodes needed per archetype")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    rows = fetch_stamped()
    out: list[str] = []
    out.append("# R2 — strategy-router measurement")
    out.append(f"Generated: {now.isoformat()}")
    out.append("Scope: docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md")
    out.append(f"Era: post {CUTOVER[:10]}, contaminated=false, stamped rows only")
    out.append(f"Bar: >= {args.min_routed} routed episodes; >= {args.min_cell} resolved per archetype")

    by_arch: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arch[r["strategy_archetype"]].append(r)

    routed = [r for r in rows if r["strategy_archetype"] != "stand_by"]
    routed_symbols = {r["symbol"] for r in routed}
    out.append(
        f"\nStamped rows: {len(rows)}  |  routed (non-stand_by): {len(routed)} across {len(routed_symbols)} symbols"
    )
    for a in sorted(by_arch):
        out.append(f"  {a:16s} total={len(by_arch[a]):5d}  resolved={metrics(by_arch[a])['n_resolved']:5d}")

    baseline = metrics(rows)
    out.append("\n## Uniform-engine baseline (all stamped)")
    out.append(
        f"  n_resolved={baseline['n_resolved']} WR={baseline['wr']:.3f} "
        f"expectancy={baseline['expectancy']} bias={baseline['bias']}"
    )

    # H1 — routed vs stand_by
    out.append("\n## H1 — routed vs stand_by")
    if len(routed) < args.min_routed:
        out.append(
            f"  UNDECIDABLE — {len(routed)} routed episodes < bar {args.min_routed}. "
            f"The router declines {100 * (1 - len(routed) / len(rows)):.1f}% of signals."
        )
    else:
        split, rh1, rh2 = walkforward(routed, None)
        sb1 = metrics([r for r in by_arch.get("stand_by", []) if r["ts"] < split])
        sb2 = metrics([r for r in by_arch.get("stand_by", []) if r["ts"] >= split])
        out.append(f"  split @ {split}")
        out.append(
            f"  routed   H1 n={rh1['n_resolved']} exp={rh1['expectancy']} | "
            f"H2 n={rh2['n_resolved']} exp={rh2['expectancy']}"
        )
        out.append(
            f"  stand_by H1 n={sb1['n_resolved']} exp={sb1['expectancy']} | "
            f"H2 n={sb2['n_resolved']} exp={sb2['expectancy']}"
        )

    # H2 / H3 — per archetype
    out.append("\n## Per-archetype verdicts")
    for a in sorted(by_arch):
        m = metrics(by_arch[a])
        split, h1, h2 = walkforward(by_arch[a], None)
        v = verdict(m, baseline, args.min_cell, h1, h2)
        out.append(f"\n### {a}")
        out.append(
            f"  n_total={m['n_total']} n_resolved={m['n_resolved']} n_expired={m['n_expired']} "
            f"expired_share={m['expired_share']:.3f}"
        )
        out.append(
            f"  WR={m['wr']:.3f} expectancy={m['expectancy']} avg_p_win_dir={m['avg_p_win_dir']} bias={m['bias']}"
        )
        out.append(f"  walk-forward split @ {split}")
        out.append(
            f"    H1 n={h1['n_resolved']} exp={h1['expectancy']} | H2 n={h2['n_resolved']} exp={h2['expectancy']}"
        )
        out.append(f"  VERDICT: {v}")

    text = "\n".join(out)
    print(text)
    report_dir = ROOT / "worklog" / "kairos" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{now.strftime('%Y%m%d')}_r2_router_measure.md"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"\n[saved] {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
