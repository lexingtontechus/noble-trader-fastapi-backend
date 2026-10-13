#!/usr/bin/env python3
"""Validate the G1/G2/G3 geometry flow against real sweeps.

Reads the orchestrator log and checks, per sweep, that the coherent flow is
actually taking hold — not just unit-tested.

  G1 (one config authority) — the band the solve LOGS must equal the band the
      config resolves for that symbol (by_symbol → global). Any mismatch means
      something other than `get_dynamic_atr_config` chose the grid.
  G2 (the solve always returns geometry) — every symbol in the active sweep must
      produce a geometry. A symbol with no `[dynamic:atr:light] ... APPLIED`
      (or would-apply) line kept the BRICK bracket.
  G3 (null → recompute) — the light path resolves through `dynamic_atr_pick`, so
      a cold store still yields geometry. Observable as: symbols whose store has
      no fresh pick still show a resolve (band-centre or walked) rather than
      nothing.

Usage (on the Studio, where the log lives):
  PYTHONPATH="$PWD" python scripts/validate_geometry_flow.py --since "00:09:1"
  PYTHONPATH="$PWD" python scripts/validate_geometry_flow.py --last-sweeps 3

Exit code 1 when any active symbol kept brick, or any band mismatched.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GRID_RE = re.compile(
    r"\[dynamic:atr\] scope=(\S+) class=(\S+) grid tp=\[([^\]]*)\] sl=\[([^\]]*)\] "
    r"clamps tp=\[([^,]*),([^\]]*)\] sl=\[([^,]*),([^\]]*)\]"
)
APPLY_RE = re.compile(r"\[dynamic:atr:light\] (\S+) (APPLIED|would apply)")
FALLBACK_RE = re.compile(r"\[dynamic:atr\] band-centre fallback")
# a symbol can be SKIPPED — not a geometry miss: locked by a concurrent sweep,
# or dropped by the data-freshness guard ("price stale > 1.0h", which trips at
# the top of the hour for feeds whose latest CLOSED bar is exactly 1h old).
LOCKED_RE = re.compile(r"(\S+): locked — skipping \(concurrent sweep\)")
STALE_RE = re.compile(r"Skipping \d+ symbols \(price stale [^)]*\): \[(.*?)\]\s*$")
SWEEP_RE = re.compile(r"Running sweep group \[\*/15 \* \* \* \*\]")
ACTIVE_RE = re.compile(r"(\d+) symbols active, \d+ filtered \(market closed\): \[(.*?)\]\s*$")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
# the light cron's symbol list — the authoritative registry (NOT class scopes)
LIGHT_LIST_RE = re.compile(r"Light \[\*/15 \* \* \* \*\]\s*:\s*(\[.*\])")


def _floats(s: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in s.split(",") if x.strip())


def parse(lines: list[str]):
    """Return (sweeps, bands); bands maps the SCOPE logged to its grid+clamps."""
    sweeps: list[dict] = []
    bands: dict[str, tuple] = {}
    cur: dict | None = None
    for ln in lines:
        if SWEEP_RE.search(ln):
            m = TS_RE.match(ln)
            cur = {"ts": f"{m.group(1)} {m.group(2)}" if m else "?",
                   "filtered": set(), "applied": {}, "fallback": 0, "skipped": set(),
                   "active_n": None}
            # the marker line ALSO carries the active/filtered counts:
            # "Running sweep group [...]: 11 symbols active, 3 filtered (market
            # closed): [('AAPL', 'stocks'), ...]"
            a = ACTIVE_RE.search(ln)
            if a:
                cur["active_n"] = int(a.group(1))
                cur["filtered"] = set(re.findall(r"\('([A-Z0-9]+)'", a.group(2)))
            sweeps.append(cur)
            continue
        if cur is None:
            continue
        m = GRID_RE.search(ln)
        if m:
            sym = m.group(1)
            bands[sym] = (_floats(m.group(3)), _floats(m.group(4)),
                          float(m.group(5)), float(m.group(6)),
                          float(m.group(7)), float(m.group(8)))
        if FALLBACK_RE.search(ln):
            cur["fallback"] += 1
        m = LOCKED_RE.search(ln)
        if m:
            cur["skipped"].add(m.group(1))
        m = STALE_RE.search(ln)
        if m:
            cur["skipped"] |= set(re.findall(r"\('([A-Z0-9]+)'", m.group(1)))
        m = APPLY_RE.search(ln)
        if m:
            cur["applied"][m.group(1)] = m.group(2)
    return sweeps, bands


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/orchestrator.log")
    ap.add_argument("--since", default=None,
                    help="'YYYY-MM-DD HH:MM' (cross-midnight) or bare 'HH:MM' (last date)")
    ap.add_argument("--last-sweeps", type=int, default=None)
    args = ap.parse_args()

    log = Path(args.log)
    if not log.exists():
        print(f"log not found: {log}")
        return 2
    all_lines = log.read_text(encoding="utf-8", errors="replace").splitlines()

    # registry: the light cron's symbol list (last occurrence in the log)
    registry: set[str] = set()
    for ln in all_lines:
        m = LIGHT_LIST_RE.search(ln)
        if m:
            registry = set(re.findall(r"'([A-Z0-9]+)'", m.group(1)))
    if not registry:
        print("could not find the light-cron symbol list in the log")
        return 2

    lines = all_lines
    if args.since:
        # full "YYYY-MM-DD HH:MM[:SS]" for a cross-midnight window; a bare
        # "HH:MM[:SS]" anchors to the log's last date.
        last_date = next((TS_RE.match(ln).group(1) for ln in reversed(all_lines)
                          if TS_RE.match(ln)), "")
        cutoff = (args.since if " " in args.since.strip() else f"{last_date} {args.since}")[:19]
        lines = [ln for ln in all_lines
                 if (m := TS_RE.match(ln)) and f"{m.group(1)} {m.group(2)}" >= cutoff]

    sweeps, bands = parse(lines)
    if args.last_sweeps:
        sweeps = sweeps[-args.last_sweeps:]
    if not sweeps:
        print("no sweeps matched")
        return 2

    from regime_platform.core.feature_flags import get_dynamic_atr_config

    print(f"sweeps analysed: {len(sweeps)}   registry: {len(registry)} symbols\n")
    total_brick: list[str] = []
    g1_bad: list[str] = []
    print(f"{'sweep (UTC)':21} {'active':>6} {'ATR geom':>9} {'brick':>6} {'skip':>5} {'band-ctr':>9}")
    for s in sweeps:
        active = registry - s.get("filtered", set())
        got = {k for k in s["applied"] if k in active}
        skipped = {k for k in s["skipped"] if k in active}
        # A sweep declares how many symbols it actually RAN ("N symbols active").
        # A startup catch-up runs only the missed subset, so the registry minus
        # market-closed over-counts it. Compare COUNTS against the declaration.
        declared = s.get("active_n")
        brick_n = max(0, (declared if declared is not None else len(active))
                      - len(got) - len(skipped))
        brick = (active - got - skipped) if brick_n else set()
        total_brick += sorted(brick)
        print(f"{s['ts']:21} {(declared if declared is not None else len(active)):>6} "
              f"{len(got):>9} {brick_n:>6} {len(skipped):>5} {s['fallback']:>9}"
              + (f"   brick kept: {sorted(brick)}" if brick else ""))

    # G1 — logged band vs resolved config band
    print("\nG1 — band the solve used vs the config's resolved band:")
    checked = sorted(set(bands) & registry)
    for sym in checked:
        b = bands[sym]
        c = get_dynamic_atr_config(None, sym)
        want = (tuple(float(x) for x in c["tp_atr"]), tuple(float(x) for x in c["sl_atr"]),
                float(c["tp_atr_min"]), float(c["tp_atr_max"]),
                float(c["sl_atr_min"]), float(c["sl_atr_max"]))
        ok = (len(b[0]) == len(want[0]) and len(b[1]) == len(want[1])
              and all(abs(x - y) < 1e-9 for x, y in zip(b[0], want[0]))
              and all(abs(x - y) < 1e-9 for x, y in zip(b[1], want[1]))
              and all(abs(x - y) < 1e-9 for x, y in zip(b[2:], want[2:])))
        print(f"  {sym:9} {'ok' if ok else 'MISMATCH':9} logged tp={list(b[0])} sl={list(b[1])}")
        if not ok:
            g1_bad.append(sym)
    if not checked:
        print("  (no per-symbol band lines in this window)")

    print("\n" + "=" * 62)
    if total_brick:
        print(f"G2/G3 FAIL — kept BRICK across sweeps: {sorted(set(total_brick))}")
    else:
        print("G2/G3 ok — every active symbol resolved ATR geometry (no brick kept)")
    if g1_bad:
        print(f"G1 FAIL — band mismatch vs config: {sorted(set(g1_bad))}")
    else:
        print("G1 ok — every logged band equals the config's resolved band")
    return 1 if (total_brick or g1_bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
