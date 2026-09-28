# Scope — nobletradingapp dashboard × Kairos data

**Date:** 2026-09-28 · **Type:** product scope · **Companion:** `20260928_RANK_BOARD_BROADCAST_DISCORD.md`  
**Source of truth:** `nt_signal_sim` / `nt_sweep_result` / `nt_macro_forecast` + Realtime `realtime:signals`

**Principle:** every widget is a **read** of existing Kairos stamps. No new LLM stores. Fail-open on nulls (show `—`, never invent).

---

## 0. Already shipped (backend slice A)

| Field | On |
|---|---|
| `rank` / `kairos_rank_score` | Realtime payload + Discord embed + `nt_signal_sim` |
| `state` / `kairos_event_window` | FRESH / WARM / STALE / DEEP / PRE |
| `transition` | `dir_flip`, `vol_expand`, … |
| `p_win`, `ev_r` | Realtime / Discord |

---

## 1. Dashboard components to enhance

### 1.1 **Signal ranking board** (primary — in scope above)

SIGNAL · P_WIN · **RANK** · EV · **STATE** · side · TP/SL. Sort **RANK desc**.

### 1.2 **Last published signal / hero card**

| Add | Why |
|---|---|
| **RANK** chip + **STATE** badge | Instant “is this still hot?” |
| `transition` if `dir_flip` | Explains high rank (fresh direction flip) |
| Footnote | “Rank = opportunity score, not win%” |

Keep `p_win` and EV visible. Hero = `gate_outcome=qualified` only (existing contract).

### 1.3 **Signal health / quality board** (L1)

| Existing | Kairos add-on |
|---|---|
| Calibration bias, Brier, gate pass rates | **Rank IC** trailing (from `kairos_rank_score` vs `y_acc` when resolved) |
| Era splits | Rank tercile hit-rate (top vs bot) — users see *why* rank matters |
| | `% of published with rank ≥ P60` |

### 1.4 **Pairs / agreement matrix**

| Add | Why |
|---|---|
| Row: rank-weighted agreement | Weight co-directional pairs by RANK not just count |
| Cell tooltip: mean RANK | Distinguishes “both long, both weak” vs “both long, both strong” |

### 1.5 **Macro / context panel**

| Source | UI |
|---|---|
| `nt_macro_forecast.current_*` | Strategic bull/bear + `thesis` |
| `structure_bias` | Short-term (TA) — **show disagreement** (bull + structure bear = tension) |
| `pivot_label` + `pivot_summary` | Event chip (major_pivot rare) |
| `events[]` | Mini timeline (last 8) |

### 1.6 **Open positions / paper book**

| Add | Why |
|---|---|
| Entry **RANK** + **STATE** | “Did we take fresh or stale?” |
| Optional: rank-at-exit vs entry | Research → product honesty |

### 1.7 **EOD / weekly digest card**

| Add | Why |
|---|---|
| Top 5 by RANK that week | Education: prioritize vs p_win |
| Hit-rate by STATE | Proves FRESH ≠ DEEP |
| Best/worst rank decile vs p_win decile | Product proof |

### 1.8 **Admin / diagnostics (optional)**

| Component | Data |
|---|---|
| Kairos event-time histogram | τ vs flips (P0) |
| Half-life by type | `dir_flip` h≈6h (research report) |
| Macro universe coverage | 33/33 `nt_macro_forecast` |

---

## 2. Priority

| P | Component | Effort | User value |
|---|---|---|---|
| **P0** | Ranking board + hero RANK/STATE | S–M | Capital prioritization |
| **P1** | Macro/structure/pivot panel | M | Context without leaving app |
| **P2** | EOD top-by-RANK digest | S | Trust + education |
| **P3** | Health board rank IC / terciles | M | Evidence |
| **P4** | Pairs rank-weight · open-entry RANK | S | Polish |

---

## 3. Explicit non-goals

- Auto-trade / auto-size from RANK  
- Replacing Kelly, EV, or gates in UI copy  
- Building a second research terminal  
- Live `kairos_rank_gate_shadow` as a user control  

---

## 4. Contracts

| Rule | Detail |
|---|---|
| Publish | Only `gate_outcome=qualified` on hero / Discord |
| Nulls | `rank` / `state` / `structure_*` omitted or `—` |
| Copy | Never call RANK “win rate” or “confidence” without “opportunity” |
| Latency | Realtime push; board can also poll `nt_signal_sim` last 24h published |

---

## 5. Suggested slice order

1. Backend slice **A** (this change): payload + Discord **done**  
2. nobletradingapp board + hero (realtime fields)  
3. Macro panel (`nt_macro_forecast`)  
4. EOD digest + health rank stats  
