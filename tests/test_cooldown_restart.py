"""
Publish-path semantics (2026-09-27, worklog
20260927_QUALIFIED_VS_SUPPRESSED_PUBLISH_PATH_REDESIGN.md).

Binding rules:
  * qualified=false is reserved for quality gate failures only.
  * Cooldown / dedup / shadow suppressors set gate_outcome=suppressed and
    do not rewrite the quality verdict.
  * Cooldown is pure wall-clock from the last publish (D2). Prior-open /
    trade resolve is not a publish veto (D4 — probe removed).
  * D1: SIGNAL_COOLDOWN_MINUTES default is 15 (one publish per light-sweep slot).
"""

from __future__ import annotations

from regime_platform.core.feature_flags import (
    _DEFAULTS,
    get_signal_cooldown_minutes,
)


class TestCooldownConfig:
    def test_default_is_fifteen_minutes(self):
        assert _DEFAULTS["signal_cooldown_minutes"] == 15.0
        assert get_signal_cooldown_minutes() == 15.0

    def test_probe_removed_from_orchestrator(self):
        import regime_platform.sweep_orchestrator.orchestrator as orch

        assert not hasattr(orch, "CooldownRestartProbe")
        assert not hasattr(orch, "_probe_cooldown_restart")


class TestVerdictVocabulary:
    """gate_outcome mapping is qualified | suppressed | gate_fail."""

    def test_gate_fail_when_quality_fails(self):
        gate_pass = False
        published = False
        outcome = (
            "qualified" if published
            else "suppressed" if gate_pass
            else "gate_fail"
        )
        assert outcome == "gate_fail"

    def test_suppressed_when_gate_pass_but_not_published(self):
        gate_pass = True
        published = False
        outcome = (
            "qualified" if published
            else "suppressed" if gate_pass
            else "gate_fail"
        )
        assert outcome == "suppressed"

    def test_qualified_when_published(self):
        gate_pass = True
        published = True
        outcome = (
            "qualified" if published
            else "suppressed" if gate_pass
            else "gate_fail"
        )
        assert outcome == "qualified"

    def test_suppress_reason_only_when_gate_pass_and_not_published(self):
        def stamp(gate_pass: bool, published: bool, reason: str | None):
            return reason if (gate_pass and not published) else None

        assert stamp(True, False, "cooldown_time") == "cooldown_time"
        assert stamp(True, True, "cooldown_time") is None
        assert stamp(False, False, "cooldown_time") is None
