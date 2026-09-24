# qualified_geometry_shadow stamp (B2 complete) — Build Log

**Date:** 2026-09-23  
**Scope:** Persist light_publish_geometry_gate shadow veto on sim rows  
**Status:** Built. Migration **164** must be applied in Supabase.

## Gap

B2 (worklog 20260920) required `qualified_geometry_shadow=true` on sim when
shadow would veto. Code only logged `[light:geometry] SHADOW would reject` —
B5 reject-rate could only be grepped from logs.

## Shipped

| File | Change |
|---|---|
| `164_qualified_geometry_shadow.sql` | `nt_signal_sim.qualified_geometry_shadow` + partial index |
| `supabase_client.NT_SIGNAL_SIM_COLUMNS` | whitelist key |
| `_build_signal_sim_payload(..., qualified_geometry_shadow=)` | stamp true/false/omit |
| Light B gate | `True` on shadow veto, `False` on would-qualify pass, omit otherwise |

Fail-open unchanged: unscored geometry/bracket → no stamp, no reject.

## B5 review SQL (after n is useful)

```sql
-- reject rate among would-qualify (flag not null ⇒ gate evaluated after qualified)
SELECT
  count(*) FILTER (WHERE qualified_geometry_shadow) AS would_reject,
  count(*)                                          AS would_qualify,
  round(
    count(*) FILTER (WHERE qualified_geometry_shadow)::numeric
    / nullif(count(*), 0), 3
  ) AS reject_rate
FROM nt_signal_sim
WHERE source = 'sweep-light'
  AND qualified_geometry_shadow IS NOT NULL
  AND ts >= now() - interval '14 days';
```

If `reject_rate > ~0.70` → **tune `dynamic_atr_risk_cap`**, do not flip live.
D1/D2 remain blocked on this review.

> **Superseded 2026-09-24** (`20260924_B5_VETO_SPLIT_SCOPE.md`): the B veto is
> now geometry-risk only (bracket clause moved to R2 alone). The total
> `reject_rate` above is retired. Use `scripts/pulse_check_r2.py`
> `b5_risk_share` / `b5_veto_outcomes` / `b5_cap_replay`.

## Tests

`tests/test_qualified_geometry_shadow.py` + light-gate / sim-capture / bracket suites: **37 passed**.
