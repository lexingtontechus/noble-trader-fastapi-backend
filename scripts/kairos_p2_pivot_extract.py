"""
kairos_p2_pivot_extract.py — P2.1: typed pivot extraction eval.

Scope: worklog/kairos/20260927_P0_P4_PROGRAMME.md §4
  Stage P2.1: extraction eval on labeled events (gold + calendar sample).
  Does NOT stamp signals. No gates. LLM via existing OpenRouter/NVIDIA path.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_p2_pivot_extract.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local")

# OpenRouter returns HTTP 402 (no credits) as of 2026-09-28 — skip it so NIM is first.
if os.getenv("P2_SKIP_OPENROUTER", "1") == "1":
    os.environ["OPENROUTER_API_KEY"] = ""

from regime_platform.sweep_orchestrator.kairos import DEFAULT_CALENDAR_PATH  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_pivot import (  # noqa: E402
    PIVOT_LABELS,
    _PIVOT_SYSTEM,
    build_pivot_prompt,
    extract_json_array,
    is_policy_event,
    pivot_prior_ok,
    validate_pivot,
)
from regime_platform.services.self_learning.llm_client import (  # noqa: E402
    call_llm_with_provider,
)

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260927_p2_pivot_extract.md"

# Compact gold set — known policy pivots vs routine data. Independent of calendar IDs.
GOLD = [
    ({"title": "FOMC Statement", "country": "US", "date": "2024-09-18",
      "context": "First rate cut of the cycle; Fed shifts from inflation fight to labor-market balance."},
     "major_pivot", "first cut of the cycle / reaction-function shift"),
    ({"title": "Fed Interest Rate Decision", "country": "US", "date": "2022-03-16", "actual": 0.5, "forecast": 0.5, "previous": 0.25,
      "context": "First hike since 2018; start of hiking cycle after inflation surge."},
     "major_pivot", "start of hiking cycle"),
    ({"title": "ECB Interest Rate Decision", "country": "EU", "date": "2024-06-06", "actual": 3.75, "forecast": 3.75, "previous": 4.0,
      "context": "ECB delivers first cut of the cycle ahead of the Fed."},
     "major_pivot", "ECB first cut"),
    ({"title": "BoJ Monetary Policy Statement", "country": "JP", "date": "2024-03-19",
      "context": "End of negative rates and YCC; first hike since 2007."},
     "major_pivot", "end of YCC / negative rates"),
    ({"title": "Fed Interest Rate Decision", "country": "US", "date": "2024-11-07", "actual": 4.5, "forecast": 4.5, "previous": 4.75,
      "context": "Expected 25bp cut mid-easing-cycle; guidance largely unchanged."},
     "incremental", "mid-cycle move expected"),
    ({"title": "Fed Interest Rate Decision", "country": "US", "date": "2025-01-29", "actual": 4.5, "forecast": 4.5, "previous": 4.5,
      "context": "Hold as widely expected; no change to reaction function."},
     "no_change", "hold as expected"),
    ({"title": "Non Farm Payrolls", "country": "US", "date": "2025-03-07", "actual": 151, "forecast": 160, "previous": 125,
      "context": "Payrolls near consensus; no policy regime implication."},
     "incremental", "data print, not a policy pivot"),
    ({"title": "Inflation Rate YoY", "country": "US", "date": "2025-02-12", "actual": 3.0, "forecast": 2.9, "previous": 2.9,
      "context": "CPI close to consensus; gradual disinflation continues."},
     "incremental", "CPI near consensus"),
    ({"title": "Retail Sales MoM", "country": "US", "date": "2025-01-16", "actual": 0.4, "forecast": 0.6, "previous": 0.8,
      "context": "Routine consumer data print."},
     "no_change", "routine data"),
    ({"title": "Caixin Manufacturing PMI", "country": "CN", "date": "2025-01-02", "actual": 50.5, "forecast": 51.7, "previous": 51.5,
      "context": "Routine PMI around the 50 line."},
     "no_change", "routine PMI"),
]


async def llm_label_batch(events: list[dict], kind: str) -> list[dict | None]:
    """One event per LLM call (NIM is reliable on single-object JSON)."""
    out: list[dict | None] = []
    for ev in events:
        prompt = build_pivot_prompt([ev], kind=kind)
        result = await call_llm_with_provider(_PIVOT_SYSTEM, prompt, max_tokens=300)
        if result is None:
            out.append(None)
            continue
        parsed, _provider = result
        v = None
        if isinstance(parsed, dict):
            v = validate_pivot(parsed)
            if v is None and isinstance(parsed.get("events"), list) and parsed["events"]:
                v = validate_pivot(parsed["events"][0])
        out.append(v)
    return out


async def main() -> None:
    import os

    now = datetime.now(timezone.utc)
    has_llm = bool(os.getenv("OPENROUTER_API_KEY", "").strip() or os.getenv("NVIDIA_NIM_API_KEY", "").strip())
    lines = [
        "# P2.1 — typed pivot extraction eval",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · LLM via OpenRouter/NVIDIA · no stamps",
        "",
        "Labels: `major_pivot` / `incremental` / `no_change` · product map: major → `dir_flip_candidate`",
        "",
    ]
    if not has_llm:
        lines += [
            "> **BLOCKED:** `OPENROUTER_API_KEY` / `NVIDIA_NIM_API_KEY` not set in this environment.",
            "> Code path is wired (same client as `macro_forecast_agent`).",
            "> Re-run on Studio (or export a key) for gold accuracy + calendar rates.",
            "",
        ]

    # ── Gold set ─────────────────────────────────────────────────────────
    gold_events = [g[0] for g in GOLD]
    print("gold LLM…", flush=True)
    gold_out = await llm_label_batch(gold_events, kind="policy")
    hits = 0
    n_scored = 0
    rows_g = []
    for (ev, exp, why), got in zip(GOLD, gold_out):
        lab = got["label"] if got else None
        ok = lab == exp
        if lab in PIVOT_LABELS:
            n_scored += 1
            if ok:
                hits += 1
        rows_g.append((ev.get("title"), ev.get("date"), exp, lab, ok, why))
    lines.append("## 1. Gold set (10 hand-labeled events)")
    lines.append("")
    lines.append("| event | date | expected | got | ok | note |")
    lines.append("|---|---|---|---|---|---|")
    for t, d, exp, lab, ok, why in rows_g:
        lines.append(f"| {t} | {d} | {exp} | {lab or 'FAIL'} | {'✓' if ok else '✗'} | {why} |")
    acc = (hits / n_scored) if n_scored else 0.0
    lines.append("")
    lines.append(f"**Accuracy (scored): {hits}/{n_scored} = {acc:.0%}**")
    lines.append("")

    # ── Calendar sample ──────────────────────────────────────────────────
    lines.append("## 2. Live calendar sample")
    lines.append("")
    if not DEFAULT_CALENDAR_PATH.exists():
        lines.append("No calendar cache at `.cache/kairos/econ_calendar.json` — skip live sample.")
    else:
        cal = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))
        hi = [e for e in cal if (e.get("importance") or 0) >= 1 and e.get("title")]
        # prefer recent + policy-looking, else high-importance
        pol = [e for e in hi if is_policy_event(e.get("title"), e.get("country"))]
        data = [e for e in hi if not is_policy_event(e.get("title"), e.get("country"))]
        sample = pol[:12] + data[:12]
        kinds = ["policy"] * min(12, len(pol)) + ["data"] * min(12, len(data))
        print(f"calendar sample n={len(sample)} (policy={len(pol)} data={len(data)})", flush=True)
        outs = await llm_label_batch(sample, kind="data")
        # re-score policy vs data separately
        lab_counts: Counter[str] = Counter()
        scored = 0
        majors = 0
        by_kind = {"policy": Counter(), "data": Counter()}
        for ev, got, kind in zip(sample, outs, kinds):
            lab = got["label"] if got else "fail"
            lab_counts[lab] += 1
            by_kind[kind][lab] += 1
            if got and lab in PIVOT_LABELS:
                scored += 1
                if lab == "major_pivot":
                    majors += 1
        lines.append(f"- sampled **{len(sample)}** importance≥1 events ({len(pol)} policy / {len(data)} data)")
        lines.append(f"- label counts: {dict(lab_counts)}")
        lines.append(f"- schema-valid rate: **{scored}/{len(sample)}**")
        lines.append(
            f"- major_pivot rate (data): "
            f"{by_kind['data']['major_pivot']}/{sum(by_kind['data'].values()) or 1} "
            f"→ prior_ok={pivot_prior_ok(by_kind['data']['major_pivot'], sum(by_kind['data'].values()) or 0, kind='data')}"
        )
        lines.append(
            f"- major_pivot rate (policy): "
            f"{by_kind['policy']['major_pivot']}/{sum(by_kind['policy'].values()) or 1} "
            f"→ prior_ok={pivot_prior_ok(by_kind['policy']['major_pivot'], sum(by_kind['policy'].values()) or 0, kind='policy')}"
        )
        lines.append("")
        lines.append("| # | kind | title | label | axis | intensity |")
        lines.append("|---:|---|---|---|---|---:|")
        for i, (ev, got, kind) in enumerate(zip(sample, outs, kinds)):
            if not got:
                lines.append(f"| {i} | {kind} | {ev.get('title')} | FAIL | — | — |")
                continue
            lines.append(
                f"| {i} | {kind} | {ev.get('title')} | {got['label']} | {got['axis']} | {got['intensity']:.2f} |"
            )
    lines.append("")

    lines.append("## 3. P2.1 notes")
    lines.append("")
    lines.append("- Product mapping is research-only: `major_pivot` → `dir_flip_candidate`.")
    lines.append("- P2.2 (lead-lag vs HMM `dir_flip`) and P2.3 (stamp) are **not** built here.")
    lines.append("- Gate: LLM features off under macro-shock / vol_expand (programme §4).")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
