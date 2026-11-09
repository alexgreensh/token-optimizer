#!/usr/bin/env python3
"""Cursor rules / memory audit (U9, Slice E) - standalone module.

History: these ten symbols first shipped inside the frozen _measure/audit.py
provider (81412770), the first post-freeze provider evolution, which broke
the lane5 frozen-provider proofs (the u5 tooling has no extension path).
Per independent review they now live here as a sibling module beside
cursor_coach.py; the frozen provider, the split manifest and the measure
facade are byte-restored to their historical state, and the frozen
attention-scoring machinery is REUSED by importing the provider read-only
(never duplicated, never modified).

Runtime dependency, stated plainly: the SUPPORTED usage is the measure CLI
(``measure.py cursor-audit``), where the measure facade is installed. The
frozen attention provider resolves that facade at call time, so rule-file
attention scoring requires it; direct module import without measure works
for everything else (hooks health, coexistence detection, file discovery)
and ``_claude_dir`` falls back to runtime_env there.

Scope honesty contract (same style as docs/cursor.md "Honest gaps"):
- Audits FILE surfaces only: .cursor/rules/**/*.mdc (native project rules),
  stray .md files inside .cursor/rules (ignored by Cursor per the official
  docs), legacy .cursorrules, AGENTS.md, and ~/.cursor/hooks.json health.
- Cursor's native IDE Memories, User Rules (Settings UI) and Team Rules
  (dashboard) have no verified local file surface; they are reported as
  not covered, never implied audited.
- Per-channel hook ownership (KTD9) is a demonstrated runtime function
  decided by cursor-doctor; this audit reports config presence only and
  never assigns ownership.
- Read-only: writes nothing anywhere, recurses into no directory symlinks,
  caps the per-file read size, and never ships file content beyond a single
  matched rule line (capped) into a finding. A rule file whose symlink
  target escapes the project directory is flagged but NOT read. The Claude
  settings files (user + project) are read, never written, solely to
  detect hook-import coexistence (KTD9); no other ~/.claude state is read.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from _measure import audit as _frozen_audit  # frozen attention machinery, read-only

CHARS_PER_TOKEN = 4.0  # mirrors the measure facade constant
_MR_RULE_RE = re.compile(r'\b(NEVER|ALWAYS|MUST|CRITICAL|NON-NEGOTIABLE)\b')  # mirrors the measure facade regex


def _claude_dir():
    """Claude home: the measure facade constant wins inside the measure
    runtime (it is env-resolved once at import and test-monkeypatchable);
    standalone falls back to runtime_env."""
    facade = sys.modules.get("measure")
    if facade is not None and hasattr(facade, "CLAUDE_DIR"):
        return facade.CLAUDE_DIR
    from runtime_env import claude_home  # noqa: PLC0415
    return claude_home()


def _cursor_rule_targets(project_dir):
    """Discover Cursor rule/instruction files under a project directory.

    Returns a list of {"path": Path, "kind": str} where kind is one of:
      - "project_rule":       .cursor/rules/**/*.mdc (the documented format)
      - "ignored_md":         .cursor/rules/**/*.md (Cursor ignores these --
                              no frontmatter surface; docs say use .mdc or
                              AGENTS.md instead)
      - "legacy_cursorrules": <project>/.cursorrules (legacy single file)
      - "agents_md":          <project>/AGENTS.md (documented alternative)
    Directory symlinks are never recursed into (pathlib glob default); a
    symlinked rule FILE is still read (Cursor reads it too) and flagged by
    the caller.
    """
    root = Path(project_dir)
    targets = []
    rules_dir = root / ".cursor" / "rules"
    try:
        is_dir = rules_dir.is_dir()
    except OSError:
        is_dir = False
    if is_dir:
        try:
            candidates = sorted(rules_dir.rglob("*"))
        except OSError:
            candidates = []
        for f in candidates:
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            suffix = f.suffix.lower()
            if suffix == ".mdc":
                targets.append({"path": f, "kind": "project_rule"})
            elif suffix == ".md":
                targets.append({"path": f, "kind": "ignored_md"})
            # other extensions: not rule files, skip silently
    legacy = root / ".cursorrules"
    try:
        if legacy.is_file():
            targets.append({"path": legacy, "kind": "legacy_cursorrules"})
    except OSError:
        pass
    agents = root / "AGENTS.md"
    try:
        if agents.is_file():
            targets.append({"path": agents, "kind": "agents_md"})
    except OSError:
        pass
    return targets


def _cursor_parse_rule_frontmatter(text):
    """Parse the leading ``---`` block of an .mdc rule file.

    Returns (meta, body) where meta maps lowercased keys to raw string
    values, or (None, text) when there is no complete frontmatter block.
    Deliberately a line parser, not YAML: the documented rule frontmatter is
    three scalar fields (description, globs, alwaysApply) and a YAML
    dependency must never gate an audit. An unterminated block is treated as
    no frontmatter (honest parse, no crash).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    meta = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return None, text


def _cursor_rule_activation(meta):
    """Classify how a rule activates from its frontmatter (docs table).

    alwaysApply: true            -> "always" (injected into every chat)
    globs present                -> "glob" (auto-attached on matching files)
    description present          -> "intelligent" (agent pulls it in)
    nothing usable               -> "manual" (@-mention only)
    """
    if not meta:
        return "manual"
    meta = {str(k).strip().lower(): v for k, v in meta.items()}
    if str(meta.get("alwaysapply", "")).strip().lower() in ("true", "yes"):
        return "always"
    if str(meta.get("globs", "")).strip():
        return "glob"
    if str(meta.get("description", "")).strip():
        return "intelligent"
    return "manual"


def _cursor_rule_lines_findings(file_rules):
    """Cross-file duplicate-rule check. file_rules: {path_str: [rule_line,...]}"""
    rule_text_cap = 120
    seen = {}  # normalized rule -> list of (path, raw_line)
    for path_str, lines in file_rules.items():
        for raw in lines:
            normalized = raw.strip().lower()
            for prefix in ("- ", "* ", "> "):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):]
                    break
            seen.setdefault(normalized, []).append((path_str, raw.strip()))
    findings = []
    for normalized, locations in seen.items():
        files = sorted({loc[0] for loc in locations})
        if len(files) > 1:
            sample = locations[0][1][:rule_text_cap]
            findings.append({
                "category": "duplicate_rule",
                "severity": "low",
                "detail": "Rule appears in {n} files: {files} -- \"{rule}\"".format(
                    n=len(files), files=", ".join(Path(f).name for f in files),
                    rule=sample),
                "files": files,
                "fix": "Keep the rule in one file; duplicated rules cost tokens in every chat that loads both.",
                "savings": 0,
            })
    return findings


def _cursor_audit_rule_file(path, kind, project_root):
    """Audit one rule file. Returns (record, findings); never raises.

    record carries the per-file inventory the report renders; findings are
    memory-review-shaped dicts (category/severity/detail/fix/savings).
    """
    rule_max_bytes = 1_000_000
    # Advisory budget, not a host-documented limit: an alwaysApply rule is
    # injected into every chat, so past this rough token estimate it deserves
    # a trim or glob scoping. Same rough-estimate convention as the
    # memory-review detectors (chars / CHARS_PER_TOKEN).
    always_apply_token_budget = 1000
    rule_text_cap = 120
    # Below this many non-blank lines a file is short enough that nothing is
    # really "buried in the middle"; the low-zone finding would be noise. The
    # attention SCORE is still computed and reported for every file.
    low_zone_min_nonblank_lines = 30
    record = {
        "path": str(path),
        "name": path.name,
        "kind": kind,
        "symlink": False,
        "bytes": None,
        "lines": None,
        "tokens": None,
        "attention_score": None,
        "low_zone_critical": [],
        "activation": None,
    }
    findings = []

    try:
        record["symlink"] = path.is_symlink()
    except OSError:
        pass
    if record["symlink"]:
        try:
            resolved = path.resolve()
            record["resolved"] = str(resolved)
        except OSError:
            resolved = None
            record["resolved"] = None
        # A symlink whose target escapes the project is flagged and NOT read:
        # its content sits outside the audited boundary, and even a capped
        # rule-line sample would cross it.
        escapes = False
        if resolved is not None:
            try:
                root_resolved = Path(project_root).resolve()
                escapes = not str(resolved).startswith(str(root_resolved) + os.sep)
            except OSError:
                escapes = True
        else:
            escapes = True  # unresolvable link: prove nothing, read nothing
        if escapes:
            findings.append({
                "category": "symlink_escape",
                "severity": "low",
                "detail": f"'{path.name}' is a symlink to {record['resolved'] or 'an unresolvable target'} outside the project directory; not audited (outside the project boundary)",
                "fix": "Vendoring the rule into the project keeps it version-controlled with the repo and auditable.",
                "savings": 0,
            })
            return record, findings

    try:
        size = path.stat().st_size
    except OSError as exc:
        findings.append({
            "category": "unreadable",
            "severity": "medium",
            "detail": f"Could not stat '{path}' ({type(exc).__name__}); skipped",
            "fix": "Check the file's permissions.",
            "savings": 0,
        })
        return record, findings
    record["bytes"] = size
    if size > rule_max_bytes:
        findings.append({
            "category": "skipped_too_large",
            "severity": "medium",
            "detail": f"'{path.name}' is {size:,} bytes (over the {rule_max_bytes:,}-byte audit cap); skipped",
            "fix": "A rule file this large is a context liability regardless of the audit; split it.",
            "savings": 0,
        })
        return record, findings
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        findings.append({
            "category": "unreadable",
            "severity": "medium",
            "detail": f"Could not read '{path}' ({type(exc).__name__}); skipped",
            "fix": "Check the file's permissions.",
            "savings": 0,
        })
        return record, findings

    record["lines"] = len(text.splitlines())
    record["tokens"] = int(len(text) / CHARS_PER_TOKEN)

    meta, body = None, text
    if kind == "project_rule":
        meta, body = _cursor_parse_rule_frontmatter(text)
        activation = _cursor_rule_activation(meta)
        record["activation"] = activation
        if meta is None:
            findings.append({
                "category": "mdc_missing_frontmatter",
                "severity": "medium",
                "detail": f"'{path.name}' has no frontmatter; Cursor can only apply it via manual @-mention",
                "fix": "Add frontmatter with description, globs, or alwaysApply so the rule activates on its own.",
                "savings": 0,
            })
        elif activation == "manual":
            findings.append({
                "category": "manual_only_rule",
                "severity": "low",
                "detail": f"'{path.name}' sets no description, globs, or alwaysApply; it only applies when @-mentioned",
                "fix": "Add a description (agent picks it when relevant) or globs (auto-attached to matching files).",
                "savings": 0,
            })
        if activation == "always" and record["tokens"] > always_apply_token_budget:
            findings.append({
                "category": "always_apply_oversized",
                "severity": "medium",
                "detail": f"'{path.name}' is alwaysApply and ~{record['tokens']:,} tokens; it is injected into every chat",
                "fix": "Trim it, or scope it with globs so only matching work pays for it.",
                "savings": record["tokens"] - always_apply_token_budget,
            })

    if kind == "ignored_md":
        findings.append({
            "category": "rules_md_ignored",
            "severity": "medium",
            "detail": f"'{path.name}' is a plain .md file inside .cursor/rules; Cursor's rules system ignores it (no frontmatter surface)",
            "fix": "Rename to .mdc with frontmatter, or move the content to AGENTS.md.",
            "savings": 0,
        })
    if kind == "legacy_cursorrules":
        findings.append({
            "category": "legacy_cursorrules",
            "severity": "low",
            "detail": "Legacy .cursorrules file; project rules now live in .cursor/rules/*.mdc (or AGENTS.md)",
            "fix": "Migrate to .cursor/rules/*.mdc for scoped activation, or AGENTS.md for plain markdown.",
            "savings": 0,
        })

    # Attention scoring: critical rules (NEVER/ALWAYS/MUST/...) that land in
    # the low-attention middle of the file. Body only for .mdc (frontmatter is
    # activation metadata, not model-attended content); whole file otherwise.
    score_text = body if kind == "project_rule" else text
    tmp_sections_source = score_text
    # _parse_sections takes a path; reuse it on a synthetic section list when
    # the body differs from the file. Simplest honest route: parse the body
    # directly through the same section shape.
    sections = []
    if tmp_sections_source.strip():
        header_re = re.compile(r'^(#{1,2})\s+(.+)', re.MULTILINE)
        matches = list(header_re.finditer(tmp_sections_source))
        if not matches:
            lines = tmp_sections_source.splitlines()
            sections = [{
                "title": path.name, "level": 0, "content": tmp_sections_source,
                "char_start": 0, "char_end": len(tmp_sections_source), "lines": lines,
            }]
        else:
            if matches[0].start() > 0 and tmp_sections_source[:matches[0].start()].strip():
                pre = tmp_sections_source[:matches[0].start()]
                sections.append({"title": "(preamble)", "level": 0, "content": pre,
                                 "char_start": 0, "char_end": matches[0].start(),
                                 "lines": pre.splitlines()})
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(tmp_sections_source)
                content = tmp_sections_source[m.start():end]
                sections.append({"title": m.group(2).strip(), "level": len(m.group(1)),
                                 "content": content, "char_start": m.start(), "char_end": end,
                                 "lines": content.splitlines()})
    if sections:
        analyzed, _total_chars, _total_tokens = _frozen_audit._analyze_attention_sections(sections)
        record["attention_score"] = _frozen_audit._score_attention(analyzed)
        low_rules = []
        for a in analyzed:
            if a["zone"] == "LOW":
                for rule in a["critical_rules"]:
                    low_rules.append(rule[:rule_text_cap])
        record["low_zone_critical"] = low_rules
        nonblank = len([ln for ln in score_text.splitlines() if ln.strip()])
        if low_rules and nonblank >= low_zone_min_nonblank_lines:
            shown = "; ".join(f'"{r}"' for r in low_rules[:3])
            more = f" (+{len(low_rules) - 3} more)" if len(low_rules) > 3 else ""
            findings.append({
                "category": "critical_rule_low_zone",
                "severity": "low",
                "detail": f"'{path.name}': {len(low_rules)} critical rule(s) sit in the low-attention middle: {shown}{more}",
                "fix": "Move critical rules to the top or bottom of the file (attention-optimize FILE proposes the reorder).",
                "savings": 0,
            })

    # Collect rule lines for the cross-file duplicate check (caller passes
    # them to _cursor_rule_lines_findings).
    record["_rule_lines"] = [
        ln for ln in score_text.splitlines() if _MR_RULE_RE.search(ln)
    ]
    return record, findings


def _cursor_audit_rules(project_dir):
    """Audit all discovered Cursor rule files under project_dir."""
    targets = _cursor_rule_targets(project_dir)
    records = []
    findings = []
    file_rules = {}
    for t in targets:
        record, file_findings = _cursor_audit_rule_file(t["path"], t["kind"], project_dir)
        rule_lines = record.pop("_rule_lines", [])
        if rule_lines:
            file_rules[str(t["path"])] = rule_lines
        records.append(record)
        findings.extend(file_findings)
    findings.extend(_cursor_rule_lines_findings(file_rules))
    return records, findings


def _cursor_hook_commands_from_settings(settings):
    """Extract hook command strings from a Claude Code settings blob."""
    commands = []

    def _walk(node):
        if isinstance(node, dict):
            cmd = node.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if isinstance(hooks, (dict, list)):
        _walk(hooks)
    return commands


def _cursor_audit_hooks(cursor_home, project_dir=None):
    from runtime_env import with_measure_cli as _hint  # noqa: PLC0415
    """Health-check ~/.cursor/hooks.json and note import coexistence.

    Presence-level only: per-channel OWNERSHIP between imported Claude hooks
    and the Cursor bridge is a demonstrated runtime function (KTD9) reported
    by cursor-doctor; this audit never assigns it. Reads Claude settings
    files (user and project) strictly read-only to detect import
    coexistence; nothing else under ~/.claude is read.
    """
    # Events Token Optimizer's adapter knows about. Anything else in
    # hooks.json is reported as unrecognized-by-this-version, never as
    # invalid: Cursor ships new events faster than this set moves.
    known_hook_events = frozenset({
        "sessionStart", "sessionEnd", "stop",
        "preToolUse", "postToolUse", "postToolUseFailure",
        "preCompact", "beforeSubmitPrompt",
        "afterAgentResponse", "afterShellExecution",
        "subagentStart", "subagentStop",
    })
    hooks_path = Path(cursor_home) / "hooks.json"
    result = {
        "path": str(hooks_path),
        "present": False,
        "valid": None,
        "events": [],
        "to_bridge_events": [],
        "third_party_entry_count": 0,
        "claude_import_coexistence": None,
    }
    findings = []

    try:
        exists = hooks_path.is_file()
    except OSError:
        exists = False
    if not exists:
        result["present"] = False
        findings.append({
            "category": "hooks_json_missing",
            "severity": "info",
            "detail": f"No hooks.json at {hooks_path}; the Token Optimizer Cursor bridge is not wired here",
            "fix": _hint("Run: python3 measure.py cursor-install (merged, never overwriting other entries)."),
            "savings": 0,
        })
        # Coexistence is still worth reporting below.
    else:
        result["present"] = True
        try:
            raw = hooks_path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
            result["valid"] = True
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            result["valid"] = False
            findings.append({
                "category": "hooks_json_invalid",
                "severity": "high",
                "detail": f"hooks.json at {hooks_path} is unreadable or invalid JSON ({type(exc).__name__}); Cursor silently runs no hooks from it",
                "fix": "Fix or regenerate the file; cursor-install merges into valid JSON without touching other entries.",
                "savings": 0,
            })
            data = None

        if result["valid"]:
            if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
                findings.append({
                    "category": "hooks_json_shape",
                    "severity": "medium",
                    "detail": "hooks.json has no top-level \"hooks\" object; no hook events are wired",
                    "fix": "Expected shape: {\"hooks\": {\"<event>\": [{\"command\": ...}]}}.",
                    "savings": 0,
                })
            else:
                hooks = data["hooks"]
                for event in sorted(hooks):
                    entries = hooks[event]
                    result["events"].append(event)
                    if event not in known_hook_events:
                        findings.append({
                            "category": "hooks_unknown_event",
                            "severity": "low",
                            "detail": f"hooks.json wires '{event}', which this Token Optimizer version does not recognize (may be valid in newer Cursor releases)",
                            "fix": "No action needed if Cursor documents the event; otherwise remove the entry.",
                            "savings": 0,
                        })
                    if not isinstance(entries, list):
                        findings.append({
                            "category": "hooks_json_shape",
                            "severity": "medium",
                            "detail": f"hooks.json entry for '{event}' is not a list; Cursor will not run it",
                            "fix": "Each event maps to a list of {command, type, ...} objects.",
                            "savings": 0,
                        })
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
                            findings.append({
                                "category": "hooks_json_shape",
                                "severity": "medium",
                                "detail": f"hooks.json entry under '{event}' lacks a string 'command'; Cursor will not run it",
                                "fix": "Each entry needs a 'command' string.",
                                "savings": 0,
                            })
                            continue
                        if "cursor_hook_bridge" in entry["command"]:
                            result["to_bridge_events"].append(event)
                        else:
                            result["third_party_entry_count"] += 1
                if not result["to_bridge_events"]:
                    findings.append({
                        "category": "bridge_not_wired",
                        "severity": "info",
                        "detail": "hooks.json exists but wires no Token Optimizer cursor_hook_bridge entries",
                        "fix": _hint("Run: python3 measure.py cursor-install (merged, never overwriting other entries)."),
                        "savings": 0,
                    })
                if result["third_party_entry_count"]:
                    findings.append({
                        "category": "third_party_hooks",
                        "severity": "info",
                        "detail": (f"hooks.json also wires {result['third_party_entry_count']} non-Token-Optimizer "
                                   "hook entr" + ("y" if result["third_party_entry_count"] == 1 else "ies") +
                                   "; Cursor merges all hook sources (deny > ask > allow, messages concatenate)"),
                        "fix": "No action required. Per-channel ownership is demonstrated at runtime and reported by cursor-doctor; this audit does not assign it.",
                        "savings": 0,
                    })

    # Claude-side TO hook presence: Cursor imports Claude Code hooks by
    # default (KTD9), from BOTH the user settings (~/.claude/settings.json,
    # settings.local.json) and the project settings (.claude/settings.json,
    # .claude/settings.local.json). Detection is config presence only;
    # ownership is never assigned here. Reads are read-only and
    # failure-tolerant.
    claude_settings_paths = [
        Path(_claude_dir()) / "settings.json",
        Path(_claude_dir()) / "settings.local.json",
    ]
    if project_dir is not None:
        project_claude = Path(project_dir) / ".claude"
        claude_settings_paths.append(project_claude / "settings.json")
        claude_settings_paths.append(project_claude / "settings.local.json")
    claude_hits = []
    for settings_path in claude_settings_paths:
        try:
            if not settings_path.is_file():
                continue
            blob = json.loads(settings_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        for cmd in _cursor_hook_commands_from_settings(blob):
            if "token-optimizer" in cmd or "measure.py" in cmd:
                claude_hits.append(str(settings_path))
                break
    if claude_hits:
        result["claude_import_coexistence"] = True
        findings.append({
            "category": "claude_import_coexistence",
            "severity": "info",
            "detail": ("Claude Code Token Optimizer hooks found in " + ", ".join(sorted(set(claude_hits))) +
                       "; Cursor imports Claude hooks by default"),
            "fix": ("No action required. The bridge and the Claude side defer to each other per channel based on "
                    "demonstrated ownership; cursor-doctor reports the owner with evidence."),
            "savings": 0,
        })
    else:
        result["claude_import_coexistence"] = False

    return result, findings


def _cursor_not_covered():
    """What this audit deliberately does NOT claim to cover (honest gaps)."""
    return [
        "Cursor native IDE Memories (Settings > Memories, beta): managed by the IDE with no verified local file surface; "
        "the IDE state DB schema for memories is unverified, so this audit performs no IDE DB reads and does not score them",
        "User Rules (Cursor Settings UI): global, not file-backed; not audited",
        "Team Rules (Cursor dashboard, Team/Enterprise plans): not file-backed; not audited",
        "Cursor's third-party Claude-hook import toggle: an IDE setting with no verified file surface; not audited",
        "Per-channel hook ownership between imported Claude hooks and the Cursor bridge: a demonstrated runtime function "
        "(KTD9) reported by cursor-doctor; this audit reports config presence only",
    ]


def cursor_audit(project_dir=None, cursor_home=None, as_json=None):
    """Cursor rules + memory/setup audit. Read-only, never fatal.

    Scores .cursor/rules/*.mdc, stray .md, legacy .cursorrules and AGENTS.md
    against the same attention curve CLAUDE.md gets, health-checks
    ~/.cursor/hooks.json, and lists what is honestly not covered.
    """

    if cursor_home is None:
        try:
            from runtime_env import cursor_home as _resolve_cursor_home  # noqa: PLC0415
            cursor_home = _resolve_cursor_home()
        except Exception:
            cursor_home = Path.home() / ".cursor"
    cursor_home = Path(cursor_home)

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir).expanduser()
    try:
        project_dir = project_dir.resolve()
    except OSError:
        pass

    project_exists = False
    try:
        project_exists = project_dir.is_dir()
    except OSError:
        project_exists = False

    rule_records = []
    rule_findings = []
    if project_exists:
        rule_records, rule_findings = _cursor_audit_rules(project_dir)
    else:
        rule_findings.append({
            "category": "project_dir_missing",
            "severity": "medium",
            "detail": f"Project directory not found: {project_dir}",
            "fix": "Pass --project-dir PATH for the repository whose Cursor rules should be audited.",
            "savings": 0,
        })

    hooks_result, hooks_findings = _cursor_audit_hooks(cursor_home, project_dir=project_dir if project_exists else None)

    all_findings = rule_findings + hooks_findings
    severity_counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in all_findings:
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    result = {
        "schema_version": 1,
        "runtime": "cursor",
        "project_dir": str(project_dir),
        "cursor_home": str(cursor_home),
        "rule_files": rule_records,
        "hooks": hooks_result,
        "findings": all_findings,
        "severity_counts": severity_counts,
        "not_covered": _cursor_not_covered(),
        "summary": {
            "rule_files": len(rule_records),
            "findings": len(all_findings),
            "high": severity_counts.get("high", 0),
            "medium": severity_counts.get("medium", 0),
            "low": severity_counts.get("low", 0),
            "info": severity_counts.get("info", 0),
        },
    }

    if as_json:
        return result

    # CLI output (memory-review style)
    print(f"\n{'=' * 55}")
    print("  CURSOR RULES + MEMORY AUDIT")
    print(f"{'=' * 55}")
    print(f"\n  Project: {project_dir}")
    print(f"  Cursor home: {cursor_home}")

    print(f"\n  Rule files found: {len(rule_records)}")
    if rule_records:
        for rec in rule_records:
            score = f"{rec['attention_score']}/100" if rec["attention_score"] is not None else "n/a"
            tokens = f"~{rec['tokens']:,} tok" if rec["tokens"] is not None else "?"
            activation = f", {rec['activation']}" if rec.get("activation") else ""
            print(f"    [{rec['kind']}] {rec['name']} ({tokens}, attention {score}{activation})")
    else:
        print("    No Cursor rules configured for this project (.cursor/rules/*.mdc, AGENTS.md, .cursorrules).")

    hooks_line = "absent"
    if hooks_result["present"]:
        if hooks_result["valid"]:
            wired = ", ".join(sorted(set(hooks_result["to_bridge_events"]))) or "none"
            hooks_line = f"valid; TO bridge events: {wired}"
        else:
            hooks_line = "INVALID"
    print(f"\n  hooks.json: {hooks_line}")

    if all_findings:
        sev_order = ["high", "medium", "low", "info"]
        sev_labels = {"high": "CRITICAL", "medium": "WARNING", "low": "NOTICE", "info": "INFO"}
        print(f"\n  Found {len(all_findings)} issue(s):")
        for sev in sev_order:
            sev_findings = [f for f in all_findings if f.get("severity") == sev]
            if not sev_findings:
                continue
            print(f"\n  [{sev_labels[sev]}]")
            for f in sev_findings:
                print(f"    {f['detail']}")
                if f.get("fix"):
                    print(f"      Fix: {f['fix']}")
    else:
        print("\n  No issues found.")

    print("\n  Not covered by this audit:")
    for line in result["not_covered"]:
        print(f"    - {line}")
    print()
    return result
