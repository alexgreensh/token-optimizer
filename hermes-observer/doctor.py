#!/usr/bin/env python3
"""Health check for the standalone Hermes observer."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Importing the sibling module must not create __pycache__. The only permitted
# filesystem mutation is SQLite-managed state.db-shm WAL read coordination.
sys.dont_write_bytecode = True
import observer


_FAILED_ALL = {"database": "failed", "home": "failed", "schema": "failed"}
_ERROR_CHECKS: dict[str, dict[str, str]] = {
    # The home itself could not be established as a stable, canonical source.
    "home_invalid": _FAILED_ALL,
    "home_relative": _FAILED_ALL,
    "home_missing": _FAILED_ALL,
    "home_symlink": _FAILED_ALL,
    "home_not_directory": _FAILED_ALL,
    "source_changed": _FAILED_ALL,
    # The home was validated, but the database or its WAL source was not.
    **{
        code: {"database": "failed", "home": "ok", "schema": "failed"}
        for code in (
            "database_missing",
            "database_symlink",
            "database_not_regular",
            "database_invalid",
            "database_escape",
            "database_oversized",
            "database_malformed",
            "database_busy",
            "unsupported_schema",
            "wal_state_unsafe",
            "wal_sidecars_unavailable",
        )
    },
    # These indicate a defect in the observer rather than a validated source.
    "internal_error": _FAILED_ALL,
    "output_limit": _FAILED_ALL,
}


def _checks_for_error(code: str) -> dict[str, str]:
    """Classify known observer failures explicitly; unknown failures fail closed."""
    return dict(_ERROR_CHECKS.get(code, _FAILED_ALL))


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
        return (
            {
                "checks": _checks_for_error(error.code),
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
