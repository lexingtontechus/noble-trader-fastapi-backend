# feature_flags: which version is correct — and the RR-dead band defect

**Date:** 2026-10-12
**Trigger:** "which version of feature_flags is correct? one version has to survive.
why is by_class=wide and by_symbol=tight … the replay (git) either confirms or not."

## The answer: git survives as the file — but git was not *correct*

One version has to survive, and it is **git's**. The file's own header says so
("Config a person deliberately sets → HERE. Committed, reviewable in a diff"),
and the Studio's copy is a working-tree modification nobody committed. So git is
the authority and the Studio gets reconciled *to* it.

But "authority" is not "correct", and the replay shows git's file carries a real
defect. So the resolution is neither "restore git" nor "keep the Studio": it is
**git's file with three evidence-backed corrections committed into it.**

## Why `by_class` was wide and `by_symbol` was tight

They were written by **two different processes, for two different scopes, at two
different times** — and the resolver reads them as a precedence chain
(`by_symbol` → `by_class` → global).

| key | value | origin |
|---|---|---|
| `by_symbol.FOREX` | tight `1.71 / 0.35` | the original hand-set operator band; committed to git |
| `by_class.forex` | wide `4.52 / 2.75` | the output of `scripts/dynamic_atr_class_replay.py` (a class-level fit over decided light outcomes), pasted into the Studio config **only** |

Tracing it: the reopt has **no `by_class` write path at all** — `atr_reopt.py`
only ever patched `dynamic_atr.by_symbol.<SYMBOL>` (`_patch_symbol_clamps`). The
wide `by_class` block matches `dynamic_atr_class_replay.py`'s emitted
recommendation format (`# feature_flags dynamic_atr.by_class.{cls}`) almost
exactly, so it was a paste from that script, never committed.

**The logical issue:** because `by_symbol` wins whenever a symbol is passed —
and every real call passes one — the wide class band was **dead code for the
whole forex book**. It was live only on the pooled path (`symbol=None`). So the
Studio ran tight geometry for every forex symbol while carrying a wide class band
that only the pooled fallback could see. Two answers to the same question, stored
under two keys, one of them inert.

**And yes, that was a second drift** — `by_class.forex` itself differed between
git (tight) and the Studio (wide). So there were two drifts:

1. `by_class.forex` — git tight vs Studio wide. *(now removed on both sides)*
2. `by_symbol.{XAGUSD,XAUAUD,XAUEUR,US30USD,US500}` — git original vs Studio reopt.
   *(76 diff lines; resolved by this reconciliation)*

## The defect the audit found

Auditing **every** band in both versions for RR-validity (does any grid cell
clear `min_rr = 1.5`?):

| version | RR-dead bands |
|---|---|
| **git HEAD** | **XAGUSD** (RR 0.99–1.07 — tp ≈7.96 vs sl ≈7.76, a 1:1 band) and **XAUAUD** (1.23–1.34) |
| Studio (live) | none |

A band where every cell fails the RR floor can never produce a *walked*
candidate. **What actually happened next is not what this worklog first
claimed** — verified empirically after the fact:

| band | grid RR | cells clearing `min_rr` | `select_tp_sl` | `dynamic_atr_pick` |
|---|---|---|---|---|
| git XAGUSD | 0.99–1.07 | **0/9** | `None` | `None` → **light path kept BRICK** |
| reconciled XAGUSD | 2.19–2.58 | 9/9 | `6.39/2.69`, exp +1.687 | band centre (consistency reject) |

So the consequence was **"the ATR geometry never reached those two symbols — they
traded brick"**, *not* "the book shipped a bracket below its RR floor". The
band-centre fallback was never reached, because `dynamic_atr_pick` returned the
`None` straight through.

That also exposed a real gap in G2 as first implemented: the band-centre
fallback had been added at the five *early* exits but not after the solve, so the
"never returns `None`" invariant held only for bands whose grid clears the floor.
Fixed — `dynamic_atr_pick` now falls back to `_band_center_pick(cfg)` when
`select_tp_sl` finds nothing, and the same probe returns `7.96/7.76` instead of
`None`.

## `min_rr` has two uses — only one of them is inert

The 2026-09-20 determination (`worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md`
§1.2) that **`min_rr` is inert is correct, and it is about the *gate*:**

| use | site | reads | binding? |
|---|---|---|---|
| qualification **gate** | `ev_engine.py:737` `rr >= self.min_rr` | **`tp_bricks / sl_bricks`** (`ev_engine.py:726-728`) — the *brick* counts, never the ATR bracket | **inert**: brick RR sits ≈1.67–2.0, so `>= 1.5` almost never binds; rejects are kelly/regime/p_win |
| geometry **solver pre-filter** | `tiered_atr.py:510` `if tp_f / sl_f < min_rr: continue` | the **ATR** grid cell `tp_atr / sl_atr` | **not inert** — it is what makes git's XAGUSD/XAUAUD bands yield nothing |

This worklog originally blurred the two. The gate is inert (as documented); the
solver pre-filter is not, and the defect above lives entirely in the second use.
Correcting the record matters because "min_rr is inert" is true of the gate and
would be a wrong reason to ignore a grid whose every cell sits below the floor.

The reopt's values fixed exactly those two. That is why the Studio's bands could
not simply be discarded.

## The replay — per key, which version does the evidence confirm?

Both versions of each drifted key, same evidence, own grid + clamps:

| key | git band | Studio band | e_mean git | e_mean studio | verdict |
|---|---|---|---|---|---|
| XAGUSD | 7.96/7.76 | 6.39/2.69 | *RR-dead* | +1.662 | **STUDIO** (git unusable) |
| XAUAUD | 6.31/4.91 | 6.57/4.31 | *RR-dead* | +1.583 | **STUDIO** (git unusable) |
| XAUEUR | 4.20/1.82 | 5.52/3.03 | −0.003 | −0.353 | **GIT** |
| US30USD | 12.80/2.41 | 10.99/5.63 | −1.539 | −3.356 | **GIT** |
| US500 | 13.23/8.09 | 2.93/0.69 | — | — | **NO_DATA** → keep git |
| FOREX | 1.71/0.35 | 4.52/2.75 | −0.316 | +0.079 | **STUDIO** (wide) |

So the replay **neither wholesale confirms nor refutes** either version. It is
mixed, which is exactly why "one version has to survive" needed a third answer:
git's file, corrected where the evidence says so.

## What was applied

`scripts/reconcile_feature_flags.py` (section-aware — the FIRST `by_symbol:` in
the file belongs to `macro`, so it anchors on the `dynamic_atr:` section) made
exactly three edits to git's file:

| key | from (git) | to | why |
|---|---|---|---|
| `by_symbol.FOREX` | `[1.65,1.71,1.75,1.80] / [0.30,0.35,0.40]` | `[4.27,4.52,4.77] / [2.63,2.75,2.87]` | replay: wide dominates tight on e_mean **and** e1 **and** e2 |
| `by_symbol.XAGUSD` | `[7.80,7.96,8.10] / [7.60,7.76,7.90]` | `[6.14,6.39,6.64] / [2.57,2.69,2.81]` | git's was RR-dead |
| `by_symbol.XAUAUD` | `[6.20,6.31,6.45] / [4.80,4.91,5.05]` | `[6.32,6.57,6.82] / [4.19,4.31,4.43]` | git's was RR-dead |

Kept git's bands: `XAUEUR`, `US30USD` (Studio's scored worse), `US500`
(undecidable), `XAUUSD`, `AAPL`, `BTCUSD`.

After: **0 RR-dead bands** across all 9 symbol bands + the global.

## Guard

`tests/test_geometry_config_single_source.py::test_no_band_is_rr_dead` — asserts
every configured band admits at least one cell clearing `min_rr`. Verified to
**fail on git HEAD's config** (flags XAGUSD, XAUAUD) and pass on the reconciled
one, so it is a real guard rather than a tautology.

Two existing tests (`test_forex_majors_share_one_band`,
`test_pooled_pick_inherits_each_symbols_own_band`) pinned band *values*; they now
assert the *invariants* (all majors share ONE band, and it is `by_symbol.FOREX`;
one pooled solve per distinct band) and derive expected numbers from the config,
so a future band retune doesn't produce a false failure.

## Standing rule

A grid whose every cell sits below the solver's `min_rr` pre-filter yields no
walked candidate, so the ATR geometry never reaches that symbol. That is a config
**defect**, not a preference — and it is independent of the (genuinely inert)
qualification gate, which reads brick counts and would not have caught it. The
replay decides *between usable bands*; it does not excuse an unusable one.
