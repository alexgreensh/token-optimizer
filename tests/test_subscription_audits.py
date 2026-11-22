"""Tests for B3 subscription-limit advisory audits (items 1, 4, 5, 6) and verbosity-steer gating."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import measure  # noqa: E402
from _measure import subscription_audits as sa  # noqa: E402


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate HOME, CLAUDE_DIR, RUNTIME_DIR, TRENDS_DB, and cwd for each test."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    codex_dir = home / ".codex"
    proj_dir = tmp_path / "repo"
    for d in (claude_dir, codex_dir, proj_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)
    monkeypatch.setattr(measure, "HOME", home)
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(measure, "RUNTIME_DIR", codex_dir)
    monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "trends.db")
    monkeypatch.setattr(measure, "_codex_config_cache", None)
    monkeypatch.chdir(proj_dir)
    return {"home": home, "claude_dir": claude_dir, "codex_dir": codex_dir, "proj_dir": proj_dir}


# ---------------------------------------------------------------------------
# 1. Effort audit (Claude settings, env var, Codex config.toml, subagents)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["xhigh", "max"])
def test_claude_effort_flags_xhigh_and_max_without_mutating(isolated_env, level):
    settings_path = isolated_env["claude_dir"] / "settings.json"
    original = json.dumps({"effortLevel": level}, indent=2)
    settings_path.write_text(original, encoding="utf-8")

    components = {"settings_local": {"effortLevel": level}}
    findings = sa.audit_claude_effort(components, measure, cwd=isolated_env["proj_dir"])
    assert len(findings) == 1
    assert findings[0]["kind"] == "settings_effort"
    assert "weekly" in findings[0]["markdown"].lower()
    assert "medium" in findings[0]["markdown"].lower()
    assert "never auto-downgraded" in findings[0]["markdown"].lower()
    assert settings_path.read_text(encoding="utf-8") == original


def test_claude_effort_high_is_informational_and_medium_is_clean(isolated_env):
    f_high = sa.audit_claude_effort({"settings_local": {"effortLevel": "high"}}, measure, cwd=isolated_env["proj_dir"])
    assert len(f_high) == 1
    assert f_high[0]["severity"] == "info"

    f_med = sa.audit_claude_effort({"settings_local": {"effortLevel": "medium"}}, measure, cwd=isolated_env["proj_dir"])
    assert f_med == []


def test_claude_effort_env_var_flagged(isolated_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    findings = sa.audit_claude_effort({"settings_local": {}}, measure, cwd=isolated_env["proj_dir"])
    assert any(f["kind"] == "env_effort" and "CLAUDE_CODE_EFFORT_LEVEL" in f["markdown"] for f in findings)


def test_claude_subagent_effort_flagged_without_mutation(isolated_env):
    agents_dir = isolated_env["proj_dir"] / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    agent_md = agents_dir / "deep-reviewer.md"
    raw = "---\nname: deep-reviewer\nmodel: opus\neffort: xhigh\n---\nReview code.\n"
    agent_md.write_text(raw, encoding="utf-8")

    findings = sa.audit_claude_effort({"settings_local": {}}, measure, cwd=isolated_env["proj_dir"])
    assert any(f["kind"] == "subagent_effort" and "deep-reviewer" in f["markdown"] for f in findings)
    assert agent_md.read_text(encoding="utf-8") == raw


def test_codex_effort_flags_xhigh_and_allows_medium(isolated_env):
    cfg_path = isolated_env["codex_dir"] / "config.toml"
    raw = 'model = "gpt-5.4"\nmodel_reasoning_effort = "xhigh"\n'
    cfg_path.write_text(raw, encoding="utf-8")

    findings = sa.audit_codex_effort(measure, cwd=isolated_env["proj_dir"])
    assert len(findings) == 1
    assert findings[0]["kind"] == "codex_model_reasoning_effort"
    assert "weekly" in findings[0]["markdown"].lower()
    assert cfg_path.read_text(encoding="utf-8") == raw

    cfg_path.write_text('model_reasoning_effort = "medium"\n', encoding="utf-8")
    measure._codex_config_cache = None
    assert sa.audit_codex_effort(measure, cwd=isolated_env["proj_dir"]) == []


# ---------------------------------------------------------------------------
# 4. Monorepo CLAUDE.md stack
# ---------------------------------------------------------------------------

def test_monorepo_claude_md_suggests_rules_and_excludes(isolated_env):
    repo = isolated_env["proj_dir"]
    root_md = repo / "CLAUDE.md"
    root_md.write_text("# Repo\n" + ("rule line\n" * 60), encoding="utf-8")
    for pkg in ("packages/web", "packages/api"):
        p = repo / pkg
        p.mkdir(parents=True)
        (p / "CLAUDE.md").write_text(f"# {pkg}\n" + ("guideline\n" * 50), encoding="utf-8")

    components = {
        "claude_md_project_repo": {"exists": True, "path": str(root_md), "tokens": 900, "lines": 61},
    }
    findings = sa.audit_monorepo_claude_md(components, measure, cwd=repo)
    assert len(findings) == 1
    assert findings[0]["file_count"] == 3
    assert ".claude/rules/" in findings[0]["markdown"]
    assert "claudeMdExcludes" in findings[0]["markdown"]

    # When claudeMdExcludes is already configured, omit the excludes suggestion
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"claudeMdExcludes": ["**/packages/web/**"]}), encoding="utf-8"
    )
    findings_after = sa.audit_monorepo_claude_md(components, measure, cwd=repo)
    assert len(findings_after) == 1
    assert findings_after[0]["has_claude_md_excludes"] is True
    assert "claudeMdExcludes" not in findings_after[0]["markdown"]


# ---------------------------------------------------------------------------
# 5. Codex MCP unused servers
# ---------------------------------------------------------------------------

def test_codex_mcp_unused_servers_flagged_from_session_logs(isolated_env):
    cfg_path = isolated_env["codex_dir"] / "config.toml"
    cfg_path.write_text(
        '[mcp_servers.docs]\ncommand = "npx"\n\n'
        '[mcp_servers.legacy_jira]\ncommand = "npx"\n\n'
        '[mcp_servers.turned_off]\ncommand = "npx"\nenabled = false\n',
        encoding="utf-8",
    )

    log_file = isolated_env["codex_dir"] / "sessions" / "rollout-1.jsonl"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(
        json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "mcp__docs__search"}}) + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "mcp_tool_call_end", "server": "docs", "tool": "search"}}) + "\n",
        encoding="utf-8",
    )

    findings = sa.audit_codex_mcp_unused(measure, cwd=isolated_env["proj_dir"], session_files=[log_file])
    assert len(findings) == 1
    unused_names = [u["name"] for u in findings[0]["unused_servers"]]
    assert unused_names == ["legacy_jira"]
    assert "enabled_tools" in findings[0]["markdown"]
    assert "disabled_tools" in findings[0]["markdown"]

    # Zero session logs -> no false positives
    assert sa.audit_codex_mcp_unused(measure, cwd=isolated_env["proj_dir"], session_files=[]) == []


# ---------------------------------------------------------------------------
# 6. Output style audit + verbosity-steer suppression
# ---------------------------------------------------------------------------

def test_output_style_audit_and_verbosity_steer_suppression(isolated_env, tmp_path):
    db = measure.TRENDS_DB
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE savings_events (timestamp TEXT, event_type TEXT, tokens_saved INTEGER, session_uuid TEXT, detail TEXT)")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for i in range(6):
        conn.execute("INSERT INTO savings_events VALUES (?, 'verbosity_steer', 0, ?, 'tier=strong')", (now_iso, f"s-{i}"))
    conn.commit()
    conn.close()

    # Without concise outputStyle -> audit flags it
    findings = sa.audit_output_style(measure)
    assert len(findings) == 1
    assert findings[0]["nudge_count"] == 6
    assert '"outputStyle": "Concise"' in findings[0]["markdown"]

    # Prepare a degraded session transcript + quality cache so run_verbosity_steer fires
    transcript = tmp_path / "sess-abc123.jsonl"
    transcript.write_text('{"type":"assistant","message":{"usage":{"output_tokens":600}}}\n', encoding="utf-8")
    cache_path = measure._quality_cache_path_for(transcript)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "fill_pct": 80,
        "score": 55,
        "session_efficiency": 60,
        "model_context_window": 200000,
    }), encoding="utf-8")

    # Baseline: no concise outputStyle -> nudge fires
    before_payload = measure.run_verbosity_steer(transcript_path=str(transcript), quiet=True, session_id="sess-abc123")
    assert before_payload
    parsed = json.loads(before_payload)
    assert "additionalContext" in parsed["hookSpecificOutput"]
    assert len(parsed["hookSpecificOutput"]["additionalContext"]) > 50

    # Enable concise outputStyle -> audit clears AND verbosity-steer is suppressed (0 bytes)
    (isolated_env["claude_dir"] / "settings.json").write_text(json.dumps({"outputStyle": "Concise"}), encoding="utf-8")
    assert sa.is_concise_output_style_active(measure) is True
    assert sa.audit_output_style(measure) == []

    # Reset cooldown so suppression is provably due to outputStyle, not cooldown
    cache_path.write_text(json.dumps({
        "fill_pct": 80,
        "score": 55,
        "session_efficiency": 60,
        "model_context_window": 200000,
    }), encoding="utf-8")
    after_payload = measure.run_verbosity_steer(transcript_path=str(transcript), quiet=True, session_id="sess-abc123")
    assert after_payload == ""


# ---------------------------------------------------------------------------
# Guardrails: fail-open, ANSI sanitization, and dropped items absent
# ---------------------------------------------------------------------------

def test_corrupt_files_fail_open_and_strip_ansi(isolated_env):
    agents_dir = isolated_env["proj_dir"] / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "bad.md").write_text("---\nname: \x1b[31mred-agent\x1b[0m\neffort: max\n---\nbody\n", encoding="utf-8")
    (isolated_env["codex_dir"] / "config.toml").write_text("not valid = [toml\n", encoding="utf-8")

    assert sa.audit_codex_effort(measure, cwd=isolated_env["proj_dir"]) == []
    eff = sa.audit_claude_effort({"settings_local": {}}, measure, cwd=isolated_env["proj_dir"])
    assert len(eff) == 1
    assert "\x1b" not in eff[0]["markdown"]
    assert "red-agent" in eff[0]["markdown"]


def test_dropped_items_never_suggested(isolated_env, monkeypatch):
    """Owner decision: never suggest subagent caps (maxTurns/disallowedTools) or skill context: fork."""
    assert not hasattr(sa, "audit_subagent_governance")
    assert not hasattr(sa, "audit_noisy_skills_without_fork")

    # Uncapped agent + noisy skill present on disk -> neither triggers any recommendation
    agents_dir = isolated_env["proj_dir"] / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "helper.md").write_text("---\nname: helper\n---\nHelp.\n", encoding="utf-8")
    skill_dir = isolated_env["claude_dir"] / "skills" / "repo-audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: repo-audit\nallowed-tools: [Bash, Grep]\n---\nRun pytest, git log, and grep.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    comps = {"settings_local": {}, "file_exclusion": {"has_rules": True}}
    plan_md, _ = measure.generate_auto_recommendations(comps, trends={})
    assert "maxTurns" not in plan_md
    assert "disallowedTools" not in plan_md
    assert "context: fork" not in plan_md


def test_end_to_end_recommendations_and_coach_integration(isolated_env, monkeypatch):
    (isolated_env["claude_dir"] / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}), encoding="utf-8")
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    comps = {"settings_local": {"effortLevel": "xhigh"}, "file_exclusion": {"has_rules": True}}
    plan_md, count = measure.generate_auto_recommendations(comps, trends={"verbosity_steer_count": 9})
    assert count >= 2
    assert "effortLevel" in plan_md and "xhigh" in plan_md
    assert "Concise" in plan_md

    coach = measure.generate_coach_data(components=comps, trends={"verbosity_steer_count": 9})
    bad_names = [p["name"] for p in coach["patterns_bad"]]
    assert any("effortLevel" in n for n in bad_names)
    assert any("Concise" in n for n in bad_names)

    # Codex runtime: model_reasoning_effort=xhigh
    (isolated_env["codex_dir"] / "config.toml").write_text('model_reasoning_effort = "xhigh"\n', encoding="utf-8")
    measure._codex_config_cache = None
    monkeypatch.setattr(measure, "detect_runtime", lambda: "codex")
    codex_md, codex_count = measure.generate_auto_recommendations({}, trends={})
    assert codex_count >= 1
    assert "model_reasoning_effort" in codex_md
