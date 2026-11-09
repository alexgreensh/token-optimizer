#!/usr/bin/env python3
"""Unit tests for the Cursor rules + memory audit (U9, Slice E).

The audit lives in the standalone module skills/token-optimizer/scripts/
cursor_audit.py (extracted from the frozen _measure audit provider after
independent review - the u5 tooling has no post-freeze extension path).
Tests import the module directly.

Contract under test:
  - .cursor/rules/*.mdc are scored against the attention curve, with
    frontmatter-driven activation findings (missing frontmatter, manual-only,
    alwaysApply oversized).
  - Plain .md files inside .cursor/rules are flagged as ignored by Cursor.
  - Legacy .cursorrules and AGENTS.md are discovered and scored.
  - ~/.cursor/hooks.json health: missing/invalid/shape/unknown-event/bridge
    detection, third-party and Claude-import coexistence reported as presence
    only (ownership stays with cursor-doctor, KTD9).
  - Honest "not covered" lines always present, including Cursor's native IDE
    Memories (no verified local file surface).
  - Read-only and never fatal: unreadable files become findings, nothing is
    written anywhere.

All fixtures live under tmp_path; no network, no writes outside tmp_path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402  (installs the facade the frozen provider resolves)
import cursor_audit as audit  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _project(tmp_path, files):
    """Create a project dir with the given {relpath: content} files."""
    proj = tmp_path / "proj"
    proj.mkdir()
    for rel, content in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj


def _cursor_home(tmp_path, hooks=None):
    home = tmp_path / "cursor-home"
    home.mkdir(exist_ok=True)
    if hooks is not None:
        (home / "hooks.json").write_text(hooks, encoding="utf-8")
    return home


RULE_MDC = """---
description: React component standards
globs: src/components/**/*.tsx
alwaysApply: false
---
# Components

- Use named exports.
"""

ALWAYS_MDC = """---
alwaysApply: true
---
# House rules

- NEVER commit secrets.
- ALWAYS run tests before pushing.
"""

NO_FRONTMATTER_MDC = "# Loose notes\n\n- Some guidance.\n"

MANUAL_MDC = """---
---
# Empty frontmatter

- Guidance here.
"""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovers_mdc_agents_and_legacy(tmp_path):
    proj = _project(tmp_path, {
        ".cursor/rules/react.mdc": RULE_MDC,
        ".cursor/rules/nested/api.mdc": ALWAYS_MDC,
        ".cursorrules": "- legacy rule\n",
        "AGENTS.md": "# Agents\n\n- Guidance.\n",
    })
    targets = audit._cursor_rule_targets(proj)
    kinds = {t["path"].name: t["kind"] for t in targets}
    assert kinds["react.mdc"] == "project_rule"
    assert kinds["api.mdc"] == "project_rule"
    assert kinds[".cursorrules"] == "legacy_cursorrules"
    assert kinds["AGENTS.md"] == "agents_md"


def test_md_inside_rules_dir_is_ignored_kind(tmp_path):
    proj = _project(tmp_path, {".cursor/rules/notes.md": "# notes\n"})
    targets = audit._cursor_rule_targets(proj)
    assert [t["kind"] for t in targets] == ["ignored_md"]


def test_non_rule_extensions_skipped(tmp_path):
    proj = _project(tmp_path, {
        ".cursor/rules/data.json": "{}",
        ".cursor/rules/script.py": "print(1)",
    })
    assert audit._cursor_rule_targets(proj) == []


# ---------------------------------------------------------------------------
# Frontmatter / activation
# ---------------------------------------------------------------------------

def test_frontmatter_parsing_and_activation_modes():
    meta, body = audit._cursor_parse_rule_frontmatter(RULE_MDC)
    assert meta["description"] == "React component standards"
    assert meta["globs"] == "src/components/**/*.tsx"
    assert audit._cursor_rule_activation(meta) == "glob"
    assert audit._cursor_rule_activation({"alwaysApply": "true"}) == "always"
    assert audit._cursor_rule_activation({"description": "d"}) == "intelligent"
    assert audit._cursor_rule_activation({}) == "manual"
    meta2, body2 = audit._cursor_parse_rule_frontmatter(NO_FRONTMATTER_MDC)
    assert meta2 is None and body2 == NO_FRONTMATTER_MDC
    meta3, _ = audit._cursor_parse_rule_frontmatter("---\nunterminated: true\n")
    assert meta3 is None  # unterminated block -> honest no-frontmatter


# ---------------------------------------------------------------------------
# Per-file audit findings
# ---------------------------------------------------------------------------

def test_ignored_md_finding(tmp_path):
    proj = _project(tmp_path, {".cursor/rules/notes.md": "# notes\n- ALWAYS x.\n"})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "rules_md_ignored" in cats
    f = next(f for f in result["findings"] if f["category"] == "rules_md_ignored")
    assert f["severity"] == "medium"
    assert "ignores" in f["detail"]


def test_mdc_missing_frontmatter_finding(tmp_path):
    proj = _project(tmp_path, {".cursor/rules/loose.mdc": NO_FRONTMATTER_MDC})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    rec = result["rule_files"][0]
    assert rec["activation"] == "manual"
    cats = [f["category"] for f in result["findings"]]
    assert "mdc_missing_frontmatter" in cats


def test_manual_only_rule_finding(tmp_path):
    proj = _project(tmp_path, {".cursor/rules/manual.mdc": MANUAL_MDC})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "manual_only_rule" in cats


def test_always_apply_oversized_finding(tmp_path):
    # CHARS_PER_TOKEN=4.0 and the advisory budget is 1000 tokens -> >4000 chars.
    big = "---\nalwaysApply: true\n---\n# Big\n\n" + ("padding line\n" * 400)
    proj = _project(tmp_path, {".cursor/rules/big.mdc": big})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    rec = result["rule_files"][0]
    assert rec["activation"] == "always"
    assert rec["tokens"] > 1000  # advisory budget mirrors audit.py local (split policy: no module constants)
    cats = [f["category"] for f in result["findings"]]
    assert "always_apply_oversized" in cats


def test_critical_rule_low_zone_finding(tmp_path):
    # Critical rule in the middle third of the file.
    top = "- ordinary guidance line\n" * 40
    mid = "- NEVER leak API keys.\n"
    bottom = "- more ordinary guidance\n" * 40
    body = "---\nalwaysApply: true\n---\n# Rules\n\n" + top + mid + bottom
    proj = _project(tmp_path, {".cursor/rules/security.mdc": body})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    rec = result["rule_files"][0]
    assert rec["attention_score"] is not None and rec["attention_score"] < 100
    cats = [f["category"] for f in result["findings"]]
    assert "critical_rule_low_zone" in cats


def test_duplicate_rule_across_files(tmp_path):
    proj = _project(tmp_path, {
        ".cursor/rules/a.mdc": ALWAYS_MDC,
        "AGENTS.md": "# Agents\n\n- NEVER commit secrets.\n",
    })
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "duplicate_rule" in cats
    f = next(f for f in result["findings"] if f["category"] == "duplicate_rule")
    assert "a.mdc" in f["detail"] and "AGENTS.md" in f["detail"]


def test_legacy_cursorrules_finding(tmp_path):
    proj = _project(tmp_path, {".cursorrules": "- old rule\n"})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "legacy_cursorrules" in cats


# ---------------------------------------------------------------------------
# Empty / error paths: never fatal
# ---------------------------------------------------------------------------

def test_no_rules_is_clean_not_error(tmp_path):
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["rule_files"] == []
    assert result["summary"]["rule_files"] == 0
    assert not any(f["severity"] == "high" for f in result["findings"])


def test_missing_project_dir_is_finding_not_crash(tmp_path):
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=tmp_path / "nope", cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "project_dir_missing" in cats


def test_unreadable_rule_file_skipped_with_named_finding(tmp_path, monkeypatch):
    proj = _project(tmp_path, {".cursor/rules/react.mdc": RULE_MDC})
    home = _cursor_home(tmp_path)
    orig_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name == "react.mdc":
            raise PermissionError("denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "unreadable" in cats
    f = next(f for f in result["findings"] if f["category"] == "unreadable")
    assert "react.mdc" in f["detail"]
    assert "PermissionError" in f["detail"]


def test_oversized_file_skipped(tmp_path):
    # 1_000_000 mirrors the audit.py local read cap (split policy: no module constants)
    big = "---\nalwaysApply: false\n---\n" + ("x" * (1_000_000 + 10))
    proj = _project(tmp_path, {".cursor/rules/huge.mdc": big})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    cats = [f["category"] for f in result["findings"]]
    assert "skipped_too_large" in cats


# ---------------------------------------------------------------------------
# hooks.json health
# ---------------------------------------------------------------------------

def test_hooks_missing_is_info(tmp_path):
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["present"] is False
    f = next(f for f in result["findings"] if f["category"] == "hooks_json_missing")
    assert f["severity"] == "info"


def test_hooks_invalid_json_is_high(tmp_path):
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path, hooks="{not json")
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["valid"] is False
    f = next(f for f in result["findings"] if f["category"] == "hooks_json_invalid")
    assert f["severity"] == "high"


def test_hooks_detects_bridge_and_third_party(tmp_path):
    hooks = json.dumps({"hooks": {
        "sessionStart": [{"command": "python3 /x/cursor_hook_bridge.py sessionStart", "type": "command"}],
        "preToolUse": [
            {"command": "python3 /x/cursor_hook_bridge.py preToolUse", "type": "command", "matcher": "Shell"},
            {"command": "/usr/local/bin/other-tool --flag", "type": "command"},
        ],
    }})
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path, hooks=hooks)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    h = result["hooks"]
    assert h["valid"] is True
    assert sorted(h["to_bridge_events"]) == ["preToolUse", "sessionStart"]
    assert h["third_party_entry_count"] == 1
    cats = [f["category"] for f in result["findings"]]
    assert "third_party_hooks" in cats
    assert "bridge_not_wired" not in cats
    # Ownership must not be assigned by this audit.
    f = next(f for f in result["findings"] if f["category"] == "third_party_hooks")
    assert "cursor-doctor" in f["fix"]


def test_hooks_unknown_event_advisory(tmp_path):
    hooks = json.dumps({"hooks": {"onQuantumFlux": [{"command": "x", "type": "command"}]}})
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path, hooks=hooks)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    f = next(f for f in result["findings"] if f["category"] == "hooks_unknown_event")
    assert f["severity"] == "low"
    assert "onQuantumFlux" in f["detail"]


def test_hooks_malformed_event_entry(tmp_path):
    hooks = json.dumps({"hooks": {"stop": {"command": "x"}}})
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path, hooks=hooks)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert any(f["category"] == "hooks_json_shape" for f in result["findings"])


def test_hooks_bridge_not_wired_info(tmp_path):
    hooks = json.dumps({"hooks": {"stop": [{"command": "/bin/other", "type": "command"}]}})
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path, hooks=hooks)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert any(f["category"] == "bridge_not_wired" for f in result["findings"])


def test_claude_import_coexistence_reported_as_presence_only(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude-home"
    claude_dir.mkdir()
    settings = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "python3 /x/skills/token-optimizer/scripts/measure.py session-start"}]}]}}
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude_dir)
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["claude_import_coexistence"] is True
    f = next(f for f in result["findings"] if f["category"] == "claude_import_coexistence")
    assert f["severity"] == "info"
    assert "cursor-doctor" in f["fix"]


def test_project_claude_settings_coexistence_detected(tmp_path, monkeypatch):
    """Project-level .claude/settings.json is also imported by Cursor (KTD9)."""
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "no-user-claude")
    settings = {"hooks": {"PostToolUse": [{"hooks": [
        {"type": "command", "command": "python3 /x/measure.py post-tool"}]}]}}
    proj = _project(tmp_path, {".claude/settings.json": json.dumps(settings)})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["claude_import_coexistence"] is True
    f = next(f for f in result["findings"] if f["category"] == "claude_import_coexistence")
    assert str(proj / ".claude" / "settings.json") in f["detail"]


def test_project_claude_settings_local_coexistence_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "no-user-claude")
    settings = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "python3 /x/token-optimizer/scripts/measure.py s"}]}]}}
    proj = _project(tmp_path, {".claude/settings.local.json": json.dumps(settings)})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["claude_import_coexistence"] is True


def test_project_claude_settings_without_to_hooks_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "no-user-claude")
    settings = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "/bin/other-tool"}]}]}}
    proj = _project(tmp_path, {".claude/settings.json": json.dumps(settings)})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["claude_import_coexistence"] is False


def test_no_claude_dir_no_false_coexistence(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "no-such-dir")
    proj = _project(tmp_path, {})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["hooks"]["claude_import_coexistence"] is False
    assert not any(f["category"] == "claude_import_coexistence" for f in result["findings"])


# ---------------------------------------------------------------------------
# Honest coverage + read-only guarantee
# ---------------------------------------------------------------------------

def test_not_covered_names_native_memories_and_ownership():
    lines = audit._cursor_not_covered()
    joined = "\n".join(lines).lower()
    assert "memories" in joined           # Cursor native IDE Memories named
    assert "user rules" in joined
    assert "team rules" in joined
    assert "cursor-doctor" in joined      # ownership stays with the doctor


def test_result_shape_and_not_covered_always_present(tmp_path):
    proj = _project(tmp_path, {".cursor/rules/react.mdc": RULE_MDC})
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    assert result["schema_version"] == 1
    assert result["runtime"] == "cursor"
    for key in ("project_dir", "cursor_home", "rule_files", "hooks",
                "findings", "severity_counts", "not_covered", "summary"):
        assert key in result
    assert result["not_covered"] == audit._cursor_not_covered()
    json.dumps(result)  # JSON-serializable


def test_audit_writes_nothing(tmp_path):
    proj = _project(tmp_path, {
        ".cursor/rules/react.mdc": RULE_MDC,
        "AGENTS.md": "# Agents\n",
    })
    hooks = json.dumps({"hooks": {"stop": [{"command": "/bin/x", "type": "c"}]}})
    home = _cursor_home(tmp_path, hooks=hooks)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="no symlink support")
def test_symlinked_rule_escaping_project_flagged_and_not_read(tmp_path):
    proj = _project(tmp_path, {})
    outside = tmp_path / "outside.mdc"
    outside.write_text(ALWAYS_MDC, encoding="utf-8")
    rules = proj / ".cursor" / "rules"
    rules.mkdir(parents=True)
    import os
    os.symlink(outside, rules / "linked.mdc")
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    rec = result["rule_files"][0]
    assert rec["symlink"] is True
    assert rec["resolved"] == str(outside.resolve())
    cats = [f["category"] for f in result["findings"]]
    assert "symlink_escape" in cats
    # The escaping target is flagged but NOT read: no content-derived fields,
    # and no sampled rule text from outside the project leaks into findings.
    assert rec["bytes"] is None
    assert rec["tokens"] is None
    assert rec["attention_score"] is None
    assert rec["low_zone_critical"] == []
    leaked = json.dumps(result["findings"])
    assert "House rules" not in leaked
    assert "NEVER commit secrets" not in leaked


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="no symlink support")
def test_symlinked_rule_inside_project_still_audited(tmp_path):
    proj = _project(tmp_path, {})
    real = proj / "real.mdc"
    real.write_text(RULE_MDC, encoding="utf-8")
    rules = proj / ".cursor" / "rules"
    rules.mkdir(parents=True)
    import os
    os.symlink(real, rules / "linked.mdc")
    home = _cursor_home(tmp_path)
    result = audit.cursor_audit(project_dir=proj, cursor_home=home, as_json=True)
    rec = next(r for r in result["rule_files"] if r["name"] == "linked.mdc")
    assert rec["symlink"] is True
    assert rec["attention_score"] is not None  # inside-boundary links are read
    assert not any(f["category"] == "symlink_escape" for f in result["findings"])


def test_direct_import_without_measure_facade_exercises_claude_dir_fallback(tmp_path):
    """Independent-retest regression: cursor_audit must import and resolve
    _claude_dir() without the measure facade in sys.modules (fallback goes
    through runtime_env.claude_home, not infinite recursion). Attention
    scoring is NOT promised standalone; module import + fallback are."""
    import subprocess

    code = (
        "import sys, json\n"
        "sys.path.insert(0, {scripts!r})\n"
        "import cursor_audit\n"
        "assert 'measure' not in sys.modules\n"
        "print(json.dumps(str(cursor_audit._claude_dir())))\n"
    ).format(scripts=str(SCRIPTS))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    resolved = json.loads(proc.stdout.strip())
    assert resolved.endswith(".claude") or ".claude" in resolved


def test_rule_reached_through_directory_symlink_is_not_read(tmp_path):
    proj = _project(tmp_path, {})
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "secret.mdc").write_text("secret content")
    rules = proj / ".cursor" / "rules"
    rules.mkdir(parents=True)
    try:
        (rules / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    record, findings = audit._cursor_audit_rule_file(rules / "link" / "secret.mdc", "project_rule", proj)
    assert record["bytes"] is None
    assert any(f["category"] == "symlink_escape" for f in findings)
