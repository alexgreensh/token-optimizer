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
    python3 measure.py dashboard --coord-path /tmp/...  # Generate interactive dashboard
    python3 measure.py health             # Check running session health
    python3 measure.py trends             # Usage trends (last 30 days)
    python3 measure.py trends --days 7    # Usage trends (shorter window)
    python3 measure.py trends --json      # Machine-readable output
    python3 measure.py collect             # Collect sessions into SQLite DB
    python3 measure.py collect --quiet     # Silent mode (for SessionEnd hook)

Snapshots are saved to SNAPSHOT_DIR (default: ~/.claude/_backups/token-optimizer/)

Copyright (C) 2026 Alex Greenshpun
SPDX-License-Identifier: AGPL-3.0-only
"""

import json
import os
import glob
import re
import subprocess
import sys
import platform
from datetime import datetime
from pathlib import Path

CHARS_PER_TOKEN = 4.0

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SNAPSHOT_DIR = CLAUDE_DIR / "_backups" / "token-optimizer"

# Tokens per skill frontmatter (loaded at startup)
TOKENS_PER_SKILL_APPROX = 100
# Tokens per command frontmatter (loaded at startup)
TOKENS_PER_COMMAND_APPROX = 50
# Tokens per MCP deferred tool name in Tool Search menu
TOKENS_PER_DEFERRED_TOOL = 15
# Average tools per MCP server (rough estimate when tool count unknown)
AVG_TOOLS_PER_SERVER = 8


def estimate_tokens_from_file(filepath):
    """Estimate tokens by reading file content (character count / 4)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return int(len(content) / CHARS_PER_TOKEN)
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def estimate_tokens_from_frontmatter(filepath):
    """Estimate tokens from YAML frontmatter only (between --- delimiters)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Extract frontmatter between first pair of ---
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                return max(int(len(frontmatter) / CHARS_PER_TOKEN), 20)
        # No frontmatter found, use rough estimate
        return TOKENS_PER_SKILL_APPROX
    except (FileNotFoundError, PermissionError, OSError):
        return TOKENS_PER_SKILL_APPROX


def count_lines(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def resolve_real_path(filepath):
    """Resolve symlinks to avoid double-counting."""
    try:
        return filepath.resolve()
    except OSError:
        return filepath


def cwd_to_project_dir_name():
    """Convert cwd to Claude Code project directory name format.

    Claude Code encodes project paths by replacing / with - and dropping leading -.
    e.g., /Users/alex/myproject -> -Users-alex-myproject
    """
    cwd = str(Path.cwd())
    return "-" + cwd.replace("/", "-").lstrip("-")


def find_projects_dir():
    """Find the Claude Code projects directory matching the current working directory."""
    projects_base = CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None

    # Try to match current working directory first
    expected_name = cwd_to_project_dir_name()
    expected_dir = projects_base / expected_name
    if expected_dir.exists():
        return expected_dir

    # Fallback: try parent directories (user may be in a subdirectory)
    cwd = Path.cwd()
    for parent in list(cwd.parents)[:5]:
        parent_name = "-" + str(parent).replace("/", "-").lstrip("-")
        parent_dir = projects_base / parent_name
        if parent_dir.exists():
            return parent_dir

    # Last resort: most recently modified (with warning)
    dirs = [d for d in projects_base.iterdir() if d.is_dir()]
    if not dirs:
        return None
    result = max(dirs, key=lambda d: d.stat().st_mtime)
    print(f"  [Warning] Could not match cwd to project dir. Using most recent: {result.name}")
    return result


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
        try:
            mtime = os.path.getmtime(jf)
            first_usage = None
            with open(jf, "r", encoding="utf-8", errors="replace") as f:
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
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue

            if first_usage:
                baselines.append({
                    "date": datetime.fromtimestamp(mtime).isoformat(),
                    "baseline_tokens": first_usage,
                })
        except (PermissionError, OSError):
            continue

    return baselines


def get_mcp_config_paths():
    """Return MCP config paths for the current platform."""
    paths = [
        CLAUDE_DIR / "settings.json",  # Claude Code primary config
    ]

    system = platform.system()
    if system == "Darwin":
        paths.append(HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    elif system == "Linux":
        paths.append(HOME / ".config" / "Claude" / "claude_desktop_config.json")

    return paths


def count_mcp_tools_and_servers():
    """Count MCP servers and estimate deferred tool overhead."""
    server_count = 0
    tool_count_estimate = 0
    seen_names = set()
    server_names = []

    for config_path in get_mcp_config_paths():
        if not config_path.exists():
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            servers = config.get("mcpServers", config.get("mcp_servers", {}))
            for name in servers:
                if name not in seen_names:
                    seen_names.add(name)
                    server_names.append(name)
                    server_count += 1
        except (json.JSONDecodeError, PermissionError, OSError):
            continue

    # Estimate tool count: avg tools per server
    tool_count_estimate = server_count * AVG_TOOLS_PER_SERVER
    # Deferred tool tokens: ~15 tokens per tool name in Tool Search menu
    tokens = tool_count_estimate * TOKENS_PER_DEFERRED_TOOL

    return {
        "server_count": server_count,
        "server_names": server_names,
        "tool_count_estimate": tool_count_estimate,
        "tokens": tokens,
        "note": f"Estimated ~{AVG_TOOLS_PER_SERVER} tools/server x ~{TOKENS_PER_DEFERRED_TOOL} tokens/tool (Tool Search deferred)",
    }


def _has_paths_frontmatter(filepath):
    """Check if a rules file has paths: frontmatter (path-scoped rule)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(2048)  # Only need to check frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                return "paths:" in frontmatter
        return False
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _detect_imports(claude_md_path):
    """Detect @import patterns in a CLAUDE.md file and estimate token cost."""
    imports = []
    try:
        with open(claude_md_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Match lines starting with @ followed by a path-like string
        pattern = re.compile(r'^@(\S+\.(?:md|txt|yaml|yml|json))\s*$', re.MULTILINE)
        project_root = claude_md_path.parent.resolve()
        for match in pattern.finditer(content):
            import_path = match.group(1)
            resolved = (project_root / import_path).resolve()
            # Security: ensure resolved path stays under project root
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue  # Skip path traversal attempts
            tokens = estimate_tokens_from_file(resolved) if resolved.exists() else 0
            imports.append({
                "pattern": f"@{import_path}",
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
                "tokens": tokens,
            })
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return imports


TOKEN_RELEVANT_ENV_VARS = [
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_MAX_THINKING_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "MAX_MCP_OUTPUT_TOKENS",
    "ENABLE_TOOL_SEARCH",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "BASH_MAX_OUTPUT_LENGTH",
]


def _check_settings_env(settings_path):
    """Check settings.json for token-relevant environment variables."""
    result = {"found": {}, "settings_exists": settings_path.exists()}
    if not settings_path.exists():
        return result
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        env = settings.get("env", {})
        for var in TOKEN_RELEVANT_ENV_VARS:
            if var in env:
                result["found"][var] = env[var]
    except (json.JSONDecodeError, PermissionError, OSError):
        pass
    return result


def _get_frontmatter_description_length(filepath):
    """Get the character length of the description field in YAML frontmatter."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4096)
        if not content.startswith("---"):
            return 0
        end = content.find("---", 3)
        if end <= 0:
            return 0
        frontmatter = content[3:end]
        lines = frontmatter.split("\n")
        desc_text = ""
        in_desc = False
        for line in lines:
            if line.startswith("description:"):
                value = line[len("description:"):].strip()
                if value in ("|", ">", "|+", "|-", ">+", ">-"):
                    # Multi-line block scalar
                    in_desc = True
                    continue
                # Single-line value (possibly quoted)
                if value.startswith('"') and value.endswith('"'):
                    desc_text = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    desc_text = value[1:-1]
                else:
                    desc_text = value
                break
            elif in_desc:
                if line and (line[0] == " " or line[0] == "\t"):
                    desc_text += line.strip() + " "
                else:
                    break
        return len(desc_text.strip())
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def measure_components():
    """Measure all controllable token overhead components."""
    components = {}
    seen_real_paths = set()

    # CLAUDE.md files (with symlink dedup)
    for name, path in [
        ("claude_md_global", CLAUDE_DIR / "CLAUDE.md"),
        ("claude_md_home", HOME / "CLAUDE.md"),
    ]:
        real = resolve_real_path(path)
        if real in seen_real_paths:
            components[name] = {"path": str(path), "exists": False, "tokens": 0, "lines": 0, "note": "duplicate (symlink)"}
            continue
        if path.exists():
            seen_real_paths.add(real)
        components[name] = {
            "path": str(path),
            "exists": path.exists(),
            "tokens": estimate_tokens_from_file(path),
            "lines": count_lines(path),
        }

    # Find project CLAUDE.md files in cwd and parents
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents)[:3]:
        if parent == HOME:
            continue  # Already checked ~/CLAUDE.md
        claude_md = parent / "CLAUDE.md"
        if claude_md.exists():
            real = resolve_real_path(claude_md)
            if real not in seen_real_paths:
                seen_real_paths.add(real)
                components[f"claude_md_project_{parent.name}"] = {
                    "path": str(claude_md),
                    "exists": True,
                    "tokens": estimate_tokens_from_file(claude_md),
                    "lines": count_lines(claude_md),
                }

    # MEMORY.md
    projects_dir = find_projects_dir()
    if projects_dir:
        memory_path = projects_dir / "memory" / "MEMORY.md"
        components["memory_md"] = {
            "path": str(memory_path),
            "exists": memory_path.exists(),
            "tokens": estimate_tokens_from_file(memory_path) if memory_path.exists() else 0,
            "lines": count_lines(memory_path) if memory_path.exists() else 0,
        }

    # Skills (read actual frontmatter size + check description quality in single pass)
    skills_dir = CLAUDE_DIR / "skills"
    skill_count = 0
    skill_tokens = 0
    skill_names = []
    verbose_skills = []
    skills_detail = {}
    if skills_dir.exists():
        for item in sorted(skills_dir.iterdir()):
            skill_md = item / "SKILL.md"
            if item.is_dir() and skill_md.exists():
                skill_count += 1
                skill_names.append(item.name)
                fm_tokens = estimate_tokens_from_frontmatter(skill_md)
                skill_tokens += fm_tokens
                desc_len = _get_frontmatter_description_length(skill_md)
                if desc_len > 200:
                    verbose_skills.append({
                        "name": item.name,
                        "description_chars": desc_len,
                    })
                # Collect per-skill detail for dashboard
                detail = {
                    "name": item.name,
                    "frontmatter_tokens": fm_tokens,
                    "description_chars": desc_len,
                }
                # Gather file structure (top-level only)
                try:
                    children = sorted(p.name for p in item.iterdir() if not p.name.startswith("."))
                    detail["files"] = children
                except OSError:
                    detail["files"] = []
                # Read description from frontmatter or first paragraph
                try:
                    with open(skill_md, "r", encoding="utf-8") as f:
                        content = f.read(4000)  # first 4K is enough
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end > 0:
                            fm_block = content[3:end]
                            for line in fm_block.split("\n"):
                                stripped = line.strip()
                                if stripped.startswith("description:"):
                                    desc_text = stripped[12:].strip().strip("|").strip(">").strip()
                                    if not desc_text:
                                        # Multi-line description
                                        desc_lines = []
                                        for dl in fm_block.split("\n")[fm_block.split("\n").index(line)+1:]:
                                            if dl and dl[0] in (' ', '\t'):
                                                desc_lines.append(dl.strip())
                                            else:
                                                break
                                        desc_text = " ".join(desc_lines)
                                    detail["description"] = desc_text[:200]
                                    break
                    # Fallback: no YAML frontmatter, grab first non-heading paragraph
                    if "description" not in detail:
                        for line in content.split("\n"):
                            stripped = line.strip()
                            if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                                detail["description"] = stripped[:200]
                                break
                except (OSError, UnicodeDecodeError):
                    pass
                skills_detail[item.name] = detail
    components["skills"] = {
        "count": skill_count,
        "tokens": skill_tokens,
        "names": skill_names,
    }
    components["skills_detail"] = skills_detail

    # Commands (read actual file sizes for frontmatter estimate)
    commands_dir = CLAUDE_DIR / "commands"
    cmd_count = 0
    cmd_tokens = 0
    cmd_names = []
    if commands_dir.exists():
        for f in sorted(commands_dir.glob("*.md")):
            cmd_count += 1
            cmd_names.append(f.stem)
            cmd_tokens += estimate_tokens_from_frontmatter(f)
        for subdir in sorted(commands_dir.iterdir()):
            if subdir.is_dir():
                for f in sorted(subdir.glob("*.md")):
                    cmd_count += 1
                    cmd_names.append(f"{subdir.name}/{f.stem}")
                    cmd_tokens += estimate_tokens_from_frontmatter(f)
    components["commands"] = {
        "count": cmd_count,
        "tokens": cmd_tokens,
        "names": cmd_names,
    }

    # MCP servers and deferred tools
    mcp = count_mcp_tools_and_servers()
    components["mcp_tools"] = {
        "server_count": mcp["server_count"],
        "server_names": mcp["server_names"],
        "tool_count_estimate": mcp["tool_count_estimate"],
        "tokens": mcp["tokens"],
        "note": mcp["note"],
    }

    # .claudeignore (check both global and project-level)
    global_ignore = CLAUDE_DIR / ".claudeignore"
    project_ignore = cwd / ".claudeignore"
    components["claudeignore"] = {
        "global_exists": global_ignore.exists(),
        "project_exists": project_ignore.exists(),
        "exists": global_ignore.exists() or project_ignore.exists(),
    }

    # Read settings.json once (used for hooks, env vars, MCP)
    settings_path = CLAUDE_DIR / "settings.json"
    _cached_settings = None
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                _cached_settings = json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError):
            pass

    # Hooks
    hooks_configured = False
    hook_names = []
    if _cached_settings:
        hooks = _cached_settings.get("hooks", {})
        if hooks:
            hooks_configured = True
            hook_names = list(hooks.keys())
    components["hooks"] = {
        "configured": hooks_configured,
        "names": hook_names,
    }

    # .claude/rules/ directory
    rules_dir = CLAUDE_DIR / "rules"
    rules_count = 0
    rules_tokens = 0
    rules_files = []
    rules_always_loaded = 0
    if rules_dir.exists() and rules_dir.is_dir():
        for f in sorted(rules_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                rules_count += 1
                tokens = estimate_tokens_from_file(f)
                rules_tokens += tokens
                has_paths = _has_paths_frontmatter(f)
                rules_files.append({
                    "name": f.name,
                    "tokens": tokens,
                    "path_scoped": has_paths,
                })
                if not has_paths:
                    rules_always_loaded += 1
    components["rules"] = {
        "count": rules_count,
        "tokens": rules_tokens,
        "files": rules_files,
        "always_loaded": rules_always_loaded,
    }

    # @imports in CLAUDE.md
    imports_tokens = 0
    imports_found = []
    for key in components:
        if key.startswith("claude_md") and components[key].get("exists"):
            found = _detect_imports(Path(components[key]["path"]))
            for imp in found:
                imports_tokens += imp["tokens"]
            imports_found.extend(found)
    components["imports"] = {
        "count": len(imports_found),
        "tokens": imports_tokens,
        "files": imports_found,
    }

    # CLAUDE.local.md
    claude_local = cwd / "CLAUDE.local.md"
    components["claude_local_md"] = {
        "path": str(claude_local),
        "exists": claude_local.exists(),
        "tokens": estimate_tokens_from_file(claude_local) if claude_local.exists() else 0,
        "lines": count_lines(claude_local) if claude_local.exists() else 0,
    }

    # settings.json env vars (token-relevant) — use cached settings
    if _cached_settings:
        env = _cached_settings.get("env", {})
        found_vars = {var: env[var] for var in TOKEN_RELEVANT_ENV_VARS if var in env}
        components["settings_env"] = {"found": found_vars, "settings_exists": True}
    else:
        components["settings_env"] = {"found": {}, "settings_exists": settings_path.exists()}

    # settings.local.json existence
    settings_local = CLAUDE_DIR / "settings.local.json"
    project_settings_local = cwd / ".claude" / "settings.local.json"
    components["settings_local"] = {
        "global_exists": settings_local.exists(),
        "project_exists": project_settings_local.exists(),
        "exists": settings_local.exists() or project_settings_local.exists(),
    }

    # Skill frontmatter quality (collected during skills scan above)
    components["skill_frontmatter_quality"] = {
        "verbose_count": len(verbose_skills),
        "verbose_skills": verbose_skills,
    }

    # Fixed overhead
    components["core_system"] = {
        "tokens": 15000,
        "note": "System prompt (~3,000) + built-in tools (~12,000). Fixed. Source: Piebald-AI tracking, v2.1.59.",
    }

    return components


def calculate_totals(components):
    """Calculate total controllable and estimated overhead."""
    controllable = 0
    fixed = 0
    # Keys that don't contribute direct token overhead (metadata only)
    non_token_keys = {
        "claudeignore", "hooks", "settings_env", "settings_local",
        "skill_frontmatter_quality",
    }

    for name, info in components.items():
        if name in non_token_keys:
            continue
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


def sanitize_label(label):
    """Sanitize snapshot label to prevent path traversal."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', label):
        print("[Error] Snapshot label must contain only letters, numbers, hyphens, underscores.")
        sys.exit(1)
    return label


def take_snapshot(label):
    """Save a measurement snapshot (before or after)."""
    label = sanitize_label(label)

    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(str(SNAPSHOT_DIR), 0o700)
    except OSError as e:
        print(f"[Error] Cannot create snapshot directory: {e}")
        sys.exit(1)

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
    if filepath.exists():
        print(f"  [Note] Overwriting existing snapshot '{label}'")
    fd = os.open(str(filepath), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"\n[Token Optimizer] Snapshot '{label}' saved to {filepath}")
    print(f"  [Note] Snapshot contains system config details. Do not share publicly.")
    print_snapshot_summary(snapshot)
    return snapshot


def print_snapshot_summary(snapshot):
    """Print a human-readable summary of a snapshot."""
    c = snapshot["components"]
    t = snapshot["totals"]

    print(f"\n{'=' * 55}")
    print(f"  Snapshot: {snapshot['label']} ({snapshot['timestamp'][:16]})")
    print(f"{'=' * 55}")

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
    print(f"  {'Skills (frontmatter)':<35s} {s.get('tokens', 0):>6,} tokens  [{s.get('count', 0)} skills]")

    # Commands
    cmd = c.get("commands", {})
    print(f"  {'Commands (frontmatter)':<35s} {cmd.get('tokens', 0):>6,} tokens  [{cmd.get('count', 0)} commands]")

    # MCP
    mcp = c.get("mcp_tools", {})
    mcp_tokens = mcp.get("tokens", 0)
    srv_count = mcp.get("server_count", 0)
    tool_est = mcp.get("tool_count_estimate", 0)
    print(f"  {'MCP deferred tools (est.)':<35s} {mcp_tokens:>6,} tokens  [{srv_count} servers, ~{tool_est} tools]")

    # Rules
    rules = c.get("rules", {})
    if rules.get("count", 0) > 0:
        print(f"  {'Rules (.claude/rules/)':<35s} {rules.get('tokens', 0):>6,} tokens  [{rules.get('count', 0)} files, {rules.get('always_loaded', 0)} always-loaded]")

    # @imports
    imports = c.get("imports", {})
    if imports.get("count", 0) > 0:
        print(f"  {'@imports in CLAUDE.md':<35s} {imports.get('tokens', 0):>6,} tokens  [{imports.get('count', 0)} imports]")

    # CLAUDE.local.md
    cl = c.get("claude_local_md", {})
    if cl.get("exists"):
        print(f"  {'CLAUDE.local.md':<35s} {cl.get('tokens', 0):>6,} tokens  [{cl.get('lines', 0)} lines]")

    # Core
    core = c.get("core_system", {})
    print(f"  {'Core system (fixed)':<35s} {core.get('tokens', 0):>6,} tokens")

    print(f"  {'=' * 53}")
    print(f"  {'ESTIMATED TOTAL':<35s} {t['estimated_total']:>6,} tokens")
    pct_of_200k = t['estimated_total'] / 200_000 * 100
    print(f"  {'Context used before typing':<35s} {pct_of_200k:>5.1f}% of 200K window")

    # Session baselines
    baselines = snapshot.get("session_baselines", [])
    if baselines:
        avg = sum(b["baseline_tokens"] for b in baselines) / len(baselines)
        print(f"\n  Real session baseline (avg of {len(baselines)}): {avg:,.0f} tokens")
        print(f"  (includes system reminders, conversation history, etc.)")

    # Extras
    ignore = c.get("claudeignore", {})
    hooks = c.get("hooks", {})
    ignore_str = "Global" if ignore.get("global_exists") else ""
    if ignore.get("project_exists"):
        ignore_str += ("+Project" if ignore_str else "Project")
    print(f"\n  .claudeignore: {ignore_str if ignore_str else 'MISSING'}")
    print(f"  Hooks: {', '.join(hooks.get('names', [])) if hooks.get('configured') else 'NONE'}")

    # Settings env vars
    settings_env = c.get("settings_env", {})
    found_vars = settings_env.get("found", {})
    if found_vars:
        print(f"  Settings env vars: {', '.join(f'{k}={v}' for k, v in found_vars.items())}")

    # Settings local
    settings_local = c.get("settings_local", {})
    if settings_local.get("exists"):
        print(f"  settings.local.json: Found")

    # Verbose skill descriptions
    quality = c.get("skill_frontmatter_quality", {})
    verbose_count = quality.get("verbose_count", 0)
    if verbose_count > 0:
        names = [s["name"] for s in quality.get("verbose_skills", [])]
        print(f"  Verbose skill descriptions (>200 chars): {verbose_count} ({', '.join(names[:5])}{'...' if verbose_count > 5 else ''})")


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

    try:
        with open(before_path, "r", encoding="utf-8") as f:
            before = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"\n[Error] Cannot read 'before' snapshot: {e}")
        print(f"  Re-run: python3 measure.py snapshot before")
        return

    try:
        with open(after_path, "r", encoding="utf-8") as f:
            after = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"\n[Error] Cannot read 'after' snapshot: {e}")
        print(f"  Re-run: python3 measure.py snapshot after")
        return

    # Warn if 'before' snapshot is stale (>24h old)
    try:
        before_ts = datetime.fromisoformat(before["timestamp"])
        age_seconds = (datetime.now() - before_ts).total_seconds()
        if age_seconds > 86400:
            age_days = int(age_seconds / 86400)
            print(f"\n  [Warning] 'before' snapshot is {age_days}d old. Consider re-taking it.")
    except (KeyError, ValueError):
        pass  # Missing or unparseable timestamp, not critical

    bc = before["components"]
    ac = after["components"]

    print(f"\n{'=' * 65}")
    print(f"  TOKEN OPTIMIZER - BEFORE vs AFTER")
    print(f"  Before: {before['timestamp'][:16]}")
    print(f"  After:  {after['timestamp'][:16]}")
    print(f"{'=' * 65}")

    print(f"\n  {'Component':<25s} {'Before':>8s} {'After':>8s} {'Saved':>8s} {'%':>6s}")
    print(f"  {'-' * 57}")

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

    # MCP (now included!)
    rows.append((
        "MCP deferred tools",
        bc.get("mcp_tools", bc.get("mcp_servers", {})).get("tokens", 0),
        ac.get("mcp_tools", ac.get("mcp_servers", {})).get("tokens", 0),
    ))

    # Rules
    rows.append((
        "Rules (.claude/rules/)",
        bc.get("rules", {}).get("tokens", 0),
        ac.get("rules", {}).get("tokens", 0),
    ))

    # @imports
    rows.append((
        "@imports",
        bc.get("imports", {}).get("tokens", 0),
        ac.get("imports", {}).get("tokens", 0),
    ))

    # CLAUDE.local.md
    rows.append((
        "CLAUDE.local.md",
        bc.get("claude_local_md", {}).get("tokens", 0),
        ac.get("claude_local_md", {}).get("tokens", 0),
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

    print(f"  {'-' * 57}")
    total_pct = f"{total_saved / total_before * 100:.0f}%" if total_before > 0 else "-"
    print(f"  {'CONTROLLABLE TOTAL':<25s} {total_before:>7,} {total_after:>7,} {total_saved:>+7,} {total_pct:>6s}")

    # Context budget impact (not dollar amounts)
    if total_saved > 0:
        before_pct = (total_before + 15000) / 200_000 * 100
        after_pct = (total_after + 15000) / 200_000 * 100
        print(f"\n  Context budget: {before_pct:.1f}% -> {after_pct:.1f}% of 200K window")
        print(f"  That's {total_saved:,} more tokens for actual work per message.")

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

    # Session baseline comparison (with honest caveat)
    bb = before.get("session_baselines", [])
    ab = after.get("session_baselines", [])
    if bb and ab:
        avg_before = sum(b["baseline_tokens"] for b in bb) / len(bb)
        avg_after = sum(b["baseline_tokens"] for b in ab) / len(ab)
        if abs(avg_before - avg_after) < 100:
            print(f"\n  Session baselines: {avg_before:,.0f} -> {avg_after:,.0f} tokens")
            print(f"  [Note] These are from the same recent sessions. Start new sessions")
            print(f"         after optimizing to see real baseline changes.")
        else:
            print(f"\n  Real session baseline: {avg_before:,.0f} -> {avg_after:,.0f} tokens")

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


def _open_in_browser(filepath):
    """Open a file in the default browser. Cross-platform."""
    filepath = str(filepath)
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", filepath], check=True)
        elif system == "Linux":
            subprocess.run(["xdg-open", filepath], check=True)
        elif system == "Windows":
            os.startfile(filepath)
        else:
            raise OSError(f"Unsupported platform: {system}")
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        url = Path(filepath).as_uri()
        print(f"\n  Could not auto-open browser. Open manually:")
        print(f"  {url}")


def generate_dashboard(coord_path):
    """Generate an interactive HTML dashboard from audit results."""
    coord = Path(coord_path)
    if not coord.exists():
        print(f"Error: coord-path does not exist: {coord_path}")
        print("Usage: python3 measure.py dashboard --coord-path /tmp/token-optimizer-XXXXXXXXXX")
        sys.exit(1)

    # Locate the template
    script_dir = Path(__file__).resolve().parent
    template_path = script_dir.parent / "assets" / "dashboard.html"
    if not template_path.exists():
        print(f"Error: dashboard template not found at: {template_path}")
        sys.exit(1)

    # Re-measure current state
    print("  Measuring current token overhead...")
    components = measure_components()
    totals = calculate_totals(components)
    baselines = get_session_baselines(5)

    snapshot = {
        "components": components,
        "totals": totals,
        "session_baselines": baselines,
    }

    # Read audit files
    audit_dir = coord / "audit"
    audit = {}
    audit_files = {
        "claudemd": "claudemd.md",
        "memorymd": "memorymd.md",
        "skills": "skills.md",
        "mcp": "mcp.md",
        "commands": "commands.md",
        "advanced": "advanced.md",
    }
    for key, filename in audit_files.items():
        fpath = audit_dir / filename
        if fpath.exists():
            try:
                audit[key] = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                audit[key] = None
        else:
            audit[key] = None

    found = sum(1 for v in audit.values() if v)
    print(f"  Loaded {found}/{len(audit_files)} audit files")

    # Read optimization plan
    plan_path = coord / "analysis" / "optimization-plan.md"
    plan = None
    if plan_path.exists():
        try:
            plan = plan_path.read_text(encoding="utf-8")
            print(f"  Loaded optimization plan ({len(plan)} chars)")
        except (OSError, UnicodeDecodeError):
            pass

    # Collect trends and health data
    print("  Collecting usage trends...")
    try:
        trends = _collect_trends_data(days=30)
    except Exception:
        trends = None
    print("  Checking session health...")
    try:
        health = _collect_health_data()
    except Exception:
        health = None

    # Assemble data
    data = {
        "snapshot": snapshot,
        "audit": audit,
        "plan": plan,
        "trends": trends,
        "health": health,
        "generated_at": datetime.now().isoformat(),
    }

    # Load template and inject data
    template = template_path.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    injected = template.replace(
        "window.__TOKEN_DATA__ = null;",
        f"window.__TOKEN_DATA__ = {data_json};",
        1,
    )

    # Write output
    out_dir = coord / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dashboard.html"
    out_path.write_text(injected, encoding="utf-8")
    print(f"  Dashboard written to: {out_path}")

    # Open in browser
    _open_in_browser(out_path)
    print(f"  Opened in browser.")
    return str(out_path)


def _find_all_jsonl_files(days=30):
    """Find all JSONL session files across all projects within the given day window."""
    projects_base = CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return []

    cutoff = datetime.now().timestamp() - (days * 86400)
    results = []
    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
                if mtime >= cutoff:
                    results.append((jf, mtime, project_dir.name))
            except OSError:
                continue
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _find_subagent_jsonl_files(session_jsonl_path):
    """Find subagent JSONL files for a given session.

    Claude Code stores subagent logs in {session-uuid}/subagents/*.jsonl
    next to the parent {session-uuid}.jsonl file.
    """
    session_dir = session_jsonl_path.parent / session_jsonl_path.stem
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.is_dir():
        return []
    results = []
    for jf in subagent_dir.glob("*.jsonl"):
        try:
            if jf.stat().st_size > 0:
                results.append(jf)
        except OSError:
            continue
    return results


def _extract_skills_and_agents_from_subagent(filepath):
    """Parse a subagent JSONL file for Skill and Task tool calls only.

    Returns (skills_dict, subagents_dict) without extracting token usage
    (parent session already accounts for API cost).
    """
    skills = {}
    subagents = {}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                content = record.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    inp = block.get("input", {})
                    if tool_name == "Skill":
                        skill = inp.get("skill", "unknown")
                        skills[skill] = skills.get(skill, 0) + 1
                    elif tool_name == "Task":
                        agent_type = inp.get("subagent_type", "unknown")
                        subagents[agent_type] = subagents.get(agent_type, 0) + 1
    except (PermissionError, OSError):
        pass
    return skills, subagents


def _clean_project_name(raw_project):
    """Map Claude Code dashed directory names to human-readable labels.

    e.g. "-Users-jane" -> "home"
         "-Users-jane-projects-acme-api" -> "acme/api"
         "-Users-jane-myproject" -> "myproject"
    """
    if not raw_project:
        return "unknown"
    # Strip the leading "-Users-<username>-" prefix
    import re as _re
    cleaned = _re.sub(r"^-Users-[^-]+-?", "", raw_project)
    if not cleaned:
        return "home"
    # Split remaining path segments and take the last 1-2 meaningful ones
    parts = cleaned.split("-")
    # Filter out empty parts
    parts = [p for p in parts if p]
    if not parts:
        return "home"
    # If the path is long, use last 2 segments joined by /
    if len(parts) > 2:
        return "/".join(parts[-2:])
    return "/".join(parts)


def _extract_topic(text):
    """Extract a clean topic from the first user message text.

    Strips common prefixes like 'Implement the following plan:' and
    extracts the plan title if present. Truncates to 120 chars.
    """
    if not text or not isinstance(text, str):
        return None
    # Strip leading whitespace/newlines
    text = text.strip()
    # Remove common prefixes
    prefixes = [
        "Implement the following plan:",
        "Implement the following plan\n",
        "Please implement the following plan:",
        "Execute the following plan:",
    ]
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    # If it starts with a markdown heading, extract that as the topic
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            text = line.lstrip("# ").strip()
            break
        if line:
            text = line
            break
    # Truncate
    if len(text) > 120:
        text = text[:117] + "..."
    return text or None


def _parse_session_jsonl(filepath):
    """Parse a single JSONL session file in one streaming pass.

    Returns a dict with extracted session metrics, or None if the file
    is empty or unparseable.
    """
    skills_used = {}
    subagents_used = {}
    tool_calls = {}
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    model_usage = {}
    version = None
    slug = None
    topic = None
    first_ts = None
    last_ts = None
    message_count = 0
    api_calls = 0

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Extract version (take the first non-None we see)
                if version is None:
                    v = record.get("version")
                    if v:
                        version = v

                # Extract slug (first record that has one)
                if slug is None:
                    s = record.get("slug")
                    if s:
                        slug = s

                # Extract timestamp
                ts_str = record.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except (ValueError, TypeError):
                        pass

                rec_type = record.get("type")

                # Extract topic from first user message
                if rec_type == "user" and topic is None:
                    msg = record.get("message", {})
                    content = msg.get("content") if isinstance(msg, dict) else msg
                    if isinstance(content, str):
                        topic = _extract_topic(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                topic = _extract_topic(block.get("text", ""))
                                if topic:
                                    break
                            elif isinstance(block, str):
                                topic = _extract_topic(block)
                                if topic:
                                    break

                # Count user/assistant messages
                if rec_type in ("user", "assistant"):
                    message_count += 1

                # Extract tool usage from assistant messages
                if rec_type == "assistant":
                    msg = record.get("message", {})
                    content = msg.get("content", [])

                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_use":
                                continue

                            tool_name = block.get("name", "")
                            tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1

                            inp = block.get("input", {})
                            if tool_name == "Skill":
                                skill = inp.get("skill", "unknown")
                                skills_used[skill] = skills_used.get(skill, 0) + 1
                            elif tool_name == "Task":
                                agent_type = inp.get("subagent_type", "unknown")
                                subagents_used[agent_type] = subagents_used.get(agent_type, 0) + 1

                    # Extract usage/token data
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

                        # Model usage: count all input types + output
                        model = msg.get("model", "unknown")
                        model_usage[model] = model_usage.get(model, 0) + inp_tok + cr + cc + out_tok

    except (PermissionError, OSError):
        return None

    if message_count == 0:
        return None

    # Calculate duration
    duration_minutes = 0
    if first_ts and last_ts:
        delta = (last_ts - first_ts).total_seconds()
        duration_minutes = max(0, delta / 60)

    # Full input = uncached + cache reads + cache creation
    total_full_input = total_input + total_cache_read + total_cache_create

    # Cache hit rate
    cache_hit_rate = 0.0
    if total_full_input > 0:
        cache_hit_rate = total_cache_read / total_full_input

    return {
        "version": version,
        "slug": slug,
        "topic": topic,
        "duration_minutes": duration_minutes,
        "total_input_tokens": total_full_input,
        "total_output_tokens": total_output,
        "cache_hit_rate": cache_hit_rate,
        "model_usage": model_usage,
        "skills_used": skills_used,
        "subagents_used": subagents_used,
        "tool_calls": tool_calls,
        "message_count": message_count,
        "api_calls": api_calls,
        "first_ts": first_ts.isoformat() if first_ts else None,
    }


def _normalize_model_name(model_id):
    """Collapse model IDs like 'claude-sonnet-4-6' into 'sonnet'.

    Returns None for synthetic/internal model IDs that should be skipped.
    """
    if not model_id or model_id.startswith("<"):
        return None
    m = model_id.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return model_id


def _load_overhead_snapshots():
    """Load any saved token-optimizer snapshots for overhead trajectory.

    Returns snapshots sorted chronologically by timestamp.
    """
    snapshots = []
    if not SNAPSHOT_DIR.exists():
        return snapshots
    for sf in sorted(SNAPSHOT_DIR.glob("snapshot_*.json")):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            snapshots.append({
                "label": data.get("label", sf.stem),
                "timestamp": data.get("timestamp", ""),
                "total": data.get("totals", {}).get("estimated_total", 0),
            })
        except (json.JSONDecodeError, PermissionError, OSError):
            continue
    # Sort by timestamp so trajectory reads chronologically
    snapshots.sort(key=lambda s: s["timestamp"])
    return snapshots


# ========== SQLite Trends DB ==========
# Pure Python, no Claude API. Runs standalone via `measure.py collect`.

import sqlite3

TRENDS_DB = SNAPSHOT_DIR / "trends.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE,
    date TEXT NOT NULL,
    project TEXT,
    duration_minutes REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    message_count INTEGER,
    api_calls INTEGER,
    cache_hit_rate REAL,
    skills_json TEXT,
    subagents_json TEXT,
    tool_calls_json TEXT,
    model_usage_json TEXT,
    version TEXT,
    slug TEXT,
    topic TEXT,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    session_count INTEGER,
    total_input INTEGER,
    total_output INTEGER,
    total_duration REAL,
    avg_cache_hit REAL
);

CREATE TABLE IF NOT EXISTS skill_daily (
    date TEXT,
    skill TEXT,
    session_count INTEGER,
    invocations INTEGER,
    PRIMARY KEY (date, skill)
);

CREATE TABLE IF NOT EXISTS model_daily (
    date TEXT,
    model TEXT,
    total_tokens INTEGER,
    PRIMARY KEY (date, model)
);

CREATE TABLE IF NOT EXISTS subagent_daily (
    date TEXT,
    agent_type TEXT,
    spawn_count INTEGER,
    PRIMARY KEY (date, agent_type)
);
"""


def _init_trends_db():
    """Initialize the trends SQLite DB. Returns a connection."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRENDS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # Migrate existing DBs: add slug/topic columns if missing
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        if "slug" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN slug TEXT")
        if "topic" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN topic TEXT")
        conn.commit()
    except sqlite3.Error:
        pass
    return conn


def _is_file_collected(conn, jsonl_path):
    """Check if a JSONL file has already been collected."""
    cur = conn.execute(
        "SELECT 1 FROM session_log WHERE jsonl_path = ?",
        (str(jsonl_path),),
    )
    return cur.fetchone() is not None


def collect_sessions(days=90, quiet=False):
    """Parse new JSONL files and insert into SQLite. Zero token cost.

    Skips files already collected. Safe to run repeatedly.
    """
    conn = _init_trends_db()
    files = _find_all_jsonl_files(days)
    if not files:
        if not quiet:
            print(f"No session logs found in the last {days} days.")
        conn.close()
        return 0

    new_count = 0
    for filepath, mtime, project_name in files:
        if _is_file_collected(conn, filepath):
            continue

        parsed = _parse_session_jsonl(filepath)
        if not parsed:
            continue

        # Scan subagent JSONL files for additional skills and agent types
        for sub_jf in _find_subagent_jsonl_files(filepath):
            sub_skills, sub_agents = _extract_skills_and_agents_from_subagent(sub_jf)
            for sk, cnt in sub_skills.items():
                parsed["skills_used"][sk] = parsed["skills_used"].get(sk, 0) + cnt
            for ag, cnt in sub_agents.items():
                parsed["subagents_used"][ag] = parsed["subagents_used"].get(ag, 0) + cnt

        date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        skills_used = parsed["skills_used"]
        subagents_used = parsed["subagents_used"]

        # Insert session_log
        conn.execute(
            """INSERT OR IGNORE INTO session_log
               (jsonl_path, date, project, duration_minutes, input_tokens,
                output_tokens, message_count, api_calls, cache_hit_rate,
                skills_json, subagents_json, tool_calls_json, model_usage_json,
                version, slug, topic, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(filepath), date, project_name,
                parsed["duration_minutes"],
                parsed["total_input_tokens"],
                parsed["total_output_tokens"],
                parsed["message_count"],
                parsed.get("api_calls", 0),
                parsed["cache_hit_rate"],
                json.dumps(skills_used),
                json.dumps(subagents_used),
                json.dumps(parsed["tool_calls"]),
                json.dumps(parsed["model_usage"]),
                parsed["version"],
                parsed.get("slug"),
                parsed.get("topic"),
                datetime.now().isoformat(),
            ),
        )

        # Upsert daily_stats
        conn.execute(
            """INSERT INTO daily_stats (date, session_count, total_input, total_output, total_duration, avg_cache_hit)
               VALUES (?, 1, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 session_count = session_count + 1,
                 total_input = total_input + excluded.total_input,
                 total_output = total_output + excluded.total_output,
                 total_duration = total_duration + excluded.total_duration,
                 avg_cache_hit = (avg_cache_hit * session_count + excluded.avg_cache_hit) / (session_count + 1)""",
            (date, parsed["total_input_tokens"], parsed["total_output_tokens"],
             parsed["duration_minutes"], parsed["cache_hit_rate"]),
        )

        # Upsert skill_daily (session-level: count each skill once per session)
        for skill, invocations in skills_used.items():
            conn.execute(
                """INSERT INTO skill_daily (date, skill, session_count, invocations)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(date, skill) DO UPDATE SET
                     session_count = session_count + 1,
                     invocations = invocations + excluded.invocations""",
                (date, skill, invocations),
            )

        # Upsert model_daily
        for model_id, tokens in parsed["model_usage"].items():
            normalized = _normalize_model_name(model_id)
            if normalized is None:
                continue
            conn.execute(
                """INSERT INTO model_daily (date, model, total_tokens)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date, model) DO UPDATE SET
                     total_tokens = total_tokens + excluded.total_tokens""",
                (date, normalized, tokens),
            )

        # Upsert subagent_daily
        for agent_type, count in subagents_used.items():
            conn.execute(
                """INSERT INTO subagent_daily (date, agent_type, spawn_count)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date, agent_type) DO UPDATE SET
                     spawn_count = spawn_count + excluded.spawn_count""",
                (date, agent_type, count),
            )

        new_count += 1

    conn.commit()
    conn.close()

    if not quiet:
        total = conn_total_sessions() if TRENDS_DB.exists() else new_count
        print(f"[Token Optimizer] Collected {new_count} new sessions. Total in DB: {total}")
    return new_count


def conn_total_sessions():
    """Quick count of total sessions in the DB."""
    try:
        conn = sqlite3.connect(str(TRENDS_DB))
        cur = conn.execute("SELECT COUNT(*) FROM session_log")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except (sqlite3.Error, OSError):
        return 0


def _collect_trends_from_db(days=30):
    """Query SQLite trends DB for aggregated usage data.

    Returns same dict shape as _collect_trends_from_jsonl, or None if DB
    doesn't exist or has no data for the requested period.
    """
    if not TRENDS_DB.exists():
        return None

    try:
        conn = sqlite3.connect(str(TRENDS_DB))
        conn.row_factory = sqlite3.Row
        # Verify it's a valid DB before proceeding
        conn.execute("SELECT 1 FROM session_log LIMIT 1")
    except (sqlite3.Error, sqlite3.DatabaseError):
        try:
            conn.close()
        except Exception:
            pass
        return None

    try:
        return _query_trends_db(conn, days)
    except (sqlite3.Error, sqlite3.DatabaseError):
        conn.close()
        return None


def _query_trends_db(conn, days):
    """Internal: run all queries against the trends DB. Caller handles errors."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Basic stats
    row = conn.execute(
        """SELECT COUNT(*) as cnt,
                  COALESCE(SUM(duration_minutes), 0) as total_dur,
                  COALESCE(SUM(input_tokens), 0) as total_in,
                  COALESCE(SUM(output_tokens), 0) as total_out
           FROM session_log WHERE date >= ?""", (cutoff,)
    ).fetchone()

    session_count = row["cnt"]
    if session_count == 0:
        conn.close()
        return None

    total_duration = row["total_dur"]
    total_input = row["total_in"]
    total_output = row["total_out"]

    # Skill usage
    skill_rows = conn.execute(
        """SELECT skill, SUM(session_count) as sess, SUM(invocations) as inv
           FROM skill_daily WHERE date >= ? GROUP BY skill ORDER BY sess DESC""",
        (cutoff,),
    ).fetchall()
    skill_sessions = {r["skill"]: r["sess"] for r in skill_rows}

    # Model mix
    model_rows = conn.execute(
        """SELECT model, SUM(total_tokens) as tot
           FROM model_daily WHERE date >= ? GROUP BY model ORDER BY tot DESC""",
        (cutoff,),
    ).fetchall()
    model_mix = {r["model"]: r["tot"] for r in model_rows}

    # Subagents
    sub_rows = conn.execute(
        """SELECT agent_type, SUM(spawn_count) as tot
           FROM subagent_daily WHERE date >= ? GROUP BY agent_type ORDER BY tot DESC""",
        (cutoff,),
    ).fetchall()
    subagents = {r["agent_type"]: r["tot"] for r in sub_rows}

    # Tool calls (aggregate from session_log JSON)
    total_tools = {}
    tool_rows = conn.execute(
        "SELECT tool_calls_json FROM session_log WHERE date >= ? AND tool_calls_json IS NOT NULL",
        (cutoff,),
    ).fetchall()
    for tr in tool_rows:
        try:
            calls = json.loads(tr["tool_calls_json"])
            for tool, count in calls.items():
                total_tools[tool] = total_tools.get(tool, 0) + count
        except (json.JSONDecodeError, TypeError):
            pass
    total_tools = dict(sorted(total_tools.items(), key=lambda x: -x[1]))

    # Installed skills vs used
    components = measure_components()
    installed_skills = set(components.get("skills", {}).get("names", []))
    used_skills = set(skill_sessions.keys())
    never_used = installed_skills - used_skills
    never_used_overhead = len(never_used) * TOKENS_PER_SKILL_APPROX

    # Trajectory
    snapshots = _load_overhead_snapshots()
    current_total = calculate_totals(components).get("estimated_total", 0)

    # Daily breakdown from session_log
    daily = {}
    session_rows = conn.execute(
        """SELECT date, duration_minutes, input_tokens, output_tokens,
                  message_count, api_calls, cache_hit_rate, skills_json,
                  subagents_json, slug, topic, project
           FROM session_log WHERE date >= ? ORDER BY date DESC""",
        (cutoff,),
    ).fetchall()
    for sr in session_rows:
        date = sr["date"]
        if date not in daily:
            daily[date] = {
                "date": date,
                "sessions": 0,
                "total_input": 0,
                "total_output": 0,
                "skills_used": {},
                "session_details": [],
            }
        d = daily[date]
        d["sessions"] += 1
        d["total_input"] += sr["input_tokens"] or 0
        d["total_output"] += sr["output_tokens"] or 0

        try:
            skills = json.loads(sr["skills_json"]) if sr["skills_json"] else {}
        except (json.JSONDecodeError, TypeError):
            skills = {}
        for skill, cnt in skills.items():
            d["skills_used"][skill] = d["skills_used"].get(skill, 0) + cnt

        try:
            subagents = json.loads(sr["subagents_json"]) if sr["subagents_json"] else {}
        except (json.JSONDecodeError, TypeError):
            subagents = {}

        d["session_details"].append({
            "duration_minutes": round(sr["duration_minutes"] or 0, 1),
            "input_tokens": sr["input_tokens"] or 0,
            "output_tokens": sr["output_tokens"] or 0,
            "message_count": sr["message_count"] or 0,
            "api_calls": sr["api_calls"] or 0,
            "skills": list(skills.keys()),
            "subagents": list(subagents.keys()),
            "cache_hit_rate": round(sr["cache_hit_rate"] or 0, 3),
            "slug": sr["slug"],
            "topic": sr["topic"],
            "project": _clean_project_name(sr["project"]),
        })

    daily_sorted = sorted(daily.values(), key=lambda x: x["date"], reverse=True)

    conn.close()

    return {
        "period_days": days,
        "session_count": session_count,
        "avg_duration_minutes": round(total_duration / session_count, 1) if session_count else 0,
        "avg_input_tokens": round(total_input / session_count) if session_count else 0,
        "avg_output_tokens": round(total_output / session_count) if session_count else 0,
        "skills": {
            "used": dict(sorted(skill_sessions.items(), key=lambda x: -x[1])),
            "installed_count": len(installed_skills),
            "never_used": sorted(never_used),
            "never_used_overhead": never_used_overhead,
        },
        "subagents": subagents,
        "model_mix": model_mix,
        "tool_calls": total_tools,
        "trajectory": {
            "snapshots": snapshots,
            "current_total": current_total,
        },
        "daily": daily_sorted,
        "source": "sqlite",
    }


def _collect_trends_from_jsonl(days=30):
    """Collect usage trends by parsing JSONL files directly (fallback).

    Returns a dict with aggregated trends data, or None if no data found.
    """
    files = _find_all_jsonl_files(days)
    if not files:
        return None

    sessions = []
    for filepath, mtime, project_name in files:
        parsed = _parse_session_jsonl(filepath)
        if parsed:
            # Scan subagent JSONL files for additional skills and agent types
            for sub_jf in _find_subagent_jsonl_files(filepath):
                sub_skills, sub_agents = _extract_skills_and_agents_from_subagent(sub_jf)
                for sk, cnt in sub_skills.items():
                    parsed["skills_used"][sk] = parsed["skills_used"].get(sk, 0) + cnt
                for ag, cnt in sub_agents.items():
                    parsed["subagents_used"][ag] = parsed["subagents_used"].get(ag, 0) + cnt
            parsed["project"] = project_name
            parsed["date"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            sessions.append(parsed)

    if not sessions:
        return None

    total_skills = {}
    total_subagents = {}
    total_tools = {}
    total_model_tokens = {}
    total_input = 0
    total_output = 0
    total_duration = 0
    session_count = len(sessions)

    for s in sessions:
        total_input += s["total_input_tokens"]
        total_output += s["total_output_tokens"]
        total_duration += s["duration_minutes"]

        for skill, count in s["skills_used"].items():
            total_skills[skill] = total_skills.get(skill, 0) + count

        for agent, count in s["subagents_used"].items():
            total_subagents[agent] = total_subagents.get(agent, 0) + count

        for tool, count in s["tool_calls"].items():
            total_tools[tool] = total_tools.get(tool, 0) + count

        for model, tokens in s["model_usage"].items():
            normalized = _normalize_model_name(model)
            if normalized is None:
                continue
            total_model_tokens[normalized] = total_model_tokens.get(normalized, 0) + tokens

    skill_sessions = {}
    for s in sessions:
        for skill in s["skills_used"]:
            skill_sessions[skill] = skill_sessions.get(skill, 0) + 1

    components = measure_components()
    installed_skills = set(components.get("skills", {}).get("names", []))
    used_skills = set(total_skills.keys())
    never_used = installed_skills - used_skills
    never_used_overhead = len(never_used) * TOKENS_PER_SKILL_APPROX

    snapshots = _load_overhead_snapshots()
    current_total = calculate_totals(components).get("estimated_total", 0)

    # Build daily breakdown
    daily = {}
    for s in sessions:
        date = s["date"]
        if date not in daily:
            daily[date] = {
                "date": date,
                "sessions": 0,
                "total_input": 0,
                "total_output": 0,
                "skills_used": {},
                "session_details": [],
            }
        d = daily[date]
        d["sessions"] += 1
        d["total_input"] += s["total_input_tokens"]
        d["total_output"] += s["total_output_tokens"]
        for skill in s["skills_used"]:
            d["skills_used"][skill] = d["skills_used"].get(skill, 0) + s["skills_used"][skill]
        d["session_details"].append({
            "duration_minutes": round(s["duration_minutes"], 1),
            "input_tokens": s["total_input_tokens"],
            "output_tokens": s["total_output_tokens"],
            "message_count": s["message_count"],
            "api_calls": s.get("api_calls", 0),
            "skills": list(s["skills_used"].keys()),
            "subagents": list(s["subagents_used"].keys()),
            "cache_hit_rate": round(s["cache_hit_rate"], 3),
            "slug": s.get("slug"),
            "topic": s.get("topic"),
            "project": _clean_project_name(s.get("project")),
        })

    # Sort daily by date descending
    daily_sorted = sorted(daily.values(), key=lambda x: x["date"], reverse=True)

    return {
        "period_days": days,
        "session_count": session_count,
        "avg_duration_minutes": round(total_duration / session_count, 1) if session_count else 0,
        "avg_input_tokens": round(total_input / session_count) if session_count else 0,
        "avg_output_tokens": round(total_output / session_count) if session_count else 0,
        "skills": {
            "used": dict(sorted(skill_sessions.items(), key=lambda x: -x[1])),
            "installed_count": len(installed_skills),
            "never_used": sorted(never_used),
            "never_used_overhead": never_used_overhead,
        },
        "subagents": dict(sorted(total_subagents.items(), key=lambda x: -x[1])),
        "model_mix": total_model_tokens,
        "tool_calls": dict(sorted(total_tools.items(), key=lambda x: -x[1])),
        "trajectory": {
            "snapshots": snapshots,
            "current_total": current_total,
        },
        "daily": daily_sorted,
    }


def _collect_git_commits(days=30):
    """Scan known git repos for commits within the time window.

    Checks project directories under ~/.claude/projects/ (reversing the
    dashed name to a real path) and skill repos under ~/.claude/skills/.

    Returns: { "2026-03-01": [{"repo": "name", "commits": ["msg1", ...]}], ... }
    """
    from datetime import timedelta
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Collect candidate repo paths
    repo_paths = {}  # path -> display name

    # 1. From project directories
    projects_base = CLAUDE_DIR / "projects"
    if projects_base.exists():
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            # Reverse dashed name to real path: -Users-alex-myproject -> /Users/alex/myproject
            real_path = "/" + project_dir.name.lstrip("-").replace("-", "/")
            rp = Path(real_path)
            if rp.is_dir() and (rp / ".git").exists():
                repo_paths[str(rp)] = _clean_project_name(project_dir.name)

    # 2. From skill repos
    skills_dir = CLAUDE_DIR / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / ".git").exists():
                repo_paths[str(skill_dir)] = skill_dir.name

    if not repo_paths:
        return {}

    result = {}  # date -> [{"repo": name, "commits": [msg, ...]}]

    for repo_path, display_name in repo_paths.items():
        try:
            proc = subprocess.run(
                ["git", "-C", repo_path, "log", "--oneline",
                 f"--since={cutoff_date}", "--format=%ai|%s"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            for line in proc.stdout.strip().split("\n"):
                if "|" not in line:
                    continue
                date_part, msg = line.split("|", 1)
                date = date_part.strip()[:10]  # YYYY-MM-DD
                if date not in result:
                    result[date] = []
                # Find or create repo entry for this date
                repo_entry = None
                for entry in result[date]:
                    if entry["repo"] == display_name:
                        repo_entry = entry
                        break
                if repo_entry is None:
                    repo_entry = {"repo": display_name, "commits": []}
                    result[date].append(repo_entry)
                repo_entry["commits"].append(msg.strip())
        except (subprocess.TimeoutExpired, OSError):
            continue

    return result


def _collect_trends_data(days=30):
    """Collect trends data, preferring SQLite DB when available.

    Falls back to live JSONL parsing if DB doesn't exist or is empty.
    """
    # Try SQLite first (faster, accumulated data)
    result = _collect_trends_from_db(days)
    if result is not None:
        result["git_commits"] = _collect_git_commits(days)
        return result
    # Fall back to live JSONL parsing
    result = _collect_trends_from_jsonl(days)
    if result is not None:
        result["git_commits"] = _collect_git_commits(days)
    return result


def usage_trends(days=30, as_json=False):
    """Analyze usage trends across all Claude Code sessions."""
    trends = _collect_trends_data(days)
    if trends is None:
        print(f"\nNo session logs found in the last {days} days.")
        print(f"Looked in: {CLAUDE_DIR / 'projects' / '*' / '*.jsonl'}")
        return

    if as_json:
        result = dict(trends)
        result.pop("trajectory", None)
        print(json.dumps(result, indent=2, default=str))
        return

    session_count = trends["session_count"]
    avg_dur = trends["avg_duration_minutes"]
    avg_in = trends["avg_input_tokens"]
    avg_out = trends["avg_output_tokens"]

    def _fmt_tokens(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(int(n))

    print(f"\nUSAGE TRENDS (last {days} days)")
    print("=" * 55)
    print(f"\n  Sessions: {session_count} | Avg duration: {avg_dur:.0f} min | Avg tokens/session: {_fmt_tokens(avg_in)} in / {_fmt_tokens(avg_out)} out")

    skill_sessions = trends["skills"]["used"]
    installed_count = trends["skills"]["installed_count"]
    never_used = trends["skills"]["never_used"]

    print(f"\nSKILLS")
    if skill_sessions:
        print(f"  Used ({len(skill_sessions)} of {installed_count} installed):")
        for skill, count in sorted(skill_sessions.items(), key=lambda x: -x[1])[:15]:
            dots = "." * max(2, 30 - len(skill))
            print(f"    {skill} {dots} {count} session{'s' if count != 1 else ''}")
        if len(skill_sessions) > 15:
            print(f"    ... and {len(skill_sessions) - 15} more")
    else:
        print(f"  No skill invocations found in {session_count} sessions.")

    if never_used:
        approx_overhead = len(never_used) * TOKENS_PER_SKILL_APPROX
        print(f"\n  Never used (last {days} days):")
        names = sorted(never_used)
        line = "    "
        for i, name in enumerate(names):
            addition = name + (", " if i < len(names) - 1 else "")
            if len(line) + len(addition) > 72:
                print(line.rstrip(", "))
                line = "    " + addition
            else:
                line += addition
        if line.strip():
            print(line.rstrip(", "))
        print(f"    ({len(never_used)} skills, ~{approx_overhead:,} tokens overhead)")

    total_subagents = trends["subagents"]
    if total_subagents:
        print(f"\nSUBAGENTS")
        for agent, count in sorted(total_subagents.items(), key=lambda x: -x[1]):
            dots = "." * max(2, 30 - len(agent))
            print(f"  {agent} {dots} {count} spawned")

    total_model_tokens = trends["model_mix"]
    if total_model_tokens:
        print(f"\nMODEL MIX")
        grand_total = sum(total_model_tokens.values())
        for model, tokens in sorted(total_model_tokens.items(), key=lambda x: -x[1]):
            pct = tokens / grand_total * 100 if grand_total else 0
            dots = "." * max(2, 26 - len(model))
            print(f"  {model} {dots} {pct:.0f}% of tokens ({_fmt_tokens(tokens)})")

    trajectory = trends.get("trajectory", {})
    snapshots = trajectory.get("snapshots", [])
    if snapshots:
        print(f"\nOVERHEAD TRAJECTORY (from saved snapshots)")
        for snap in snapshots:
            ts = snap["timestamp"][:10] if snap["timestamp"] else "unknown"
            label = snap["label"]
            total = snap["total"]
            print(f"  {ts}: {total:,} tokens ({label})")

        current_total = trajectory.get("current_total", 0)
        if snapshots and current_total:
            latest = snapshots[-1]["total"]
            drift = current_total - latest
            if abs(drift) > 500:
                direction = "+" if drift > 0 else ""
                print(f"  Today:  {current_total:,} tokens (current)")
                print(f"  Drift since last snapshot: {direction}{drift:,} tokens")

    print()


def _parse_elapsed_time(elapsed_str):
    """Parse ps elapsed time format (dd-HH:MM:SS or HH:MM:SS or MM:SS) to seconds."""
    elapsed_str = elapsed_str.strip()
    days = 0
    if "-" in elapsed_str:
        parts = elapsed_str.split("-", 1)
        days = int(parts[0])
        elapsed_str = parts[1]

    parts = elapsed_str.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = int(parts[0]), int(parts[1])
    else:
        return 0

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _format_elapsed(seconds):
    """Format seconds into a human-readable elapsed string."""
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours = seconds // 3600
    if hours < 24:
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"
    d = hours // 24
    h = hours % 24
    return f"{d}d {h}h"


def _find_session_version_for_pid(pid):
    """Try to find the Claude Code version for a running process by matching its session JSONL.

    We look for recently modified JSONL files and check if their sessionId
    matches anything we can correlate to the PID. Falls back to reading the
    version field from the most recent JSONL that started around the process
    start time.
    """
    projects_base = CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None

    # Get process start time for correlation
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        lstart_str = result.stdout.strip()
        # Parse "Fri Feb 27 10:18:43 2026"
        proc_start = datetime.strptime(lstart_str, "%c")
    except (subprocess.SubprocessError, ValueError, OSError):
        return None

    # Find JSONL files modified around the process start time
    best_match = None
    best_diff = float("inf")

    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
                file_time = datetime.fromtimestamp(mtime)
                # Only consider files modified after process start
                if file_time < proc_start:
                    continue
                # Read first few lines for version and timestamp
                with open(jf, "r", encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f):
                        if line_num > 5:
                            break
                        try:
                            record = json.loads(line)
                            ts_str = record.get("timestamp")
                            if not ts_str:
                                continue
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                            diff = abs((ts - proc_start).total_seconds())
                            if diff < best_diff:
                                best_diff = diff
                                v = record.get("version")
                                if v:
                                    best_match = v
                        except (json.JSONDecodeError, ValueError):
                            continue
            except (PermissionError, OSError):
                continue

    # Only return if we found a reasonable match (within 5 minutes of start)
    if best_match and best_diff < 300:
        return best_match
    return best_match  # Return best guess even if not close


def _collect_health_data():
    """Collect session health data.

    Returns a dict with health information, or None on unsupported platforms.
    """
    system = platform.system()
    if system == "Windows":
        return None

    installed_version = None
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            installed_version = result.stdout.strip().split()[0] if result.stdout.strip() else None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    running_sessions = []
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,lstart,etime,command"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 8:
                    continue
                command = " ".join(parts[7:])
                if command.strip() == "claude" or command.startswith("claude "):
                    pid = int(parts[0])
                    lstart = " ".join(parts[1:6])
                    elapsed = parts[6]
                    elapsed_seconds = _parse_elapsed_time(elapsed)

                    running_sessions.append({
                        "pid": pid,
                        "started": lstart,
                        "elapsed_seconds": elapsed_seconds,
                        "elapsed_human": _format_elapsed(elapsed_seconds),
                        "command": command,
                    })
    except (subprocess.SubprocessError, OSError):
        return None

    for session in running_sessions:
        session["version"] = _find_session_version_for_pid(session["pid"])

    # Flag sessions
    for s in running_sessions:
        flags = []
        if s["version"] and installed_version and s["version"] != installed_version:
            flags.append("OUTDATED")
        if s["elapsed_seconds"] > 172800:
            flags.append("ZOMBIE")
        elif s["elapsed_seconds"] > 86400:
            flags.append("STALE")
        s["flags"] = flags

    automated = []
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if "claude" in line.lower() or "anthropic" in line.lower():
                        automated.append(line.strip())
        except (subprocess.SubprocessError, OSError):
            pass

    # Build recommendations
    recommendations = []
    outdated_count = sum(1 for s in running_sessions if "OUTDATED" in s.get("flags", []))
    stale_count = sum(1 for s in running_sessions if any(f in s.get("flags", []) for f in ("STALE", "ZOMBIE")))

    if outdated_count > 0 and installed_version:
        recommendations.append(
            f"{outdated_count} session{'s' if outdated_count != 1 else ''} running "
            f"older version (installed: {installed_version}). "
            f"Restart to get latest fixes: close and reopen these terminals."
        )
    if stale_count > 0:
        recommendations.append(
            f"{stale_count} session{'s' if stale_count != 1 else ''} running "
            f"24+ hours. Check if still needed, long sessions accumulate context bloat."
        )

    return {
        "installed_version": installed_version,
        "running_sessions": running_sessions,
        "automated": automated,
        "recommendations": recommendations,
    }


def session_health():
    """Check health of running Claude Code sessions."""
    health = _collect_health_data()
    if health is None:
        print("\nSession health check is not supported on this platform.")
        return

    installed_version = health["installed_version"]
    running_sessions = health["running_sessions"]
    automated = health["automated"]
    recommendations = health["recommendations"]

    print(f"\nSESSION HEALTH CHECK")
    print("=" * 55)

    if installed_version:
        print(f"\n  Installed version: {installed_version}")
    else:
        print(f"\n  Installed version: unknown (could not run 'claude --version')")

    if not running_sessions:
        print(f"\n  No running Claude Code CLI sessions found.")
    else:
        print(f"\nRUNNING SESSIONS ({len(running_sessions)})")

        for s in running_sessions:
            flags = s.get("flags", [])
            version_str = s["version"] or "unknown"
            flag_str = f"  {'  '.join(flags)}" if flags else ""
            print(f"  PID {s['pid']:<7d} Started: {s['started']}  ({s['elapsed_human']} ago)")
            print(f"             Version: {version_str}{flag_str}")

        if recommendations:
            print(f"\nRECOMMENDATIONS")
            for rec in recommendations:
                print(f"  - {rec}")

    if automated:
        print(f"\nAUTOMATED PROCESSES")
        for proc in automated:
            print(f"  {proc}")

    print()


# ========== Hook Management ==========

SETTINGS_PATH = CLAUDE_DIR / "settings.json"
MEASURE_PY_PATH = Path(__file__).resolve()
HOOK_COMMAND = f"python3 {MEASURE_PY_PATH} collect --quiet"


def _is_hook_installed(settings=None):
    """Check if the SessionEnd measure.py collect hook is installed.

    Returns True if any SessionEnd hook command contains 'measure.py collect'.
    """
    if settings is None:
        if not SETTINGS_PATH.exists():
            return False
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError):
            return False

    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])
    if not isinstance(session_end, list):
        return False
    for entry in session_end:
        hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
        for hook in hook_list:
            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
            if "measure.py" in cmd and "collect" in cmd:
                return True
    return False


def check_hook():
    """Exit 0 if SessionEnd measure.py collect hook is installed, 1 if not."""
    sys.exit(0 if _is_hook_installed() else 1)


def _write_settings_atomic(settings_data):
    """Write settings.json atomically using tempfile + os.replace()."""
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(SETTINGS_PATH.parent),
        prefix=".settings-",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(settings_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, str(SETTINGS_PATH))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def setup_hook(dry_run=False):
    """Install the SessionEnd hook for automatic usage collection."""
    # Load existing settings
    settings = {}
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError) as e:
            print(f"[Error] Could not read {SETTINGS_PATH}: {e}")
            sys.exit(1)

    # Idempotency check
    if _is_hook_installed(settings):
        print("[Token Optimizer] SessionEnd hook already installed. Nothing to do.")
        return

    # Build the hook entry
    new_hook = {"type": "command", "command": HOOK_COMMAND}

    # Handle 4 scenarios
    if "hooks" not in settings:
        settings["hooks"] = {}

    hooks = settings["hooks"]
    if "SessionEnd" not in hooks:
        hooks["SessionEnd"] = [{"hooks": [new_hook]}]
    else:
        session_end = hooks["SessionEnd"]
        if isinstance(session_end, list) and len(session_end) > 0:
            # Append to the first entry's hooks array
            first_entry = session_end[0]
            if isinstance(first_entry, dict):
                if "hooks" not in first_entry:
                    first_entry["hooks"] = []
                first_entry["hooks"].append(new_hook)
            else:
                session_end.append({"hooks": [new_hook]})
        else:
            hooks["SessionEnd"] = [{"hooks": [new_hook]}]

    if dry_run:
        print("[Token Optimizer] Dry run. Proposed SessionEnd hooks:\n")
        print(json.dumps(hooks.get("SessionEnd", []), indent=2))
        print("\nNo changes written.")
        return

    # Backup settings.json
    backup_dir = CLAUDE_DIR / "_backups" / "token-optimizer"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"settings.json.pre-hook-{ts}"
    if SETTINGS_PATH.exists():
        import shutil
        shutil.copy2(str(SETTINGS_PATH), str(backup_path))

    # Write atomically
    try:
        _write_settings_atomic(settings)
        print(f"[Token Optimizer] SessionEnd hook installed.")
        print(f"  Backup: {backup_path}")
        print(f"  Hook: {HOOK_COMMAND}")
    except PermissionError:
        print(f"[Error] Permission denied writing {SETTINGS_PATH}.")
        print(f"Add this manually to your settings.json hooks.SessionEnd:\n")
        print(json.dumps({"type": "command", "command": HOOK_COMMAND}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "report":
        full_report()
    elif args[0] == "snapshot" and len(args) > 1:
        take_snapshot(args[1])
    elif args[0] == "compare":
        compare_snapshots()
    elif args[0] == "dashboard":
        cp = None
        for i, a in enumerate(args):
            if a == "--coord-path" and i + 1 < len(args):
                cp = args[i + 1]
                break
        if not cp:
            print("Usage: python3 measure.py dashboard --coord-path /tmp/token-optimizer-XXXXXXXXXX")
            sys.exit(1)
        generate_dashboard(cp)
    elif args[0] == "collect":
        days = 90
        quiet = "--quiet" in args or "-q" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
        collect_sessions(days=days, quiet=quiet)
    elif args[0] == "health":
        session_health()
    elif args[0] == "check-hook":
        check_hook()
    elif args[0] == "setup-hook":
        dry = "--dry-run" in args
        setup_hook(dry_run=dry)
    elif args[0] == "trends":
        days = 30
        output_json = False
        i = 1
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                    if days < 1:
                        print("[Error] --days must be a positive integer.")
                        sys.exit(1)
                except ValueError:
                    print(f"[Error] Invalid --days value: {args[i + 1]}")
                    sys.exit(1)
                i += 2
            elif args[i] == "--json":
                output_json = True
                i += 1
            else:
                print(f"[Error] Unknown flag: {args[i]}")
                sys.exit(1)
        usage_trends(days=days, as_json=output_json)
    else:
        print("Usage:")
        print("  python3 measure.py report              # Full report")
        print("  python3 measure.py snapshot before      # Save pre-optimization snapshot")
        print("  python3 measure.py snapshot after       # Save post-optimization snapshot")
        print("  python3 measure.py compare              # Compare before vs after")
        print("  python3 measure.py dashboard --coord-path PATH  # Interactive dashboard")
        print("  python3 measure.py health               # Check running session health")
        print("  python3 measure.py trends               # Usage trends (last 30 days)")
        print("  python3 measure.py trends --days 7      # Usage trends (last 7 days)")
        print("  python3 measure.py trends --json        # Machine-readable output")
        print("  python3 measure.py collect              # Collect sessions into SQLite DB")
        print("  python3 measure.py collect --quiet      # Silent mode (for hooks)")
        print("  python3 measure.py check-hook           # Check if SessionEnd hook is installed")
        print("  python3 measure.py setup-hook           # Install SessionEnd hook")
        print("  python3 measure.py setup-hook --dry-run # Show what would be installed")
