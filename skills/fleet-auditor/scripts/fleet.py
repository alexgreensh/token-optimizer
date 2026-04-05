#!/usr/bin/env python3
"""Fleet Auditor: Cross-Platform Agent Token Waste Auditor.

Detects agent systems (Claude Code, OpenClaw, NanoClaw, Hermes, OpenCode, IronClaw),
collects token usage data, identifies waste patterns, and recommends fixes with
dollar savings estimates.

Zero external dependencies. Python 3.10+.

Usage:
    python3 fleet.py detect                         # What systems are installed?
    python3 fleet.py scan [--system X] [--days 30]  # Collect into fleet.db
    python3 fleet.py audit [--system X] [--days 30] # Waste detection
    python3 fleet.py report [--system X] [--json]   # Full report with $ savings
    python3 fleet.py dashboard [--serve]             # Generate fleet dashboard
"""

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Resolve shared.py: works from skill dir, plugin cache, or standalone
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from shared import (
    HOME,
    CLAUDE_DIR,
    normalize_model_name,
    iter_jsonl,
    parse_timestamp,
    find_claude_jsonl_files,
    find_subagent_jsonl_files,
    clean_project_name,
    init_sqlite_db,
    migrate_add_columns,
    estimate_tokens_from_file,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLEET_DB_DIR = CLAUDE_DIR / "_backups" / "token-optimizer"
FLEET_DB = FLEET_DB_DIR / "fleet.db"
FLEET_DASHBOARD_PATH = FLEET_DB_DIR / "fleet-dashboard.html"

SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# Pricing (USD per token)
# ---------------------------------------------------------------------------

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Claude (Opus 4.6 / Sonnet 4.6 / Haiku 4.5 pricing, verified 2026-03-17)
    "opus":           {"input": 5.0/1e6,  "output": 25.0/1e6, "cache_read": 0.5/1e6,  "cache_write": 6.25/1e6},
    "sonnet":         {"input": 3.0/1e6,  "output": 15.0/1e6, "cache_read": 0.3/1e6,  "cache_write": 3.75/1e6},
    "haiku":          {"input": 1.0/1e6,  "output": 5.0/1e6,  "cache_read": 0.1/1e6,  "cache_write": 1.25/1e6},
    "gpt-4o":         {"input": 2.5/1e6,  "output": 10.0/1e6, "cache_read": 1.25/1e6, "cache_write": 0},
    "gpt-4o-mini":    {"input": 0.15/1e6, "output": 0.6/1e6,  "cache_read": 0,        "cache_write": 0},
    "gemini-2.5-pro": {"input": 1.25/1e6, "output": 10.0/1e6, "cache_read": 0.315/1e6, "cache_write": 0},
}

_pricing_override: dict | None = None


def _load_pricing() -> dict[str, dict[str, float]]:
    """Load pricing with optional user override from pricing.json."""
    global _pricing_override
    if _pricing_override is not None:
        return _pricing_override

    pricing = dict(DEFAULT_PRICING)
    override_path = FLEET_DB_DIR / "pricing.json"
    if override_path.exists():
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                user_pricing = json.load(f)
            for model, rates in user_pricing.items():
                pricing[model] = {**pricing.get(model, {}), **rates}
        except (json.JSONDecodeError, PermissionError, OSError):
            pass
    _pricing_override = pricing
    return pricing


def calculate_cost(tokens: "TokenBreakdown", model: str) -> float:
    """Calculate USD cost for a token breakdown at given model rates."""
    pricing = _load_pricing()
    rates = pricing.get(model, pricing.get("sonnet", {}))
    cost = 0.0
    cost += tokens.input * rates.get("input", 0)
    cost += tokens.output * rates.get("output", 0)
    cost += tokens.cache_read * rates.get("cache_read", 0)
    cost += tokens.cache_write * rates.get("cache_write", 0)
    return cost


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

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
    system: str                    # "claude", "openclaw", "hermes", "opencode", "nanoclaw", "ironclaw"
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


@dataclass
class WasteFinding:
    system: str
    agent_name: str = ""
    waste_type: str = ""
    tier: int = 1
    severity: str = "medium"       # "low", "medium", "high", "critical"
    confidence: float = 0.8
    description: str = ""
    monthly_waste_usd: float = 0.0
    monthly_waste_tokens: int = 0
    recommendation: str = ""
    fix_snippet: str = ""
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQLite Schema
# ---------------------------------------------------------------------------

_FLEET_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    system TEXT NOT NULL,
    session_id TEXT,
    date TEXT NOT NULL,
    agent_name TEXT,
    project TEXT,
    duration_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,
    model TEXT,
    context_window_size INTEGER,
    run_type TEXT,
    outcome TEXT,
    message_count INTEGER,
    error_message TEXT,
    exit_code INTEGER,
    collected_at TEXT,
    UNIQUE(system, source_path, session_id)
);

CREATE TABLE IF NOT EXISTS fleet_waste (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL,
    agent_name TEXT,
    waste_type TEXT NOT NULL,
    tier INTEGER NOT NULL,
    severity TEXT,
    confidence REAL,
    description TEXT,
    monthly_waste_usd REAL,
    monthly_waste_tokens INTEGER,
    recommendation TEXT,
    fix_snippet TEXT,
    evidence_json TEXT,
    detected_at TEXT
);

CREATE TABLE IF NOT EXISTS fleet_daily (
    date TEXT,
    system TEXT,
    run_count INTEGER,
    total_input INTEGER,
    total_output INTEGER,
    total_cost REAL,
    empty_heartbeat_count INTEGER,
    PRIMARY KEY (date, system)
);

CREATE TABLE IF NOT EXISTS fleet_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _init_fleet_db() -> sqlite3.Connection:
    """Initialize fleet.db with schema and version tracking."""
    conn = init_sqlite_db(FLEET_DB, _FLEET_SCHEMA)
    # Set schema version if not present
    cur = conn.execute("SELECT value FROM fleet_meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if not row:
        conn.execute(
            "INSERT INTO fleet_meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.commit()
    return conn


def _is_run_collected(conn: sqlite3.Connection, system: str, source_path: str, session_id: str) -> bool:
    """Check if a specific run has already been collected."""
    cur = conn.execute(
        "SELECT 1 FROM fleet_runs WHERE system = ? AND source_path = ? AND session_id = ?",
        (system, source_path, session_id),
    )
    return cur.fetchone() is not None


def _insert_run(conn: sqlite3.Connection, run: AgentRun):
    """Insert an AgentRun into fleet_runs."""
    conn.execute(
        """INSERT OR IGNORE INTO fleet_runs
        (source_path, system, session_id, date, agent_name, project,
         duration_seconds, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens, cost_usd, model, context_window_size,
         run_type, outcome, message_count, error_message, exit_code, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.source_path,
            run.system,
            run.session_id,
            run.timestamp.strftime("%Y-%m-%d"),
            run.agent_name,
            run.project,
            run.duration_seconds,
            run.tokens.input,
            run.tokens.output,
            run.tokens.cache_read,
            run.tokens.cache_write,
            run.cost_usd,
            run.model,
            run.context_window_size,
            run.run_type,
            run.outcome,
            run.message_count,
            run.error_message,
            run.exit_code,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _insert_waste(conn: sqlite3.Connection, finding: WasteFinding):
    """Insert a WasteFinding into fleet_waste."""
    conn.execute(
        """INSERT INTO fleet_waste
        (system, agent_name, waste_type, tier, severity, confidence,
         description, monthly_waste_usd, monthly_waste_tokens,
         recommendation, fix_snippet, evidence_json, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            finding.system,
            finding.agent_name,
            finding.waste_type,
            finding.tier,
            finding.severity,
            finding.confidence,
            finding.description,
            finding.monthly_waste_usd,
            finding.monthly_waste_tokens,
            finding.recommendation,
            finding.fix_snippet,
            json.dumps(finding.evidence) if finding.evidence else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _update_daily_aggregates(conn: sqlite3.Connection):
    """Rebuild fleet_daily from fleet_runs."""
    conn.execute("DELETE FROM fleet_daily")
    conn.execute("""
        INSERT INTO fleet_daily (date, system, run_count, total_input, total_output, total_cost, empty_heartbeat_count)
        SELECT
            date, system,
            COUNT(*) as run_count,
            SUM(input_tokens) as total_input,
            SUM(output_tokens) as total_output,
            SUM(cost_usd) as total_cost,
            SUM(CASE WHEN run_type = 'heartbeat' AND outcome = 'empty' THEN 1 ELSE 0 END) as empty_heartbeat_count
        FROM fleet_runs
        GROUP BY date, system
    """)
    conn.commit()


# ===================================================================
# ADAPTER PATTERN
# ===================================================================

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
        """
        return [], []

    def parse_config(self) -> dict:
        """Parse system configuration for Tier 1 waste analysis.

        Returns dict with system-specific config details like:
        - model settings, skill counts, cron configs, tool definitions
        """
        return {}


class ClaudeCodeAdapter(BaseAdapter):
    """Adapter for Claude Code (~/.claude/projects/)."""

    name = "claude"
    display_name = "Claude Code"

    def detect(self) -> tuple[bool, float, str]:
        projects_dir = CLAUDE_DIR / "projects"
        if not projects_dir.exists():
            return False, 0.0, "~/.claude/projects/ not found"

        # Count JSONL files
        jsonl_count = 0
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                jsonl_count += sum(1 for _ in pd.glob("*.jsonl"))
            if jsonl_count > 5:
                break

        if jsonl_count == 0:
            return True, 0.3, "~/.claude/projects/ exists but no session logs found"

        return True, 1.0, f"Found {jsonl_count}+ session logs in ~/.claude/projects/"

    def scan(self, since: datetime, conn: sqlite3.Connection | None = None) -> tuple[list[AgentRun], list[str]]:
        days = max(1, int((datetime.now(timezone.utc) - since).total_seconds() / 86400) + 1)
        files = find_claude_jsonl_files(days=days)

        runs = []
        errors = []

        for filepath, mtime, project_dir_name in files:
            session_id = filepath.stem

            # Skip already collected
            if conn and _is_run_collected(conn, "claude", str(filepath), session_id):
                continue

            run = self._parse_session(filepath, project_dir_name)
            if run:
                runs.append(run)

            # Also scan subagent files
            for sub_path in find_subagent_jsonl_files(filepath):
                sub_id = f"{session_id}/sub/{sub_path.stem}"
                if conn and _is_run_collected(conn, "claude", str(sub_path), sub_id):
                    continue
                sub_run = self._parse_session(sub_path, project_dir_name, is_subagent=True, parent_session=session_id)
                if sub_run:
                    runs.append(sub_run)

        return runs, errors

    def _parse_session(self, filepath: Path, project_dir_name: str,
                       is_subagent: bool = False, parent_session: str = "") -> AgentRun | None:
        """Parse a single Claude Code JSONL session file into an AgentRun."""
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_create = 0
        model_usage: dict[str, int] = {}
        message_count = 0
        api_calls = 0
        tools_used_set: set[str] = set()
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        version = None

        for record in iter_jsonl(filepath):
            # Version
            if version is None:
                v = record.get("version")
                if v:
                    version = v

            # Timestamp
            ts = parse_timestamp(record.get("timestamp"))
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            rec_type = record.get("type")

            # Count messages
            if rec_type in ("user", "assistant"):
                message_count += 1

            # Extract tool usage + token data from assistant messages
            if rec_type == "assistant":
                msg = record.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tools_used_set.add(block.get("name", ""))

                usage = msg.get("usage", {})
                if usage:
                    inp_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    total_input += inp_tok
                    total_output += out_tok
                    total_cache_read += cr
                    total_cache_create += cc
                    api_calls += 1

                    model_id = msg.get("model", "unknown")
                    model_usage[model_id] = model_usage.get(model_id, 0) + inp_tok + cr + cc + out_tok

        if message_count == 0:
            return None

        # Duration
        duration_seconds = 0.0
        if first_ts and last_ts:
            duration_seconds = max(0.0, (last_ts - first_ts).total_seconds())

        # Dominant model
        dominant_model_raw = max(model_usage, key=model_usage.get) if model_usage else "unknown"
        model = normalize_model_name(dominant_model_raw) or dominant_model_raw

        # Context window heuristic
        context_window = 200_000
        if model in ("opus", "sonnet") or "1m" in dominant_model_raw.lower():
            context_window = 1_000_000

        # input_tokens from API = uncached input only
        # cache_read_input_tokens = tokens read from cache
        # cache_creation_input_tokens = tokens written to cache
        # Total context sent = uncached + cache_read + cache_create
        # Each priced at its own rate, so store separately.
        tokens = TokenBreakdown(
            input=total_input,           # Uncached input only
            output=total_output,
            cache_read=total_cache_read,
            cache_write=total_cache_create,
        )

        # Determine outcome (use total context = uncached + cache_read + cache_create)
        total_context = total_input + total_cache_read + total_cache_create
        outcome = "success"
        if message_count <= 2 and total_output < 200:
            outcome = "abandoned"
        elif total_output < 100 and total_context > 50_000:
            outcome = "empty"

        # Determine run type
        run_type = "delegate" if is_subagent else "manual"

        cost = calculate_cost(tokens, model)

        return AgentRun(
            system="claude",
            session_id=filepath.stem if not is_subagent else f"{parent_session}/sub/{filepath.stem}",
            agent_name="subagent" if is_subagent else "main",
            project=clean_project_name(project_dir_name),
            timestamp=first_ts or datetime.now(timezone.utc),
            duration_seconds=duration_seconds,
            tokens=tokens,
            cost_usd=cost,
            model=model,
            context_window_size=context_window,
            run_type=run_type,
            outcome=outcome,
            message_count=message_count,
            tools_used=sorted(tools_used_set),
            source_path=str(filepath),
        )

    def parse_config(self) -> dict:
        """Parse Claude Code configuration for Tier 1 analysis."""
        config: dict[str, Any] = {
            "skills": [],
            "mcp_servers": [],
            "claude_md_tokens": 0,
            "memory_md_tokens": 0,
            "commands": [],
            "hooks": {},
        }

        # Skills
        skills_dir = CLAUDE_DIR / "skills"
        if skills_dir.exists():
            for sd in skills_dir.iterdir():
                if sd.is_dir():
                    skill_md = sd / "SKILL.md"
                    if skill_md.exists():
                        config["skills"].append({
                            "name": sd.name,
                            "tokens": estimate_tokens_from_file(skill_md),
                        })

        # CLAUDE.md
        claude_md = CLAUDE_DIR / "CLAUDE.md"
        if claude_md.exists():
            config["claude_md_tokens"] = estimate_tokens_from_file(claude_md)

        # MEMORY.md files
        projects_dir = CLAUDE_DIR / "projects"
        if projects_dir.exists():
            for pd in projects_dir.iterdir():
                mem_file = pd / "memory" / "MEMORY.md"
                if mem_file.exists():
                    config["memory_md_tokens"] = max(
                        config["memory_md_tokens"],
                        estimate_tokens_from_file(mem_file),
                    )

        # MCP servers from settings
        settings_path = CLAUDE_DIR / "settings.json"
        if settings_path.exists():
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                mcp = settings.get("mcpServers", {})
                config["mcp_servers"] = list(mcp.keys())

                # Hooks
                hooks = settings.get("hooks", {})
                config["hooks"] = hooks
            except (json.JSONDecodeError, PermissionError):
                pass

        # Commands
        cmd_dir = CLAUDE_DIR / "commands"
        if cmd_dir.exists():
            for cf in cmd_dir.glob("*.md"):
                config["commands"].append(cf.stem)

        return config


# Stubs for Phase 1.5+ adapters
class OpenClawAdapter(BaseAdapter):
    name = "openclaw"
    display_name = "OpenClaw"

    def detect(self) -> tuple[bool, float, str]:
        for p in [HOME / ".openclaw", HOME / ".clawdbot", HOME / ".moltbot"]:
            if p.exists():
                sessions_json = p / "agents" if (p / "agents").is_dir() else None
                if sessions_json:
                    return True, 0.8, f"Found {p.name} directory with agents/"
                return True, 0.4, f"Found {p.name} directory (no agents/ yet)"
        return False, 0.0, "No OpenClaw directories found"


class NanoClawAdapter(BaseAdapter):
    name = "nanoclaw"
    display_name = "NanoClaw"

    def detect(self) -> tuple[bool, float, str]:
        nc_dir = HOME / ".nanoclaw"
        if nc_dir.exists():
            return True, 0.6, "Found ~/.nanoclaw/"
        return False, 0.0, "~/.nanoclaw/ not found"


class HermesAdapter(BaseAdapter):
    name = "hermes"
    display_name = "Hermes"

    def detect(self) -> tuple[bool, float, str]:
        state_db = HOME / ".hermes" / "state.db"
        if state_db.exists():
            return True, 1.0, "Found ~/.hermes/state.db"
        hermes_dir = HOME / ".hermes"
        if hermes_dir.exists():
            return True, 0.4, "Found ~/.hermes/ (no state.db)"
        return False, 0.0, "~/.hermes/ not found"


class OpenCodeAdapter(BaseAdapter):
    name = "opencode"
    display_name = "OpenCode"

    def detect(self) -> tuple[bool, float, str]:
        # Check env var first
        data_dir = os.environ.get("OPENCODE_DATA_DIR")
        if data_dir and Path(data_dir).exists():
            return True, 0.9, f"Found $OPENCODE_DATA_DIR at {data_dir}"

        oc_dir = HOME / ".local" / "share" / "opencode"
        if oc_dir.exists():
            return True, 0.8, "Found ~/.local/share/opencode/"
        return False, 0.0, "No OpenCode data directory found"


class IronClawAdapter(BaseAdapter):
    name = "ironclaw"
    display_name = "IronClaw"

    def detect(self) -> tuple[bool, float, str]:
        ic_dir = HOME / ".ironclaw"
        if ic_dir.exists():
            return True, 0.3, "Found ~/.ironclaw/ (needs connection config for full scan)"
        return False, 0.0, "~/.ironclaw/ not found"


# Adapter registry
ADAPTER_REGISTRY: list[type[BaseAdapter]] = [
    ClaudeCodeAdapter,
    OpenClawAdapter,
    NanoClawAdapter,
    HermesAdapter,
    OpenCodeAdapter,
    IronClawAdapter,
]


# ===================================================================
# WASTE PATTERN DETECTORS
# ===================================================================

class BaseDetector:
    """Base class for waste pattern detectors."""

    name: str = ""
    tier: int = 1
    description: str = ""

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        """Run detection and return findings. Empty list = no waste found."""
        return []


# --- Tier 1: Static Config Analysis ---

class HeartbeatModelWaste(BaseDetector):
    """Detect expensive models used for heartbeat/cron runs."""
    name = "heartbeat_model_waste"
    tier = 1
    description = "Expensive model for heartbeat/cron tasks"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        findings = []
        heartbeat_runs = [r for r in runs if r.run_type in ("heartbeat", "cron")]
        if not heartbeat_runs:
            return findings

        expensive_hb = [r for r in heartbeat_runs if r.model in ("opus", "sonnet")]
        if not expensive_hb:
            return findings

        total_cost = sum(r.cost_usd for r in expensive_hb)
        days_spanned = max(1, len({r.timestamp.strftime("%Y-%m-%d") for r in expensive_hb}))
        monthly_cost = (total_cost / days_spanned) * 30

        # Calculate savings if switched to haiku
        haiku_cost = 0.0
        for r in expensive_hb:
            haiku_cost += calculate_cost(r.tokens, "haiku")
        haiku_monthly = (haiku_cost / days_spanned) * 30
        savings = monthly_cost - haiku_monthly

        if savings < 0.10:
            return findings

        findings.append(WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="high" if savings > 5.0 else "medium",
            confidence=0.9,
            description=f"{len(expensive_hb)} heartbeat/cron runs using {expensive_hb[0].model} instead of Haiku",
            monthly_waste_usd=savings,
            monthly_waste_tokens=sum(r.tokens.total for r in expensive_hb),
            recommendation=f"Route heartbeat/cron tasks to Haiku. Saves ~${savings:.2f}/month.",
            fix_snippet='# In your agent config, set heartbeat model:\nmodel: "haiku"  # was: opus/sonnet',
            evidence={"expensive_count": len(expensive_hb), "models_used": list({r.model for r in expensive_hb})},
        ))
        return findings


class SkillBloat(BaseDetector):
    """Detect too many skills loaded per agent."""
    name = "skill_bloat"
    tier = 1
    description = "Too many skills loaded (~100 tokens each, every API call)"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        skills = config.get("skills", [])
        if len(skills) <= 10:
            return []

        total_skill_tokens = sum(s.get("tokens", 100) for s in skills)
        # ~100 tokens per skill per API call, estimate 20 API calls per session average
        monthly_sessions = 30  # rough estimate
        monthly_waste = total_skill_tokens * 20 * monthly_sessions

        cost_per_token = 3.0 / 1e6  # sonnet input rate as baseline
        monthly_cost = monthly_waste * cost_per_token

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="medium" if len(skills) <= 20 else "high",
            confidence=0.8,
            description=f"{len(skills)} skills loaded ({total_skill_tokens:,} tokens overhead per API call)",
            monthly_waste_usd=monthly_cost,
            monthly_waste_tokens=monthly_waste,
            recommendation=f"Archive unused skills. Each removed skill saves ~100 tokens per API call across all sessions.",
            fix_snippet="# Move unused skills out of ~/.claude/skills/\n# Check which skills you actually use:\n# python3 measure.py trends --days 30",
            evidence={"skill_count": len(skills), "skill_names": [s.get("name", "?") for s in skills]},
        )]


class ToolDefinitionBloat(BaseDetector):
    """Detect tool definitions consuming >20% of context."""
    name = "tool_definition_bloat"
    tier = 1
    description = "Tool definitions consuming excessive context"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        mcp_servers = config.get("mcp_servers", [])
        if len(mcp_servers) <= 3:
            return []

        # Estimate: ~150 tokens per eager tool, ~15 per deferred, ~10 tools per server
        # Assume 30% eager, 70% deferred after ToolSearch
        tools_per_server = 10
        total_tools = len(mcp_servers) * tools_per_server
        eager_tools = int(total_tools * 0.3)
        deferred_tools = total_tools - eager_tools
        tool_tokens = (eager_tools * 150) + (deferred_tools * 15)

        # Check if >20% of smallest likely context window
        context_pct = (tool_tokens / 200_000) * 100
        if context_pct < 15:
            return []

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="high" if context_pct > 30 else "medium",
            confidence=0.6,  # Rough estimate
            description=f"~{len(mcp_servers)} MCP servers with ~{total_tools} tools consuming ~{tool_tokens:,} tokens ({context_pct:.0f}% of 200K context)",
            monthly_waste_tokens=tool_tokens * 20 * 30,  # per call * calls/session * sessions/month
            monthly_waste_usd=0,  # Hard to monetize config overhead accurately
            recommendation="Disable unused MCP servers. Use ToolSearch (deferred loading) for large tool sets.",
            fix_snippet="# Check which MCP servers are actually used:\n# Review ~/.claude/settings.json mcpServers section",
            evidence={"mcp_count": len(mcp_servers), "servers": mcp_servers[:10]},
        )]


class MemoryConfigOverhead(BaseDetector):
    """Detect memory/config files exceeding 5,000 tokens."""
    name = "memory_config_overhead"
    tier = 1
    description = "Injected config files exceeding 5,000 tokens"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        findings = []

        claude_md_tokens = config.get("claude_md_tokens", 0)
        if claude_md_tokens > 5000:
            findings.append(WasteFinding(
                system=system,
                waste_type=self.name,
                tier=self.tier,
                severity="medium" if claude_md_tokens <= 10000 else "high",
                confidence=0.9,
                description=f"CLAUDE.md is {claude_md_tokens:,} tokens (injected every API call)",
                monthly_waste_tokens=claude_md_tokens * 20 * 30,
                monthly_waste_usd=0,
                recommendation="Slim CLAUDE.md to <2,000 tokens. Move reference material to files loaded on demand.",
                fix_snippet="# Run /token-optimizer for guided CLAUDE.md optimization",
                evidence={"claude_md_tokens": claude_md_tokens},
            ))

        memory_tokens = config.get("memory_md_tokens", 0)
        if memory_tokens > 5000:
            findings.append(WasteFinding(
                system=system,
                waste_type=self.name,
                tier=self.tier,
                severity="medium",
                confidence=0.9,
                description=f"MEMORY.md is {memory_tokens:,} tokens (injected every API call)",
                monthly_waste_tokens=memory_tokens * 20 * 30,
                monthly_waste_usd=0,
                recommendation="Prune MEMORY.md. Keep it under 200 lines. Move details to individual memory files.",
                evidence={"memory_md_tokens": memory_tokens},
            ))

        return findings


class StaleCronConfig(BaseDetector):
    """Detect cron jobs hitting dead or archived repos."""
    name = "stale_cron"
    tier = 1
    description = "Cron jobs configured for non-existent paths"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        # Claude Code doesn't have native cron, but hooks can serve as cron-like triggers
        # This detector is more relevant for OpenClaw/Hermes but we check hook paths here
        hooks = config.get("hooks", {})
        if not hooks:
            return []

        findings = []
        for hook_name, hook_list in hooks.items():
            if not isinstance(hook_list, list):
                continue
            for hook in hook_list:
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command", "")
                # Check if command references a path that doesn't exist
                if cmd:
                    parts = cmd.split()
                    for part in parts:
                        if part.startswith("/") and not part.startswith("/usr") and not part.startswith("/bin"):
                            if not Path(part).exists() and not part.startswith("$"):
                                findings.append(WasteFinding(
                                    system=system,
                                    waste_type=self.name,
                                    tier=self.tier,
                                    severity="low",
                                    confidence=0.5,
                                    description=f"Hook '{hook_name}' references non-existent path: {part}",
                                    recommendation="Remove or fix the hook referencing a dead path.",
                                    evidence={"hook": hook_name, "command": cmd, "missing_path": part},
                                ))
        return findings


class HeartbeatOverFrequency(BaseDetector):
    """Detect heartbeat intervals < 5 minutes."""
    name = "heartbeat_over_frequency"
    tier = 1
    description = "Heartbeat interval too frequent (< 5 minutes)"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        heartbeats = sorted(
            [r for r in runs if r.run_type in ("heartbeat", "cron")],
            key=lambda r: r.timestamp,
        )
        if len(heartbeats) < 3:
            return []

        # Check intervals between consecutive heartbeats
        short_intervals = []
        for i in range(1, len(heartbeats)):
            gap = (heartbeats[i].timestamp - heartbeats[i - 1].timestamp).total_seconds()
            if 0 < gap < 300:  # Less than 5 minutes
                short_intervals.append(gap)

        if len(short_intervals) < 3:
            return []

        avg_interval = sum(short_intervals) / len(short_intervals)
        # Estimate waste: each heartbeat costs ~input tokens at model rate
        avg_cost_per_hb = sum(r.cost_usd for r in heartbeats) / len(heartbeats)
        # How many extra heartbeats per month if interval is X instead of 5min?
        runs_per_hour_actual = 3600 / avg_interval
        runs_per_hour_optimal = 3600 / 300  # 12 per hour
        extra_runs_per_hour = max(0, runs_per_hour_actual - runs_per_hour_optimal)
        # Assume 16 active hours per day
        monthly_extra = extra_runs_per_hour * 16 * 30
        monthly_waste = monthly_extra * avg_cost_per_hb

        if monthly_waste < 0.10:
            return []

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="medium" if monthly_waste < 2.0 else "high",
            confidence=0.7,
            description=f"Heartbeats averaging {avg_interval:.0f}s interval ({len(short_intervals)} intervals < 5 min)",
            monthly_waste_usd=monthly_waste,
            recommendation=f"Increase heartbeat interval to 5+ minutes. Current average: {avg_interval:.0f}s.",
            fix_snippet="# Increase heartbeat interval in agent config:\nheartbeat_interval: 300  # 5 minutes",
            evidence={"avg_interval_seconds": avg_interval, "short_count": len(short_intervals)},
        )]


# --- Tier 2: Session Log Analysis ---

class EmptyHeartbeatRuns(BaseDetector):
    """Detect heartbeat runs with high input but near-zero output."""
    name = "empty_heartbeat"
    tier = 2
    description = "High input, <100 output, no state change (the #1 waste pattern)"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        # Look for runs with high total context but near-zero output.
        # tokens.input = uncached only, so use .total for full context loaded.
        empty_runs = [
            r for r in runs
            if r.tokens.total > 5000 and r.tokens.output < 100 and r.message_count <= 4
        ]

        if not empty_runs:
            return []

        # Filter false positives: require substantial context load or explicit "empty" outcome
        confirmed_empty = [r for r in empty_runs if r.tokens.total > 50_000 or r.outcome == "empty"]

        if len(confirmed_empty) < 2:
            return []

        total_waste_cost = sum(r.cost_usd for r in confirmed_empty)
        days = max(1, len({r.timestamp.strftime("%Y-%m-%d") for r in confirmed_empty}))
        monthly_cost = (total_waste_cost / days) * 30
        monthly_tokens = sum(r.tokens.total for r in confirmed_empty)

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="critical" if monthly_cost > 10 else "high" if monthly_cost > 2 else "medium",
            confidence=0.85,
            description=f"{len(confirmed_empty)} empty runs: high context load, near-zero useful output",
            monthly_waste_usd=monthly_cost,
            monthly_waste_tokens=monthly_tokens,
            recommendation="Add guard conditions to skip runs when there's nothing to do. Route idle checks to Haiku.",
            fix_snippet='# Add early-exit check in heartbeat script:\nif ! has_pending_work; then exit 0; fi',
            evidence={
                "empty_count": len(confirmed_empty),
                "avg_input": int(sum(r.tokens.input for r in confirmed_empty) / len(confirmed_empty)),
                "avg_output": int(sum(r.tokens.output for r in confirmed_empty) / len(confirmed_empty)),
            },
        )]


class SessionHistoryBloat(BaseDetector):
    """Detect sessions where context grows monotonically without compaction."""
    name = "session_history_bloat"
    tier = 2
    description = "Context tokens growing without compaction"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        # Look for long sessions (many messages) with high token totals
        long_sessions = [r for r in runs if r.message_count > 30 and r.tokens.total > 500_000]

        if not long_sessions:
            return []

        total_bloat_tokens = sum(r.tokens.input for r in long_sessions)
        # Estimate what compacted sessions would have cost (roughly 40% savings)
        savings_tokens = int(total_bloat_tokens * 0.4)
        days = max(1, len({r.timestamp.strftime("%Y-%m-%d") for r in long_sessions}))

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="medium",
            confidence=0.6,
            description=f"{len(long_sessions)} long sessions without apparent compaction (30+ messages, 500K+ input tokens)",
            monthly_waste_tokens=int((savings_tokens / days) * 30),
            recommendation="Use /compact at 50-70% context fill. Set up Smart Compaction for automatic protection.",
            fix_snippet="# Install Smart Compaction:\npython3 measure.py setup-smart-compact",
            evidence={"long_session_count": len(long_sessions), "total_input_tokens": total_bloat_tokens},
        )]


class LoopDetection(BaseDetector):
    """Detect sessions with many messages but trivially small output (stuck loops)."""
    name = "loop_detection"
    tier = 2
    description = "Many messages with near-zero output (stuck agent loops)"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        # Signal: many messages but almost zero TOTAL output, suggesting the agent
        # is stuck retrying without producing anything useful.
        # IMPORTANT: In Claude Code, output_tokens in JSONL only counts text output,
        # not tool calls. Many productive sessions have low output_tokens because
        # the real work happens via tool_use blocks. So we need very strict thresholds.
        # Only flag sessions with truly pathological output: <2 tokens per message average.
        loop_suspects = [
            r for r in runs
            if r.message_count > 20
            and r.tokens.output < (r.message_count * 2)  # <2 tokens output per message
            and r.tokens.total > 100_000  # Heavy context loaded
            and r.outcome not in ("empty", "abandoned")
            and r.run_type == "manual"  # Skip subagents (different output patterns)
        ]

        if len(loop_suspects) < 2:
            return []

        # Estimate waste: these sessions loaded heavy context for nothing
        total_waste = sum(r.cost_usd for r in loop_suspects)
        days = max(1, len({r.timestamp.strftime("%Y-%m-%d") for r in loop_suspects}))
        monthly_cost = (total_waste / days) * 30

        if monthly_cost < 1.00:
            return []

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="medium" if monthly_cost < 10 else "high",
            confidence=0.6,
            description=f"{len(loop_suspects)} sessions with 15+ messages but near-zero output (potential stuck loops)",
            monthly_waste_usd=monthly_cost,
            monthly_waste_tokens=sum(r.tokens.total for r in loop_suspects),
            recommendation="Check these sessions for retry storms or stuck tool calls. Consider adding timeout/loop-break logic.",
            evidence={
                "suspect_count": len(loop_suspects),
                "avg_messages": int(sum(r.message_count for r in loop_suspects) / len(loop_suspects)),
                "avg_output": int(sum(r.tokens.output for r in loop_suspects) / len(loop_suspects)),
            },
        )]


class AbandonedSessions(BaseDetector):
    """Detect sessions with 1-2 messages then stop (wasted startup cost)."""
    name = "abandoned_sessions"
    tier = 2
    description = "1-2 messages then abandoned (wasted context loading cost)"

    def detect(self, runs: list[AgentRun], config: dict, system: str) -> list[WasteFinding]:
        abandoned = [
            r for r in runs
            if r.message_count <= 2
            and r.tokens.total > 10_000  # Had significant context loading
            and r.run_type == "manual"  # Not a legitimate heartbeat/cron
        ]

        if len(abandoned) < 3:
            return []

        total_waste = sum(r.cost_usd for r in abandoned)
        days = max(1, len({r.timestamp.strftime("%Y-%m-%d") for r in abandoned}))
        monthly_cost = (total_waste / days) * 30

        if monthly_cost < 0.20:
            return []

        return [WasteFinding(
            system=system,
            waste_type=self.name,
            tier=self.tier,
            severity="low" if monthly_cost < 1.0 else "medium",
            confidence=0.7,
            description=f"{len(abandoned)} sessions abandoned after 1-2 messages (startup cost wasted)",
            monthly_waste_usd=monthly_cost,
            monthly_waste_tokens=sum(r.tokens.total for r in abandoned),
            recommendation="Consider batching quick questions into fewer sessions to amortize startup cost.",
            evidence={"abandoned_count": len(abandoned), "avg_input": int(sum(r.tokens.input for r in abandoned) / len(abandoned))},
        )]


# Detector registry
DETECTOR_REGISTRY: list[type[BaseDetector]] = [
    # Tier 1: Static Config
    HeartbeatModelWaste,
    HeartbeatOverFrequency,
    SkillBloat,
    ToolDefinitionBloat,
    MemoryConfigOverhead,
    StaleCronConfig,
    # Tier 2: Session Analysis
    EmptyHeartbeatRuns,
    SessionHistoryBloat,
    LoopDetection,
    AbandonedSessions,
]


# ===================================================================
# COMMANDS
# ===================================================================

def cmd_detect(args: list[str]):
    """Detect installed agent systems."""
    as_json = "--json" in args
    results = []

    for adapter_cls in ADAPTER_REGISTRY:
        adapter = adapter_cls()
        found, confidence, detail = adapter.detect()
        results.append({
            "system": adapter.name,
            "display_name": adapter.display_name,
            "found": found,
            "confidence": confidence,
            "detail": detail,
        })

    if as_json:
        print(json.dumps(results, indent=2))
        return

    found_any = False
    print("\n  Fleet Auditor: System Detection")
    print("  " + "=" * 40)
    for r in results:
        if r["found"]:
            found_any = True
            conf_label = "HIGH" if r["confidence"] >= 0.8 else "MEDIUM" if r["confidence"] >= 0.5 else "LOW"
            print(f"  {r['display_name']:15s}  [{conf_label}]  {r['detail']}")
    if not found_any:
        print("  No agent systems detected.")
        print("  Supported: Claude Code, OpenClaw, NanoClaw, Hermes, OpenCode, IronClaw")
    print()


def cmd_scan(args: list[str]):
    """Collect agent runs into fleet.db."""
    days = 30
    target_system = None
    as_json = "--json" in args
    quiet = "--quiet" in args or "-q" in args

    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass
        elif a == "--system" and i + 1 < len(args):
            target_system = args[i + 1]

    since = datetime.now(timezone.utc) - timedelta(days=days)
    conn = _init_fleet_db()

    total_new = 0
    total_errors = []
    scan_results = []

    for adapter_cls in ADAPTER_REGISTRY:
        adapter = adapter_cls()
        if target_system and adapter.name != target_system:
            continue

        found, confidence, _ = adapter.detect()
        if not found or confidence < 0.3:
            continue

        t0 = time.time()
        runs, errors = adapter.scan(since, conn)
        elapsed = time.time() - t0

        new_count = 0
        for run in runs:
            if not _is_run_collected(conn, run.system, run.source_path, run.session_id):
                _insert_run(conn, run)
                new_count += 1

        conn.commit()
        total_new += new_count
        total_errors.extend(errors)

        scan_results.append({
            "system": adapter.name,
            "scanned": len(runs),
            "new": new_count,
            "errors": len(errors),
            "elapsed_ms": int(elapsed * 1000),
        })

    # Update daily aggregates
    _update_daily_aggregates(conn)
    conn.close()

    if as_json:
        print(json.dumps({"total_new": total_new, "systems": scan_results, "errors": total_errors}, indent=2))
        return

    if not quiet:
        print(f"\n  Fleet Scan: {total_new} new runs collected ({days}-day window)")
        for sr in scan_results:
            print(f"    {sr['system']:12s}  {sr['new']:4d} new / {sr['scanned']:4d} scanned  ({sr['elapsed_ms']}ms)")
        if total_errors:
            print(f"  Warnings: {len(total_errors)}")
            for e in total_errors[:5]:
                print(f"    - {e}")
        print()


def cmd_audit(args: list[str]):
    """Run waste detection on collected data."""
    days = 30
    target_system = None
    as_json = "--json" in args
    min_confidence = 0.4

    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass
        elif a == "--system" and i + 1 < len(args):
            target_system = args[i + 1]

    # First, ensure we have data
    if not FLEET_DB.exists():
        print("  No fleet.db found. Run 'fleet.py scan' first.")
        sys.exit(1)

    conn = _init_fleet_db()

    # Load runs from DB
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = "SELECT * FROM fleet_runs WHERE date >= ?"
    params: list = [cutoff]
    if target_system:
        query += " AND system = ?"
        params.append(target_system)

    rows = conn.execute(query, params).fetchall()
    col_names = [desc[0] for desc in conn.execute(query, params).description] if rows else []

    # Reconstruct AgentRun objects
    runs_by_system: dict[str, list[AgentRun]] = {}
    for row in rows:
        rd = dict(zip(col_names, row))
        system = rd["system"]
        run = AgentRun(
            system=system,
            session_id=rd.get("session_id", ""),
            agent_name=rd.get("agent_name", ""),
            project=rd.get("project", ""),
            timestamp=datetime.fromisoformat(rd["date"]) if rd.get("date") else datetime.now(timezone.utc),
            duration_seconds=rd.get("duration_seconds", 0) or 0,
            tokens=TokenBreakdown(
                input=rd.get("input_tokens", 0) or 0,
                output=rd.get("output_tokens", 0) or 0,
                cache_read=rd.get("cache_read_tokens", 0) or 0,
                cache_write=rd.get("cache_write_tokens", 0) or 0,
            ),
            cost_usd=rd.get("cost_usd", 0) or 0,
            model=rd.get("model", ""),
            context_window_size=rd.get("context_window_size", 200_000) or 200_000,
            run_type=rd.get("run_type", "manual") or "manual",
            outcome=rd.get("outcome", "success") or "success",
            message_count=rd.get("message_count", 0) or 0,
            tools_used=[],
            source_path=rd.get("source_path", ""),
            error_message=rd.get("error_message"),
            exit_code=rd.get("exit_code"),
        )
        runs_by_system.setdefault(system, []).append(run)

    # Clear old waste findings
    conn.execute("DELETE FROM fleet_waste")

    # Run detectors per system
    all_findings: list[WasteFinding] = []

    for system, system_runs in runs_by_system.items():
        # Get config for Tier 1 detectors
        config = {}
        for adapter_cls in ADAPTER_REGISTRY:
            if adapter_cls.name == system:
                config = adapter_cls().parse_config()
                break

        for detector_cls in DETECTOR_REGISTRY:
            detector = detector_cls()
            findings = detector.detect(system_runs, config, system)
            for f in findings:
                if f.confidence >= min_confidence:
                    all_findings.append(f)
                    _insert_waste(conn, f)

    conn.commit()
    conn.close()

    if as_json:
        output = []
        for f in all_findings:
            output.append({
                "system": f.system,
                "waste_type": f.waste_type,
                "tier": f.tier,
                "severity": f.severity,
                "confidence": f.confidence,
                "description": f.description,
                "monthly_waste_usd": f.monthly_waste_usd,
                "monthly_waste_tokens": f.monthly_waste_tokens,
                "recommendation": f.recommendation,
                "fix_snippet": f.fix_snippet,
                "evidence": f.evidence,
            })
        print(json.dumps(output, indent=2))
        return

    # Text report
    print("\n  Fleet Audit: Waste Pattern Analysis")
    print("  " + "=" * 50)

    if not all_findings:
        print("  No waste patterns detected. Your fleet looks clean!")
        print()
        return

    # Sort by monthly waste (highest first), then severity
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_findings.sort(key=lambda f: (severity_rank.get(f.severity, 4), -f.monthly_waste_usd))

    total_monthly_waste = sum(f.monthly_waste_usd for f in all_findings)

    for i, f in enumerate(all_findings, 1):
        sev_marker = {"critical": "!!!", "high": "!!", "medium": "!", "low": "."}
        marker = sev_marker.get(f.severity, "")
        print(f"\n  {i}. [{f.severity.upper():8s}] {f.description}")
        print(f"     System: {f.system} | Tier {f.tier} | Confidence: {f.confidence:.0%}")
        if f.monthly_waste_usd > 0:
            print(f"     Est. waste: ${f.monthly_waste_usd:.2f}/month")
        if f.monthly_waste_tokens > 0:
            print(f"     Tokens: {f.monthly_waste_tokens:,}/month")
        print(f"     Fix: {f.recommendation}")
        if f.fix_snippet:
            for line in f.fix_snippet.split("\n"):
                print(f"       {line}")

    if total_monthly_waste > 0:
        print(f"\n  {'=' * 50}")
        print(f"  Total estimated waste: ${total_monthly_waste:.2f}/month")
    print()


def cmd_report(args: list[str]):
    """Generate a full fleet report combining scan + audit data."""
    days = 30
    target_system = None
    as_json = "--json" in args

    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass
        elif a == "--system" and i + 1 < len(args):
            target_system = args[i + 1]

    if not FLEET_DB.exists():
        print("  No fleet.db found. Run 'fleet.py scan' first.")
        sys.exit(1)

    conn = _init_fleet_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    # Summary stats
    where = "WHERE date >= ?"
    params: list = [cutoff]
    if target_system:
        where += " AND system = ?"
        params.append(target_system)

    stats = conn.execute(f"""
        SELECT
            system,
            COUNT(*) as runs,
            SUM(input_tokens) as total_input,
            SUM(output_tokens) as total_output,
            SUM(cost_usd) as total_cost,
            SUM(CASE WHEN outcome = 'empty' THEN 1 ELSE 0 END) as empty_runs,
            SUM(CASE WHEN outcome = 'abandoned' THEN 1 ELSE 0 END) as abandoned_runs,
            AVG(duration_seconds) as avg_duration,
            COUNT(DISTINCT date) as active_days
        FROM fleet_runs
        {where}
        GROUP BY system
    """, params).fetchall()

    # Waste findings
    waste_rows = conn.execute("""
        SELECT system, waste_type, severity, description, monthly_waste_usd, recommendation
        FROM fleet_waste
        ORDER BY monthly_waste_usd DESC
    """).fetchall()

    conn.close()

    if as_json:
        report = {
            "period_days": days,
            "systems": [],
            "waste_findings": [],
            "total_cost": 0,
            "total_waste": 0,
        }
        for row in stats:
            system_data = {
                "system": row[0], "runs": row[1],
                "total_input": row[2] or 0, "total_output": row[3] or 0,
                "total_cost": row[4] or 0, "empty_runs": row[5] or 0,
                "abandoned_runs": row[6] or 0, "avg_duration_s": row[7] or 0,
                "active_days": row[8] or 0,
            }
            report["systems"].append(system_data)
            report["total_cost"] += system_data["total_cost"]
        for wr in waste_rows:
            report["waste_findings"].append({
                "system": wr[0], "type": wr[1], "severity": wr[2],
                "description": wr[3], "monthly_waste_usd": wr[4] or 0,
                "recommendation": wr[5],
            })
            report["total_waste"] += wr[4] or 0
        print(json.dumps(report, indent=2))
        return

    # Text report
    print("\n  Fleet Report")
    print("  " + "=" * 60)
    print(f"  Period: last {days} days")

    total_cost = 0.0
    total_runs = 0

    for row in stats:
        system, runs, inp, out, cost, empty, abandoned, avg_dur, active_days = row
        cost = cost or 0
        total_cost += cost
        total_runs += runs

        adapter_name = system
        for ac in ADAPTER_REGISTRY:
            if ac.name == system:
                adapter_name = ac.display_name
                break

        print(f"\n  {adapter_name}")
        print(f"    Runs: {runs} ({active_days or 0} active days)")
        print(f"    Tokens: {(inp or 0):,} input / {(out or 0):,} output")
        print(f"    Cost: ${cost:.2f}")
        if empty:
            print(f"    Empty runs: {empty} ({empty/runs*100:.0f}%)")
        if abandoned:
            print(f"    Abandoned: {abandoned}")
        if avg_dur:
            print(f"    Avg duration: {avg_dur/60:.1f} min")

    print(f"\n  {'=' * 60}")
    print(f"  Total: {total_runs} runs, ${total_cost:.2f}")

    if waste_rows:
        total_waste = sum((wr[4] or 0) for wr in waste_rows)
        print(f"\n  Waste detected: ${total_waste:.2f}/month potential savings")
        print(f"  Run 'fleet.py audit' for detailed recommendations.")
    print()


def cmd_dashboard(args: list[str]):
    """Generate and open the fleet dashboard."""
    serve = "--serve" in args
    serve_port = 8080
    serve_host = "127.0.0.1"

    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            try:
                serve_port = int(args[i + 1])
            except ValueError:
                pass
        elif a == "--host" and i + 1 < len(args):
            serve_host = args[i + 1]

    if not FLEET_DB.exists():
        print("  No fleet.db found. Run 'fleet.py scan' first.")
        sys.exit(1)

    # Load data for dashboard
    conn = _init_fleet_db()
    daily_rows = conn.execute(
        "SELECT date, system, run_count, total_input, total_output, total_cost, empty_heartbeat_count "
        "FROM fleet_daily ORDER BY date"
    ).fetchall()

    waste_rows = conn.execute(
        "SELECT system, waste_type, tier, severity, confidence, description, "
        "monthly_waste_usd, monthly_waste_tokens, recommendation, fix_snippet "
        "FROM fleet_waste ORDER BY monthly_waste_usd DESC"
    ).fetchall()

    system_stats = conn.execute("""
        SELECT system, COUNT(*), SUM(cost_usd), SUM(input_tokens), SUM(output_tokens),
               AVG(duration_seconds), COUNT(DISTINCT date),
               SUM(CASE WHEN outcome='empty' THEN 1 ELSE 0 END),
               SUM(CASE WHEN outcome='abandoned' THEN 1 ELSE 0 END)
        FROM fleet_runs GROUP BY system
    """).fetchall()

    # Model mix per system
    model_mix = conn.execute("""
        SELECT system, model, COUNT(*), SUM(cost_usd)
        FROM fleet_runs WHERE model IS NOT NULL AND model != ''
        GROUP BY system, model ORDER BY SUM(cost_usd) DESC
    """).fetchall()

    # Top projects by cost
    top_projects = conn.execute("""
        SELECT project, COUNT(*), SUM(cost_usd), SUM(input_tokens + cache_read_tokens + cache_write_tokens)
        FROM fleet_runs WHERE project IS NOT NULL AND project != ''
        GROUP BY project ORDER BY SUM(cost_usd) DESC LIMIT 10
    """).fetchall()

    conn.close()

    # Generate dashboard HTML
    dashboard_html = _generate_dashboard_html(daily_rows, waste_rows, system_stats, model_mix, top_projects)

    FLEET_DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FLEET_DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(dashboard_html)

    print(f"  Fleet dashboard generated: {FLEET_DASHBOARD_PATH}")

    if serve:
        _serve_dashboard(serve_host, serve_port)
    else:
        _open_in_browser(FLEET_DASHBOARD_PATH)


def _generate_dashboard_html(daily_rows, waste_rows, system_stats, model_mix, top_projects) -> str:
    """Generate standalone fleet dashboard HTML matching Token Optimizer design system."""
    import html as html_mod

    # Prepare data
    daily_data = [{"date": r[0], "system": r[1], "runs": r[2], "input": r[3] or 0,
                   "output": r[4] or 0, "cost": r[5] or 0, "empty_hb": r[6] or 0} for r in daily_rows]

    waste_data = [{"system": r[0], "type": r[1], "tier": r[2], "severity": r[3], "confidence": r[4],
                   "description": r[5], "monthly_usd": r[6] or 0, "monthly_tokens": r[7] or 0,
                   "recommendation": r[8], "fix": r[9] or ""} for r in waste_rows]

    systems_data = [{"system": r[0], "runs": r[1], "cost": r[2] or 0, "input": r[3] or 0,
                     "output": r[4] or 0, "avg_dur": r[5] or 0, "active_days": r[6] or 0,
                     "empty": r[7] or 0, "abandoned": r[8] or 0} for r in system_stats]

    # Model mix grouped by system
    model_data = {}
    for r in model_mix:
        model_data.setdefault(r[0], []).append({"model": r[1], "runs": r[2], "cost": r[3] or 0})

    project_data = [{"project": r[0], "runs": r[1], "cost": r[2] or 0, "tokens": r[3] or 0} for r in top_projects]

    total_cost = sum(s["cost"] for s in systems_data)
    total_waste = sum(w["monthly_usd"] for w in waste_data)
    total_runs = sum(s["runs"] for s in systems_data)
    total_active_days = max((s["active_days"] for s in systems_data), default=0)
    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for w in waste_data:
        sev_counts[w["severity"]] = sev_counts.get(w["severity"], 0) + 1

    def esc(s):
        return html_mod.escape(str(s))

    def fmt_tokens(n):
        if n >= 1_000_000_000:
            return f"{n/1e9:.1f}B"
        if n >= 1_000_000:
            return f"{n/1e6:.1f}M"
        if n >= 1_000:
            return f"{n/1e3:.1f}K"
        return str(n)

    def fmt_cost(n):
        if n >= 1000:
            return f"${n:,.0f}"
        return f"${n:.2f}"

    # System display names
    sys_names = {ac.name: ac.display_name for ac in [cls() for cls in ADAPTER_REGISTRY]}

    # Daily chart data as JSON
    daily_json = json.dumps(daily_data)
    model_json = json.dumps(model_data)
    project_json = json.dumps(project_data)
    waste_json = json.dumps(waste_data)
    generated_at = datetime.now(timezone.utc).isoformat()

    # Build system cards
    sys_html = ""
    for s in systems_data:
        name = sys_names.get(s["system"], s["system"])
        pct_empty = (s["empty"] / s["runs"] * 100) if s["runs"] > 0 else 0
        sys_html += f'''
      <div class="card">
        <div class="card-header"><span>{esc(name)}</span><span class="label">{esc(s["system"])}</span></div>
        <div class="metric-large">{fmt_cost(s["cost"])}</div>
        <div class="metric-sub">{s["runs"]:,} runs over {s["active_days"]} days</div>
        <div class="mini-stats">
          <div class="mini-stat-item"><span class="mini-val">{s["avg_dur"]/60:.0f}m</span><span class="mini-label">avg duration</span></div>
          <div class="mini-stat-item"><span class="mini-val">{pct_empty:.0f}%</span><span class="mini-label">empty runs</span></div>
          <div class="mini-stat-item"><span class="mini-val">{s["abandoned"]}</span><span class="mini-label">abandoned</span></div>
        </div>
      </div>'''

    # Build waste cards
    waste_html = ""
    severity_colors = {"critical": "var(--c-danger)", "high": "var(--c-warning)", "medium": "var(--c-accent-cyan)", "low": "var(--c-text-dim)"}
    for i, w in enumerate(waste_data):
        waste_html += f'''
      <div class="waste-card {esc(w["severity"])}">
        <div class="waste-header">
          <div>
            <span class="waste-severity {esc(w["severity"])}">{esc(w["severity"])}</span>
            <span class="waste-system">{esc(sys_names.get(w["system"], w["system"]))}</span>
          </div>
          {f'<span class="waste-savings">{fmt_cost(w["monthly_usd"])}/mo</span>' if w["monthly_usd"] > 0.01 else ''}
        </div>
        <div class="waste-desc">{esc(w["description"])}</div>
        <div class="waste-rec">{esc(w["recommendation"])}</div>
        {f'<div class="waste-fix">{esc(w["fix"])}</div>' if w["fix"] else ''}
      </div>'''

    # Build project rows
    proj_html = ""
    for p in project_data[:8]:
        proj_html += f'''
        <div class="proj-row">
          <span class="proj-name">{esc(p["project"])}</span>
          <span class="proj-stat">{p["runs"]:,} runs</span>
          <span class="proj-stat">{fmt_tokens(p["tokens"])} tok</span>
          <span class="proj-cost">{fmt_cost(p["cost"])}</span>
        </div>'''

    # Main dashboard link
    main_dash = FLEET_DB_DIR / "dashboard.html"
    back_link = f'<a class="nav-item nav-back" href="file://{main_dash}">Token Optimizer</a>' if main_dash.exists() else ''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fleet Auditor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;500&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; outline: none; }}
:root {{
  --c-bg: #0a0b10;
  --c-surface: #13151a;
  --c-surface-hover: #1c1f26;
  --c-accent-cyan: #00f0ff;
  --c-accent-blue: #0066ff;
  --c-accent-glow: rgba(0, 240, 255, 0.4);
  --c-text-main: #ffffff;
  --c-text-dim: #7d8ca3;
  --c-border: rgba(255, 255, 255, 0.08);
  --c-success: #22c55e;
  --c-warning: #f59e0b;
  --c-danger: #ef4444;
  --font-sans: 'Space Grotesk', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --s-1: 4px; --s-2: 8px; --s-3: 16px; --s-4: 24px; --s-5: 32px; --s-6: 64px;
  --glow-sm: 0 0 10px var(--c-accent-glow);
  --glow-text: 0 0 8px rgba(0, 240, 255, 0.6);
}}
body {{
  background-color: var(--c-bg);
  color: var(--c-text-main);
  font-family: var(--font-sans);
  font-weight: 300;
  font-size: 18px;
  line-height: 1.5;
  min-height: 100vh;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
  background-image: radial-gradient(circle at 50% 0%, #1a253a 0%, var(--c-bg) 60%);
}}
body::before {{
  content: "";
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background-image:
    linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
  z-index: 0;
}}
h1, h2, h3, h4 {{ font-weight: 400; }}
::-webkit-scrollbar {{ width: 6px; }}
::-webkit-scrollbar-track {{ background: var(--c-bg); }}
::-webkit-scrollbar-thumb {{ background: #333; border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: #555; }}

.layout {{
  display: grid;
  grid-template-columns: 260px 1fr 340px;
  height: 100vh;
  width: 100vw;
  max-width: 1800px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}

/* NAV */
.nav-col {{
  padding: var(--s-4);
  border-right: 1px solid var(--c-border);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(90deg, transparent, rgba(0,0,0,0.2));
  backdrop-filter: blur(10px);
}}
.brand {{
  font-family: var(--font-sans);
  font-weight: 700;
  font-size: 24px;
  margin-bottom: var(--s-5);
  display: flex;
  align-items: center;
  gap: var(--s-2);
  color: var(--c-text-main);
  letter-spacing: -0.02em;
}}
.brand span {{
  width: 12px; height: 12px;
  background: var(--c-accent-cyan);
  border-radius: 2px;
  display: inline-block;
  box-shadow: var(--glow-sm);
}}
.nav-menu {{ display: flex; flex-direction: column; gap: 2px; }}
.nav-item {{
  padding: 12px var(--s-2);
  cursor: pointer;
  opacity: 0.6;
  transition: all 0.3s cubic-bezier(0.2, 0.8, 0.2, 1);
  text-decoration: none;
  color: var(--c-text-main);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: var(--font-sans);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 15px;
  border-left: 2px solid transparent;
}}
.nav-item:hover, .nav-item.active {{
  opacity: 1;
  background: linear-gradient(90deg, rgba(0,240,255,0.05), transparent);
  border-left-color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}}
.nav-item.active {{ font-weight: 600; }}
.nav-badge {{
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--c-accent-cyan);
  border: 1px solid var(--c-accent-cyan);
  padding: 0 4px;
  border-radius: 2px;
  text-shadow: 0 0 5px var(--c-accent-cyan);
}}
.nav-separator {{
  height: 1px;
  background: var(--c-border);
  margin: var(--s-2) 0;
}}
.nav-back {{
  opacity: 0.4;
  font-size: 13px;
  margin-top: var(--s-3);
  border-left: 2px solid transparent;
}}
.nav-back:hover {{ opacity: 0.8; border-left-color: var(--c-text-dim); }}
.user-profile {{
  font-size: 15px;
  color: var(--c-text-dim);
  border-top: 1px solid var(--c-border);
  padding-top: var(--s-3);
  font-family: var(--font-mono);
}}
.user-profile i {{ font-style: italic; color: var(--c-accent-cyan); }}

/* MAIN */
.main-col {{
  padding: var(--s-5);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--s-5);
}}
.view {{ display: none; }}
.view.active {{ display: flex; flex-direction: column; gap: var(--s-5); }}
.section-header {{ margin-bottom: var(--s-2); }}
.label {{
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  font-weight: 500;
}}
.section-header h1 {{
  font-family: var(--font-sans);
  font-weight: 300;
  font-size: 48px;
  margin: var(--s-2) 0;
  line-height: 1;
  letter-spacing: -0.02em;
  background: linear-gradient(180deg, #fff, #aaa);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.section-header p {{
  font-size: 12px;
  color: var(--c-text-dim);
  max-width: 460px;
  line-height: 1.6;
  font-family: var(--font-mono);
}}

/* CARDS */
.card {{
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  transition: border-color 0.3s;
}}
.card:hover {{ border-color: rgba(255,255,255,0.15); }}
.card-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--s-3);
  font-size: 15px;
}}
.card-header span:first-child {{ font-weight: 500; }}
.dashboard-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--s-3);
}}
.metric-large {{
  font-family: var(--font-mono);
  font-size: 36px;
  font-weight: 500;
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
  margin-bottom: var(--s-1);
}}
.metric-sub {{
  font-size: 13px;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
}}
.mini-stats {{
  display: flex;
  gap: var(--s-4);
  margin-top: var(--s-3);
  padding-top: var(--s-3);
  border-top: 1px solid var(--c-border);
}}
.mini-stat-item {{ display: flex; flex-direction: column; }}
.mini-val {{
  font-family: var(--font-mono);
  font-size: 16px;
  color: var(--c-text-main);
}}
.mini-label {{
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--c-text-dim);
}}

/* HERO STATS */
.stat-row {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--s-3);
  margin-bottom: var(--s-4);
}}
.stat-card {{
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  text-align: center;
}}
.stat-card-value {{
  font-family: var(--font-mono);
  font-size: 32px;
  font-weight: 500;
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}}
.stat-card-label {{
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--c-text-dim);
  margin-top: var(--s-1);
}}

/* WASTE */
.waste-card {{
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: var(--s-4);
  margin-bottom: var(--s-3);
  border-left: 3px solid var(--c-text-dim);
  transition: border-color 0.3s;
}}
.waste-card.critical {{ border-left-color: var(--c-danger); }}
.waste-card.high {{ border-left-color: var(--c-warning); }}
.waste-card.medium {{ border-left-color: var(--c-accent-cyan); }}
.waste-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--s-2);
}}
.waste-severity {{
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 2px 8px;
  border-radius: 3px;
  margin-right: var(--s-2);
}}
.waste-severity.critical {{ background: rgba(239,68,68,0.2); color: var(--c-danger); }}
.waste-severity.high {{ background: rgba(245,158,11,0.2); color: var(--c-warning); }}
.waste-severity.medium {{ background: rgba(0,240,255,0.1); color: var(--c-accent-cyan); }}
.waste-severity.low {{ background: rgba(125,140,163,0.1); color: var(--c-text-dim); }}
.waste-system {{ font-size: 13px; color: var(--c-text-dim); }}
.waste-desc {{ font-size: 16px; margin-bottom: var(--s-2); }}
.waste-rec {{ font-size: 14px; color: var(--c-text-dim); margin-bottom: var(--s-2); }}
.waste-savings {{
  font-family: var(--font-mono);
  font-size: 16px;
  color: var(--c-success);
  text-shadow: 0 0 6px rgba(34,197,94,0.4);
}}
.waste-fix {{
  margin-top: var(--s-2);
  padding: var(--s-2) var(--s-3);
  background: rgba(0,0,0,0.3);
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--c-text-dim);
  white-space: pre-wrap;
  line-height: 1.6;
}}

/* CHART (daily cost bars) */
.chart-container {{ padding: var(--s-2) 0; }}
.bar-chart {{
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 120px;
  padding-top: var(--s-2);
}}
.bar {{
  flex: 1;
  background: var(--c-accent-cyan);
  border-radius: 2px 2px 0 0;
  min-width: 4px;
  opacity: 0.7;
  transition: opacity 0.2s;
  position: relative;
}}
.bar:hover {{ opacity: 1; }}
.bar-label {{
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  margin-top: var(--s-1);
}}

/* PROJECTS */
.proj-row {{
  display: flex;
  align-items: center;
  gap: var(--s-3);
  padding: var(--s-2) 0;
  border-bottom: 1px solid var(--c-border);
  font-size: 14px;
}}
.proj-row:last-child {{ border-bottom: none; }}
.proj-name {{ flex: 1; font-weight: 400; }}
.proj-stat {{ color: var(--c-text-dim); font-family: var(--font-mono); font-size: 13px; min-width: 80px; text-align: right; }}
.proj-cost {{ font-family: var(--font-mono); font-size: 13px; color: var(--c-accent-cyan); min-width: 70px; text-align: right; }}

/* MODEL BAR */
.model-bar {{
  display: flex;
  height: 24px;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: var(--s-2);
}}
.model-segment {{ transition: width 0.5s; }}
.model-segment.opus {{ background: #a855f7; }}
.model-segment.sonnet {{ background: #3b82f6; }}
.model-segment.haiku {{ background: #22c55e; }}
.model-segment.other {{ background: #6b7280; }}
.model-legend {{
  display: flex;
  gap: var(--s-3);
  flex-wrap: wrap;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--c-text-dim);
}}
.model-legend-item {{ display: flex; align-items: center; gap: 6px; }}
.model-legend-dot {{ width: 8px; height: 8px; border-radius: 2px; }}
.model-legend-dot.opus {{ background: #a855f7; }}
.model-legend-dot.sonnet {{ background: #3b82f6; }}
.model-legend-dot.haiku {{ background: #22c55e; }}
.model-legend-dot.other {{ background: #6b7280; }}

/* RIGHT PANEL */
.config-col {{
  border-left: 1px solid var(--c-border);
  padding: var(--s-4);
  background: rgba(10,11,16,0.8);
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
  overflow-y: auto;
}}
.section-title {{
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  margin-bottom: var(--s-3);
  color: var(--c-text-dim);
  padding-bottom: var(--s-1);
  border-bottom: 1px solid var(--c-border);
  font-family: var(--font-mono);
}}
.summary-metric {{
  font-family: var(--font-mono);
  font-size: 18px;
  color: var(--c-text-dim);
  margin-bottom: var(--s-3);
}}
.summary-metric .num {{
  color: var(--c-accent-cyan);
  text-shadow: var(--glow-text);
}}
.version-footer {{
  margin-top: auto;
  padding-top: var(--s-4);
  border-top: 1px solid var(--c-border);
  font-size: 13px;
  color: var(--c-text-dim);
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.version-footer a {{ color: var(--c-accent-cyan); text-decoration: none; }}
.social-icons {{ display: flex; gap: var(--s-2); }}
.social-link {{ color: var(--c-text-dim); transition: color 0.2s; }}
.social-link:hover {{ color: var(--c-accent-cyan); }}

.empty-state {{
  text-align: center;
  padding: var(--s-6) var(--s-4);
  color: var(--c-text-dim);
  font-family: var(--font-mono);
  font-size: 14px;
}}

@media (max-width: 1100px) {{
  .layout {{ grid-template-columns: 1fr; height: auto; }}
  .nav-col {{ display: none; }}
  .config-col {{ display: none; }}
  .stat-row {{ grid-template-columns: repeat(2, 1fr); }}
}}
</style>
</head>
<body>

<div class="layout">
  <!-- NAV -->
  <nav class="nav-col">
    <div>
      <div class="brand"><span></span> Fleet Auditor</div>
      <div class="nav-menu">
        <a class="nav-item active" data-view="overview">Overview</a>
        <a class="nav-item" data-view="waste">Waste <span class="nav-badge">{len(waste_data)}</span></a>
        <a class="nav-item" data-view="systems">Systems</a>
        <a class="nav-item" data-view="daily">Daily</a>
        <div class="nav-separator"></div>
        {back_link}
      </div>
    </div>
    <div class="user-profile">generated: <i>{datetime.now().strftime("%Y-%m-%d %H:%M")}</i></div>
  </nav>

  <!-- MAIN -->
  <main class="main-col">
    <!-- Overview -->
    <div class="view active" id="view-overview">
      <div class="section-header">
        <div class="label">Fleet Token Audit</div>
        <h1>Overview</h1>
        <p>Cross-platform agent token usage and waste detection.</p>
      </div>

      <div class="stat-row">
        <div class="stat-card">
          <div class="stat-card-value">{total_runs:,}</div>
          <div class="stat-card-label">Total Runs</div>
        </div>
        <div class="stat-card">
          <div class="stat-card-value">{fmt_cost(total_cost)}</div>
          <div class="stat-card-label">Total Cost</div>
        </div>
        <div class="stat-card">
          <div class="stat-card-value">{len(systems_data)}</div>
          <div class="stat-card-label">Systems</div>
        </div>
        <div class="stat-card">
          <div class="stat-card-value" style="color: var(--c-success)">{fmt_cost(total_waste)}/mo</div>
          <div class="stat-card-label">Potential Savings</div>
        </div>
      </div>

      <div class="dashboard-grid">
        {sys_html if sys_html else '<div class="empty-state">No systems scanned yet.<br>Run: python3 fleet.py scan</div>'}
      </div>
    </div>

    <!-- Waste -->
    <div class="view" id="view-waste">
      <div class="section-header">
        <div class="label">Waste Detection</div>
        <h1>Findings</h1>
        <p>Identified waste patterns with dollar savings estimates and fix snippets.</p>
      </div>
      {waste_html if waste_html else '<div class="empty-state">No waste patterns detected.<br>Run: python3 fleet.py audit</div>'}
    </div>

    <!-- Systems -->
    <div class="view" id="view-systems">
      <div class="section-header">
        <div class="label">System Breakdown</div>
        <h1>Systems</h1>
        <p>Per-system usage, model mix, and top projects.</p>
      </div>
      <div class="dashboard-grid">
        {sys_html if sys_html else '<div class="empty-state">No data yet.</div>'}
      </div>
      <div class="card" id="model-mix-card" style="margin-top: var(--s-3);">
        <div class="card-header"><span>Model Mix</span><span class="label">Cost distribution by model</span></div>
        <div id="model-mix-content"></div>
      </div>
      <div class="card" style="margin-top: var(--s-3);">
        <div class="card-header"><span>Top Projects</span><span class="label">By total cost</span></div>
        {proj_html if proj_html else '<div class="empty-state" style="padding: var(--s-3);">No project data.</div>'}
      </div>
    </div>

    <!-- Daily -->
    <div class="view" id="view-daily">
      <div class="section-header">
        <div class="label">Time Series</div>
        <h1>Daily</h1>
        <p>Daily cost and run count over the scan window.</p>
      </div>
      <div class="card">
        <div class="card-header"><span>Daily Cost</span></div>
        <div class="chart-container">
          <div class="bar-chart" id="daily-chart"></div>
          <div class="bar-label" id="daily-labels"></div>
        </div>
      </div>
      <div class="card" style="margin-top: var(--s-3);">
        <div class="card-header"><span>Daily Runs</span></div>
        <div class="chart-container">
          <div class="bar-chart" id="runs-chart"></div>
          <div class="bar-label" id="runs-labels"></div>
        </div>
      </div>
    </div>
  </main>

  <!-- RIGHT PANEL -->
  <aside class="config-col">
    <div class="section-title">Fleet Summary</div>
    <div class="summary-metric"><span class="num">{total_runs:,}</span> runs</div>
    <div class="summary-metric"><span class="num">{fmt_cost(total_cost)}</span> total</div>
    <div class="summary-metric"><span class="num">{total_active_days}</span> active days</div>

    <div class="section-title">Waste Breakdown</div>
    <div class="summary-metric" style="font-size:14px;">
      {"".join(f'<div style="margin-bottom:4px;"><span style="color:{severity_colors.get(sev, "var(--c-text-dim)")}">{count}</span> {sev}</div>' for sev, count in sev_counts.items() if count > 0) or '<span style="color:var(--c-success)">Clean fleet</span>'}
    </div>
    <div class="summary-metric">
      Potential savings: <span class="num">{fmt_cost(total_waste)}/mo</span>
    </div>

    <div class="section-title">Quick Commands</div>
    <div style="font-family: var(--font-mono); font-size: 12px; color: var(--c-text-dim); line-height: 2;">
      <div>fleet.py detect</div>
      <div>fleet.py scan --days 30</div>
      <div>fleet.py audit --json</div>
      <div>fleet.py report</div>
      <div>fleet.py dashboard</div>
    </div>

    <div class="version-footer">
      <div>Built by <a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener">Alex Greenshpun</a></div>
      <div class="social-icons">
        <a href="https://github.com/alexgreensh/token-optimizer" target="_blank" rel="noopener" title="GitHub" class="social-link">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
        </a>
        <a href="https://linkedin.com/in/alexgreensh" target="_blank" rel="noopener" title="LinkedIn" class="social-link">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
        </a>
      </div>
    </div>
  </aside>
</div>

<script>
var dailyData = {daily_json};
var modelData = {model_json};

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function fmtCost(n) {{ return n >= 1000 ? '$' + n.toLocaleString(undefined, {{maximumFractionDigits:0}}) : '$' + n.toFixed(2); }}

// Nav switching
document.querySelectorAll('.nav-item[data-view]').forEach(function(lnk) {{
  lnk.addEventListener('click', function(e) {{
    e.preventDefault();
    var v = this.getAttribute('data-view');
    document.querySelectorAll('.view').forEach(function(el) {{ el.classList.remove('active'); }});
    document.querySelectorAll('.nav-item').forEach(function(n) {{ n.classList.remove('active'); }});
    var target = document.getElementById('view-' + v);
    if (target) target.classList.add('active');
    this.classList.add('active');
    document.querySelector('.main-col').scrollTop = 0;
  }});
}});

// Daily cost chart
(function() {{
  var chart = document.getElementById('daily-chart');
  var labels = document.getElementById('daily-labels');
  if (!chart || !dailyData.length) return;

  // Aggregate by date
  var byDate = {{}};
  dailyData.forEach(function(d) {{
    byDate[d.date] = (byDate[d.date] || 0) + d.cost;
  }});
  var dates = Object.keys(byDate).sort();
  var maxCost = Math.max.apply(null, dates.map(function(d) {{ return byDate[d]; }})) || 1;

  dates.forEach(function(date) {{
    var bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = Math.max(2, (byDate[date] / maxCost) * 100) + '%';
    bar.title = date + ': ' + fmtCost(byDate[date]);
    chart.appendChild(bar);
  }});

  if (dates.length > 0) {{
    labels.innerHTML = '<span>' + dates[0] + '</span><span>' + dates[dates.length - 1] + '</span>';
  }}

  // Runs chart
  var runsChart = document.getElementById('runs-chart');
  var runsLabels = document.getElementById('runs-labels');
  if (!runsChart) return;
  var byDateRuns = {{}};
  dailyData.forEach(function(d) {{
    byDateRuns[d.date] = (byDateRuns[d.date] || 0) + d.runs;
  }});
  var maxRuns = Math.max.apply(null, dates.map(function(d) {{ return byDateRuns[d] || 0; }})) || 1;
  dates.forEach(function(date) {{
    var bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = Math.max(2, ((byDateRuns[date] || 0) / maxRuns) * 100) + '%';
    bar.style.background = '#3b82f6';
    bar.title = date + ': ' + (byDateRuns[date] || 0) + ' runs';
    runsChart.appendChild(bar);
  }});
  if (dates.length > 0) {{
    runsLabels.innerHTML = '<span>' + dates[0] + '</span><span>' + dates[dates.length - 1] + '</span>';
  }}
}})();

// Model mix
(function() {{
  var container = document.getElementById('model-mix-content');
  if (!container) return;
  var html = '';
  var modelColors = {{ opus: '#a855f7', sonnet: '#3b82f6', haiku: '#22c55e' }};

  for (var sys in modelData) {{
    var models = modelData[sys];
    var totalCost = 0;
    models.forEach(function(m) {{ totalCost += m.cost; }});
    if (totalCost <= 0) continue;

    html += '<div style="margin-bottom: var(--s-3);"><div style="font-size:13px;color:var(--c-text-dim);margin-bottom:4px;">' + esc(sys) + '</div>';
    html += '<div class="model-bar">';
    models.forEach(function(m) {{
      var pct = (m.cost / totalCost * 100).toFixed(1);
      var cls = m.model.replace(/[^a-z0-9-]/g, '');
      html += '<div class="model-segment ' + cls + '" style="width:' + pct + '%" title="' + esc(m.model) + ': ' + pct + '% (' + fmtCost(m.cost) + ')"></div>';
    }});
    html += '</div><div class="model-legend">';
    models.forEach(function(m) {{
      var pct = (m.cost / totalCost * 100).toFixed(0);
      var cls = m.model.replace(/[^a-z0-9-]/g, '');
      html += '<div class="model-legend-item"><div class="model-legend-dot ' + cls + '"></div>' + esc(m.model) + ' ' + pct + '% (' + fmtCost(m.cost) + ')</div>';
    }});
    html += '</div></div>';
  }}

  container.innerHTML = html || '<div class="empty-state" style="padding:var(--s-3);">No model data.</div>';
}})();
</script>
</body>
</html>'''


def _open_in_browser(path: Path):
    """Open a file in the default browser."""
    import subprocess
    import platform as _platform
    system = _platform.system()
    if system == "Darwin":
        subprocess.run(["open", str(path)], check=False)
    elif system == "Linux":
        subprocess.run(["xdg-open", str(path)], check=False)
    elif system == "Windows":
        subprocess.run(["start", str(path)], shell=True, check=False)


def _serve_dashboard(host: str, port: int):
    """Serve the fleet dashboard over HTTP."""
    import http.server
    import functools

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(FLEET_DASHBOARD_PATH.parent),
    )
    with http.server.HTTPServer((host, port), handler) as server:
        print(f"  Serving fleet dashboard at http://{host}:{port}/fleet-dashboard.html")
        print("  Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server stopped.")


# ===================================================================
# CLI ENTRY POINT
# ===================================================================

def fleet_cli(args: list[str] | None = None):
    """Main CLI entry point. Can be called from measure.py via lazy import."""
    if args is None:
        args = sys.argv[1:]

    if not args or args[0] == "--help" or args[0] == "-h":
        print("""
  Fleet Auditor: Cross-Platform Agent Token Waste Auditor

  Commands:
    detect                         Detect installed agent systems
    scan [--system X] [--days 30]  Collect agent runs into fleet.db
    audit [--system X] [--days 30] Run waste pattern detection
    report [--system X] [--json]   Full report with cost breakdown
    dashboard [--serve]            Generate and open fleet dashboard

  Flags:
    --json          Output as JSON
    --days N        Look back N days (default: 30)
    --system X      Filter to one system (claude, openclaw, hermes, etc.)
    --serve         Start HTTP server for dashboard
    --quiet, -q     Suppress non-essential output
""")
        return

    cmd = args[0]
    cmd_args = args[1:]

    if cmd == "detect":
        cmd_detect(cmd_args)
    elif cmd == "scan":
        cmd_scan(cmd_args)
    elif cmd == "audit":
        cmd_audit(cmd_args)
    elif cmd == "report":
        cmd_report(cmd_args)
    elif cmd == "dashboard":
        cmd_dashboard(cmd_args)
    else:
        print(f"  Unknown command: {cmd}")
        print("  Run 'fleet.py --help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    fleet_cli()
