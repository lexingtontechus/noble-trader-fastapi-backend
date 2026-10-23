"""
kairos_p22_leadlag.py — P2.2: do calendar/policy events lead HMM dir_flip?

Scope: worklog/kairos/20260927_P0_P4_PROGRAMME.md §4 (P2.2).
Read-only event study. No stamps, no gates, no LLM required.

T0 candidates:
  - policy events (FOMC/ECB/BoJ/…)
  - high-|surprise| importance-1 data prints (actual vs forecast)

Outcome: HMM regime-cell `dir_flip` times from light-sweep labels.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_p22_leadlag.py
"""
from __future__ import annotations

import json
import math
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

from regime_platform.sweep_orchestrator.kairos import DEFAULT_CALENDAR_PATH  # noqa: E402
from regime_platform.sweep_orchestrator.kairos import regime_cell  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_pivot import is_policy_event  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import transition_type  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260927_p22_leadlag.md"
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

    # dir_flip times per symbol (and pooled)
    flips: list[float] = []  # epoch seconds
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in raw:
        ts = parse_ts(r.get("ts"))
        if ts is None:
            continue
        vol, d = split_cell(r.get("regime"))
        by_sym[r.get("symbol")].append({"ts": ts, "vol": vol, "dir": d})

    for sym, rows in by_sym.items():
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
    print(f"  dir_flip/both events: {len(flips)}", flush=True)

    # calendar T0 candidates
    events: list[tuple[float, str, str]] = []  # (epoch, kind, title)
    if DEFAULT_CALENDAR_PATH.exists():
        cal = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))
        for ev in cal:
            if (ev.get("importance") or 0) < 1:
                continue
            ts = parse_ts(ev.get("date"))
            if ts is None:
                continue
            title = ev.get("title") or ""
            if is_policy_event(title, ev.get("country")):
                events.append((ts.timestamp(), "policy", title))
                continue
            act, fc = ev.get("actual"), ev.get("forecast")
            if act is not None and fc is not None:
                try:
                    surprise = abs(float(act) - float(fc))
                    scale = abs(float(fc)) if float(fc) != 0 else 1.0
                    if surprise / scale >= 0.05:  # ≥5% miss
                        events.append((ts.timestamp(), "surprise", title))
                except (TypeError, ValueError):
                    pass
    events.sort(key=lambda t: t[0])
    n_pol = sum(1 for e in events if e[1] == "policy")
    n_sur = sum(1 for e in events if e[1] == "surprise")
    print(f"  T0 candidates: {len(events)} (policy={n_pol} surprise={n_sur})", flush=True)

    # For each flip: hours since most recent prior event of each kind
    def lead_hours(flip_t: float, kind: str | None = None) -> float | None:
        best = None
        for et, k, _title in events:
            if kind and k != kind:
                continue
            if et <= flip_t:
                dt = (flip_t - et) / 3600.0
                if best is None or dt < best:
                    best = dt
        return best

    leads_all = [lead_hours(f) for f in flips]
    leads_pol = [lead_hours(f, "policy") for f in flips]
    leads_sur = [lead_hours(f, "surprise") for f in flips]

    def summarize(xs: list[float | None], label: str, lines: list[str]) -> None:
        vals = [x for x in xs if x is not None]
        if not vals:
            lines.append(f"| {label} | 0 | — | — | — | — |")
            return
        q = lambda p: statistics.quantiles(vals, n=100)[int(p * 100) - 1] if len(vals) >= 10 else statistics.median(vals)
        w24 = sum(1 for v in vals if v <= 24) / len(vals)
        w72 = sum(1 for v in vals if v <= 72) / len(vals)
        w168 = sum(1 for v in vals if v <= 168) / len(vals)
        lines.append(
            f"| {label} | {len(vals)} | {statistics.median(vals):.1f} | {q(0.25):.1f} | "
            f"{w24:.0%} / {w72:.0%} / {w168:.0%} | {min(vals):.1f}–{max(vals):.0f} |"
        )

    lines = [
        "# P2.2 — calendar / policy events → HMM dir_flip lead-lag",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · read-only · flips n={len(flips)} · T0 n={len(events)}",
        "",
        "Question: do typed macro events **lead** regime direction flips (T-Layer), or only coincide?",
        "",
        "## 1. Time from T0 (most recent prior event) to dir_flip",
        "",
        "| channel | n flips with prior T0 | median h | p25 | share ≤24/72/168h | range |",
        "|---|---:|---:|---:|---|---|",
    ]
    summarize(leads_all, "any T0", lines)
    summarize(leads_pol, "policy", lines)
    summarize(leads_sur, "surprise data", lines)
    lines.append("")

    # Event study: flip intensity (flips / hour-in-window) vs overall baseline
    lines.append("## 2. Event study — flip intensity after T0 (rate-normalized)")
    lines.append("")
    if not events:
        lines.append("No calendar events loaded.")
    else:
        t0s = [e[0] for e in events]
        t0s.sort()
        t_min = min(t0s)
        t_max = max(flips) if flips else max(t0s)
        span_h = max((t_max - t_min) / 3600.0, 1.0)
        base_rate = len(flips) / span_h  # flips per hour over full span

        # union of post-event windows (avoid double-counting overlapping events)
        buckets = [("0-6h", 0, 6), ("6-24h", 6, 24), ("24-72h", 24, 72), ("72-168h", 72, 168)]
        lines.append("| window after T0 | n flips | exposure h (union) | flips/h | lift vs baseline |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, lo, hi in buckets:
            # minutes of exposure: for each event, [t+lo, t+hi) clipped to span, then union via sort+merge
            spans = []
            for t0 in t0s:
                a = t0 + lo * 3600.0
                b = t0 + hi * 3600.0
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
            n_flips = 0
            for f in flips:
                # flip falls in window after some T0?
                hit = False
                for t0 in t0s:
                    dt = (f - t0) / 3600.0
                    if lo <= dt < hi:
                        hit = True
                        break
                if hit:
                    n_flips += 1
            rate = n_flips / exp_h if exp_h > 0 else float("nan")
            lift = rate / base_rate if base_rate > 0 else float("nan")
            lines.append(
                f"| {name} | {n_flips} | {exp_h:.0f} | {rate:.4f} | **{lift:.2f}×** |"
            )
        lines.append("")
        lines.append(f"Baseline flip intensity: **{base_rate:.4f} /h** over {span_h:.0f}h ({len(flips)} flips).")
        lines.append("Lift > 1 means flips cluster after events more than calendar time alone would imply.")
    lines.append("")

    lines.append("## 3. P2.2 notes")
    lines.append("")
    lines.append("- This is **association / lag**, not causal. P2.3 stamps remain gated.")
    lines.append("- `dir_flip` includes `both` (direction change with vol).")
    lines.append("- Surprise channel uses |actual−forecast|/|forecast| ≥ 5% on importance-1 prints.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
