# Scope — Signal ranking board → Realtime (nobletradingapp) + Discord

**Date:** 2026-09-28 · **Type:** product scope (not built) · **Status:** ready to implement  
**Parent:** `worklog/kairos/20260927_P0_P4_PROGRAMME.md` §5 (P3 R-Layer) · `20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md`  
**Frontend:** `nobletradingapp` (portal / Talaria realtime) · **Delivery:** `discord_delivery.py` + Supabase Realtime `realtime:signals`

---

## 0. Why

Users need **capital prioritization**, not another `p_win`.  
`p_win` is a **level**; **`kairos_rank_score`** ranks *which* opportunity is best **now** (freshness, transition type, recent TP, cell hit-rate). Example:

```text
SIGNAL              P_WIN   RANK   EV      STATE
XAUUSD                0.64   0.91  +0.43R  FRESH
EURUSD CRT            0.67   0.76  +0.36R  WARM
GBPUSD LVN            0.61   0.68  +0.27R  FRESH
BTC Continuation      0.59   0.43  +0.18R  STALE
USDJPY OB             0.62   0.31  +0.21R  DEEP
```

XAUUSD wins on **RANK** without the highest `p_win`. That is the product insight.

---

## 1. User decision model (copy / UI contract)

| Step | Use | Field |
|---|---|---|
| 1. Eligibility | Is this tradeable? | Gates · `p_win` floor · EV &gt; 0 |
| 2. **Prioritize** | **Which to take first?** | **RANK** (`kairos_rank_score`) |
| 3. Size | How much? | EV + Kelly (RANK does **not** replace Kelly) |
| 4. Context | Why now? | **STATE** (`FRESH`/`WARM`/`STALE`/`DEEP`) + transition type |

**Do not** market RANK as win probability. It is a **relative opportunity score** (0–1).

---

## 2. Data contract (already mostly stored)

| Field | Source | Status |
|---|---|---|
| `kairos_rank_score` | `nt_signal_sim` (migration 172) | **Live stamp (shadow meaning)** |
| `kairos_rank_gate_shadow` | would-publish bit | Shadow only |
| `p_win`, `ev`, symbol, side, TP/SL | `nt_signal_sim` | Live |
| STATE | `event_window` / `τ` (`fresh`…`deep`) | Computed in `kairos_rank`; **not stored** — add `kairos_event_window` or derive in payload |
| Setup name (CRT / LVN / OB…) | Not modeled today | Optional later; v1 = symbol + side |

**v1 payload addition** (additive columns or enrichment at broadcast time):

```json
{
  "symbol": "XAUUSD",
  "signal": "buy",
  "p_win": 0.64,
  "ev_r": 0.43,
  "rank": 0.91,
  "state": "fresh",
  "transition": "dir_flip",
  "gate_outcome": "qualified",
  "ts": "..."
}
```

---

## 3. Surfaces

### 3.1 nobletradingapp — ranking board (realtime)

| Piece | Spec |
|---|---|
| **Board** | Table like §0: SIGNAL · P_WIN · **RANK** · EV · **STATE**; sort default **RANK desc** |
| **Realtime** | Extend `broadcast_signal` / `build_signal_payload` (already `realtime:signals`) with `rank`, `state`, `transition` |
| **Ranking rules** | One board = **open / recent published** window (e.g. last 24h published or open signals); not full research stream |
| **Visual** | RANK bar or 0–1 chip; STATE badge (FRESH green … DEEP gray); never hide `p_win` |
| **Copy** | “Rank = opportunity score (freshness + setup type). P_win = probability. EV = risk reward. Size with Kelly, prioritize by Rank.” |
| **Honesty** | If `kairos_rank_score` is null (pre-stamp / fail-open), show `—` and sort last — do not invent 0.5 |

**Backend (this repo):** ensure every **published** signal payload carries `rank` + `state`. Fail-open (missing → omit field), same as other Kairos stamps.

### 3.2 Discord — embed + optional board message

| Piece | Spec |
|---|---|
| **Signal embed** | Add **RANK** and **STATE** fields beside `p_win` / EV / TP / SL |
| **Order** | Pin RANK under the title so scouts see priority without parsing |
| **Optional digest** | Top-N by RANK (e.g. every 15m or daily): compact table of open/recent signals (same columns as board) |
| **Routing** | Unchanged: plan / venue webhooks (`plan_ids`, asset-class) |
| **Suppressed** | Do **not** Discord suppressed rows; only `gate_outcome=qualified` (existing contract) |

Example embed fields:

```text
RANK   0.91  ·  STATE  FRESH (dir_flip)
P_WIN  0.64  ·  EV  +0.43R  ·  Kelly (as today)
```

### 3.3 What stays out of Discord

- Research-only stamps (`kairos_rank_gate_shadow`)  
- Unscored / fail-closed rows  
- Any “auto-size from rank” language

---

## 4. Implementation slices

| Slice | Repo | Work | Est. |
|---|---|---|---|
| **A** | backend | Persist `kairos_event_window` (or compute in payload builder); extend `build_signal_payload` + `discord_delivery` embed fields | S |
| **B** | nobletradingapp | Ranking board UI + realtime subscribe; sort RANK; STATE badges | M |
| **C** | backend + Discord | Optional Top-N rank digest (cron or on publish batch) | S |
| **D** | both | Copy + help tooltip; EOD mention “rank vs p_win” | S |

**Order:** A → B → C. Discord embed (A) can ship before the full board.

---

## 5. Stage gates

| Gate | Pass |
|---|---|
| G1 | Published Discord embed + realtime payload include `rank` + `state` |
| G2 | Board renders live via `realtime:signals` with RANK sort |
| G3 | User test: “which of 5 signals gets capital?” matches rank order |
| G4 | (Later) Rank-conditional publish/size **only** after P3 promote bar (ΔD CI &gt; 0 post-ATR) — **not** in this scope |

---

## 6. Explicit non-goals

- Replacing Kelly / EV / 7 gates with rank  
- Auto-trading or auto-sizing from RANK  
- Live gate flip (`kairos_rank_gate_shadow` stays shadow)  
- New LLM copy generation per signal  
- Setup-name taxonomy (CRT/LVN/OB) — v2  

---

## 7. Success

Users can open **one board** (or read Discord) and answer: *“Of everything firing now, what deserves capital first?”* — and see that **highest `p_win` ≠ best opportunity**.

---

## 8. Related

| Path | What |
|---|---|
| `regime_platform/sweep_orchestrator/kairos_rank.py` | `rank_score`, event windows |
| migration 172 | `kairos_rank_score` stamp |
| `discord_delivery.py` | Direct Discord |
| `broadcast_signal` / Talaria | Realtime `realtime:signals` |
| `nobletradingapp` | Portal + dashboard |
