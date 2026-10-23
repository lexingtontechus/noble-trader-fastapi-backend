"""EOD + TimesFM knobs migrated to config/feature_flags.yaml (2026-10-20).

These nine values used to be read as env-overridable ``settings.*`` fields
(EOD_BIAS_*, EOD_CORRECTION_*, EOD_DRIFT_MONITOR_ENABLED) or ``os.getenv``
calls (TIMESFM_DISABLE_FALLBACK / _MODEL_ID / _MAX_CONTEXT / _MAX_HORIZON).
Because Studio injects Secrets as env vars, a leftover Secret with a matching
name silently overrode the code default — the same undetectable-divergence
shape as the MIN_RR incident (worklog/20260904_MIN_RR_REGRESSION.md).

The migration makes ``config/feature_flags.yaml`` the sole source. These tests
pin the three properties that make that true:

  1. Behaviour is preserved — every getter returns the value that was live
     immediately before the migration.
  2. Env no longer has ANY effect — setting the old var names must not move
     a getter. This is the property the migration exists to guarantee, and
     the one a future refactor could silently regress.
  3. The YAML block is authoritative — an edited block is picked up, and
     ``_DEFAULTS`` mirrors the shipped YAML so a missing config file cannot
     change behaviour.

``TIMESFM_READY_TIMEOUT`` is deliberately absent: it is read by start.sh,
lightningstudio/on_start.sh and the Dockerfile (shell has no YAML parser), so
it stays an env var. See config/feature_flags.yaml's own comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regime_platform.core import feature_flags
from regime_platform.core.settings import settings

# The values that were live immediately before the migration. Pinned so a
# future edit to either the YAML or _DEFAULTS that changes behaviour has to
# do so deliberately, in a diff that this test forces to be updated.
SHIPPED = {
    "eod_correction_intraday_enabled": True,
    "eod_correction_interval_hours": 4,
    "eod_bias_window_days": 7,
    "eod_bias_min_records": 20,
    "eod_drift_monitor_enabled": True,
    "timesfm_model_id": "google/timesfm-2.5-200m-pytorch",
    "timesfm_max_context": 1024,
    "timesfm_max_horizon": 256,
    "timesfm_disable_fallback": False,
}

# The pre-migration env var names, and the value each would have injected.
# Setting them all to something absurd and asserting nothing moves is the
# core regression guard for this migration.
OLD_ENV_VARS = {
    "EOD_CORRECTION_INTRADAY_ENABLED": "0",
    "EOD_CORRECTION_INTERVAL_HOURS": "99",
    "EOD_BIAS_WINDOW_DAYS": "99",
    "EOD_BIAS_MIN_RECORDS": "99",
    "EOD_DRIFT_MONITOR_ENABLED": "0",
    "TIMESFM_DISABLE_FALLBACK": "1",
    "TIMESFM_MODEL_ID": "example/should-be-ignored",
    "TIMESFM_MAX_CONTEXT": "1",
    "TIMESFM_MAX_HORIZON": "1",
}


def _getters() -> dict[str, object]:
    return {
        "eod_correction_intraday_enabled": feature_flags.get_eod_correction_intraday_enabled(),
        "eod_correction_interval_hours": feature_flags.get_eod_correction_interval_hours(),
        "eod_bias_window_days": feature_flags.get_eod_bias_window_days(),
        "eod_bias_min_records": feature_flags.get_eod_bias_min_records(),
        "eod_drift_monitor_enabled": feature_flags.get_eod_drift_monitor_enabled(),
        "timesfm_model_id": feature_flags.get_timesfm_model_id(),
        "timesfm_max_context": feature_flags.get_timesfm_max_context(),
        "timesfm_max_horizon": feature_flags.get_timesfm_max_horizon(),
        "timesfm_disable_fallback": feature_flags.get_timesfm_disable_fallback(),
    }


class TestBehaviourPreserved:
    def test_every_getter_returns_the_pre_migration_live_value(self):
        assert _getters() == SHIPPED

    def test_the_migrated_settings_fields_are_gone(self):
        """The fields were removed from settings.py. If one reappears, env
        becomes a second source of truth and the divergence returns."""
        for name in (
            "EOD_CORRECTION_INTRADAY_ENABLED",
            "EOD_CORRECTION_INTERVAL_HOURS",
            "EOD_BIAS_WINDOW_DAYS",
            "EOD_BIAS_MIN_RECORDS",
            "EOD_DRIFT_MONITOR_ENABLED",
        ):
            assert not hasattr(settings, name), f"{name} is back on settings"

    def test_the_migrated_env_names_are_not_read_anywhere_in_regime_platform(self):
        """No live module may consult the old names. Commented mentions in
        historical worklogs are fine; a live read is not."""
        import re

        root = Path(feature_flags.__file__).resolve().parents[1]
        offenders = []
        pattern = re.compile(
            r'(getenv|environ\.get|environ\[)\s*\(?\s*["\']('
            + "|".join(OLD_ENV_VARS)
            + r')["\']'
        )
        for path in root.rglob("*.py"):
            if any(part in {".venv", ".git", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                offenders.append(str(path.relative_to(root)))
        assert not offenders, f"old env names still read in: {offenders}"


class TestEnvNoLongerHasAnyEffect:
    @pytest.fixture(autouse=True)
    def _set_every_old_env_var(self, monkeypatch):
        """Set ALL the pre-migration names to values that would change every
        getter if env were still consulted."""
        for name, value in OLD_ENV_VARS.items():
            monkeypatch.setenv(name, value)

    def test_getters_are_unchanged_by_the_old_env_names(self):
        assert _getters() == SHIPPED


class TestYamlIsAuthoritative:
    """An edited YAML block is picked up on the next read (mtime hot-reload),
    and a missing/unreadable config degrades to _DEFAULTS rather than raising."""

    @pytest.fixture
    def _isolated_yaml(self, tmp_path, monkeypatch):
        path = tmp_path / "feature_flags.yaml"
        path.write_text(
            "eod_bias:\n  window_days: 14\n  min_records: 40\n"
            "timesfm_service:\n  max_context: 2048\n  max_horizon: 512\n"
            "  model_id: example/from-yaml\n  disable_fallback: true\n"
            "eod_correction:\n  intraday_enabled: false\n  interval_hours: 6\n"
            "eod_drift_monitor:\n  enabled: false\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(feature_flags, "_YAML_PATH", path)
        monkeypatch.setattr(feature_flags, "_cache", None)
        monkeypatch.setattr(feature_flags, "_cache_mtime", None)
        yield path
        monkeypatch.setattr(feature_flags, "_cache", None)
        monkeypatch.setattr(feature_flags, "_cache_mtime", None)

    def test_edited_block_is_read(self, _isolated_yaml):
        assert feature_flags.get_eod_bias_window_days() == 14
        assert feature_flags.get_eod_bias_min_records() == 40
        assert feature_flags.get_timesfm_max_context() == 2048
        assert feature_flags.get_timesfm_max_horizon() == 512
        assert feature_flags.get_timesfm_model_id() == "example/from-yaml"
        assert feature_flags.get_timesfm_disable_fallback() is True
        assert feature_flags.get_eod_correction_intraday_enabled() is False
        assert feature_flags.get_eod_correction_interval_hours() == 6
        assert feature_flags.get_eod_drift_monitor_enabled() is False

    def test_missing_yaml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """A missing config file must degrade to the shipped defaults, never
        raise — the model server (server.py) imports these getters at startup,
        so an exception here would prevent TimesFM from loading."""
        monkeypatch.setattr(feature_flags, "_YAML_PATH", tmp_path / "absent.yaml")
        monkeypatch.setattr(feature_flags, "_cache", None)
        monkeypatch.setattr(feature_flags, "_cache_mtime", None)
        assert _getters() == SHIPPED

    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        bad = tmp_path / "feature_flags.yaml"
        bad.write_text("eod_bias: [this is not a mapping\n  - broken", encoding="utf-8")
        monkeypatch.setattr(feature_flags, "_YAML_PATH", bad)
        monkeypatch.setattr(feature_flags, "_cache", None)
        monkeypatch.setattr(feature_flags, "_cache_mtime", None)
        assert _getters() == SHIPPED


class TestDefaultsMirrorShippedYaml:
    """_DEFAULTS must equal the shipped YAML for every migrated key. If they
    drift, a fresh environment with no config file runs different values than
    the deployed one — the heavy_sweep_grid drift this repo already hit once
    (docs/scopes/20261017_HEAVY_SWEEP_GRID_AND_COST_NOTE.md)."""

    def test_defaults_match_the_yaml_file(self):
        import yaml

        path = Path(feature_flags.__file__).resolve().parents[2] / "config" / "feature_flags.yaml"
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        for block, keys in (
            ("eod_correction", ("intraday_enabled", "interval_hours")),
            ("eod_bias", ("window_days", "min_records")),
            ("eod_drift_monitor", ("enabled",)),
            ("timesfm_service", ("model_id", "max_context", "max_horizon", "disable_fallback")),
        ):
            for key in keys:
                assert feature_flags._DEFAULTS[block][key] == loaded[block][key], (
                    f"{block}.{key}: _DEFAULTS and YAML disagree"
                )

    def test_the_existing_timesfm_block_is_not_shadowed(self):
        """timesfm_service is a SEPARATE block on purpose: _load() shallow-
        merges, so folding these keys into `timesfm` would replace that dict
        and drop sigmoid_k / clamp_min / clamp_max."""
        assert feature_flags.get_timesfm_sigmoid_k() == 10.0
        assert feature_flags.get_timesfm_clamp_min() == 0.05
        assert feature_flags.get_timesfm_clamp_max() == 0.95
        assert "model_id" not in feature_flags._DEFAULTS["timesfm"]


class TestReadyTimeoutStaysAnEnvVar:
    """TIMESFM_READY_TIMEOUT is the one deliberate exception: start.sh,
    lightningstudio/on_start.sh and the Dockerfile read it, and shell has no
    YAML parser. It must remain env-readable and must NOT have been given a
    feature_flags getter (which would imply it moved)."""

    def test_no_getter_exists_for_it(self):
        assert not hasattr(feature_flags, "get_timesfm_ready_timeout")

    def test_background_py_still_reads_it_from_env(self):
        source = (
            Path(feature_flags.__file__).resolve().parents[2] / "background.py"
        ).read_text(encoding="utf-8")
        assert 'os.environ.get("TIMESFM_READY_TIMEOUT"' in source
