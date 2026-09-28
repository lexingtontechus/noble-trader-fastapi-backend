"""Run macro agent on a subset, then verify nt_macro_forecast against the process."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")
# OpenRouter has no credits (402) — use NVIDIA NIM only for this run.
os.environ["OPENROUTER_API_KEY"] = ""

import httpx

from regime_platform.services.self_learning import macro_forecast_agent as mfa


async def main() -> None:
    # Small subset: one commodity + metals + FX + index
    sources = (
        mfa.ForecastSource(
            name="commodities_12m",
            doc_path=mfa._DOCS_LLM_DIR / "commodity_12m_forecast_model_2027.md",
            symbols=("HO", "SI"),
        ),
        mfa.ForecastSource(
            name="existing_symbols_12m",
            doc_path=mfa._DOCS_LLM_DIR / "existing_symbols_12m_forecast_model_2027.md",
            symbols=("XAUUSD", "EURUSD", "US500", "BTCUSD"),
        ),
    )
    print("running subset…", flush=True)
    summary = await mfa.run_macro_forecast(sources=sources)
    print("summary", summary, flush=True)

    # ── verify against process ─────────────────────────────────────────
    U = os.environ["SUPABASE_URL"].rstrip("/")
    K = os.environ["SUPABASE_KEY"]
    H = {"apikey": K, "Authorization": f"Bearer {K}"}
    r = httpx.get(
        f"{U}/rest/v1/nt_macro_forecast",
        headers=H,
        params={"select": "*", "order": "symbol"},
        timeout=30,
    )
    rows = r.json()
    print(f"rows in table: {len(rows)}", flush=True)

    required = [
        "symbol", "current_trend_label", "current_macro_label", "current_macro_strength",
        "narrative", "model_provider", "updated_at",
        "thesis", "drivers", "risks", "horizon_days",
        "structure_bias", "structure_source",
        "pivot_label", "source_doc",
    ]
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    chk("table_has_rows", len(rows) >= 1, f"n={len(rows)}")
    by_sym = {x.get("symbol"): x for x in rows}
    for sym in ("HO", "SI", "XAUUSD", "EURUSD", "US500", "BTCUSD"):
        row = by_sym.get(sym)
        if not row:
            chk(f"row_{sym}", False, "missing")
            continue
        chk(f"row_{sym}", True)
        chk(f"{sym}.trend_vocab", row.get("current_trend_label") in {
            "trending_up", "trending_down", "reversal_up", "reversal_down", "consolidating",
        }, str(row.get("current_trend_label")))
        chk(f"{sym}.macro_vocab", row.get("current_macro_label") in ("bull", "bear", "neutral"), str(row.get("current_macro_label")))
        chk(f"{sym}.strength_01", isinstance(row.get("current_macro_strength"), (int, float)) and 0 <= float(row["current_macro_strength"]) <= 1, str(row.get("current_macro_strength")))
        chk(f"{sym}.narrative", bool(row.get("narrative")))
        chk(f"{sym}.thesis_field", "thesis" in row, "present" if "thesis" in row else "missing col")
        chk(f"{sym}.drivers_is_list", isinstance(row.get("drivers"), list), str(type(row.get("drivers")).__name__))
        chk(f"{sym}.risks_is_list", isinstance(row.get("risks"), list), str(type(row.get("risks")).__name__))
        chk(f"{sym}.source_doc", bool(row.get("source_doc")), str(row.get("source_doc")))
        # structure optional but column must exist after 182
        chk(f"{sym}.structure_cols", "structure_bias" in row, str(row.get("structure_bias")))
        chk(f"{sym}.pivot_cols", "pivot_label" in row, str(row.get("pivot_label")))

    # sample one full row
    if rows:
        sample = by_sym.get("XAUUSD") or rows[0]
        print("SAMPLE", {k: sample.get(k) for k in required}, flush=True)
        print("SAMPLE drivers", sample.get("drivers"), flush=True)
        print("SAMPLE risks", sample.get("risks"), flush=True)
        print("SAMPLE structure", sample.get("structure_bias"), sample.get("structure_note"), flush=True)
        print("SAMPLE pivot", sample.get("pivot_label"), sample.get("pivot_summary"), flush=True)

    print("\n=== PROCESS VERIFICATION ===")
    n_ok = 0
    for name, ok, detail in checks:
        print(f"{'✓' if ok else '✗'} {name} {detail}")
        n_ok += int(ok)
    print(f"\n{n_ok}/{len(checks)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
