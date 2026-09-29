"""
control_geometry_replay.py — per-symbol geometry search on decided light signals.

For control (and optionally all) symbols: grid TP/SL ATR, report exp%, wr, pnl.
Goal: pick a class band that is +EV for XAUUSD, XAUEUR, USDCAD, AAPL.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/control_geometry_replay.py
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260929_control_geometry_replay.md"

CONTROL = ("XAUUSD", "XAUEUR", "USDCAD", "AAPL")
# Pre-registered grid — class bands from worklog 20260929_CONTROL_GEOMETRY_AND_EXPIRY
TP_GRID = (
    (3.0, 8.0), (6.0, 12.0), (9.0, 17.0), (12.0, 24.0),
    (17.0, 30.0), (30.0, 80.0),  # live wide FX/gold bands
)
SL_GRID = (
    (0.5, 1.5), (0.5, 2.0), (1.0, 2.0), (2.0, 4.0),
    (4.0, 8.0), (8.0, 40.0),  # live wide bands
)


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


def score(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    exp = sum(1 for x in rows if x.get("outcome") == "expired") / n
    dec = [x for x in rows if x.get("outcome") in ("tp_hit", "sl_hit")]
    wr = sum(1 for x in dec if x.get("outcome") == "tp_hit") / len(dec) if dec else None
    pnl = sum(num(x.get("pnl_bricks")) or 0.0 for x in rows) / n
    return {"n": n, "exp": round(exp, 3), "wr": round(wr, 3) if wr is not None else None, "pnl": round(pnl, 3)}


def main() -> None:
    rows = fetch_all(
        "nt_signal_sim",
        {
            "select": "symbol,ts,outcome,tp_distance_atr,sl_distance_atr,pnl_bricks",
            "source": "eq.sweep-light",
            "ts": "gte.2026-09-20T00:00:00Z",
            "order": "ts.asc",
        },
    )
    # settled only
    rows = [
        x for x in rows
        if x.get("outcome") in ("tp_hit", "sl_hit", "expired")
        and str(x.get("ts") or "")[:10] <= "2026-09-27"
        and x.get("tp_distance_atr") is not None
        and x.get("sl_distance_atr") is not None
    ]

    lines = [
        "# Control geometry replay — per-symbol TP/SL ATR grid",
        "",
        "Settled light outcomes 2026-09-20→27. Grid is the class bands from",
        "`20260929_CONTROL_GEOMETRY_AND_EXPIRY.md`. Research-only.",
        "",
    ]

    for sym in CONTROL:
        srows = [x for x in rows if x.get("symbol") == sym]
        lines.append(f"## {sym} (n={len(srows)})")
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
                s = score(sel)
                if s["n"] < 5:
                    continue
                lines.append(
                    f"| TP {tlo:g}-{thi:g} | SL {slo:g}-{shi:g} | {s['n']} | "
                    f"{s.get('exp')} | {s.get('wr')} | {s.get('pnl')} |"
                )
                if s.get("pnl") is not None and s["n"] >= 15:
                    if best is None or s["pnl"] > best[0]["pnl"]:
                        best = (s, tlo, thi, slo, shi)
        lines.append("")
        if best:
            s, tlo, thi, slo, shi = best
            lines.append(
                f"**Best in grid:** TP {tlo:g}-{thi:g} / SL {slo:g}-{shi:g} → "
                f"n={s['n']} exp={s.get('exp')} wr={s.get('wr')} **pnl={s.get('pnl')}**"
            )
        else:
            lines.append("**Best in grid:** none (n too small in every cell)")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    print("\n".join(lines[:80]), flush=True)


if __name__ == "__main__":
    main()
