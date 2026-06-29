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
  • Native Redis protocol via redis-py instead of Upstash REST API
"""

from .orchestrator import SweepOrchestrator

__all__ = ["SweepOrchestrator"]
