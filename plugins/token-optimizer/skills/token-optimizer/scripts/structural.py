#!/usr/bin/env python3
"""Setup-trim ledger CLI (kept outside measure.py's frozen dispatch table).

    python3 structural.py status [--days 30]
    python3 structural.py tick [--force]            snapshot now, open/close trims
    python3 structural.py seed --backup-dir DIR [--skills-archive DIR]
                           [--commands-archive DIR] [--ts ISO] [--apply]
                           [--revert BATCH --apply]  one-time past-audit import, dry run by default
    python3 structural.py attest --tokens N --date ISO [--note TEXT] [--apply]
    python3 structural.py whatif --tokens 5150,11550,15000 --date ISO
    python3 structural.py close --id TRIM_ID [--reason TEXT]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import measure  # noqa: E402  (binds the facade the helpers resolve through)
from _measure import structural_report  # noqa: E402

if __name__ == "__main__":
    if measure._is_foreign_runtime():
        print("structural: Claude Code setups only", file=sys.stderr)
        sys.exit(2)
    sys.exit(structural_report.cli(sys.argv[1:]))
