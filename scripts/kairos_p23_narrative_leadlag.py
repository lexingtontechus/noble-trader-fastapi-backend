"""
kairos_p23_narrative_leadlag.py — P2.3: do narrative/pivot labels lead dir_flip?

Unlike P2.2 (calendar *timestamps*), this uses **typed pivot labels** and
`pivot_summary` event times from `nt_macro_forecast.events[]` / `pivot_as_of`.
Outcome: HMM `dir_flip`/`both` from light-sweep labels.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_p23_narrative_leadlag.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.kairos import regime_cell  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import transition_type  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260928_p23_narrative_leadlag.md"
VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")


def fetch(table: str, params: dict) -> list[dict]:
    def page(off: int) -> tuple[list[dict], str | None]:
        with httpx.Client(timeout=120) as c:
            r = c.get(
                f"{U}/rest/v1/{table}",
                headers={**H, "Prefer": "count=exact"},
                params={**params, "limit": "1000", "offset": str(off)},
            )
            r.raise_for_status()
            return r.json(), r.headers.get("content-range")

    head, cr = page(0)
    total = None
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            total = None
    if not total or total <= 1000:
        return head
    rows = list(head)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(page, o) for o in range(1000, total, 1000)]
        for fut in as_completed(futs):
            b, _ = fut.result()
            rows.extend(b)
    return rows


def parse_ts(s) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def split_cell(regime: str | None) -> tuple[str | None, str | None]:
    if not regime:
        return None, None
    cell = regime_cell(regime)
    src = cell if cell and "|" in cell else str(regime)
    if "|" in src:
        vol, d = src.split("|", 1)
        return vol, d
    return None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    print("fetch macro events…", flush=True)
    macro = fetch("nt_macro_forecast", {"select": "symbol,pivot_label,pivot_as_of,pivot_summary,pivot_event_title,events"})

    # unique narrative/pivot events across symbols
    ev_map: dict[tuple[str, str], dict] = {}
    for row in macro:
        for e in (row.get("events") or []):
            key = (str(e.get("as_of")), str(e.get("title") or e.get("event_title") or ""))
            if key[0] in ("None", ""):
                continue
            prev = ev_map.get(key)
            # prefer major_pivot
            if prev is None or (e.get("label") == "major_pivot" and prev.get("label") != "major_pivot"):
                ev_map[key] = {
                    "as_of": e.get("as_of"),
                    "title": e.get("title") or e.get("event_title"),
                    "label": e.get("label"),
                    "summary": e.get("summary"),
                }
        if row.get("pivot_as_of") and row.get("pivot_label"):
            key = (str(row["pivot_as_of"]), str(row.get("pivot_event_title") or ""))
            ev_map.setdefault(key, {
                "as_of": row.get("pivot_as_of"),
                "title": row.get("pivot_event_title"),
                "label": row.get("pivot_label"),
                "summary": row.get("pivot_summary"),
            })

    events = []
    for e in ev_map.values():
        ts = parse_ts(e.get("as_of"))
        if ts is None:
            continue
        events.append((ts.timestamp(), e.get("label") or "unknown", e.get("title") or "", e.get("summary") or ""))
    events.sort(key=lambda t: t[0])
    n_major = sum(1 for e in events if e[1] == "major_pivot")
    n_incr = sum(1 for e in events if e[1] == "incremental")
    n_none = sum(1 for e in events if e[1] == "no_change")
    print(f"  unique pivot events: {len(events)} (major={n_major} incremental={n_incr} no_change={n_none})", flush=True)

    print("fetch labels…", flush=True)
    raw = fetch(
        "nt_signal_sim",
        {
            "select": "symbol,ts,regime",
            "source": "eq.sweep-light",
            "signal": "in.(buy,sell)",
            "ts": "gte.2025-01-01T00:00:00Z",
            "order": "ts.asc",
        },
    )
    print(f"  {len(raw)} rows", flush=True)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in raw:
        ts = parse_ts(r.get("ts"))
        if ts is None:
            continue
        vol, d = split_cell(r.get("regime"))
        by_sym[r.get("symbol")].append({"ts": ts, "vol": vol, "dir": d})

    flips: list[float] = []
    for rows in by_sym.values():
        rows.sort(key=lambda x: x["ts"])
        prev = None
        for x in rows:
            cur = (x["vol"], x["dir"])
            if prev is not None and cur != prev and None not in cur:
                ttype = transition_type(prev[0], prev[1], cur[0], cur[1], VOL_ORDER)
                if ttype in ("dir_flip", "both"):
                    flips.append(x["ts"].timestamp())
            prev = cur
    flips.sort()
    print(f"  dir_flip/both: {len(flips)}", flush=True)

    def lead_hours(flip_t: float, label_filter: str | None = None) -> float | None:
        best = None
        for et, lab, _t, _s in events:
            if label_filter and lab != label_filter:
                continue
            if et <= flip_t:
                dt = (flip_t - et) / 3600.0
                if best is None or dt < best:
                    best = dt
        return best

    # rate-normalized lift after T0
    # Only past events can lead flips; drop future-dated calendar rows.
    now_ts = now.timestamp()
    past_events = [(t, lab, title, s) for t, lab, title, s in events if t <= now_ts]
    fut_n = len(events) - len(past_events)
    t0s_all = [e[0] for e in past_events]
    t0s_major = [e[0] for e in past_events if e[1] == "major_pivot"]
    t_min = min(flips) if flips else (min(t0s_all) if t0s_all else 0)
    t_max = max(flips) if flips else (max(t0s_all) if t0s_all else t_min)
    span_h = max((t_max - t_min) / 3600.0, 1.0)
    base_rate = len(flips) / span_h if span_h else 0.0

    def exposure_and_count(t0s: list[float], lo: float, hi: float) -> tuple[float, int]:
        if not t0s:
            return 0.0, 0
        spans = []
        for t0 in t0s:
            a, b = t0 + lo * 3600.0, t0 + hi * 3600.0
            if b <= t_min or a >= t_max:
                continue
            spans.append((max(a, t_min), min(b, t_max)))
        spans.sort()
        merged: list[tuple[float, float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        exp_h = sum((b - a) for a, b in merged) / 3600.0
        n = 0
        for f in flips:
            for t0 in t0s:
                dt = (f - t0) / 3600.0
                if lo <= dt < hi:
                    n += 1
                    break
        return exp_h, n

    lines = [
        "# P2.3 — narrative / pivot labels → dir_flip lead-lag",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · unique events={len(events)} (major={n_major}) · "
        f"past={len(past_events)} future_dropped={fut_n} · flips={len(flips)}",
        "",
        "T0 = `events[]` / `pivot_as_of` (LLM-typed), **not** raw calendar rows.",
        "",
        f"**Note:** `events[]` currently holds **upcoming** calendar rows (often future `as_of`). "
        f"Lead-lag needs **historical** labeled narrative/pivot events — re-run as those accumulate "
        f"or after a backfill pass.",
        "",
        "## 1. Flip intensity after labeled events (rate-normalized)",
        "",
        "| channel | window | n flips | exposure h | flips/h | lift |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for ch_name, t0s in (("all labeled", t0s_all), ("major_pivot", t0s_major)):
        for wname, lo, hi in (("0-6h", 0, 6), ("6-24h", 6, 24), ("24-72h", 24, 72), ("72-168h", 72, 168)):
            exp_h, n = exposure_and_count(t0s, lo, hi)
            rate = n / exp_h if exp_h > 0 else float("nan")
            lift = rate / base_rate if base_rate > 0 else float("nan")
            lines.append(f"| {ch_name} | {wname} | {n} | {exp_h:.0f} | {rate:.4f} | **{lift:.2f}×** |")
    lines.append("")
    lines.append(f"Baseline flip intensity: **{base_rate:.4f}/h**.")
    lines.append("")

    # median lead to nearest prior labeled event
    lines.append("## 2. Hours from last labeled T0 to flip")
    lines.append("")
    for lab_filter, name in ((None, "any"), ("major_pivot", "major_pivot"), ("incremental", "incremental")):
        leads = [lead_hours(f, lab_filter) for f in flips]
        vals = [x for x in leads if x is not None]
        if not vals:
            lines.append(f"- **{name}**: no prior T0")
            continue
        w24 = sum(1 for v in vals if v <= 24) / len(vals)
        lines.append(
            f"- **{name}**: n={len(vals)} median={statistics.median(vals):.1f}h · ≤24h {w24:.0%}"
        )
    lines.append("")
    lines.append("## 3. P2.3 read")
    lines.append("")
    lines.append("If **major_pivot** lift ≫ baseline and concentrated in 0–6h/6–24h, text is a useful **confirm/prior**.")
    lines.append("If lift ≈ 1 everywhere, narrative does **not** lead this book — keep P2.3 stamps gated.")
    lines.append("")
    lines.append("Research-only. No gate/size change.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
