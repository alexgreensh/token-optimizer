"""Default archive-exemption for verbatim-payload code/doc MCPs.

Token Optimizer compresses any tool result >= 4KB to a preview to save context.
For MCP servers whose whole purpose is returning code/docs the agent must read
inline (octocode, context7, ...), that preview defeats the tool. These now ship
exempt by default; the user's TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS remains an
additive opt-in, and TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS=off drops the
built-ins.
"""

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import archive_result  # noqa: E402


@pytest.fixture
def clean_exempt(monkeypatch):
    """Isolate from process env + user settings.json and bust the module cache."""
    monkeypatch.delenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS", raising=False)
    # Point settings.json lookup at an empty dir so no real user config leaks in.
    monkeypatch.setattr(archive_result, "claude_home", lambda: Path("/nonexistent-xyzzy"))
    archive_result._EXEMPT_PATTERNS_CACHE = archive_result._EXEMPT_PATTERNS_UNSET
    yield
    archive_result._EXEMPT_PATTERNS_CACHE = archive_result._EXEMPT_PATTERNS_UNSET


def _exempt(tool):
    archive_result._EXEMPT_PATTERNS_CACHE = archive_result._EXEMPT_PATTERNS_UNSET
    return archive_result._is_archive_exempt(tool)


def test_verbatim_content_fetchers_exempt_by_default(clean_exempt):
    assert _exempt("mcp__octocode__githubGetFileContent")
    assert _exempt("mcp__octocode__localGetFileContent")
    assert _exempt("mcp__context7__query-docs")
    assert _exempt("mcp__github__get_file_contents")
    assert _exempt("mcp__deepwiki__read_wiki_contents")


def test_search_and_tree_tools_stay_compressed(clean_exempt):
    # The defaults are scoped to content fetchers, NOT whole servers: octocode's
    # own search/tree tools must still compress, or the optimizer loses its
    # savings on the chatty calls.
    assert not _exempt("mcp__octocode__githubSearchCode")
    assert not _exempt("mcp__octocode__githubViewRepoStructure")
    assert not _exempt("mcp__octocode__localSearchCode")
    assert not _exempt("mcp__github__search_code")
    assert not _exempt("mcp__deepwiki__ask_question")  # AI-synthesized, not verbatim


def test_optimizer_still_compresses_others(clean_exempt):
    # A chatty general MCP and non-MCP tools must NOT be exempt.
    assert not _exempt("mcp__somechatty__list_issues")
    assert not _exempt("Bash")
    assert not _exempt("Read")


def test_user_list_is_additive(clean_exempt, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS", "mcp__myserver__*")
    assert _exempt("mcp__myserver__fetch")          # user opt-in honored
    assert _exempt("mcp__octocode__githubGetFileContent")  # defaults still on


def test_defaults_can_be_disabled(clean_exempt, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS", "off")
    assert not _exempt("mcp__octocode__githubGetFileContent")


def test_defaults_off_still_honors_user_list(clean_exempt, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS", "off")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS", "mcp__myserver__*")
    assert not _exempt("mcp__octocode__x")
    assert _exempt("mcp__myserver__fetch")
