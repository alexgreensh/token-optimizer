#!/usr/bin/env python3
"""Bump the plugin's patch version everywhere a release reads it.

Used by the refresh-prices workflow to cut a price-only release without a
human. Touches only the canonical manifests; the plugins/ and cowork/ mirrors
are regenerated from them by the sync scripts afterwards. Prints the new
version. Refuses to run when the manifests disagree, since shipping on top of
drift is how a release ends up with two versions.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / ".claude-plugin" / "plugin.json"
CODEX = REPO / ".codex-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
OPENCODE_DASHBOARD = REPO / "opencode" / "src" / "dashboard" / "generator.ts"
OPENCLAW_DASHBOARD = REPO / "openclaw" / "src" / "dashboard.ts"
# Marketplace entries that ship at the plugin's own version.
MARKETPLACE_ENTRIES = ("token-optimizer", "token-optimizer-cowork")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _set_version(path: Path, old: str, new: str, expected: int) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'("version"\s*:\s*")' + re.escape(old) + r'"')
    updated, count = pattern.subn(lambda m: m.group(1) + new + '"', text)
    if count != expected:
        raise SystemExit(f"{path.name}: expected {expected} version field(s) at {old}, found {count}")
    path.write_text(updated, encoding="utf-8")


def _set_core_fallback(path: Path, constant: str, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^(const " + re.escape(constant) + r' = ")' + re.escape(old) + r'(";)$', re.MULTILINE)
    updated, count = pattern.subn(lambda m: m.group(1) + new + m.group(2), text)
    if count != 1:
        raise SystemExit(f"{path}: expected one {constant} at {old}, found {count}")
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    current = json.loads(PLUGIN.read_text(encoding="utf-8"))["version"]
    codex = json.loads(CODEX.read_text(encoding="utf-8"))["version"]
    market = {p["name"]: p.get("version") for p in json.loads(MARKETPLACE.read_text(encoding="utf-8"))["plugins"]}
    seen = {current, codex, *(market.get(name) for name in MARKETPLACE_ENTRIES)}
    if len(seen) != 1:
        print(f"ERROR: manifest versions disagree: {sorted(map(str, seen))}", file=sys.stderr)
        return 1
    m = SEMVER.match(current)
    if not m:
        print(f"ERROR: version {current!r} is not MAJOR.MINOR.PATCH", file=sys.stderr)
        return 1
    new = f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    _set_version(PLUGIN, current, new, 1)
    _set_version(CODEX, current, new, 1)
    _set_version(MARKETPLACE, current, new, len(MARKETPLACE_ENTRIES))
    _set_core_fallback(OPENCODE_DASHBOARD, "CORE_VERSION", current, new)
    _set_core_fallback(OPENCLAW_DASHBOARD, "CORE_VERSION_FALLBACK", current, new)
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
