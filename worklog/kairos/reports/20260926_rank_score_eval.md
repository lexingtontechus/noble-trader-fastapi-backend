# Walk-forward rank score + shadow ΔD gate

Generated 2026-09-26 21:53 UTC · weights {'freshness': 0.35, 'type': 0.25, 'recent_tp': 0.25, 'cell_p': 0.15} · gate quantile ≥ 0.6
Decided rows scored walk-forward: **6300**

## 1. All eras (walk-forward)

n=6300 · mean y_acc=-0.036 · TP rate=0.291
- **rank_score**: Spearman(y_acc)=0.126 · AUC(y_bin)=0.67
- **p_win**: Spearman(y_acc)=-0.034 · AUC(y_bin)=0.491
- **cell_p**: Spearman(y_acc)=0.145 · AUC(y_bin)=0.657
- TP-rate top vs bot tercile of **rank_score**: top 0.420 vs bot 0.131 (Δ +0.288)
- TP-rate top vs bot tercile of **p_win**: top 0.263 vs bot 0.283 (Δ -0.020)
- TP-rate top vs bot tercile of **cell_p**: top 0.421 vs bot 0.170 (Δ +0.251)

### Shadow gate (score ≥ 0.586 ≈ P60)
- gated n=2521 · rest n=3779
- mean y_acc gated=0.104 · rest=-0.129
- **ΔD = +0.233 [+0.212, +0.256]**
- **R3 signal:** ΔD > 0 with CI — candidate for a real gate change (still require post-ATR confirm).

## 2. Post-debounce (debounce_to_atr + ATR eras)

n=518 · mean y_acc=-0.139 · TP rate=0.195
- **rank_score**: Spearman(y_acc)=0.199 · AUC(y_bin)=0.785
- **p_win**: Spearman(y_acc)=-0.009 · AUC(y_bin)=0.441
- **cell_p**: Spearman(y_acc)=0.272 · AUC(y_bin)=0.741
- TP-rate top vs bot tercile of **rank_score**: top 0.413 vs bot 0.029 (Δ +0.384)
- TP-rate top vs bot tercile of **p_win**: top 0.093 vs bot 0.169 (Δ -0.076)
- TP-rate top vs bot tercile of **cell_p**: top 0.372 vs bot 0.076 (Δ +0.297)

### Shadow gate (score ≥ 0.535 ≈ P60)
- gated n=208 · rest n=310
- mean y_acc gated=0.037 · rest=-0.257
- **ΔD = +0.295 [+0.234, +0.359]**
- **R3 signal:** ΔD > 0 with CI — candidate for a real gate change (still require post-ATR confirm).

## 3. Post-ATR only

n=43 · mean y_acc=-0.139 · TP rate=0.163
- **rank_score**: Spearman(y_acc)=0.183 · AUC(y_bin)=0.671
- **p_win**: Spearman(y_acc)=-0.225 · AUC(y_bin)=0.266
- **cell_p**: Spearman(y_acc)=-0.28 · AUC(y_bin)=0.242
- TP-rate top vs bot tercile of **rank_score**: —
- TP-rate top vs bot tercile of **p_win**: —
- TP-rate top vs bot tercile of **cell_p**: —

### Shadow gate (score ≥ 0.542 ≈ P60)
- gated n=18 · rest n=25
- mean y_acc gated=-0.073 · rest=-0.186
- **ΔD = +0.113 [-0.096, +0.356]**
- ΔD CI spans 0 — shadow only.

## 4. Qualified only (all eras)

n=89 · mean y_acc=-0.152 · TP rate=0.169
- **rank_score**: Spearman(y_acc)=-0.049 · AUC(y_bin)=0.557
- **p_win**: Spearman(y_acc)=-0.105 · AUC(y_bin)=0.409
- **cell_p**: Spearman(y_acc)=-0.033 · AUC(y_bin)=0.615
- TP-rate top vs bot tercile of **rank_score**: top 0.172 vs bot 0.138 (Δ +0.034)
- TP-rate top vs bot tercile of **p_win**: top 0.172 vs bot 0.207 (Δ -0.034)
- TP-rate top vs bot tercile of **cell_p**: top 0.241 vs bot 0.103 (Δ +0.138)

### Shadow gate (score ≥ 0.567 ≈ P60)
- gated n=37 · rest n=52
- mean y_acc gated=-0.135 · rest=-0.164
- **ΔD = +0.028 [-0.102, +0.190]**
- ΔD CI spans 0 — shadow only.

## Notes

- Scores use only trailing per-symbol decided outcomes and cell means (no lookahead).
- `cell_p` is shrunk empirical P(TP|decided); `rank_score` mixes freshness, type, recent_tp, cell_p.
- Selection metric is **ΔD = mean(y_acc|gated) − mean(y_acc|rest)** (worklog §4).
- Mixed-era blocks are discovery; promote only on post-ATR confirmation.

