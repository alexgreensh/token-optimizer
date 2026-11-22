"""Advisory subscription-limit audits for Claude Code and Codex.

All checks here are strictly advisory: they inspect configuration, frontmatter,
and local telemetry to surface quota-saving proposals and never modify user
configuration files automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

HIGH_QUOTA_EFFORT_LEVELS = frozenset(("xhigh", "max"))
VERBOSITY_NUDGE_OFTEN_THRESHOLD = 5
MONOREPO_MIN_FILES = 3
MONOREPO_MIN_COMBINED_TOKENS = 3000
MONOREPO_LARGE_ROOT_TOKENS = 2500
_SKIP_WALK_DIRS = frozenset((
    ".git", "node_modules", ".venv", "venv", "dist", "build", "target",
    "vendor", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next",
))
_NOISY_SKILL_RE = re.compile(
    r"\b(pytest|jest|vitest|cargo\s+test|go\s+test|npm\s+test|git\s+log|"
    r"ripgrep|\brg\b|\bgrep\b|audit|forensic|benchmark|scrape|crawl|"
    r"deep\s+research|log\s+analysis|stack\s+trace|triage|scan\s+the\s+codebase|"
    r"full\s+codebase|security\s+scan|dependency\s+audit)\b",
    re.IGNORECASE,
)
_INTERACTIVE_SKILL_RE = re.compile(
    r"\b(ask\s+the\s+user|interactive\s+interview|ask_question|clarifying\s+questions|"
    r"multi-turn\s+conversation|confirm\s+before\s+each\s+step)\b",
    re.IGNORECASE,
)
_DOMAIN_HEADING_RE = re.compile(
    r"^#{1,3}\s+.*\b(test|tests|testing|frontend|backend|ui|css|sql|database|db|migration|"
    r"docker|kubernetes|k8s|ci|cd|deploy|infra|terraform|mobile|ios|android|"
    r"api|graphql|docs|documentation|python|typescript|rust|go|golang|scripts)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CONCISE_STYLE_TOKENS = ("concise", "terse", "brief", "minimal")
_ANSI_FALLBACK_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|[\x00-\x1f\x7f]")


def _clean_label(value: str, facade=None) -> str:
    text = str(value or "")
    if facade is not None and hasattr(facade, "_strip_ansi"):
        try:
            return facade._strip_ansi(text).strip()
        except Exception:
            pass
    return _ANSI_FALLBACK_RE.sub("", text).strip()


def parse_yaml_frontmatter(text: str) -> dict:
    """Parse simple YAML frontmatter (scalars and flat lists) without external deps."""
    if not text:
        return {}
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end_idx is None:
        return {}

    data: dict = {}
    current_key: str | None = None
    for raw in lines[1:end_idx]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[0] in (" ", "\t") and current_key is not None:
            if stripped.startswith("-"):
                item = stripped[1:].strip().strip('"').strip("'")
                if item:
                    existing = data.get(current_key)
                    if isinstance(existing, list):
                        existing.append(item)
                    elif existing in (None, ""):
                        data[current_key] = [item]
            continue
        if ":" not in raw:
            current_key = None
            continue
        key, _, rest = raw.partition(":")
        key, val = key.strip(), rest.strip()
        if not key:
            current_key = None
            continue
        current_key = key
        if not val:
            data[key] = ""
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = (
                [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip().strip('"').strip("'")]
                if inner else []
            )
        else:
            if " #" in val:
                val = val.split(" #", 1)[0].strip()
            data[key] = val.strip('"').strip("'")
    return data


def _strip_frontmatter(text: str) -> str:
    clean = text.lstrip("\ufeff")
    lines = clean.splitlines()
    if not lines or lines[0].strip() != "---":
        return clean
    end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return "\n".join(lines[end_idx + 1:]) if end_idx is not None else clean


def _read_toml_file(path: Path, facade=None) -> dict:
    if not path.is_file():
        return {}
    tomllib_mod = getattr(facade, "tomllib", None) if facade is not None else None
    if tomllib_mod is None:
        try:
            import tomllib as tomllib_mod
        except ImportError:
            return {}
    try:
        with path.open("rb") as handle:
            data = tomllib_mod.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_merged_codex_config(facade, cwd: Path | None = None) -> tuple[dict, list[Path]]:
    merged: dict = {}
    sources: list[Path] = []
    try:
        global_cfg = facade._read_codex_config()
        if isinstance(global_cfg, dict) and global_cfg:
            merged.update(global_cfg)
            sources.append(facade.RUNTIME_DIR / "config.toml")
    except Exception:
        pass
    base = Path(cwd) if cwd is not None else Path(facade.Path.cwd())
    proj_path = base / ".codex" / "config.toml"
    proj_cfg = _read_toml_file(proj_path, facade)
    if proj_cfg:
        if isinstance(proj_cfg.get("mcp_servers"), dict) and isinstance(merged.get("mcp_servers"), dict):
            combined_mcp = {**merged["mcp_servers"], **proj_cfg["mcp_servers"]}
            merged.update(proj_cfg)
            merged["mcp_servers"] = combined_mcp
        else:
            merged.update(proj_cfg)
        sources.append(proj_path)
    return merged, sources


def _scan_claude_agents(facade, cwd: Path | None = None) -> list[dict]:
    """Scan global, project, and enabled plugin agent markdown definitions."""
    agents: list[dict] = []
    seen: set[Path] = set()
    scan_roots: list[tuple[str, Path]] = []

    try:
        claude_dir = Path(facade.CLAUDE_DIR)
        scan_roots.append(("global", claude_dir / "agents"))
    except Exception:
        claude_dir = None

    base = Path(cwd) if cwd is not None else Path(facade.Path.cwd())
    scan_roots.append(("project", base / ".claude" / "agents"))

    if claude_dir is not None:
        registry = claude_dir / "plugins" / "installed_plugins.json"
        if registry.is_file():
            try:
                reg_data = json.loads(registry.read_text(encoding="utf-8"))
                plugins = reg_data.get("plugins") or {}
                enabled_plugins = None
                settings_path = claude_dir / "settings.json"
                if settings_path.is_file():
                    try:
                        sdata = json.loads(settings_path.read_text(encoding="utf-8"))
                        if isinstance(sdata, dict):
                            enabled_plugins = sdata.get("enabledPlugins")
                    except Exception:
                        pass
                if isinstance(plugins, dict):
                    for plugin_key, installs in plugins.items():
                        if not isinstance(installs, list):
                            continue
                        if enabled_plugins is not None and not enabled_plugins.get(plugin_key, False):
                            continue
                        plugin_name = plugin_key.split("@")[0] or plugin_key
                        for inst in installs:
                            raw_path = inst.get("installPath") or "" if isinstance(inst, dict) else ""
                            inst_path = Path(raw_path) if raw_path else None
                            if inst_path and inst_path.is_absolute() and (inst_path / "agents").is_dir():
                                scan_roots.append((f"plugin:{plugin_name}", inst_path / "agents"))
            except Exception:
                pass

    for source, adir in scan_roots:
        if not adir.is_dir():
            continue
        try:
            entries = sorted(adir.glob("*.md"))
        except OSError:
            continue
        for md in entries:
            if not md.is_file():
                continue
            try:
                resolved = md.resolve()
            except OSError:
                resolved = md
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = parse_yaml_frontmatter(text)
            name = _clean_label(fm.get("name") or md.stem, facade)
            display = f"{source.split(':', 1)[1]}:{name}" if source.startswith("plugin:") else name
            agents.append({"name": display, "path": str(md), "source": source, "frontmatter": fm})
    return agents


# ---------------------------------------------------------------------------
# 1. Effort audit (Claude settings, env var, Codex config.toml, subagents)
# ---------------------------------------------------------------------------

def audit_claude_effort(components: dict, facade, cwd: Path | None = None) -> list[dict]:
    """Flag xhigh/max default effort in settings, env, or subagents. Advisory only."""
    findings: list[dict] = []

    effort_level = (components.get("settings_local") or {}).get("effortLevel")
    if effort_level is None:
        try:
            effort_level = facade._resolve_settings_value("effortLevel", None)
        except Exception:
            effort_level = None
    if effort_level:
        low_val = str(effort_level).strip().lower()
        if low_val in HIGH_QUOTA_EFFORT_LEVELS:
            findings.append({
                "kind": "settings_effort",
                "severity": "medium",
                "bucket": "quick",
                "level": low_val,
                "title": f"Default `effortLevel` is set to \"{low_val}\"",
                "markdown": (
                    f"**Lower default `effortLevel` from \"{low_val}\" to \"medium\" or \"high\"**: "
                    f"Your settings.json sets `\"effortLevel\": \"{low_val}\"` globally. "
                    f"Running `{low_val}` effort on every routine turn burns substantially more "
                    f"thinking and output tokens, accelerating 5-hour and weekly subscription quota "
                    f"exhaustion. Advisory only (never auto-downgraded): consider setting "
                    f"`\"effortLevel\": \"medium\"` (or `\"high\"` for consistently complex repos) "
                    f"and escalating individual hard turns with `/effort`."
                ),
                "coach_detail": (
                    f"settings.json sets effortLevel: \"{low_val}\" globally, burning extra "
                    "thinking/output tokens against 5-hour and weekly subscription limits on routine turns."
                ),
                "coach_fix": (
                    "Set \"effortLevel\": \"medium\" or \"high\" in settings.json and use /effort "
                    "for hard debugging or architecture turns (never auto-downgraded)."
                ),
            })
        elif low_val == "high":
            findings.append({
                "kind": "settings_effort_high_fyi",
                "severity": "info",
                "bucket": "habits",
                "level": "high",
                "title": "Effort Level Set",
                "markdown": (
                    "**`effortLevel` is set to \"high\" (FYI)**: "
                    "Your settings.json has `effortLevel: \"high\"`. This maximizes response quality "
                    "and thinking depth. If you chose this deliberately, no action needed, "
                    "the optimizer respects your model and effort choices.\n"
                    "  For awareness: \"high\" uses ~15-25% more output tokens per response than \"medium\". "
                    "You can check token usage with `/cost`. Claude's adaptive thinking still adjusts "
                    "within the effort level based on task complexity."
                ),
                "coach_detail": (
                    "effortLevel: \"high\" - deliberate quality choice. Uses ~15-25% more output tokens than \"medium\"."
                ),
                "coach_fix": "",
            })

    try:
        env_effort = facade._resolve_feature_env("CLAUDE_CODE_EFFORT_LEVEL")
    except Exception:
        env_effort = os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")
    if env_effort:
        low_env = str(env_effort).strip().lower()
        if low_env in HIGH_QUOTA_EFFORT_LEVELS:
            findings.append({
                "kind": "env_effort",
                "severity": "medium",
                "bucket": "quick",
                "level": low_env,
                "title": f"`CLAUDE_CODE_EFFORT_LEVEL` env is set to \"{low_env}\"",
                "markdown": (
                    f"**Review `CLAUDE_CODE_EFFORT_LEVEL={low_env}`**: "
                    f"This environment variable overrides `settings.json` globally for every session. "
                    f"Defaulting to `{low_env}` multiplies thinking-token usage on simple turns and "
                    f"depletes your weekly subscription quota faster. Advisory only (never auto-downgraded): "
                    f"unset `CLAUDE_CODE_EFFORT_LEVEL` or set it to `medium`/`high` and use `/effort` per task."
                ),
                "coach_detail": (
                    f"CLAUDE_CODE_EFFORT_LEVEL={low_env} overrides settings globally, draining weekly "
                    "quota on routine turns."
                ),
                "coach_fix": (
                    "Unset CLAUDE_CODE_EFFORT_LEVEL or lower it to medium/high; escalate per turn with /effort."
                ),
            })

    high_effort_agents = [
        (ag["name"], str((ag.get("frontmatter") or {}).get("effort", "")).strip().lower())
        for ag in _scan_claude_agents(facade, cwd=cwd)
        if str((ag.get("frontmatter") or {}).get("effort", "")).strip().lower() in HIGH_QUOTA_EFFORT_LEVELS
    ]
    if high_effort_agents:
        labels = ", ".join(f"{name} ({lvl})" for name, lvl in high_effort_agents[:8])
        more = f" (+{len(high_effort_agents) - 8} more)" if len(high_effort_agents) > 8 else ""
        findings.append({
            "kind": "subagent_effort",
            "severity": "medium",
            "bucket": "medium",
            "agents": high_effort_agents,
            "title": f"{len(high_effort_agents)} subagent(s) default to xhigh/max effort",
            "markdown": (
                f"**Review high-effort subagents ({labels}{more})**: "
                "Subagents with `effort: xhigh` or `effort: max` multiply thinking-token burn on every "
                "delegation, eating directly into your 5-hour and weekly subscription quota. "
                "Advisory only (never auto-downgraded): lower routine explore/review subagents to "
                "`effort: medium` or `effort: high` in their YAML frontmatter."
            ),
            "coach_detail": (
                f"{len(high_effort_agents)} subagent(s) set effort to xhigh/max ({labels}{more}), "
                "multiplying weekly quota burn per dispatch."
            ),
            "coach_fix": (
                "Review subagent frontmatter and set effort: medium or high unless maximum reasoning is required."
            ),
        })
    return findings


def audit_codex_effort(facade, cwd: Path | None = None) -> list[dict]:
    """Flag xhigh/max default model_reasoning_effort in Codex config.toml. Advisory only."""
    findings: list[dict] = []
    cfg, _ = _load_merged_codex_config(facade, cwd=cwd)
    for key in ("model_reasoning_effort", "plan_mode_reasoning_effort"):
        raw = cfg.get(key)
        low_val = str(raw).strip().lower() if raw else ""
        if low_val in HIGH_QUOTA_EFFORT_LEVELS:
            findings.append({
                "kind": f"codex_{key}",
                "severity": "medium",
                "bucket": "quick" if key == "model_reasoning_effort" else "medium",
                "key": key,
                "level": low_val,
                "title": f"Codex `{key}` is set to \"{low_val}\"",
                "markdown": (
                    f"**Lower Codex `{key}` from \"{low_val}\" to \"medium\" or \"high\"**: "
                    f"Your Codex `config.toml` sets `{key} = \"{low_val}\"`. "
                    f"Running `{low_val}` reasoning effort by default consumes significantly more "
                    f"reasoning tokens on routine turns and burns through weekly subscription quota faster. "
                    f"Advisory only (never auto-downgraded): set `{key} = \"medium\"` (or `\"high\"`) "
                    f"in `~/.codex/config.toml` and raise effort only for deep debugging or architecture tasks."
                ),
                "coach_detail": (
                    f"Codex config.toml sets {key} = \"{low_val}\", increasing reasoning-token "
                    "consumption against weekly subscription limits."
                ),
                "coach_fix": (
                    f"Set {key} = \"medium\" or \"high\" in ~/.codex/config.toml (never auto-downgraded)."
                ),
            })
    return findings


# ---------------------------------------------------------------------------
# 2. Subagent governance (.claude/agents/*.md and plugin agents)
# ---------------------------------------------------------------------------

def audit_subagent_governance(facade, cwd: Path | None = None) -> list[dict]:
    """Flag custom and plugin subagents missing maxTurns, model, effort, or tool caps."""
    flagged = []
    for ag in _scan_claude_agents(facade, cwd=cwd):
        fm = ag.get("frontmatter") or {}
        missing = [k for k in ("maxTurns", "model", "effort") if not fm.get(k)]
        if not (fm.get("tools") or fm.get("disallowedTools") or fm.get("mcpServers")):
            missing.append("tools/disallowedTools")
        if missing:
            flagged.append({"name": ag["name"], "path": ag["path"], "source": ag["source"], "missing": missing})
    if not flagged:
        return []

    summary_items = [f"{item['name']} (missing {', '.join(item['missing'])})" for item in flagged[:6]]
    more = f" (+{len(flagged) - 6} more)" if len(flagged) > 6 else ""
    detail_list = "; ".join(summary_items) + more
    return [{
        "kind": "subagent_governance",
        "severity": "medium",
        "bucket": "medium",
        "agents": flagged,
        "title": f"Subagent Governance Gaps ({len(flagged)} agent(s))",
        "markdown": (
            f"**Add governance caps to {len(flagged)} subagent(s)**: {detail_list}. "
            "Subagents without explicit caps inherit the parent model, effort level, and full MCP/tool "
            "surface and can loop indefinitely, draining subscription quota.\n"
            "  Suggested frontmatter caps (adjust per agent role): "
            "`model: sonnet` (or `haiku` for explore/search), `effort: medium`, "
            "`maxTurns: 20` (15-25 read-only, 30-50 edit agents), and `tools:` or `disallowedTools:` "
            "to avoid inheriting unneeded MCP/write tools."
        ),
        "coach_detail": f"{len(flagged)} subagent(s) lack governance caps: {detail_list}.",
        "coach_fix": (
            "Add maxTurns, model, effort, and tools/disallowedTools to .claude/agents/*.md "
            "(and review plugin agent defaults)."
        ),
    }]


# ---------------------------------------------------------------------------
# 3. Skills producing noisy output without `context: fork`
# ---------------------------------------------------------------------------

def audit_noisy_skills_without_fork(facade, trends: dict | None = None) -> list[dict]:
    """Suggest `context: fork` for skills that run noisy multi-tool workflows inline."""
    candidates = []
    seen: set[Path] = set()
    try:
        skill_dirs = list(facade._skill_scan_dirs())
    except Exception:
        skill_dirs = []
    skill_usage = (trends or {}).get("skill_usage") or {}
    if not isinstance(skill_usage, dict):
        skill_usage = {}

    for sdir in skill_dirs:
        sdir_path = Path(sdir)
        if not sdir_path.is_dir():
            continue
        try:
            items = sorted(sdir_path.iterdir())
        except OSError:
            continue
        for item in items:
            skill_md = item / "SKILL.md"
            if not item.is_dir() or not skill_md.is_file():
                continue
            try:
                resolved = skill_md.resolve()
            except OSError:
                resolved = skill_md
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = parse_yaml_frontmatter(text)
            if str(fm.get("context", "")).strip().lower() == "fork":
                continue
            body = _strip_frontmatter(text)
            if _INTERACTIVE_SKILL_RE.search(body):
                continue

            noisy_hits = sorted({m.group(1).lower() for m in _NOISY_SKILL_RE.finditer(f"{fm.get('description', '')}\n{body}")})
            allowed_tools = fm.get("allowed-tools") or fm.get("allowedTools") or []
            if isinstance(allowed_tools, str):
                allowed_tools = [t.strip() for t in allowed_tools.split(",") if t.strip()]
            has_noisy_tools = any(t.lower() in ("bash", "grep", "webfetch", "websearch") for t in allowed_tools)

            if len(noisy_hits) >= 2 or (noisy_hits and (has_noisy_tools or len(body) >= 300)):
                name = _clean_label(fm.get("name") or item.name, facade)
                uses = int(skill_usage.get(name) or skill_usage.get(item.name) or 0)
                candidates.append({"name": name, "path": str(skill_md), "signals": noisy_hits[:4], "uses": uses})

    if not candidates:
        return []
    candidates.sort(key=lambda c: (-c["uses"], c["name"]))
    names = ", ".join(c["name"] for c in candidates[:8])
    more = f" (+{len(candidates) - 8} more)" if len(candidates) > 8 else ""
    return [{
        "kind": "skill_context_fork",
        "severity": "low",
        "bucket": "medium",
        "skills": candidates,
        "title": f"Noisy Skills Without `context: fork` ({len(candidates)})",
        "markdown": (
            f"**Add `context: fork` to {len(candidates)} noisy skill(s) ({names}{more})**: "
            "These skills run multi-step test, log, search, or audit workflows inline, dumping "
            "verbose intermediate tool outputs into your main conversation context.\n"
            "  Add `context: fork` to the `SKILL.md` YAML frontmatter so noisy tool loops run "
            "in an isolated forked subagent and only the distilled summary returns to the main thread. "
            "(Skip `context: fork` for skills that need interactive Q&A or leave edits in main-thread memory.)"
        ),
        "coach_detail": f"{len(candidates)} skill(s) ({names}{more}) run verbose/noisy workflows inline without context: fork.",
        "coach_fix": "Add `context: fork` to SKILL.md YAML frontmatter for read-heavy/noisy skills so only the summary returns.",
    }]


# ---------------------------------------------------------------------------
# 4. Monorepo CLAUDE.md stack -> .claude/rules (paths:) + claudeMdExcludes
# ---------------------------------------------------------------------------

def _scan_nested_claude_md(cwd: Path, facade, existing_real: set[Path]) -> list[dict]:
    """Bounded scan (depth <= 3, <= 200 dirs) for nested package CLAUDE.md files."""
    found: list[dict] = []
    visited = 0
    queue: list[tuple[Path, int]] = [(cwd, 0)]
    while queue and visited < 200:
        cur, depth = queue.pop(0)
        if depth > 3:
            continue
        visited += 1
        try:
            children = sorted(cur.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name in _SKIP_WALK_DIRS or (child.name.startswith(".") and child.name != ".claude"):
                continue
            if depth < 3:
                queue.append((child, depth + 1))
            for cand in (child / "CLAUDE.md", child / ".claude" / "CLAUDE.md"):
                if not cand.is_file():
                    continue
                try:
                    real = cand.resolve()
                except OSError:
                    real = cand
                if real in existing_real:
                    continue
                existing_real.add(real)
                try:
                    tokens = int(facade.estimate_tokens_from_file(cand))
                except Exception:
                    tokens = max(1, cand.stat().st_size // 4)
                found.append({"path": str(cand), "tokens": tokens, "nested": True})
    return found


def audit_monorepo_claude_md(components: dict, facade, cwd: Path | None = None) -> list[dict]:
    """Flag large ancestor/nested CLAUDE.md stacks and suggest path-scoped rules or claudeMdExcludes."""
    stack: list[dict] = []
    seen_real: set[Path] = set()
    for key, info in sorted(components.items()):
        if not key.startswith("claude_md") or not isinstance(info, dict) or not info.get("exists"):
            continue
        raw_path = info.get("path", "")
        if raw_path:
            try:
                seen_real.add(Path(raw_path).resolve())
            except OSError:
                pass
        stack.append({"key": key, "path": raw_path, "tokens": int(info.get("tokens", 0) or 0), "nested": False})

    base = Path(cwd) if cwd is not None else Path(facade.Path.cwd())
    nested = _scan_nested_claude_md(base, facade, seen_real)
    all_files = stack + nested
    ancestor_tokens = sum(f["tokens"] for f in stack)
    total_tokens = sum(f["tokens"] for f in all_files)

    domain_headings: list[str] = []
    for f in stack:
        if f["tokens"] >= MONOREPO_LARGE_ROOT_TOKENS and f["path"] and Path(f["path"]).is_file():
            try:
                hits = _DOMAIN_HEADING_RE.findall(Path(f["path"]).read_text(encoding="utf-8", errors="replace"))
                if len(hits) >= 2:
                    domain_headings.extend(sorted({h.lower() for h in hits}))
            except OSError:
                pass

    is_monorepo_stack = (
        len(all_files) >= MONOREPO_MIN_FILES
        or (len(stack) >= 2 and ancestor_tokens >= MONOREPO_MIN_COMBINED_TOKENS)
    )
    if not is_monorepo_stack and not domain_headings:
        return []

    try:
        excludes_configured = facade._resolve_settings_value("claudeMdExcludes", None)
    except Exception:
        excludes_configured = None
    has_excludes = isinstance(excludes_configured, list) and len(excludes_configured) > 0

    exclude_clause = (
        "" if has_excludes else (
            " For unrelated monorepo packages or vendor trees, add `\"claudeMdExcludes\"` globs "
            "(for example `[\"**/packages/legacy-*/**\"]`) to `.claude/settings.local.json` "
            "(managed policy files cannot be excluded)."
        )
    )
    domain_clause = (
        f" Detected domain-scoped sections ({', '.join(domain_headings[:5])}) that can load lazily."
        if domain_headings else ""
    )
    return [{
        "kind": "monorepo_claude_md",
        "severity": "medium",
        "bucket": "medium",
        "file_count": len(all_files),
        "ancestor_count": len(stack),
        "nested_count": len(nested),
        "total_tokens": total_tokens,
        "has_claude_md_excludes": has_excludes,
        "title": f"Monorepo CLAUDE.md Stack ({len(all_files)} files, ~{total_tokens:,} tokens)",
        "markdown": (
            f"**Scope monorepo CLAUDE.md stack ({len(all_files)} files, ~{total_tokens:,} tokens)**: "
            "Claude Code concatenates ancestor `CLAUDE.md` files on every turn, and nested package "
            f"files add further overhead as you traverse the repo.{domain_clause}\n"
            "  Move path-specific instructions into `.claude/rules/<topic>.md` with `paths:` YAML "
            f"frontmatter so they stay at 0 tokens until matching files are touched.{exclude_clause}"
        ),
        "coach_detail": (
            f"{len(all_files)} CLAUDE.md files (~{total_tokens:,} tokens) across ancestor/nested stack"
            f"{' without claudeMdExcludes' if not has_excludes else ''}."
        ),
        "coach_fix": (
            "Split path-specific sections into .claude/rules/*.md with paths: frontmatter"
            + ("" if has_excludes else " and exclude unrelated packages via claudeMdExcludes in .claude/settings.local.json")
            + "."
        ),
    }]


# ---------------------------------------------------------------------------
# 5. Codex MCP: servers whose tools are never used in Codex session logs
# ---------------------------------------------------------------------------

def _extract_codex_mcp_servers_used(session_files: list[Path]) -> tuple[set[str], int]:
    """Return (set of used MCP server names, number of readable sessions)."""
    used: set[str] = set()
    sessions_read = 0
    for jf in session_files:
        path = Path(jf)
        if not path.is_file():
            continue
        sessions_read += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "mcp" not in line.lower():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
                    if not isinstance(payload, dict):
                        continue
                    ptype = str(payload.get("type") or "")
                    if ptype in ("function_call", "custom_tool_call"):
                        name = str(payload.get("name") or "")
                        if name.startswith("mcp__"):
                            parts = name.split("__")
                            if len(parts) >= 3 and parts[1]:
                                used.add(parts[1])
                    elif ptype in ("mcp_tool_call_begin", "mcp_tool_call_end"):
                        inv = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
                        srv = payload.get("server") or payload.get("server_name") or inv.get("server") or inv.get("server_name")
                        if isinstance(srv, str) and srv.strip():
                            used.add(srv.strip())
        except OSError:
            continue
    return used, sessions_read


def audit_codex_mcp_unused(facade, cwd: Path | None = None, session_files=None) -> list[dict]:
    """Flag enabled Codex MCP servers never used in Codex session logs; suggest enabled_tools/disabled_tools."""
    cfg, _ = _load_merged_codex_config(facade, cwd=cwd)
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict) or not servers:
        return []
    enabled_servers = {
        str(name): (scfg if isinstance(scfg, dict) else {})
        for name, scfg in servers.items()
        if not (isinstance(scfg, dict) and scfg.get("enabled", True) is False)
    }
    if not enabled_servers:
        return []

    if session_files is None:
        try:
            session_paths = [Path(e[0]) for e in facade.codex_session.find_all_jsonl_files(days=30, max_files=200)]
        except Exception:
            session_paths = []
    else:
        session_paths = [Path(p) for p in session_files]

    used_servers, sessions_read = _extract_codex_mcp_servers_used(session_paths)
    if sessions_read == 0:
        return []

    unused = [
        {"name": _clean_label(name, facade), "has_tool_filter": bool(sdict.get("enabled_tools") or sdict.get("disabled_tools"))}
        for name, sdict in sorted(enabled_servers.items())
        if name not in used_servers
    ]
    if not unused:
        return []

    names = ", ".join(u["name"] for u in unused[:8])
    more = f" (+{len(unused) - 8} more)" if len(unused) > 8 else ""
    sample = unused[0]["name"]
    return [{
        "kind": "codex_mcp_unused",
        "severity": "medium",
        "bucket": "medium",
        "unused_servers": unused,
        "sessions_scanned": sessions_read,
        "title": f"Unused Codex MCP Servers ({len(unused)} of {len(enabled_servers)})",
        "markdown": (
            f"**Prune {len(unused)} unused Codex MCP server(s) ({names}{more})**: "
            f"None of these servers' tools appeared in your last {sessions_read} Codex session log(s). "
            "Unlike deferred tool search, Codex loads enabled MCP server tool schemas into context every turn.\n"
            f"  In `~/.codex/config.toml`, restrict each server's tool surface with `enabled_tools` "
            f"or `disabled_tools` (or set `enabled = false` if unused), for example:\n"
            f"  `[mcp_servers.{sample}]` -> `enabled_tools = [\"<needed_tool>\"]` or `disabled_tools = [\"<unused_tool>\"]`."
        ),
        "coach_detail": f"{len(unused)} enabled Codex MCP server(s) ({names}{more}) had 0 tool calls across {sessions_read} session log(s).",
        "coach_fix": "Add enabled_tools / disabled_tools under [mcp_servers.<name>] (or enabled = false) in ~/.codex/config.toml.",
    }]


# ---------------------------------------------------------------------------
# 6. Output style audit + verbosity-steer suppression
# ---------------------------------------------------------------------------

def is_concise_output_style_active(facade) -> bool:
    """Return True when Claude Code has a concise outputStyle configured."""
    try:
        raw = facade._resolve_settings_value("outputStyle", None)
    except Exception:
        return False
    if not isinstance(raw, str):
        return False
    cleaned = raw.strip().lower()
    return bool(cleaned) and any(tok in cleaned for tok in _CONCISE_STYLE_TOKENS)


def count_recent_verbosity_nudges(facade, trends: dict | None = None, days: int = 30) -> int:
    """Count verbosity_steer nudges in the last N days from trends dict or trends.db."""
    if isinstance(trends, dict):
        if isinstance(trends.get("verbosity_steer_count"), int):
            return max(0, trends["verbosity_steer_count"])
        nudges = trends.get("nudges")
        if isinstance(nudges, dict) and isinstance(nudges.get("verbosity_steer"), int):
            return max(0, nudges["verbosity_steer"])

    db_path = getattr(facade, "TRENDS_DB", None)
    if not db_path or not Path(db_path).is_file():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM savings_events WHERE event_type = 'verbosity_steer' AND timestamp >= ?",
                (cutoff,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception:
        return 0


def audit_output_style(facade, trends: dict | None = None, threshold: int = VERBOSITY_NUDGE_OFTEN_THRESHOLD) -> list[dict]:
    """Suggest Claude Code's native Concise outputStyle when verbosity nudges fire often."""
    if is_concise_output_style_active(facade):
        return []
    nudge_count = count_recent_verbosity_nudges(facade, trends=trends)
    if nudge_count < threshold:
        return []
    return [{
        "kind": "output_style_concise",
        "severity": "low",
        "bucket": "quick",
        "nudge_count": nudge_count,
        "title": f"Configure Concise `outputStyle` ({nudge_count} verbosity nudges in 30d)",
        "markdown": (
            f"**Switch to a concise `outputStyle` ({nudge_count} verbosity nudges in 30d)**: "
            "Token Optimizer's `verbosity-steer` hook fired frequently because responses stayed verbose "
            "as context filled, adding per-turn `UserPromptSubmit` nudge tokens.\n"
            "  Set `\"outputStyle\": \"Concise\"` (capital C) in `~/.claude/settings.json` "
            "(or run `/output-style concise`). Native output styles live in the cached system prompt "
            "with zero per-turn injection overhead, and Token Optimizer automatically suppresses "
            "`verbosity-steer` nudges while a concise output style is active."
        ),
        "coach_detail": f"verbosity-steer fired {nudge_count} times in 30 days with no concise outputStyle configured.",
        "coach_fix": "Set \"outputStyle\": \"Concise\" in ~/.claude/settings.json (or run /output-style concise).",
    }]


# ---------------------------------------------------------------------------
# Integration entrypoints for audit.py
# ---------------------------------------------------------------------------

def _dispatch_to_bucket(finding: dict, quick: list, medium: list, deep: list, habits: list) -> None:
    md = finding.get("markdown")
    if not md:
        return
    {"quick": quick, "deep": deep, "habits": habits}.get(finding.get("bucket", "medium"), medium).append(md)


def append_claude_recommendations(components, trends, quick, medium, deep, habits, facade) -> None:
    """Append Claude Code subscription-limit advisory findings to recommendation buckets."""
    for fn, args in (
        (audit_claude_effort, (components, facade)),
        (audit_subagent_governance, (facade,)),
        (audit_noisy_skills_without_fork, (facade, trends)),
        (audit_monorepo_claude_md, (components, facade)),
        (audit_output_style, (facade, trends)),
    ):
        try:
            for finding in fn(*args):
                _dispatch_to_bucket(finding, quick, medium, deep, habits)
        except Exception:
            continue


def append_codex_recommendations(components, trends, quick, medium, deep, habits, facade) -> None:
    """Append Codex subscription-limit advisory findings to recommendation buckets."""
    for fn, args in ((audit_codex_effort, (facade,)), (audit_codex_mcp_unused, (facade,))):
        try:
            for finding in fn(*args):
                _dispatch_to_bucket(finding, quick, medium, deep, habits)
        except Exception:
            continue


def append_coach_findings(components, trends, is_claude, is_codex, patterns_good, patterns_bad, questions, facade) -> None:
    """Append subscription-limit advisory findings to generate_coach_data() outputs."""
    if is_claude:
        runners = (
            (audit_claude_effort, (components, facade)),
            (audit_subagent_governance, (facade,)),
            (audit_noisy_skills_without_fork, (facade, trends)),
            (audit_monorepo_claude_md, (components, facade)),
            (audit_output_style, (facade, trends)),
        )
    elif is_codex:
        runners = ((audit_codex_effort, (facade,)), (audit_codex_mcp_unused, (facade,)))
    else:
        return
    for fn, args in runners:
        try:
            for finding in fn(*args):
                if finding.get("severity") == "info":
                    patterns_good.append({"name": finding["title"], "detail": finding["coach_detail"]})
                else:
                    patterns_bad.append({
                        "name": finding["title"],
                        "severity": finding.get("severity", "medium"),
                        "detail": finding["coach_detail"],
                        "fix": finding["coach_fix"],
                        "savings": "Subscription quota / context hygiene (advisory)",
                    })
        except Exception:
            continue
