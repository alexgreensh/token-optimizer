"""Token Optimizer must NEVER recommend cutting its own skills.

The first thing the tool did for a user was suggest trimming the token-optimizer
bundle (token-coach, fleet-auditor) to "save ~200 tokens" — self-cannibalizing.
The unused-skill / archive recommender now excludes our own measurement skills
regardless of invocation history.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


def test_own_skills_are_recognized():
    for name in (
        "token-optimizer", "token-coach", "token-dashboard", "fleet-auditor",
        "token-optimizer:token-coach", "token-optimizer:quick",
        "token-optimizer:next", "token-optimizer:apply-safe",
        "TOKEN-OPTIMIZER",
    ):
        assert measure._is_own_tool_skill(name), name


def test_third_party_skills_are_not_flagged():
    for name in ("linkedin", "deep-research", "my-notes", "some-random-skill", "", None):
        assert not measure._is_own_tool_skill(name), name


def test_recommendations_never_list_own_skills_for_archiving():
    """generate_auto_recommendations must not name our own skills as unused/archivable."""
    trends = {
        "skills": {
            "never_used": [
                "token-optimizer", "token-coach", "token-dashboard", "fleet-auditor",
                "linkedin", "my-notes", "deep-research", "old-thing",
            ],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    for own in ("token-coach", "fleet-auditor", "token-dashboard"):
        assert own not in plan, f"recommendation plan must not suggest cutting own skill {own}"
    # third-party unused skills should still be surfaced
    assert "linkedin" in plan or "deep-research" in plan


def test_name_only_recommendation_for_claude_runtime(monkeypatch):
    """Rule 3 must offer the harness-aware slim-it tier (skillOverrides name-only +
    disable-model-invocation, which are distinct levers) ABOVE the archive step for
    the Claude runtime, and never target our own skills."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    trends = {
        "skills": {
            "never_used": [
                "token-coach", "token-dashboard",  # own skills -> must be filtered out
                "linkedin", "my-notes", "deep-research", "old-thing", "extra-a", "extra-b",
            ],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)

    # name-only tier is present and references the Claude-Code-specific key
    assert "disable-model-invocation: true" in plan
    assert "name-only" in plan
    # name-only clause sits ABOVE the archive step
    assert plan.index("disable-model-invocation: true") < plan.index("Archive (harder step")
    # own skills still never named
    for own in ("token-coach", "token-dashboard"):
        assert own not in plan


def test_name_only_recommendation_for_claude_runtime_few_unused(monkeypatch):
    """The medium-effort branch (2-4 unused) also surfaces the name-only tier."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    trends = {
        "skills": {
            "never_used": ["linkedin", "my-notes"],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    assert "disable-model-invocation: true" in plan
    assert "name-only" in plan


def test_name_only_recommendation_no_trends_fallback_for_claude(monkeypatch):
    """Rule 3a (no trends data) must also surface the name-only tier for Claude."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    components = {"skills": {"count": 20, "tokens": 4000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=None, days=30)
    assert "disable-model-invocation: true" in plan
    assert "name-only" in plan


def test_codex_runtime_never_claims_name_only(monkeypatch):
    """Codex has no per-skill name-only primitive. Its recommendations must NOT
    claim a `disable-model-invocation` name-only option exists."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "codex")
    trends = {
        "skills": {
            "never_used": ["linkedin", "my-notes", "deep-research", "old-thing", "extra-a"],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    assert "disable-model-invocation" not in plan
    assert "name-only" not in plan


def test_other_foreign_runtime_is_qualitative_no_false_claim(monkeypatch):
    """Non-Claude, non-Codex runtimes get a qualitative 'slim it' note and must
    NOT falsely claim the Claude-only levers (name-only / disable-model-invocation)
    work there. 'name-only' and 'disable-model-invocation' are Claude-specific."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "opencode")
    trends = {
        "skills": {
            "never_used": ["linkedin", "my-notes", "deep-research", "old-thing", "extra-a"],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    # qualitative softer-than-archive note present, naming the runtime
    assert "Softer than archiving" in plan
    assert "opencode" in plan
    # but NO false claim that the Claude-only levers work here
    assert "disable-model-invocation" not in plan
    assert "skillOverrides" not in plan


def test_resume_checkpoint_is_protected_own_skill(monkeypatch):
    """resume-checkpoint is one of our own skills and must never be surfaced for
    trimming/archiving (regression: it was missing from _OWN_TOOL_SKILLS)."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    assert measure._is_own_tool_skill("resume-checkpoint")
    assert measure._is_own_tool_skill("token-optimizer:resume-checkpoint")
    trends = {
        "skills": {
            "never_used": ["resume-checkpoint", "linkedin", "my-notes",
                           "deep-research", "old-thing", "extra-a"],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    assert "resume-checkpoint" not in plan


def test_claude_offers_both_levers_name_only_before_hidden(monkeypatch):
    """The Claude clause must offer BOTH correct levers with the right labels:
    skillOverrides 'name-only' (name kept) AND disable-model-invocation (fully
    hidden), and must NOT mislabel disable-model-invocation as 'name-only'.
    The discoverable name-only option is presented before the fully-hidden one."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    trends = {
        "skills": {
            "never_used": ["linkedin", "my-notes", "deep-research", "old-thing", "extra-a", "extra-b"],
            "installed_count": 40,
        }
    }
    components = {"skills": {"count": 40, "tokens": 8000}}
    plan, _count = measure.generate_auto_recommendations(components, trends=trends, days=30)
    assert "skillOverrides" in plan
    assert "name-only" in plan
    assert "disable-model-invocation: true" in plan
    # name-only (discoverable) is offered before the fully-hidden lever
    assert plan.index("Name-only") < plan.index("Fully hidden")
    # and both come before the archive step
    assert plan.index("disable-model-invocation: true") < plan.index("Archive (harder step")
