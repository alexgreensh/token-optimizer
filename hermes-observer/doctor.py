#!/usr/bin/env python3
"""Health check for the standalone Hermes observer."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Importing the sibling module must not create __pycache__; the observer suite
# is intentionally write-free even when invoked from a read-only directory.
sys.dont_write_bytecode = True
import observer


def _document(home: str) -> tuple[dict[str, Any], int]:
    try:
        report = observer.inspect_database(home)
        return (
            {
                "checks": {"database": "ok", "home": "ok", "schema": "ok"},
                "ok": True,
                "schema_version": observer.SCHEMA_VERSION,
                "session_count": report["sessions"]["total"],
            },
            0,
        )
    except observer.ObserverError as error:
        home_ok = not error.code.startswith("home_")
        database_ok = home_ok and not error.code.startswith("database_")
        return (
            {
                "checks": {
                    "database": "ok" if database_ok else "failed",
                    "home": "ok" if home_ok else "failed",
                    "schema": "failed",
                },
                "error": {"code": error.code, "message": error.message},
                "ok": False,
                "schema_version": observer.SCHEMA_VERSION,
            },
            1,
        )
    except Exception:
        return (
            {
                "checks": {"database": "failed", "home": "failed", "schema": "failed"},
                "error": {"code": "internal_error", "message": "doctor failed safely"},
                "ok": False,
                "schema_version": observer.SCHEMA_VERSION,
            },
            1,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a Hermes observer source")
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--format", choices=("json",), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    document, code = _document(args.hermes_home)
    sys.stdout.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
