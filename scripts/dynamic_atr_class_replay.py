"""
dynamic_atr_class_replay.py — fit dynamic_atr grids **by asset class** (all symbols).

Production path is already per-class (`dynamic_atr_pick(asset_class)`). This
script fits the class TP/SL ATR grids from decided light outcomes — no
per-symbol special cases.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/dynamic_atr_class_replay.py
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys_path_root = ROOT
import sys

sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260929_dynamic_atr_class_replay.md"

TP_GRID = ((3.0, 8.0), (6.0, 12.0), (9.0, 17.0), (12.0, 24.0), (17.0, 30.0), (30.0, 80.0))
SL_GRID = ((0.5, 2.0), (0.5, 1.5), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 40.0))


def fetch_all(table: str, params: dict) -> list[dict]:
    rows, off = [], 0
    while True:
        r = httpx.get(
            f"{U}/rest/v1/{table}",
            headers=H,
            params={**params, "limit": "1000", "offset": str(off)},
            timeout=90,
        )
        r.raise_for_status()
        b = r.json()
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return rows


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def main() -> None:
    syms = fetch_all("nt_symbol", {"select": "symbol,asset_class"})
    class_of = {s["symbol"]: asset_class_key(s.get("asset_class")) for s in syms}

    rows = fetch_all(
        "nt_signal_sim",
        {
            "select": "symbol,ts,outcome,tp_distance_atr,sl_distance_atr,pnl_bricks",
            "source": "eq.sweep-light",
            "ts": "gte.2026-09-20T00:00:00Z",
            "order": "ts.asc",
        },
    )
    rows = [
        x for x in rows
        if x.get("outcome") in ("tp_hit", "sl_hit", "expired")
        and str(x.get("ts") or "")[:10] <= "2026-09-27"
        and x.get("tp_distance_atr") is not None
        and x.get("sl_distance_atr") is not None
    ]

    lines = [
        "# dynamic_atr — class-level grid fit (all symbols)",
        "",
        "Settled light 2026-09-20→27. Groups via `asset_class_key` (same as",
        "`dynamic_atr_pick`). No per-symbol overrides.",
        "",
    ]

    by_class: dict[str, list] = defaultdict(list)
    for x in rows:
        by_class[class_of.get(x.get("symbol"), "default")].append(x)

    for cls in sorted(by_class):
        srows = by_class[cls]
        members = sorted({x.get("symbol") for x in srows})
        lines.append(f"## class `{cls}` (n={len(srows)} · {', '.join(members)})")
        lines.append("")
        lines.append("| TP band | SL band | n | exp% | wr | pnl_bricks |")
        lines.append("|---|---|---:|---:|---:|---:|")
        best = None
        for tlo, thi in TP_GRID:
            for slo, shi in SL_GRID:
                sel = [
                    x for x in srows
                    if tlo <= num(x.get("tp_distance_atr")) <= thi
                    and slo <= num(x.get("sl_distance_atr")) <= shi
                ]
                n = len(sel)
                if n < 50:
                    continue
                exp = sum(1 for x in sel if x.get("outcome") == "expired") / n
                dec = [x for x in sel if x.get("outcome") in ("tp_hit", "sl_hit")]
                wr = sum(1 for x in dec if x.get("outcome") == "tp_hit") / len(dec) if dec else None
                pnl = sum(num(x.get("pnl_bricks")) or 0.0 for x in sel) / n
                lines.append(
                    f"| TP {tlo:g}-{thi:g} | SL {slo:g}-{shi:g} | {n} | {exp:.3f} | "
                    f"{wr if wr is None else round(wr, 3)} | {pnl:+.3f} |"
                )
                # Sound pick: real decided wins + positive expectancy + not all-expired junk
                if wr is None or wr < 0.20 or pnl <= 0.0 or exp > 0.50:
                    continue
                score = pnl * wr  # expectancy × hit quality
                if best is None or score > best[0]:
                    best = (score, pnl, tlo, thi, slo, shi, n, exp, wr)
        lines.append("")
        if best:
            _score, pnl, tlo, thi, slo, shi, n, exp, wr = best
            lines.append(
                f"**Class pick:** TP {tlo:g}-{thi:g} / SL {slo:g}-{shi:g} → n={n} "
                f"exp={exp:.3f} wr={round(wr, 3)} **pnl={pnl:+.3f}**"
            )
            lines.append(
                f"```yaml\n# feature_flags dynamic_atr.by_class.{cls}\n"
                f"tp_atr: [{tlo:g}, {thi:g}]\nsl_atr: [{slo:g}, {shi:g}]\n"
                f"tp_atr_min: {tlo:g}\ntp_atr_max: {thi:g}\nsl_atr_min: {slo:g}\nsl_atr_max: {shi:g}\n```"
            )
        else:
            lines.append("**Class pick:** insufficient n in every grid cell — keep global defaults.")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
