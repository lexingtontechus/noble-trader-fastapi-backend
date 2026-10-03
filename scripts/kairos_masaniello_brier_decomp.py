"""
kairos_masaniello_brier_decomp.py — Brier decomposition with Masaniello urgency.

COMBINES: calibration_decomp's Murphy decomposition (Brier / REL / RES / UNC /
BSS / AUC) WITH the Masaniello urgency gate's batch-management lens — so the
calibration report doesn't just say "p_win is overconfident" but also
"this overconfidence is concentrated in batches that failed to accumulate
wins on schedule."

For each symbol:
  1. Fetch resolved nt_signal_sim rows (outcome in tp_hit/sl_hit).
  2. Reconstruct per-trade batch state (wins_so_far, trade_index) by
     grouping signals into batches of N and counting wins/losses.
  3. Compute M_i (urgency) and urgency_drift for each trade.
  4. Score p_win against the *directional* label (drift_{1h,4h,24h}_pct > 0)
     and the *bracket* label (outcome == tp_hit) using calibration_decomp.
  5. Stratify Brier bins by whether the trade was in a "high-urgency"
     batch (drift > threshold) vs "on-track" batch.

Output: worklog/kairos/reports/YYYYMMDD_masaniello_brier_decomp.md

Usage:
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_masaniello_brier_decomp.py
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from regime_platform.sweep_orchestrator import calibration_decomp as cd  # noqa: E402
from regime_platform.sweep_orchestrator import calibration_quality_gate as cqg  # noqa: E402

BATCH_SIZE = cqg.DEFAULT_BATCH_SIZE
TARGET_WINS = cqg.DEFAULT_TARGET_WINS
urgency_drift_ratio = cqg.urgency_drift_ratio
urgency_threshold = cqg.urgency_threshold

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}

LOOKBACK_DAYS = int(os.environ.get("CAL_LOOKBACK_DAYS", "45"))
MIN_SYMBOL_N = int(os.environ.get("CAL_MIN_SYMBOL_N", "200"))
PAGE_SIZE = 1000

# Which p_win column to score against which label.
# See calibration_decomp.py module docstring for why both labels matter.
CANDIDATES = [
    ("p_win",            "directional"),
    ("p_win",            "bracket"),
    ("predicted_p_win_raw", "directional"),
    ("predicted_p_win_raw", "bracket"),
    ("p_win_bracket",    "bracket"),
    ("p_win_dir",        "directional"),
]


def _fetch(params: dict) -> list[dict]:
    """Fetch rows from nt_signal_sim with pagination."""
    rows, off = [], 0
    with httpx.Client(timeout=180) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H,
                      params={**params, "limit": str(PAGE_SIZE), "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < PAGE_SIZE:
                break
            off += len(b)
    return rows


def fetch(since: str) -> list[dict]:
    return _fetch({
        "select": "symbol,signal,ts,p_win,predicted_p_win_raw,p_win_bracket,p_win_dir,"
                  "p_resolve,p_win_resolve,drift_1h_pct,drift_4h_pct,drift_24h_pct,"
                  "mae_4h_pct,mae_12h_pct,mae_24h_pct,mae_48h_pct,"
                  "mfe_4h_pct,mfe_12h_pct,mfe_24h_pct,mfe_48h_pct,"
                  "entry_price,sl_bricks,brick_size,tp_bricks,"
                  "outcome,regime",
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": f"gte.{since}",
        "order": "ts.asc",
    })


def label_directional(row: dict, col: str) -> tuple[float, bool] | None:
    """Directional label: drift_24h_pct > 0 (the event p_win forecasts)."""
    pred = row.get(col)
    drift = row.get("drift_24h_pct")
    if pred is None or drift is None:
        return None
    return float(pred), float(drift) > 0


def label_bracket(row: dict, col: str) -> tuple[float, bool] | None:
    """Bracket label: outcome == tp_hit."""
    pred = row.get(col)
    outcome = row.get("outcome")
    if pred is None or outcome not in ("tp_hit", "sl_hit"):
        return None
    return float(pred), outcome == "tp_hit"


# ─── Batch reconstruction ─────────────────────────────────────────────────────

def _reconstruct_batches(rows: list[dict],
                         batch_size: int = BATCH_SIZE,
                         target_wins: int = TARGET_WINS) -> list[dict]:
    """Group resolved sim rows into batches and compute per-trade urgency.

    Takes a symbol's resolved rows (already ordered by ts) and groups them
    into consecutive batches of ``batch_size``. For each trade, computes:
      - wins_so_far / losses_so_far within its batch
      - urgency_index (M_i = (W − w) / (N − i + 1))
      - urgency_drift (M_i / M_0)
      - regime_age_hours (hours since batch start, proxied by ts span)

    Returns a new list of dicts with ``_m_i``, ``_drift``, ``_batch_num``
    added.
    """
    enriched = []
    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start:batch_start + batch_size]
        wins = 0
        losses = 0
        batch_ts_start = batch[0].get("ts") if batch else None
        for j, row in enumerate(batch):
            i = j + 1  # 1-based trade index
            drift = urgency_drift_ratio(wins, losses, target_wins, batch_size)
            m_0 = target_wins / batch_size
            m_i = m_0 * drift

            # Regime age proxy: hours from batch start to this trade
            age_hours = 0.0
            if batch_ts_start and row.get("ts"):
                try:
                    t0 = _parse_ts(batch_ts_start)
                    t1 = _parse_ts(row["ts"])
                    if t0 and t1:
                        age_hours = (t1 - t0).total_seconds() / 3600.0
                except (ValueError, TypeError):
                    pass

            enriched.append({
                **row,
                "_batch_num": batch_start // batch_size + 1,
                "_trade_index": i,
                "_wins_so_far": wins,
                "_losses_so_far": losses,
                "_m_i": round(m_i, 4),
                "_m_0": round(m_0, 4),
                "_drift": round(drift, 4),
                "_age_hours": round(age_hours, 2),
            })
            # Update win/loss counts AFTER annotating the row so
            # wins_so_far reflects the state BEFORE this trade resolved.
            outcome = row.get("outcome")
            if outcome == "tp_hit":
                wins += 1
            elif outcome == "sl_hit":
                losses += 1
    return enriched


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ─── Reporting ────────────────────────────────────────────────────────────────

def _fmt_brier(d: dict) -> str:
    if not d.get("n"):
        return "  no data"
    bss = d.get("bss")
    bss_str = f"{bss:+.4f}" if bss is not None else "n/a"
    auc = d.get("auc")
    auc_str = f"{auc:.4f}" if auc is not None else "n/a"
    return (
        f"  n={d['n']}  base_rate={d['base_rate']:.4f}"
        f"  Brier={d['brier']:.4f}  REL={d['rel']:.4f}  RES={d['res']:.4f}"
        f"  UNC={d['unc']:.4f}  BSS={bss_str}  AUC={auc_str}"
    )


def _fmt_bins(d: dict) -> list[str]:
    lines = []
    for b in d.get("bins", []):
        gap = b.get("gap", "")
        gap_str = f" gap={gap:+.4f}" if gap != "" else ""
        lines.append(
            f"    {b['bin']:>9s}  n={b['n']:>5d}  "
            f"pred={b['mean_pred']:.4f}  realised={b['realised']:.4f}{gap_str}"
        )
    return lines


def build_report(all_rows: list[dict]) -> str:
    """Build the markdown report."""
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    lines: list[str] = []
    lines.append(f"# Masaniello Brier Decomposition — {date_str}\n")
    lines.append(f"Lookback: {LOOKBACK_DAYS}d (since {since[:16]}Z)\n")
    lines.append(f"Min symbols N: {MIN_SYMBOL_N}")
    lines.append(f"Batch size N: {BATCH_SIZE}, target wins W: {TARGET_WINS}\n")

    # Group by symbol
    by_symbol: dict[str, list[dict]] = {}
    for r in all_rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    symbols_sorted = sorted(by_symbol.keys())
    lines.append(f"Symbols: {len(symbols_sorted)}\n")

    for sym in symbols_sorted:
        rows = by_symbol[sym]
        if len(rows) < MIN_SYMBOL_N:
            lines.append(f"\n## {sym} — skipped (n={len(rows)} < {MIN_SYMBOL_N})\n")
            continue

        # Reconstruct batch state + urgency
        enriched = _reconstruct_batches(rows)
        high_urg = [e for e in enriched if e["_drift"] > urgency_threshold(e["_age_hours"])]

        # Score each candidate
        lines.append(f"\n## {sym} — n={len(rows)} resolved\n")

        # Urgency summary
        if enriched:
            drifts = [e["_drift"] for e in enriched]
            m_is = [e["_m_i"] for e in enriched]
            lines.append("### Urgency state (Masaniello M_i)\n")
            lines.append(f"  trades analysed: {len(enriched)}")
            lines.append(f"  batches: {enriched[-1]['_batch_num']}")
            lines.append(f"  M_i mean={statistics.fmean(m_is):.4f}  "
                        f"min={min(m_is):.4f}  max={max(m_is):.4f}")
            lines.append(f"  drift mean={statistics.fmean(drifts):.4f}  "
                        f"min={min(drifts):.4f}  max={max(drifts):.4f}")
            lines.append(f"  high-urgency trades: {len(high_urg)} "
                        f"({len(high_urg)/len(enriched)*100:.1f}%)")
            lines.append("")

        # Brier decomposition per candidate
        lines.append("### Brier decomposition\n")
        for col, label in CANDIDATES:
            if label == "directional":
                pairs = [p for p in (label_directional(e, col) for e in enriched) if p is not None]
            else:
                pairs = [p for p in (label_bracket(e, col) for e in enriched) if p is not None]
            scored = cd.score_candidate(col, pairs, target=label)
            lines.append(f"\n  **{col} → {label}**")
            lines.append(_fmt_brier(scored))
            for bl in _fmt_bins(scored):
                lines.append(bl)

        # Stratify by high-urgency vs on-track (directional label, p_win)
        if high_urg:
            lines.append("\n### Calibration stratified by urgency\n")
            on_track = [e for e in enriched if e not in high_urg]
            for label_str, subset, col in [
                ("high-urgency batch", high_urg, "p_win"),
                ("on-track batch", on_track, "p_win"),
            ]:
                if label == "directional":
                    pairs = [p for p in (label_directional(e, col) for e in subset) if p is not None]
                else:
                    pairs = [p for p in (label_bracket(e, col) for e in subset) if p is not None]
                scored = cd.score_candidate(col, pairs, target="directional")
                lines.append(f"\n  high-urgency n={len(high_urg)}, on-track n={len(on_track)}")
                lines.append(f"  **{col} → directional** [{label_str}]")
                lines.append(_fmt_brier(scored))

        lines.append("\n---")

    # Aggregate verdict
    lines.append("\n## Aggregate verdict\n")
    all_pairs = []
    for r in all_rows:
        p = r.get("p_win")
        drift = r.get("drift_24h_pct")
        if p is not None and drift is not None:
            all_pairs.append((float(p), float(drift) > 0))
    scored = cd.score_candidate("p_win", all_pairs, target="directional")
    lines.append(_fmt_brier(scored))
    lines.append(f"\n  Verdict: {cd.verdict(scored)}")

    return "\n".join(lines)


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = fetch(since)
    if not rows:
        print(f"No rows since {since}")
        return

    report = build_report(rows)

    out_dir = Path(ROOT) / "worklog" / "kairos" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}_masaniello_brier_decomp.md"
    out_path.write_text(report)
    print(f"Report written to {out_path}")
    print(f"\n{report[:500]}")
    print("...")


if __name__ == "__main__":
    main()
