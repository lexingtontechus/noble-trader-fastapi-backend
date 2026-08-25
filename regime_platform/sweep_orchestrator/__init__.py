"""
Sweep Orchestrator
══════════════════

Replaces Supabase Edge Functions + pg_cron as the scheduling and orchestration
layer for light and heavy sweeps. Runs as a standalone process alongside the
main FastAPI server.

Key differences from the old Deno edge functions:
  • Scheduling via APScheduler instead of pg_cron — dynamic, data-driven
  • Regime detection, Kelly sizing, and EV Engine called as direct Python
    imports — no HTTP round-trips between orchestrator and compute layer
  • Symbol registry read from nt_symbol (Supabase) at startup + periodic
    refresh — no hardcoded lists, no code changes to add/remove symbols
  • Stale-symbol tracking built into the orchestrator loop — no separate
    retry-stale-symbols cron job needed

Lazy import: ``SweepOrchestrator`` is only imported on first access, so
standalone tools in this package (e.g. ``paper_retroclose``) don't need
apscheduler installed to run.
"""

__all__ = ["SweepOrchestrator"]


def __getattr__(name: str):
    """Lazy-import ``SweepOrchestrator`` so importing submodules (e.g.
    ``paper_retroclose``) doesn't pull in apscheduler unless actually needed."""
    if name == "SweepOrchestrator":
        from .orchestrator import SweepOrchestrator as _cls
        import sys
        # Cache it on the module so subsequent imports skip __getattr__
        sys.modules[__name__].__dict__[name] = _cls
        return _cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
