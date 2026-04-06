"""WebSearch routing nudge detector for Token Optimizer.

Post-hoc detector that identifies heavy web search/fetch usage across sessions
and suggests routing through dedicated subagents or targeted queries.
"""

# Tools considered web search/fetch
_WEB_TOOLS = frozenset({
    "WebSearch", "WebFetch",
    "mcp__tavily__tavily_search", "mcp__tavily__tavily_extract",
    "mcp__tavily__tavily_crawl", "mcp__tavily__tavily_research",
    "mcp__exa__web_search_exa", "mcp__exa__crawling_exa",
    "mcp__brightdata__search_engine", "mcp__brightdata__scrape_as_markdown",
})

# MCP server prefixes known to be web search providers
_WEB_MCP_PREFIXES = ("mcp__tavily__", "mcp__exa__", "mcp__brightdata__",
                     "mcp__claude_ai_BrightData__", "mcp__perplexity")

_EST_TOKENS_PER_CALL = 5000
_MIN_CALLS = 3
_MIN_TOKENS = 50_000


def _count_web_calls(tool_calls):
    """Count web search/fetch tool calls from a tool_calls dict."""
    count = sum(tool_calls.get(t, 0) for t in _WEB_TOOLS)
    for name, n in tool_calls.items():
        if name in _WEB_TOOLS:
            continue
        if any(name.startswith(p) for p in _WEB_MCP_PREFIXES):
            count += n
    return count


def detect_websearch_routing(trends):
    """Detect heavy web search usage from aggregated trends data.

    Args:
        trends: dict from _collect_trends_data() with total_tools, session_count, days

    Returns:
        list[dict] of findings
    """
    total_tools = trends.get("tool_calls", {})
    web_calls = _count_web_calls(total_tools)
    est_tokens = web_calls * _EST_TOKENS_PER_CALL
    session_count = trends.get("session_count", 1)
    days = trends.get("days", 30)

    if web_calls < _MIN_CALLS or est_tokens < _MIN_TOKENS:
        return []

    avg_per_session = web_calls / max(session_count, 1)
    return [{
        "name": "websearch_routing",
        "confidence": 0.7 if web_calls >= 10 else 0.5,
        "evidence": (
            f"{web_calls} web search/fetch calls across {session_count} sessions "
            f"({days}d), ~{est_tokens:,} tokens of web content"
        ),
        "savings_tokens": est_tokens,
        "suggestion": (
            f"Web results consumed ~{est_tokens:,} tokens ({avg_per_session:.1f} calls/session avg). "
            "Consider: route research to a dedicated subagent (Haiku), "
            "use targeted queries instead of broad searches, "
            "or extract key facts before injecting full pages."
        ),
    }]
