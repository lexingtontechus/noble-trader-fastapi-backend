"""Calibration evidence pool (Kairos V3 §14.3, migration 171, 2026-09-26).

worklog/kairos/20260926_CALIBRATION_EVIDENCE_POOL.md. Covers:
  1. flag getters (mode default shadow; tiers default q+s, invalid → default);
  2. fetch_calibration_bias/_raw: evidence_tiers=None → table, no tier
     param (unchanged); evidence_tiers set → view + evidence_tier=in.(...);
     invalid tiers raise;
  3. _apply_evidence_pool: off → untouched, no pooled call; shadow → all-tier
     returned + comparison row; live → pooled used; live + thin pool →
     all-tier fallback; pooled failure → all-tier, never raises; raw mode
     calls the raw fetcher;
  4. migration 171 is view-only (no table / column / insert DDL).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator import supabase_client as sc
from regime_platform.sweep_orchestrator.eod_correction import _apply_evidence_pool

ROOT = Path(__file__).resolve().parents[1]


# ─── 1. flags ────────────────────────────────────────────────────────────


class TestFlags:
    @pytest.mark.parametrize("raw,expected", [
        (None, "shadow"), ("live", "live"), ("OFF", "off"), ("nope", "shadow"),
    ])
    def test_mode(self, raw, expected):
        from regime_platform.core import feature_flags as ff
        cfg = {} if raw is None else {"calibration_evidence_pool_mode": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_calibration_evidence_pool_mode() == expected

    @pytest.mark.parametrize("raw,expected", [
        (None, ("qualified", "suppressed")),
        (["qualified"], ("qualified",)),
        ("qualified, gate_fail", ("qualified", "gate_fail")),
        (["QUALIFIED", "qualified", "bogus"], ("qualified",)),
        (["bogus"], ("qualified", "suppressed")),
        ([], ("qualified", "suppressed")),
        (7, ("qualified", "suppressed")),
    ])
    def test_tiers(self, raw, expected):
        from regime_platform.core import feature_flags as ff
        cfg = {} if raw is None else {"calibration_evidence_tiers": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_calibration_evidence_tiers() == expected

    def test_yaml_ships_expected_mode_and_tiers(self):
        # 2026-10-12 (sanitize): this guard was STALE — it pinned "shadow"
        # while the YAML has shipped `calibration_evidence_pool_mode: live`
        # (the tier-filtered bias drives enforcement; the YAML comment
        # documents live as intended). Pins the ACTUAL shipped value so the
        # guard is meaningful again — flip the YAML and this test together.
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
        assert cfg["calibration_evidence_pool_mode"] == "live"
        assert cfg["calibration_evidence_tiers"] == ["qualified", "suppressed"]


# ─── 2. fetchers ─────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class _Client:
    def __init__(self):
        self.url = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.url, self.params = url, params
        return _Resp([])  # < min_records → None; we only inspect the request


def _call(fn, **kw):
    client = _Client()
    with (
        patch.object(sc, "_url", side_effect=lambda t: f"https://x/rest/v1/{t}"),
        patch.object(sc, "_headers", return_value={}),
        patch.object(sc.httpx, "AsyncClient", return_value=client),
        patch.object(sc.settings, "SUPABASE_URL", "https://x"),
    ):
        out = asyncio.run(fn("XAUUSD", **kw))
    return out, client


class TestFetchers:
    @pytest.mark.parametrize("fn", [sc.fetch_calibration_bias, sc.fetch_calibration_bias_raw])
    def test_default_reads_published_view(self, fn):
        """Updated 2026-10-12 (migration 209): the scorecard STANDARD is the
        published book (qualified=true) — what users experience. The default
        read is now v_calibration_records_published, not the cutover view. The
        full population stays in the base table as a diagnostic stratifier
        (see test_published_off_reads_cutover_view)."""
        _, c = _call(fn)
        assert c.url.endswith("/v_calibration_records_published")
        assert "evidence_tier" not in c.params

    @pytest.mark.parametrize("fn", [sc.fetch_calibration_bias, sc.fetch_calibration_bias_raw])
    def test_published_off_reads_cutover_view(self, fn, monkeypatch):
        """published_only=off restores the pre-209 default: the post-cutover
        view (the diagnostic stratifier path)."""
        import regime_platform.core.feature_flags as ff
        monkeypatch.setattr(ff, "get_calibration_bias_published_only", lambda: False)
        _, c = _call(fn)
        assert c.url.endswith("/v_calibration_records_current")

    @pytest.mark.parametrize("fn", [sc.fetch_calibration_bias, sc.fetch_calibration_bias_raw])
    def test_cutover_off_reads_base_table(self, fn, monkeypatch):
        """The rollback path: cutover_mode=off restores the base-table read."""
        import regime_platform.core.feature_flags as ff

        monkeypatch.setattr(ff, "get_calibration_cutover_mode", lambda: "off")
        _, c = _call(fn)
        assert c.url.endswith("/nt_calibration_records")

    @pytest.mark.parametrize("fn", [sc.fetch_calibration_bias, sc.fetch_calibration_bias_raw])
    def test_tiers_read_view_with_filter(self, fn):
        _, c = _call(fn, evidence_tiers=("qualified", "suppressed"))
        assert c.url.endswith("/v_calibration_records_tiered")
        assert c.params["evidence_tier"] == "in.(qualified,suppressed)"
        assert c.params["symbol"] == "eq.XAUUSD"

    def test_invalid_tiers_raise(self):
        with pytest.raises(ValueError):
            sc._apply_evidence_tiers({}, ("bogus",))

    def test_select_unchanged_on_view(self):
        _, c = _call(sc.fetch_calibration_bias_raw, evidence_tiers=("qualified",))
        assert c.params["select"] == "predicted_p_win,predicted_p_win_raw,outcome,entered_at,closed_at"


# ─── 3. _apply_evidence_pool ─────────────────────────────────────────────

ALL = {"bias": 0.12, "status": "OVERCONFIDENT", "n": 9}
POOL = {"bias": 0.31, "status": "OVERCONFIDENT", "n": 3}


def _pool(mode, pooled=POOL, raw=False, pooled_exc=None, cal=ALL):
    fb = AsyncMock(return_value=pooled, side_effect=pooled_exc)
    fbr = AsyncMock(return_value=pooled, side_effect=pooled_exc)
    summary: dict = {}
    with (
        patch("regime_platform.core.feature_flags.get_calibration_evidence_pool_mode", return_value=mode),
        patch("regime_platform.core.feature_flags.get_calibration_evidence_tiers",
              return_value=("qualified", "suppressed")),
    ):
        out = asyncio.run(_apply_evidence_pool("XAUUSD", "oanda", cal, raw, summary, fb, fbr))
    return out, summary, fb, fbr


class TestApplyPool:
    def test_off(self):
        out, summary, fb, fbr = _pool("off")
        assert out is ALL and summary == {}
        fb.assert_not_awaited()
        fbr.assert_not_awaited()

    def test_shadow_keeps_all_tier_and_records(self):
        out, summary, fb, _ = _pool("shadow")
        assert out is ALL
        fb.assert_awaited_once_with("XAUUSD", venue="oanda", evidence_tiers=("qualified", "suppressed"))
        row = summary["evidence_pool"][0]
        assert row["used"] == "all_tiers" and row["pool_bias"] == 0.31 and row["all_bias"] == 0.12
        assert row["status_differs"] is False

    def test_live_uses_pool(self):
        out, summary, *_ = _pool("live")
        assert out is POOL and summary["evidence_pool"][0]["used"] == "pool"

    def test_live_thin_pool_falls_back(self):
        out, summary, *_ = _pool("live", pooled=None)
        assert out is ALL
        row = summary["evidence_pool"][0]
        assert row["used"] == "all_tiers_fallback" and row["pool_status"] == "INSUFFICIENT"
        assert row["status_differs"] is True

    def test_pooled_failure_never_raises(self):
        out, summary, *_ = _pool("live", pooled_exc=RuntimeError("view missing"))
        assert out is ALL and summary == {}

    def test_raw_mode_uses_raw_fetcher(self):
        _, _, fb, fbr = _pool("shadow", raw=True)
        fbr.assert_awaited_once()
        fb.assert_not_awaited()

    def test_all_tier_none_still_compared(self):
        out, summary, *_ = _pool("shadow", cal=None)
        assert out is None
        assert summary["evidence_pool"][0]["all_status"] == "INSUFFICIENT"


# ─── 4. migration is view-only ───────────────────────────────────────────


def test_migration_171_is_view_only():
    sql = (ROOT / "supabase" / "migrations" / "171_calibration_records_tiered.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).upper()
    assert "CREATE OR REPLACE VIEW V_CALIBRATION_RECORDS_TIERED" in body
    assert "SECURITY_INVOKER" in body
    for forbidden in ("CREATE TABLE", "ALTER TABLE", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in body
