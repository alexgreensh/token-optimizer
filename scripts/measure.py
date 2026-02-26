#!/usr/bin/env python3
"""
Token Overhead Measurement Script
Captures real token counts from Claude Code session logs + file-level estimates.
Used by Token Optimizer skill in Phase 0 (before) and Phase 5 (after).

Usage:
    python3 measure.py snapshot before    # Save pre-optimization snapshot
    python3 measure.py snapshot after     # Save post-optimization snapshot
    python3 measure.py compare            # Compare before vs after
    python3 measure.py report             # Full standalone report

Snapshots are saved to SNAPSHOT_DIR (default: ~/.claude/_backups/token-optimizer/)

Copyright (C) 2026 Alex Greenshpun
SPDX-License-Identifier: AGPL-3.0-only
"""

import json
import os
import glob
import sys
from datetime import datetime
from pathlib import Path

CHARS_PER_TOKEN_PROSE = 4.0
CHARS_PER_TOKEN_YAML = 3.5

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SNAPSHOT_DIR = Path(
    os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(CLAUDE_DIR / "_backups" / "token-optimizer"))
)


def estimate_tokens(filepath):
    """Estimate tokens from file size (~4 chars per token)."""
    try:
        size = os.path.getsize(filepath)
        return int(size / CHARS_PER_TOKEN_PROSE)
    except (FileNotFoundError, PermissionError):
        return 0


def count_lines(filepath):
    try:
        with open(filepath, "r") as f:
            return sum(1 for _ in f)
    except (FileNotFoundError, PermissionError):
        return 0


def find_projects_dir():
    """Find the Claude Code projects directory for JSONL logs."""
    projects_base = CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None
    dirs = [d for d in projects_base.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def get_session_baselines(limit=10):
    """Extract first-message token counts from recent JSONL session logs."""
    projects_dir = find_projects_dir()
    if not projects_dir:
        return []

    jsonl_files = sorted(
        glob.glob(str(projects_dir / "*.jsonl")),
        key=os.path.getmtime,
        reverse=True,
    )

    baselines = []
    for jf in jsonl_files[:limit]:
        mtime = os.path.getmtime(jf)
        first_usage = None
        with open(jf, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    if "message" in data and isinstance(data["message"], dict):
                        msg = data["message"]
                        if "usage" in msg:
                            u = msg["usage"]
                            first_usage = (
                                u.get("input_tokens", 0)
                                + u.get("cache_creation_input_tokens", 0)
                                + u.get("cache_read_input_tokens", 0)
                            )
                            break
                except Exception:
                    pass

        if first_usage:
            baselines.append({
                "date": datetime.fromtimestamp(mtime).isoformat(),
                "baseline_tokens": first_usage,
            })

    return baselines


def measure_components():
    """Measure all controllable token overhead components."""
    components = {}

    # CLAUDE.md files
    for name, path in [
        ("claude_md_global", CLAUDE_DIR / "CLAUDE.md"),
        ("claude_md_home", HOME / "CLAUDE.md"),
    ]:
        components[name] = {
            "path": str(path),
            "exists": path.exists(),
            "tokens": estimate_tokens(path),
            "lines": count_lines(path),
        }

    # Find project CLAUDE.md files in cwd and parents
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents)[:3]:
        claude_md = parent / "CLAUDE.md"
        if claude_md.exists() and str(claude_md) != str(HOME / "CLAUDE.md"):
            components[f"claude_md_project_{parent.name}"] = {
                "path": str(claude_md),
                "exists": True,
                "tokens": estimate_tokens(claude_md),
                "lines": count_lines(claude_md),
            }

    # MEMORY.md
    projects_dir = find_projects_dir()
    if projects_dir:
        memory_path = projects_dir / "memory" / "MEMORY.md"
        components["memory_md"] = {
            "path": str(memory_path),
            "exists": memory_path.exists(),
            "tokens": estimate_tokens(memory_path) if memory_path.exists() else 0,
            "lines": count_lines(memory_path) if memory_path.exists() else 0,
        }

    # Skills
    skills_dir = CLAUDE_DIR / "skills"
    skill_count = 0
    skill_names = []
    if skills_dir.exists():
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").exists():
                skill_count += 1
                skill_names.append(item.name)
    components["skills"] = {
        "count": skill_count,
        "tokens": skill_count * 100,
        "names": skill_names,
    }

    # Commands
    commands_dir = CLAUDE_DIR / "commands"
    cmd_count = 0
    cmd_names = []
    if commands_dir.exists():
        for f in sorted(commands_dir.glob("*.md")):
            cmd_count += 1
            cmd_names.append(f.stem)
        for subdir in sorted(commands_dir.iterdir()):
            if subdir.is_dir():
                for f in sorted(subdir.glob("*.md")):
                    cmd_count += 1
                    cmd_names.append(f"{subdir.name}/{f.stem}")
    components["commands"] = {
        "count": cmd_count,
        "tokens": cmd_count * 50,
        "names": cmd_names,
    }

    # MCP config
    mcp_configs = [
        HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        HOME / ".config" / "claude" / "user_config.json",
    ]
    mcp_servers = 0
    for config_path in mcp_configs:
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
                servers = config.get("mcpServers", config.get("mcp_servers", {}))
                mcp_servers += len(servers)
            except Exception:
                pass
    components["mcp_servers"] = {
        "count": mcp_servers,
        "note": "Server count from config files (deferred tool count varies)",
    }

    # .claudeignore
    claudeignore = CLAUDE_DIR / ".claudeignore"
    components["claudeignore"] = {
        "exists": claudeignore.exists(),
        "lines": count_lines(claudeignore) if claudeignore.exists() else 0,
    }

    # Hooks
    settings_path = CLAUDE_DIR / "settings.json"
    hooks_configured = False
    hook_names = []
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            if hooks:
                hooks_configured = True
                hook_names = list(hooks.keys())
        except Exception:
            pass
    components["hooks"] = {
        "configured": hooks_configured,
        "names": hook_names,
    }

    # Fixed overhead
    components["core_system"] = {
        "tokens": 12200,
        "note": "System prompt (~2,800) + built-in tools (~9,400). Fixed, cannot change.",
    }

    return components


def calculate_totals(components):
    """Calculate total controllable and estimated overhead."""
    controllable = 0
    fixed = 0

    for name, info in components.items():
        tokens = info.get("tokens", 0)
        if name == "core_system":
            fixed += tokens
        else:
            controllable += tokens

    return {
        "controllable_tokens": controllable,
        "fixed_tokens": fixed,
        "estimated_total": controllable + fixed,
    }


def take_snapshot(label):
    """Save a measurement snapshot (before or after)."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    components = measure_components()
    baselines = get_session_baselines(5)
    totals = calculate_totals(components)

    snapshot = {
        "label": label,
        "timestamp": datetime.now().isoformat(),
        "components": components,
        "session_baselines": baselines,
        "totals": totals,
    }

    filepath = SNAPSHOT_DIR / f"snapshot_{label}.json"
    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"\n[Token Optimizer] Snapshot '{label}' saved to {filepath}")
    print_snapshot_summary(snapshot)
    return snapshot


def print_snapshot_summary(snapshot):
    """Print a human-readable summary of a snapshot."""
    c = snapshot["components"]
    t = snapshot["totals"]

    print(f"\n{'─' * 55}")
    print(f"  Snapshot: {snapshot['label']} ({snapshot['timestamp'][:16]})")
    print(f"{'─' * 55}")

    # CLAUDE.md files
    claude_total = 0
    for key in c:
        if key.startswith("claude_md"):
            tokens = c[key].get("tokens", 0)
            if tokens > 0:
                claude_total += tokens
                lines = c[key].get("lines", 0)
                print(f"  {key:<35s} {tokens:>6,} tokens  [{lines} lines]")
    if claude_total == 0:
        print(f"  {'CLAUDE.md':<35s}     0 tokens  [not found]")

    # MEMORY.md
    if "memory_md" in c:
        mem = c["memory_md"]
        print(f"  {'MEMORY.md':<35s} {mem.get('tokens', 0):>6,} tokens  [{mem.get('lines', 0)} lines]")

    # Skills
    s = c.get("skills", {})
    print(f"  {'Skills':<35s} {s.get('tokens', 0):>6,} tokens  [{s.get('count', 0)} skills]")

    # Commands
    cmd = c.get("commands", {})
    print(f"  {'Commands':<35s} {cmd.get('tokens', 0):>6,} tokens  [{cmd.get('count', 0)} commands]")

    # Core
    core = c.get("core_system", {})
    print(f"  {'Core system (fixed)':<35s} {core.get('tokens', 0):>6,} tokens")

    print(f"  {'─' * 50}")
    print(f"  {'ESTIMATED FILE-LEVEL TOTAL':<35s} {t['estimated_total']:>6,} tokens")

    # Session baselines
    baselines = snapshot.get("session_baselines", [])
    if baselines:
        avg = sum(b["baseline_tokens"] for b in baselines) / len(baselines)
        print(f"\n  Real session baseline (avg of {len(baselines)}): {avg:,.0f} tokens")
        print(f"  (includes MCP deferred tools, system reminders, etc.)")

    # Extras
    ignore = c.get("claudeignore", {})
    hooks = c.get("hooks", {})
    print(f"\n  .claudeignore: {'Yes' if ignore.get('exists') else 'MISSING'}")
    print(f"  Hooks: {', '.join(hooks.get('names', [])) if hooks.get('configured') else 'NONE'}")


def compare_snapshots():
    """Compare before and after snapshots."""
    before_path = SNAPSHOT_DIR / "snapshot_before.json"
    after_path = SNAPSHOT_DIR / "snapshot_after.json"

    if not before_path.exists():
        print("\n[Error] No 'before' snapshot found. Run: python3 measure.py snapshot before")
        return

    if not after_path.exists():
        print("\n[Error] No 'after' snapshot found. Run: python3 measure.py snapshot after")
        return

    with open(before_path) as f:
        before = json.load(f)
    with open(after_path) as f:
        after = json.load(f)

    bc = before["components"]
    ac = after["components"]

    print(f"\n{'=' * 65}")
    print(f"  TOKEN OPTIMIZER - BEFORE vs AFTER")
    print(f"  Before: {before['timestamp'][:16]}")
    print(f"  After:  {after['timestamp'][:16]}")
    print(f"{'=' * 65}")

    print(f"\n  {'Component':<25s} {'Before':>8s} {'After':>8s} {'Saved':>8s} {'%':>6s}")
    print(f"  {'─' * 57}")

    rows = []

    # CLAUDE.md total
    before_claude = sum(
        bc[k].get("tokens", 0) for k in bc if k.startswith("claude_md")
    )
    after_claude = sum(
        ac[k].get("tokens", 0) for k in ac if k.startswith("claude_md")
    )
    rows.append(("CLAUDE.md (all)", before_claude, after_claude))

    # MEMORY.md
    rows.append((
        "MEMORY.md",
        bc.get("memory_md", {}).get("tokens", 0),
        ac.get("memory_md", {}).get("tokens", 0),
    ))

    # Skills
    rows.append((
        "Skills",
        bc.get("skills", {}).get("tokens", 0),
        ac.get("skills", {}).get("tokens", 0),
    ))

    # Commands
    rows.append((
        "Commands",
        bc.get("commands", {}).get("tokens", 0),
        ac.get("commands", {}).get("tokens", 0),
    ))

    # Core (fixed)
    rows.append((
        "Core system (fixed)",
        bc.get("core_system", {}).get("tokens", 0),
        ac.get("core_system", {}).get("tokens", 0),
    ))

    total_before = 0
    total_after = 0
    total_saved = 0

    for name, bv, av in rows:
        saved = bv - av
        pct = f"{saved / bv * 100:.0f}%" if bv > 0 else "-"
        total_before += bv
        total_after += av
        total_saved += saved
        print(f"  {name:<25s} {bv:>7,} {av:>7,} {saved:>+7,} {pct:>6s}")

    print(f"  {'─' * 57}")
    total_pct = f"{total_saved / total_before * 100:.0f}%" if total_before > 0 else "-"
    print(f"  {'TOTAL':<25s} {total_before:>7,} {total_after:>7,} {total_saved:>+7,} {total_pct:>6s}")

    # .claudeignore and hooks changes
    print(f"\n  .claudeignore: {'MISSING' if not bc.get('claudeignore', {}).get('exists') else 'Yes'} -> {'MISSING' if not ac.get('claudeignore', {}).get('exists') else 'Yes'}")
    bh = bc.get("hooks", {})
    ah = ac.get("hooks", {})
    print(f"  Hooks: {'None' if not bh.get('configured') else ', '.join(bh.get('names', []))} -> {'None' if not ah.get('configured') else ', '.join(ah.get('names', []))}")

    # Archived skills
    before_skills = set(bc.get("skills", {}).get("names", []))
    after_skills = set(ac.get("skills", {}).get("names", []))
    archived = before_skills - after_skills
    if archived:
        print(f"\n  Skills archived: {', '.join(sorted(archived))}")

    # Archived commands
    before_cmds = set(bc.get("commands", {}).get("names", []))
    after_cmds = set(ac.get("commands", {}).get("names", []))
    archived_cmds = before_cmds - after_cmds
    if archived_cmds:
        print(f"  Commands archived: {', '.join(sorted(archived_cmds))}")

    # Session baseline comparison
    bb = before.get("session_baselines", [])
    ab = after.get("session_baselines", [])
    if bb and ab:
        avg_before = sum(b["baseline_tokens"] for b in bb) / len(bb)
        avg_after = sum(b["baseline_tokens"] for b in ab) / len(ab)
        print(f"\n  Real session baseline: {avg_before:,.0f} -> {avg_after:,.0f} tokens")

    # Cost estimate
    if total_saved > 0:
        daily_msgs = 100
        monthly_msgs = daily_msgs * 30
        monthly_token_savings = total_saved * monthly_msgs
        monthly_cost_savings = monthly_token_savings * 15 / 1_000_000
        print(f"\n  At {daily_msgs} messages/day:")
        print(f"    Monthly token savings: {monthly_token_savings:,.0f}")
        print(f"    Monthly cost savings:  ~${monthly_cost_savings:.2f} (Opus input pricing)")

    print(f"\n{'=' * 65}")


def full_report():
    """Print a standalone full report."""
    components = measure_components()
    baselines = get_session_baselines(10)
    totals = calculate_totals(components)

    snapshot = {
        "label": "current",
        "timestamp": datetime.now().isoformat(),
        "components": components,
        "session_baselines": baselines,
        "totals": totals,
    }

    print(f"\n{'=' * 55}")
    print(f"  TOKEN OVERHEAD REPORT")
    print(f"{'=' * 55}")

    print_snapshot_summary(snapshot)

    if baselines:
        print(f"\n  --- Recent Session Baselines (from JSONL logs) ---")
        for b in baselines:
            dt = b["date"][:16]
            print(f"    {dt}  {b['baseline_tokens']:>7,} tokens")

    print(f"\n{'=' * 55}")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "report":
        full_report()
    elif args[0] == "snapshot" and len(args) > 1:
        take_snapshot(args[1])
    elif args[0] == "compare":
        compare_snapshots()
    else:
        print("Usage:")
        print("  python3 measure.py report              # Full report")
        print("  python3 measure.py snapshot before      # Save pre-optimization snapshot")
        print("  python3 measure.py snapshot after       # Save post-optimization snapshot")
        print("  python3 measure.py compare              # Compare before vs after")
