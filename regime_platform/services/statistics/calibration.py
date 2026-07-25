"""
Calibration — DEPRECATED IN v5
════════════════════════════════

This module previously contained the in-memory ``CalibrationTracker``
class (decile-bin Brier score tracker with reliability diagrams).
That class was never instantiated in production — the orchestrator
uses Supabase ``nt_calibration_records`` + ``fetch_calibration_bias``
(in ``regime_platform/sweep_orchestrator/supabase_client.py``) as the
single source of truth for calibration bias.

Removed in v5 (this file kept as a placeholder so existing import
paths don't break — there are none in the backend, but external
tooling may still grep for the module):

    - ``CalibrationTracker``  (in-memory decile-bin tracker)
    - ``CalibrationReport``   (dataclass returned by ``CalibrationTracker.report()``)
    - ``CalibrationBin``      (single decile bin)

If you need live in-process calibration telemetry, query the
``nt_calibration_records`` Supabase table directly via
``fetch_calibration_bias(symbol, venue)``.

See:
    - Task ID 4 in /home/z/my-project/worklog.md (audit of calibration usage)
    - Task ID 8 in /home/z/my-project/worklog.md (v5 dead-code cleanup)
"""

from __future__ import annotations

import logging

log = logging.getLogger("regime.calibration")
log.debug(
    "regime_platform.services.statistics.calibration is a v5 stub — "
    "CalibrationTracker / CalibrationReport / CalibrationBin were removed "
    "(dead code; Supabase nt_calibration_records is the source of truth)."
)
