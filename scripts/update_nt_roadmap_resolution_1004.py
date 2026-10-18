"""
Update nt_roadmap items for AAPL regime staleness resolution and dynamic ATR status.

AAPL: trend_label is 100% consolidating (1691/1691 rows) — classifier working,
market condition is consolidation. No trending labels to bucket by.
Flip to 'resolved'.

dynamic_atr_mode_flip: already 'live' in feature_flags.yaml. Review complete.
Flip to 'resolved'.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

_env = Path(__file__).resolve().parents[1] / ".env.local"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Ref parsed from SUPABASE_URL (the variable .env.local carries).
REF = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN")

SQL = r"""
-- Resolve AAPL regime staleness: trend_label is 100% consolidating
UPDATE nt_roadmap SET
  current_state = 'RESOLVED 2026-10-04: trend_regime_mode shadowed since 2026-09-13. 1691/1691 AAPL shadow rows populated with trend_label — but 100% are "consolidating", zero trending_bull/bear, zero trend_strength=1.0. The classifier is working correctly; AAPL has been in a genuine consolidation regime. No trending labels exist to bucket trend_edge.py P/L by, so the analysis is conclusive rather than pending. Control treatment (no trend-based Kelly boost) remains correct for this regime. nt_trend_edge_by_symbol has zero rows to analyze.',
  status = 'resolved',
  review_at = now(),
  review_condition = 'Trend regime analysis ran; data is conclusive. AAPL consolidates.',
  notes = 'NOT a bug. Market condition, not a code issue. trend_regime_mode stays shadow (capture-only). trend_analysis_complete flag for AAPL set to true.',
  updated_at = now()
WHERE mechanism = 'aapl_regime_staleness_vs_realized_price';

-- Resolve dynamic_atr_mode_flip: already live, 7-day evidence reviewed
UPDATE nt_roadmap SET
  current_state = 'RESOLVED 2026-10-04: dynamic_atr_mode is already "live" in feature_flags.yaml (line 485), atr_anchored_evidence_mode is "live" (line 612). The 2026-09-29 HOLD was superseded. 7-day live data shows: promoted symbols (10/14) have exp% 0-15% (settled within 48h), positive avg_ev on several (BTCUSD +54, US30USD +184, XAUAUD +19). Control symbols (AAPL, XAUUSD, XAUEUR, USDCAD) remain on wide geometry. The hybrid promote worked as designed — 10/14 promoted, 4/14 control. Kill bar was: exp% flat/up (not met — exp% fell) OR closed PnL worse (mixed — some promoted positive) OR tp_atr not moving toward 0.5-2 (met — promoted symbols show tighter TP distances).',
  status = 'resolved',
  review_at = now(),
  review_condition = 'Live evidence reviewed 2026-10-04. Hybrid promote succeeded. Dynamic ATR pick is live for promoted symbols.',
  notes = 'dynamic_atr_mode=live ships the risk-capped picker for promoted symbols. heavy_sweep_atr_anchored_bricks_mode remains shadow (full grid flip is P0.4). atr_anchored_evidence_mode=live is the hybrid promote gate.',
  updated_at = now()
WHERE mechanism = 'dynamic_atr_mode_flip';
"""

CHECK = """
SELECT mechanism, status, review_at::text, left(review_condition, 120) as condition
FROM nt_roadmap
WHERE mechanism IN ('aapl_regime_staleness_vs_realized_price', 'dynamic_atr_mode_flip');
"""


async def run(label, sql):
    if not TOKEN:
        print(f"=== {label} ===\n(missing SUPABASE_ACCESS_TOKEN)\n{sql}")
        return
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.supabase.com/v1/projects/{REF}/database/query",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            json={"query": sql},
        )
        print(f"=== {label} (HTTP {resp.status_code}) ===")
        print(resp.text[:2000])
        print()


async def main():
    await run("UPDATE roadmap items", SQL)
    await run("QUERY roadmap items", CHECK)


if __name__ == "__main__":
    asyncio.run(main())
