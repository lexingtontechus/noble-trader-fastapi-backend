"""
Monthly ATR re-optimum — manual/Studio CLI entry.

  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/monthly_atr_reopt.py

Same job the orchestrator schedules (atr-reopt-monthly, 1st 03:30 UTC).
Proposal-only: report + nt_roadmap heartbeat. Never writes YAML.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")


async def main() -> None:
    from regime_platform.sweep_orchestrator.atr_reopt import run_atr_reopt

    result = await run_atr_reopt()
    print(result["summary"])
    for p in result.get("proposals") or []:
        pick = p["pick"]
        print(f"PROPOSE {p['scope']}: {pick['tp']:.2f}/{pick['sl']:.2f} EV {pick['ev']:+.3f}")


if __name__ == "__main__":
    asyncio.run(main())
