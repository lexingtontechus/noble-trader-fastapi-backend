#!/usr/bin/env python3
"""Reconcile config/feature_flags.yaml with the replay evidence.

One version survives: **git's file**. But git is not *correct* — the audit found
two bands that can never produce a pick (every cell fails min_rr=1.5):

    XAGUSD   RR 0.99-1.07   (tp ~7.96 vs sl ~7.76 — a 1:1 band)
    XAUAUD   RR 1.23-1.34

and the FOREX replay shows the wide class band strictly dominates the tight
by_symbol band (e_mean +0.079 vs -0.316, better on e1 AND e2).

This applies exactly three edits to git's file, section-aware (the FIRST
`by_symbol:` in the file belongs to `macro`, so the patcher anchors on the
`dynamic_atr:` section):

  FOREX   -> the class-replay wide band   (replay-confirmed)
  XAGUSD  -> the reopt band               (git's is RR-dead)
  XAUAUD  -> the reopt band               (git's is RR-dead)

XAUEUR / US30USD keep git's bands (the Studio's reopt values scored WORSE:
e_mean -0.353 vs -0.003 and -3.356 vs -1.539). US500 is undecidable (no data)
and keeps git's.

Usage:
  python scripts/reconcile_feature_flags.py            # dry run, prints the diff
  python scripts/reconcile_feature_flags.py --apply
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "feature_flags.yaml"

# symbol -> {key: new line body (the part after "      key: ")}
EDITS: dict[str, dict[str, str]] = {
    "FOREX": {
        "tp_atr": "[4.27, 4.52, 4.77]",
        "sl_atr": "[2.63, 2.75, 2.87]",
        "tp_atr_min": "4.02",
        "tp_atr_max": "5.02",
        "sl_atr_min": "2.51",
        "sl_atr_max": "2.99",
    },
    "XAGUSD": {
        "tp_atr": "[6.14, 6.39, 6.64]",
        "sl_atr": "[2.57, 2.69, 2.81]",
        "tp_atr_min": "5.89",
        "tp_atr_max": "6.89",
        "sl_atr_min": "2.45",
        "sl_atr_max": "2.93",
    },
    "XAUAUD": {
        "tp_atr": "[6.32, 6.57, 6.82]",
        "sl_atr": "[4.19, 4.31, 4.43]",
        "tp_atr_min": "6.07",
        "tp_atr_max": "7.07",
        "sl_atr_min": "4.07",
        "sl_atr_max": "4.55",
    },
}
PROVENANCE = {
    "FOREX": "replay 2026-10-12: wide dominates tight (e_mean +0.079 vs -0.316)",
    "XAGUSD": "reopt band; previous was RR-dead (0.99-1.07, no cell passes min_rr)",
    "XAUAUD": "reopt band; previous was RR-dead (1.23-1.34, no cell passes min_rr)",
}


def patch(text: str, symbol: str, edits: dict[str, str]) -> tuple[str, int]:
    """Replace the 6 ATR keys inside `dynamic_atr.by_symbol.<symbol>`."""
    lines = text.split("\n")
    # locate the dynamic_atr: section
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == "dynamic_atr:":
            start = i
            break
    if start is None:
        raise SystemExit("dynamic_atr: section not found")
    # locate by_symbol: within that section
    bs = None
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i].startswith(" "):     # left the section
            break
        if lines[i].rstrip() == "  by_symbol:":
            bs = i
            break
    if bs is None:
        raise SystemExit("dynamic_atr.by_symbol: not found")
    # locate the symbol block
    sym_i = None
    for i in range(bs + 1, len(lines)):
        s = lines[i]
        if s and not s.startswith("    "):                # left by_symbol
            break
        if s.rstrip() == f"    {symbol}:":
            sym_i = i
            break
    if sym_i is None:
        raise SystemExit(f"{symbol} not found under dynamic_atr.by_symbol")
    # rewrite its keys, stopping at the next 4-space key or dedent
    n = 0
    i = sym_i + 1
    while i < len(lines):
        s = lines[i]
        if s and not s.startswith("      "):              # next symbol / dedent
            break
        stripped = s.strip()
        key = stripped.split(":", 1)[0] if ":" in stripped else None
        if key in edits:
            indent = s[: len(s) - len(s.lstrip(" "))]
            lines[i] = f"{indent}{key}: {edits[key]}"
            n += 1
        i += 1
    return "\n".join(lines), n


def main() -> None:
    apply = "--apply" in sys.argv
    text = CFG.read_text(encoding="utf-8")
    before = text
    total = 0
    for sym, edits in EDITS.items():
        text, n = patch(text, sym, edits)
        total += n
        print(f"  {sym:8} {n}/6 keys rewritten")
    if total != 18:
        raise SystemExit(f"expected 18 key rewrites, got {total} — aborting")
    if text == before:
        print("no change")
        return
    if apply:
        CFG.write_text(text, encoding="utf-8", newline="\n")
        print(f"\nAPPLIED to {CFG}")
    else:
        import difflib
        d = difflib.unified_diff(before.split("\n"), text.split("\n"),
                                 "before", "after", lineterm="", n=2)
        print("\n".join(d))
        print("\n(dry run — pass --apply)")


if __name__ == "__main__":
    main()
