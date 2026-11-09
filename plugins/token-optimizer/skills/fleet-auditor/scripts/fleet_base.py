"""Shared adapter plumbing for the fleet auditor.

TokenBreakdown, AgentRun, BaseAdapter and the collected-run probe live here so
fleet.py stays under the repo's 1,000-line file-size ratchet
(scripts/check_file_size.py) and per-system adapters can live in their own
modules without importing fleet (which would double-load fleet.py when it runs
as __main__). fleet.py re-exports everything; existing fleet.* attributes and
monkeypatch seams are unchanged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class TokenBreakdown:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write



@dataclass
class AgentRun:
    system: str                    # "claude", "codex", "openclaw", "hermes", "opencode", "nanoclaw", "ironclaw"
    session_id: str = ""
    agent_name: str = ""
    project: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float = 0.0
    tokens: TokenBreakdown = field(default_factory=TokenBreakdown)
    cost_usd: float = 0.0
    model: str = ""                # Normalized: "opus", "sonnet", "haiku", etc.
    context_window_size: int = 200_000
    run_type: str = "manual"       # "heartbeat", "task", "cron", "manual", "delegate"
    outcome: str = "success"       # "success", "failure", "empty", "loop", "abandoned"
    message_count: int = 0
    tools_used: list = field(default_factory=list)
    source_path: str = ""
    error_message: str | None = None
    exit_code: int | None = None
    # Token provenance label (e.g. "cursor_rollup", "cursor_tally_only").
    # Empty when the source system carries a single authoritative token record.
    token_source: str = ""



class BaseAdapter:
    """Base class for agent system adapters.

    Each adapter implements three methods:
    - detect() -> (bool, float, str): Is this system installed? (found, confidence 0-1, detail)
    - scan(since, conn) -> (list[AgentRun], list[str]): Collect runs since date. Returns (runs, errors).
    - parse_config() -> dict: Extract system configuration for Tier 1 analysis.
    """

    name: str = "base"
    display_name: str = "Base"

    def detect(self) -> tuple[bool, float, str]:
        """Detect if this system is installed.

        Returns (found, confidence, detail_message).
        Confidence: 0.0-1.0 where 1.0 = definitely installed with data.
        """
        return False, 0.0, "Not implemented"

    def scan(self, since: datetime, conn: sqlite3.Connection | None = None) -> tuple[list[AgentRun], list[str]]:
        """Scan for agent runs since the given datetime.

        Returns (runs, errors) where errors is a list of non-fatal error messages.
        If conn is provided, skips already-collected runs.

        The default is for adapters that can detect a system but not read its
        runs yet. It says so: a bare ([], []) printed "0 scanned" for a system
        detect() had just reported as installed, the silent-zero of #149.
        """
        return [], [f"{self.display_name} fleet collection is not implemented yet, "
                    f"so its runs are not counted here (0 is not a measurement)."]

    def parse_config(self) -> dict:
        """Parse system configuration for Tier 1 waste analysis.

        Returns dict with system-specific config details like:
        - model settings, skill counts, cron configs, tool definitions
        """
        return {}



def _is_run_collected(conn: sqlite3.Connection, system: str, source_path: str, session_id: str) -> bool:
    """Check if a specific run has already been collected."""
    cur = conn.execute(
        "SELECT 1 FROM fleet_runs WHERE system = ? AND source_path = ? AND session_id = ?",
        (system, source_path, session_id),
    )
    return cur.fetchone() is not None


