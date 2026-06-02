#!/usr/bin/env python3
"""Explicit structure-map helper for Codex.

Use this when Codex cannot transparently substitute a whole-file reread. It
gives the user or agent an honest, bounded outline of the file instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from structure_map import summarize_code_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a bounded structure-map outline for a file."
    )
    parser.add_argument("path", help="File to summarize")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full structure-map payload as JSON",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    target = Path(args.path).expanduser()

    if not target.exists():
        print(f"[Token Optimizer] file not found: {target}", file=sys.stderr)
        return 1
    if not target.is_file():
        print(f"[Token Optimizer] not a file: {target}", file=sys.stderr)
        return 1

    result = summarize_code_file(str(target))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    print(result.replacement_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
