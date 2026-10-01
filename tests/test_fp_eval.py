"""
Tests for regime_platform/sweep_orchestrator/fp_eval.py — Phase 2 of
worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md.

Every function under test is pure (no I/O, no clock). The contracts that
matter for the pre-registered bar B1–B7 are pinned here:

  * B6 — episode dedupe is one decision per (symbol, direction, hour)
  * B7 — ΔD bootstrap resamples symbol-day CLUSTERS, not rows
  * R1 — no look-ahead: τ is None for a symbol's first observation, never 0
  * arms are comparable: the same "leaning past neutral = a call" rule
  * skill is measured against the base rate of the SAME population
  * the overlay is an increment on an arm, never a standalone score
"""

from __future__ import annotations

import numpy as np
import pytest

from regime_platform.sweep_orchestrator import fp_eval
from regime_platform.sweep_orchestrator.first_passage import FP_OVERLAY_W


def _row(symbol="XAUUSD", signal="buy", outcome="tp_hit", ts="2026-09-25T10:00:00+00:00",
         regime="low_vol_bull", tp=2.0, sl=1.0, sid=None, legacy=None):
    return {
        "signal_id": sid or f"{symbol}-{ts}-{signal}",
        "symbol": symbol, "signal": signal, "outcome": outcome, "ts": ts,
        "regime": regime, "tp_distance_atr": tp, "sl_distance_atr": sl,
        "tp_bricks": tp, "sl_bricks": sl, "legacy_markov_state": legacy,
    }


# ─── build_records ────────────────────────────────────────────────────────


def test_build_records_keeps_decided_only():
    rows = [_row(outcome="tp_hit"), _row(outcome="expired"), _row(outcome="pending")]
    recs = fp_eval.build_records(rows)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "tp_hit"


def test_build_records_computes_y_fields():
    recs = fp_eval.build_records([_row(outcome="tp_hit", tp=2.0, sl=1.0)])
    r = recs[0]
    assert r["tp_first"] is True
    assert r["y_bin"] == 1.0
    # y_acc = 1 - SL/(TP+SL) = 1 - 1/3
    assert r["y_acc"] == pytest.approx(1 - 1 / 3)


def test_build_records_y_acc_on_loss():
    recs = fp_eval.build_records([_row(outcome="sl_hit", tp=2.0, sl=1.0)])
    # y_acc = 0 - 1/3
    assert recs[0]["y_acc"] == pytest.approx(-1 / 3)


def test_build_records_falls_back_to_brick_counts():
    """Rows without ATR distances but with brick counts must not be dropped."""
    r = _row()
    r["tp_distance_atr"] = None
    r["sl_distance_atr"] = None
    recs = fp_eval.build_records([r])
    assert len(recs) == 1
    assert recs[0]["dist_tp"] == 2.0
    assert recs[0]["dist_sl"] == 1.0


def test_build_records_drops_missing_bracket():
    r = _row()
    for k in ("tp_distance_atr", "sl_distance_atr", "tp_bricks", "sl_bricks"):
        r[k] = None
    assert fp_eval.build_records([r]) == []


def test_build_records_drops_nonpositive_bracket():
    r = _row()
    r["tp_distance_atr"], r["sl_distance_atr"] = 0.0, 1.0
    r["tp_bricks"], r["sl_bricks"] = 0, 1
    assert fp_eval.build_records([r]) == []


# ─── split_cell ───────────────────────────────────────────────────────────


def test_split_cell_parses_real_regime_labels():
    """Must delegate to kairos.regime_cell — the frozen shared definition.
    A hand-rolled parser silently returned None for every live row."""
    assert fp_eval.split_cell("med_high_vol_strong_bull") == ("med_high_vol", "bull")
    assert fp_eval.split_cell("low_vol_bull") == ("low_vol", "bull")
    assert fp_eval.split_cell("high_vol_bear") == ("high_vol", "bear")
    # regime_cell only accepts the RAW label form — an already-collapsed
    # string is not a cell (asserted so a future caller does not assume it)
    assert fp_eval.split_cell("med_low_vol|flat") == (None, None)
    # labels with no vol tier are not cells
    assert fp_eval.split_cell("bull") == (None, None)
    assert fp_eval.split_cell(None) == (None, None)
    assert fp_eval.split_cell("") == (None, None)


def test_annotate_event_time_produces_real_windows_on_real_labels():
    """Regression: with a hand-rolled splitter every ewin was None, which made
    the whole D5 overlay a no-op that still reported 'measured'."""
    recs = fp_eval.build_records([
        _row(sid="a", ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        _row(sid="b", ts="2026-09-25T11:00:00+00:00", regime="high_vol_bear"),
        _row(sid="c", ts="2026-09-25T12:00:00+00:00", regime="high_vol_bear"),
    ])
    fp_eval.annotate_event_time(recs)
    by = {x["signal_id"]: x for x in recs}
    assert by["a"]["ewin"] is None        # first observation of the symbol
    assert by["b"]["ewin"] == "fresh"     # the flip
    assert by["c"]["ewin"] == "fresh"     # 1h after the flip, still < 3h
    assert by["b"]["ttype"] is not None


# ─── annotate_event_time (R1: no look-ahead) ──────────────────────────────


def test_first_observation_has_no_tau():
    """A symbol's first row has no prior cell, so freshness is UNKNOWN —
    it must be None, never 0 (0 would claim 'just flipped')."""
    recs = fp_eval.build_records([_row(ts="2026-09-25T10:00:00+00:00")])
    fp_eval.annotate_event_time(recs)
    assert recs[0]["tau_h"] is None
    assert recs[0]["ewin"] is None
    assert recs[0]["ttype"] is None


def test_transition_row_is_fresh_at_tau_zero():
    recs = fp_eval.build_records([
        _row(ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        _row(ts="2026-09-25T11:00:00+00:00", regime="high_vol_bear"),   # cell change
    ])
    fp_eval.annotate_event_time(recs)
    assert recs[1]["tau_h"] == pytest.approx(0.0)
    assert recs[1]["ewin"] == "fresh"
    assert recs[1]["ttype"] is not None


def test_tau_accumulates_in_hours_without_a_change():
    recs = fp_eval.build_records([
        _row(ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        _row(ts="2026-09-25T11:00:00+00:00", regime="high_vol_bear"),   # flip at 11:00
        _row(ts="2026-09-25T13:00:00+00:00", regime="high_vol_bear"),   # 2h later
    ])
    fp_eval.annotate_event_time(recs)
    assert recs[2]["tau_h"] == pytest.approx(2.0)
    assert recs[2]["ttype"] is None     # no new transition


def test_event_time_is_per_symbol():
    """A regime change in one symbol must not mark another symbol as flipped."""
    recs = fp_eval.build_records([
        _row(symbol="AAPL", ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        _row(symbol="US500", ts="2026-09-25T10:00:00+00:00", regime="high_vol_bear"),
    ])
    fp_eval.annotate_event_time(recs)
    for r in recs:
        assert r["tau_h"] is None   # both are first observations of their symbol


# ─── dedupe_episodes (B6) ─────────────────────────────────────────────────


def test_dedupe_one_decision_per_symbol_direction_hour():
    """15-min sweeps repeat a call 4×/hour — only the first survives (B6)."""
    rows = [
        _row(ts="2026-09-25T10:00:00+00:00"),
        _row(ts="2026-09-25T10:15:00+00:00"),
        _row(ts="2026-09-25T10:30:00+00:00"),
        _row(ts="2026-09-25T10:45:00+00:00"),
    ]
    # distinct signal_ids so dedupe is keyed on symbol/dir/hour, not id
    for i, r in enumerate(rows):
        r["signal_id"] = f"s{i}"
    assert len(fp_eval.dedupe_episodes(fp_eval.build_records(rows))) == 1


def test_dedupe_keeps_different_directions():
    rows = [
        _row(signal="buy", ts="2026-09-25T10:00:00+00:00", sid="a"),
        _row(signal="sell", ts="2026-09-25T10:00:00+00:00", sid="b"),
    ]
    assert len(fp_eval.dedupe_episodes(fp_eval.build_records(rows))) == 2


def test_dedupe_keeps_different_hours():
    rows = [
        _row(ts="2026-09-25T10:00:00+00:00", sid="a"),
        _row(ts="2026-09-25T11:00:00+00:00", sid="b"),
    ]
    assert len(fp_eval.dedupe_episodes(fp_eval.build_records(rows))) == 2


def test_dedupe_keeps_different_symbols():
    rows = [
        _row(symbol="AAPL", ts="2026-09-25T10:00:00+00:00", sid="a"),
        _row(symbol="US500", ts="2026-09-25T10:00:00+00:00", sid="b"),
    ]
    assert len(fp_eval.dedupe_episodes(fp_eval.build_records(rows))) == 2


# ─── arms ─────────────────────────────────────────────────────────────────


def _paths(tp_hits: int, sl_hits: int, tp=2.0, sl=1.0):
    """n paths: tp_hits that touch +tp first, sl_hits that touch -sl first."""
    rows = [[tp]] * tp_hits + [[-sl]] * sl_hits
    return np.asarray(rows, dtype="float64")


def test_first_passage_arm_is_walk_forward_not_lookahead():
    """R1: a signal must NEVER be scored on its own path. The first signal in
    a bucket has no prior evidence, so it gets the population value — not its
    own outcome."""
    recs = fp_eval.build_records([
        _row(sid="s1", ts="2026-09-25T10:00:00+00:00", outcome="tp_hit"),
        _row(sid="s2", ts="2026-09-25T11:00:00+00:00", outcome="sl_hit"),
    ])
    paths = {"s1": _paths(20, 0), "s2": _paths(0, 20)}
    arm = fp_eval.first_passage_arm(recs, paths)
    # s1 has no history -> shrunk population (0.5 with zero evidence), NOT 1.0
    # from its own path — that is the no-lookahead guarantee.
    assert arm["p"]["s1"] == pytest.approx(0.5)
    # s2 sees s1's label (1) in the same bucket -> shrunk toward the raw pop
    assert arm["p"]["s2"] == pytest.approx((1 + 20 * 1.0) / 21)


def test_first_passage_arm_conditions_on_geometry_bucket():
    """The arm's whole point: same symbol, different brackets, separate
    trailing evidence. A tight-TP bracket that keeps failing must not feed the
    wide-TP bracket's estimate."""
    recs = fp_eval.build_records([
        # tight bracket: two losses
        _row(sid="a", ts="2026-09-25T10:00:00+00:00", outcome="sl_hit", tp=1.5, sl=1.0),
        _row(sid="b", ts="2026-09-25T11:00:00+00:00", outcome="sl_hit", tp=1.5, sl=1.0),
        # wide bracket: two wins (different TP band -> its own bucket)
        _row(sid="c", ts="2026-09-25T12:00:00+00:00", outcome="tp_hit", tp=12.0, sl=1.0),
        _row(sid="d", ts="2026-09-25T13:00:00+00:00", outcome="tp_hit", tp=12.0, sl=1.0),
    ])
    tight = fp_eval.geometry_bucket(1.5, 1.0)
    wide = fp_eval.geometry_bucket(12.0, 1.0)
    assert tight != wide

    paths = {"a": _paths(0, 20, tp=1.5), "b": _paths(0, 20, tp=1.5),
             "c": _paths(20, 0, tp=12.0), "d": _paths(20, 0, tp=12.0)}
    arm = fp_eval.first_passage_arm(recs, paths)

    # the buckets really are separate
    assert arm["buckets"]["c"] != arm["buckets"]["a"]
    assert arm["buckets"]["c"] == arm["buckets"]["d"]

    # d's estimate uses ONLY its own bucket's prior (c's win) — if the tight
    # bucket's two losses leaked in, the numerator/denominator would differ.
    # pop_raw after a,b,c = 1/3 ; prior = [1] (c only)
    expected_d = (1 + fp_eval.BASE_RATE_K * (1 / 3)) / (1 + fp_eval.BASE_RATE_K)
    assert arm["p"]["d"] == pytest.approx(expected_d)

    # and c, first in its bucket, sees no bucket evidence at all
    assert 0.0 < arm["p"]["c"] < 0.5


def test_first_passage_arm_labels_from_the_walk_rule():
    recs = fp_eval.build_records([_row(sid="s1")])
    arm = fp_eval.first_passage_arm(recs, {"s1": _paths(20, 0)})
    assert arm["labels"]["s1"] == 1
    arm2 = fp_eval.first_passage_arm(recs, {"s1": _paths(0, 20)})
    assert arm2["labels"]["s1"] == 0


def test_first_passage_arm_records_missing_paths():
    recs = fp_eval.build_records([_row(sid="s1"), _row(sid="s2", ts="2026-09-25T11:00:00+00:00")])
    arm = fp_eval.first_passage_arm(recs, {"s1": _paths(20, 0)})
    assert arm["no_path"] == 1
    # both still get a probability (the score is from trailing evidence, not
    # the path) — the path only supplies the LABEL
    assert "s2" in arm["p"]


def test_geometry_bucket_bands():
    # boundary values land in the LOWER band (<=)
    assert fp_eval.geometry_bucket(1.0, 1.0) == ("<=2", "<=1")
    assert fp_eval.geometry_bucket(3.0, 2.0) == ("<=5", "<=2.5")
    assert fp_eval.geometry_bucket(12.0, 6.0) == (">9", ">5")
    # a wide TP and a tight SL separate on the TP axis
    assert fp_eval.geometry_bucket(1.5, 1.0) != fp_eval.geometry_bucket(12.0, 1.0)


def test_base_rate_arm_shrinks_toward_population():
    # 10 TP, 10 SL across two symbols -> pop 0.5; a symbol with 0 TP and n=2
    # must be pulled toward 0.5, not sit at 0.0.
    rows = [_row(symbol="A", outcome="tp_hit", sid=f"a{i}") for i in range(10)]
    rows += [_row(symbol="B", outcome="sl_hit", sid=f"b{i}") for i in range(2)]
    recs = fp_eval.build_records(rows)
    arm = fp_eval.base_rate_arm(recs, k=20.0)
    assert arm["population_tp"] == pytest.approx(10 / 12)
    # B: (0 + 20*pop) / (2 + 20)
    pop = 10 / 12
    assert arm["p"]["B"] == pytest.approx((0 + 20 * pop) / 22)


def test_incumbent_arm_excludes_flat_from_accuracy():
    """FLAT is 'no answer', not a wrong answer — same rule as the weekly
    diagnostic, so the two numbers agree."""
    rows = [
        _row(signal="buy", outcome="tp_hit", legacy="UP"),    # realized UP, right
        _row(signal="buy", outcome="tp_hit", legacy="FLAT"),  # no opinion
        _row(signal="buy", outcome="sl_hit", legacy="UP"),    # realized DOWN, wrong
    ]
    arm = fp_eval.incumbent_arm(fp_eval.build_records(rows))
    assert arm["n_calls"] == 2
    assert arm["n_correct"] == 1
    assert arm["accuracy"] == pytest.approx(0.5)


def test_incumbent_arm_no_calls():
    arm = fp_eval.incumbent_arm(fp_eval.build_records([_row(legacy="FLAT")]))
    assert arm["n_calls"] == 0
    assert arm["accuracy"] is None


# ─── score_arm: comparability + skill ─────────────────────────────────────


def test_score_arm_flat_is_not_a_call():
    """A probability exactly at 0.5 is silence — it must not count toward
    coverage or accuracy, so probability arms and the incumbent arm are
    measured on the same 'opinion vs silence' basis."""
    recs = fp_eval.build_records([_row(sid="s1"), _row(sid="s2")])
    res = fp_eval.score_arm(recs, "first_passage", {"s1": 0.5, "s2": 0.9})
    assert res["n_calls"] == 1


def test_score_arm_skill_is_accuracy_minus_base_rate():
    # one symbol, 3 TP / 1 SL -> base rate = max(3,1)/4 = 0.75
    rows = [_row(outcome="tp_hit", sid=f"s{i}") for i in range(3)]
    rows.append(_row(outcome="sl_hit", sid="s3"))
    recs = fp_eval.build_records(rows)
    # arm calls TP on all 4 -> accuracy 0.75, skill 0.0
    probs = {f"s{i}": 0.9 for i in range(4)}
    res = fp_eval.score_arm(recs, "first_passage", probs)
    assert res["base_rate"] == pytest.approx(0.75)
    assert res["accuracy"] == pytest.approx(0.75)
    assert res["skill"] == pytest.approx(0.0)


def test_score_arm_negative_skill_when_inverted():
    rows = [_row(outcome="tp_hit", sid=f"s{i}") for i in range(3)]
    rows.append(_row(outcome="sl_hit", sid="s3"))
    recs = fp_eval.build_records(rows)
    # arm calls SL on all 4 -> accuracy 0.25 vs base 0.75 -> skill -0.5
    probs = {f"s{i}": 0.1 for i in range(4)}
    res = fp_eval.score_arm(recs, "first_passage", probs)
    assert res["accuracy"] == pytest.approx(0.25)
    assert res["skill"] == pytest.approx(-0.5)


def test_score_arm_counts_positive_symbols():
    rows = [
        # AAPL: 3 TP / 1 SL -> base 0.75. Call every row correctly -> 1.0, skill +0.25
        *[_row(symbol="AAPL", outcome="tp_hit", sid=f"a{i}") for i in range(3)],
        _row(symbol="AAPL", outcome="sl_hit", sid="a3"),
        # US500: 1 TP / 3 SL -> base 0.75. Same -> 1.0, skill +0.25
        *[_row(symbol="US500", outcome="sl_hit", sid=f"u{i}") for i in range(3)],
        _row(symbol="US500", outcome="tp_hit", sid="u3"),
    ]
    recs = fp_eval.build_records(rows)
    probs = {
        **{f"a{i}": 0.9 for i in range(3)}, "a3": 0.1,   # AAPL called correctly
        **{f"u{i}": 0.1 for i in range(3)}, "u3": 0.9,   # US500 called correctly
    }
    res = fp_eval.score_arm(recs, "first_passage", probs)
    assert res["n_symbols"] == 2
    assert res["accuracy"] == pytest.approx(1.0)
    assert res["positive_symbols"] == 2


def test_score_arm_matches_accuracy_at_base_rate_is_zero_skill():
    """A model that reproduces the base rate exactly has skill 0 — the
    definition that makes 'skill' readable."""
    rows = [_row(outcome="tp_hit", sid=f"s{i}") for i in range(3)]
    rows.append(_row(outcome="sl_hit", sid="s3"))
    recs = fp_eval.build_records(rows)
    probs = {f"s{i}": 0.9 for i in range(4)}    # always calls TP
    res = fp_eval.score_arm(recs, "first_passage", probs)
    assert res["accuracy"] == pytest.approx(0.75)
    assert res["skill"] == pytest.approx(0.0)


# ─── overlay (D5, bar B5) ─────────────────────────────────────────────────


def test_overlay_damps_toward_half():
    recs = fp_eval.build_records([_row(sid="s1")])
    fp_eval.annotate_event_time(recs)
    out = fp_eval.apply_overlay(recs, "first_passage", {"s1": 0.9})
    assert 0.5 < out["s1"] <= 0.9


def test_overlay_never_passes_through_unchanged_above_ceiling():
    """The carry tops out at 1.0, so a fresh ∧ dir_flip row is the only case
    that keeps its level intact — and even then the value must stay in [0,1]."""
    recs = fp_eval.build_records([
        _row(sid="s0", ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        _row(sid="s1", ts="2026-09-25T11:00:00+00:00", regime="high_vol_bear"),
    ])
    fp_eval.annotate_event_time(recs)
    out = fp_eval.apply_overlay(recs, "first_passage", {"s0": 0.9, "s1": 0.9})
    for v in out.values():
        assert 0.0 <= v <= 1.0
    # s1 is fresh (just flipped); s0 has no τ so its carry is the floor
    assert out["s1"] > out["s0"]


def test_overlay_keyed_by_symbol_for_base_rate_arm():
    recs = fp_eval.build_records([_row(symbol="AAPL", sid="a"), _row(symbol="AAPL", sid="b")])
    fp_eval.annotate_event_time(recs)
    out = fp_eval.apply_overlay(recs, "base_rate", {"AAPL": 0.8})
    assert set(out) == {"AAPL"}


def test_overlay_skips_missing_probabilities():
    recs = fp_eval.build_records([_row(sid="s1"), _row(sid="s2")])
    fp_eval.annotate_event_time(recs)
    out = fp_eval.apply_overlay(recs, "first_passage", {"s1": 0.8})
    assert "s2" not in out


# ─── gate + ΔD (B7: clustered) ────────────────────────────────────────────


def test_gate_threshold_needs_enough_rows():
    assert fp_eval.gate_threshold([0.1] * 5) is None
    assert fp_eval.gate_threshold([float(i) / 100 for i in range(100)]) is not None


def test_gate_threshold_is_the_p60_quantile():
    vals = [float(i) for i in range(101)]
    thr = fp_eval.gate_threshold(vals, 0.60)
    assert thr == pytest.approx(60.0)


def test_delta_d_clustered_signs_correctly():
    gated = [_row(symbol="A", outcome="tp_hit", ts=f"2026-09-2{d}T10:00:00+00:00", sid=f"g{d}")
             for d in range(1, 8)]
    rest = [_row(symbol="B", outcome="sl_hit", ts=f"2026-09-2{d}T10:00:00+00:00", sid=f"r{d}")
            for d in range(1, 8)]
    g = fp_eval.build_records(gated)
    r = fp_eval.build_records(rest)
    res = fp_eval.delta_d_clustered(g, r, n_boot=200)
    assert res["dd"] > 0     # gated wins, rest loses
    assert res["lo"] is not None


def test_delta_d_clustered_needs_both_arms():
    g = fp_eval.build_records([_row(sid="a")])
    assert fp_eval.delta_d_clustered(g, [])["dd"] is None


def test_delta_d_clustered_uses_clusters_not_rows():
    """B7: many rows on ONE symbol-day are one bet. Duplicating a single
    cluster must not shrink the interval the way duplicating independent
    observations would — the cluster count is what the bootstrap resamples."""
    base_g = [_row(symbol="A", outcome="tp_hit", ts="2026-09-25T10:00:00+00:00", sid=f"g{i}")
              for i in range(10)]
    base_r = [_row(symbol="B", outcome="sl_hit", ts="2026-09-25T10:00:00+00:00", sid=f"r{i}")
              for i in range(10)]
    res = fp_eval.delta_d_clustered(
        fp_eval.build_records(base_g), fp_eval.build_records(base_r), n_boot=100,
    )
    # one cluster per arm -> below the 5-cluster floor, so no CI is claimed
    assert res["clusters_gated"] == 1
    assert res["lo"] is None
    assert res["dd"] is not None    # point estimate still reported


# ─── evaluate_block end-to-end ────────────────────────────────────────────


def test_evaluate_block_runs_all_arms_and_overlay():
    rows = []
    for i in range(30):
        sym = "AAPL" if i % 2 else "US500"
        rows.append(_row(symbol=sym, outcome="tp_hit" if i % 3 else "sl_hit",
                         ts=f"2026-09-{10 + (i % 18):02d}T{10 + (i % 8):02d}:00:00+00:00",
                         sid=f"s{i}", legacy="UP" if i % 2 else "DOWN"))
    recs = fp_eval.build_records(rows)
    fp_eval.annotate_event_time(recs)
    eps = fp_eval.dedupe_episodes(recs)
    paths = {x["signal_id"]: _paths(15, 5) for x in eps}
    out = fp_eval.evaluate_block(eps, paths, "test")
    assert out["label"] == "test"
    assert set(out["arms"]) == {"first_passage", "base_rate"}
    assert set(out["overlay"]) == {"first_passage", "base_rate"}
    assert "first_passage" in out["dd"]
    assert "trailing_base_rate" in out["dd"]      # the honest floor
    assert out["arms"]["first_passage"]["accuracy"] is not None


def test_trailing_base_rate_arm_is_walk_forward_and_bracket_blind():
    recs = fp_eval.build_records([
        _row(sid="s1", ts="2026-09-25T10:00:00+00:00", outcome="tp_hit"),
        _row(sid="s2", ts="2026-09-25T11:00:00+00:00", outcome="tp_hit"),
        _row(sid="s3", ts="2026-09-25T12:00:00+00:00", outcome="sl_hit"),
    ])
    tb = fp_eval.trailing_base_rate_arm(recs)
    assert tb["s1"] == pytest.approx(0.5)          # no history
    assert tb["s2"] == pytest.approx(1.0)          # one prior TP
    assert tb["s3"] == pytest.approx(1.0)          # two prior TPs
    # bracket-blind: changing the geometry must not change the score
    recs2 = fp_eval.build_records([
        _row(sid="s1", ts="2026-09-25T10:00:00+00:00", outcome="tp_hit", tp=12.0, sl=6.0),
        _row(sid="s2", ts="2026-09-25T11:00:00+00:00", outcome="tp_hit", tp=1.0, sl=1.0),
    ])
    assert fp_eval.trailing_base_rate_arm(recs2)["s2"] == pytest.approx(1.0)


def test_evaluate_block_handles_empty_population():
    out = fp_eval.evaluate_block([], {}, "empty")
    assert out["n_population"] == 0
    assert out["arms"]["first_passage"]["accuracy"] is None
    assert out["inc"]["accuracy"] is None


def test_overlay_floor_matches_first_passage_module():
    """The evaluation must use the SAME floor the primitive declares, or the
    Phase-2 numbers describe a different overlay than the one that shipped.

    The floor is reached at the weakest supported cell — deep freshness ×
    vol_contract type. An *unknown* window/type is NOT the floor: the Kairos
    primitives deliberately default to 0.35 (a neutral prior), which sits
    above it.
    """
    recs = fp_eval.build_records([
        _row(sid="s0", ts="2026-09-25T10:00:00+00:00", regime="low_vol_bull"),
        # a vol-contract flip, then a long quiet stretch -> deep × vol_contract
        _row(sid="s1", ts="2026-09-25T11:00:00+00:00", regime="high_vol_bull"),
        _row(sid="s2", ts="2026-09-27T11:00:00+00:00", regime="high_vol_bull"),
    ])
    fp_eval.annotate_event_time(recs)
    by_id = {x["signal_id"]: x for x in recs}
    assert by_id["s2"]["ewin"] == "deep"
    assert by_id["s2"]["ttype"] is None      # no new transition
    out = fp_eval.apply_overlay(recs, "first_passage", {"s2": 1.0})
    # deep freshness (0.00) -> carry collapses to the floor regardless of type
    assert out["s2"] == pytest.approx(0.5 + FP_OVERLAY_W["floor"] * 0.5)


def test_overlay_unknown_transition_is_not_the_floor():
    """A symbol with no observed regime change has UNKNOWN freshness, which
    must not be treated as 'deep' — the primitive's neutral prior applies."""
    recs = fp_eval.build_records([_row(sid="s1")])
    fp_eval.annotate_event_time(recs)
    out = fp_eval.apply_overlay(recs, "first_passage", {"s1": 1.0})
    floor_val = 0.5 + FP_OVERLAY_W["floor"] * 0.5
    assert out["s1"] > floor_val


# ─── verdict (the bar, computed) ──────────────────────────────────────────


def _block(label, skill, coverage, pos, tot, fp_dd, tb_dd, overlay_dd_delta=0.01):
    """A synthetic evaluate_block result. ``overlay_dd_delta`` is the overlay's
    own ΔD increment over the bare arm — B5 is measured on THAT, not on
    sign-accuracy (shrinking toward 0.5 cannot move a sign)."""
    return {
        "label": label,
        "arms": {
            "first_passage": {"skill": skill, "coverage": coverage,
                              "positive_symbols": pos, "n_symbols": tot,
                              "base_rate": 0.70},
            "base_rate": {"skill": 0.0, "coverage": 1.0,
                          "positive_symbols": 0, "n_symbols": tot},
        },
        "overlay": {"first_passage": {"skill": skill}, "base_rate": {"skill": 0.0}},
        "dd": {
            "first_passage": {"dd": fp_dd},
            "first_passage_overlay": {"dd": fp_dd + overlay_dd_delta},
            "trailing_base_rate": {"dd": tb_dd},
        },
    }


def test_verdict_all_pass():
    b = _block("1. All eras", skill=0.05, coverage=0.9, pos=12, tot=14,
               fp_dd=0.2, tb_dd=0.1)
    blocks = [b, _block("4. First half", 0.05, 0.9, 12, 14, 0.2, 0.1),
              _block("5. Second half", 0.05, 0.9, 12, 14, 0.2, 0.1)]
    v = fp_eval.verdict(blocks)
    assert v["_summary"]["passed"] == 5
    assert v["B1_beat_base_rate"]["pass"]


def test_verdict_fails_on_negative_skill():
    b = _block("1. All eras", skill=-0.07, coverage=0.99, pos=3, tot=14,
               fp_dd=0.05, tb_dd=0.06, overlay_dd_delta=0.0)
    blocks = [b, _block("4. First half", -0.1, 0.99, 3, 14, -0.05, 0.08, 0.0),
              _block("5. Second half", -0.02, 0.99, 6, 14, 0.11, 0.07, 0.0)]
    v = fp_eval.verdict(blocks)
    assert not v["B1_beat_base_rate"]["pass"]      # skill < 0
    assert not v["B3_consistency"]["pass"]          # 3/14 < 2/3
    assert not v["B4_era_halves"]["pass"]           # first half below the floor
    assert not v["B5_overlay_increment"]["pass"]    # overlay changed nothing
    assert v["_summary"]["passed"] < 5


def test_verdict_b4_requires_both_halves():
    ok = _block("4. First half", 0.05, 0.9, 12, 14, 0.20, 0.10)
    bad = _block("5. Second half", 0.05, 0.9, 12, 14, 0.05, 0.10)
    v = fp_eval.verdict([_block("1. All eras", 0.05, 0.9, 12, 14, 0.2, 0.1), ok, bad])
    assert not v["B4_era_halves"]["pass"]
    assert "1/2" in v["B4_era_halves"]["detail"]


def test_verdict_b2_coverage_threshold():
    b = _block("1. All eras", 0.05, 0.29, 12, 14, 0.2, 0.1)
    v = fp_eval.verdict([b, _block("4. First half", 0.05, 0.9, 12, 14, 0.2, 0.1),
                         _block("5. Second half", 0.05, 0.9, 12, 14, 0.2, 0.1)])
    assert not v["B2_coverage"]["pass"]


def test_verdict_handles_empty_blocks():
    v = fp_eval.verdict([])
    assert v["_summary"]["passed"] == 0
    assert not v["B1_beat_base_rate"]["pass"]
