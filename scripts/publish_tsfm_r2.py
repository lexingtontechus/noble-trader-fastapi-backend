"""Local runner / bootstrap / outage bridge for the TSFM lab R2 publish.

Runs the SAME module the Studio hourly job runs — one implementation, two
runners (docs/scopes/20261118_TSFM_R2_BACKEND_PUBLISH_SCOPE.md §2.4). Uses
.env.local TDVA creds + TSFM_R2_WRITE_SECRET; no Studio needed.

    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/publish_tsfm_r2.py --mode shadow
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/publish_tsfm_r2.py --mode live

Exit 0 when the run completed with no errors (shadow: validated; live:
published + manifest), 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_env = ROOT / ".env.local"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["shadow", "live"], default=None,
                        help="default: the tsfm_r2_publish_mode flag value")
    args = parser.parse_args()

    from regime_platform.sweep_orchestrator.tsfm_r2_publish import publish_tsfm_prices

    summary = asyncio.run(publish_tsfm_prices(args.mode, roster_fallback=True))
    print(json.dumps(summary, indent=2))
    ok = not summary.get("errors") and (
        summary.get("mode") == "off"
        or (summary.get("mode") == "shadow" and summary.get("symbols"))
        or (summary.get("mode") == "live" and summary.get("published", 0) > 0
            and summary.get("manifest_written"))
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
