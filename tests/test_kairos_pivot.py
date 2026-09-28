"""P2 typed pivot helpers."""

from __future__ import annotations

from regime_platform.sweep_orchestrator import kairos_pivot as P


class TestValidatePivot:
    def test_ok(self):
        v = P.validate_pivot({
            "label": "major_pivot",
            "axis": "reaction_function",
            "intensity": 0.9,
            "surprise": 0.8,
            "summary": "Fed pivots to cuts",
        })
        assert v is not None
        assert v["label"] == "major_pivot"
        assert v["product_type"] == "dir_flip_candidate"

    def test_bad_label(self):
        assert P.validate_pivot({"label": "pivot!"}) is None
        assert P.validate_pivot(None) is None

    def test_clamps(self):
        v = P.validate_pivot({"label": "no_change", "intensity": 9, "surprise": -1})
        assert v is not None
        assert v["intensity"] == 1.0
        assert v["surprise"] == 0.0
        assert v["product_type"] == "none"


class TestPrior:
    def test_data_rate(self):
        assert P.pivot_prior_ok(0, 100)
        assert P.pivot_prior_ok(3, 100)  # 3%
        assert not P.pivot_prior_ok(10, 100)

    def test_policy_sparse(self):
        assert P.pivot_prior_ok(1, 20, kind="policy")
        assert not P.pivot_prior_ok(10, 20, kind="policy")


class TestPolicyEvent:
    def test_fomc(self):
        assert P.is_policy_event("Fed Interest Rate Decision")
        assert P.is_policy_event("FOMC Statement", "US")
        assert not P.is_policy_event("Non Farm Payrolls", "US")


class TestPrompt:
    def test_brief_and_prompt(self):
        ev = {"title": "CPI YoY", "country": "US", "date": "2026-01-15T00:00:00Z", "actual": 2.4, "forecast": 2.2}
        s = P.event_brief(ev)
        assert "CPI" in s and "2.4" in s
        pr = P.build_pivot_prompt([ev], kind="data")
        assert '"events"' in pr and "0. " in pr

    def test_context_note_in_brief(self):
        ev = {"title": "FOMC", "date": "2024-09-18", "context": "first cut"}
        assert "note=first cut" in P.event_brief(ev)


class TestAttachPivot:
    def test_merges_fields(self):
        row = {"symbol": "B", "narrative": "x"}
        out = P.attach_pivot_fields(row, {
            "label": "major_pivot",
            "axis": "reaction_function",
            "intensity": 0.8,
            "surprise": 0.7,
            "as_of": "2024-09-18T18:00:00Z",
            "event_title": "FOMC",
            "summary": "first cut",
        })
        assert out["pivot_label"] == "major_pivot"
        assert out["pivot_event_title"] == "FOMC"
        assert out["symbol"] == "B"

    def test_none_pivot_identity(self):
        row = {"symbol": "B"}
        assert P.attach_pivot_fields(row, None) is row


class TestSelectCandidates:
    def test_policy_ranks_high(self):
        import time

        now = time.time()
        evs = [
            {"title": "Retail Sales", "country": "US", "importance": 1, "date": "2026-09-20T00:00:00Z"},
            {"title": "FOMC Statement", "country": "US", "importance": 1, "date": "2026-09-21T00:00:00Z"},
            {"title": "PMI", "country": "US", "importance": 0, "date": "2026-09-22T00:00:00Z"},
        ]
        out = P.select_pivot_candidates(evs, now_ts=now, limit=5)
        assert any("FOMC" in (e.get("title") or "") for e in out)
        assert all((e.get("importance") or 0) >= 1 for e in out)
