# Example: Coaching Session for Multi-Agent Architecture

User chose option (c): Designing a multi-agent system, want architecture advice.

---

## Coach Output

**Coach**: Multi-agent is where token optimization really matters. Every agent multiplies your config overhead. Your current setup costs ~18,500 tokens at startup. With multiple agents, that compounds fast.

Walk me through what you're building. How many agents? What does each one do?

**User**: I'm building a research automation. 4 agents: one scrapes web sources, one reads local documents, one analyzes findings, one writes the final report.

**Coach**: Four agents. Let's do the math on your current config:

- 4 agents x ~18,500 config overhead = 74,000 tokens just for agent setup
- Plus the orchestrator's own session = ~18,500 more
- Total infrastructure cost: ~92,500 tokens before any actual work

That's 46% of a single 200K context window (though agents each get their own window, this matters for cost/rate limits).

Let's optimize each agent:

**Web scraper**: Data-gathering only. This should be an Explore agent with Haiku. It reads web content, returns summaries. It does NOT need your 54 skills or Opus-level reasoning. With Haiku, you save ~60x on per-token cost for this agent.

**Document reader**: Also data-gathering. Another Explore agent with Haiku. It reads local files and extracts relevant content. Read-only is fine here.

**Analyzer**: This one needs judgment. Sonnet is the right model. It takes structured input from the first two agents and produces analysis. Still no need for full skill access unless it uses a specific analysis skill.

**Report writer**: Needs write access (to create the report file). General-purpose agent, Sonnet or Opus depending on report complexity. Only assign the specific writing-related skill if you have one.

**User**: The analyzer sometimes needs to look up additional files based on what it finds. Does that change things?

**Coach**: Good catch. If the analyzer needs to read files reactively, it can't be read-only Explore. Make it a general-purpose agent with Sonnet, but don't assign skills it doesn't need. The key distinction: Explore agents are for pre-planned reads. General-purpose agents handle reactive, multi-step tasks.

For the coordination pattern: create a temp folder. Each agent writes findings to it. The orchestrator tracks completion. The report writer reads from the findings folder directly, never from the orchestrator's context. This prevents your orchestrator from bloating with raw agent output.

```
/tmp/research-session/
  COORDINATION.md
  findings/
    web-sources.md      # Web scraper writes here
    local-docs.md       # Document reader writes here
    analysis.md         # Analyzer writes here
  output/
    final-report.md     # Report writer creates here
```

**User**: Should I slim my CLAUDE.md before building this?

**Coach**: 100%. Your CLAUDE.md at 2,180 tokens loads in EVERY agent. 5 agents (including orchestrator) = 10,900 tokens just for CLAUDE.md copies. If you trim to 800 tokens, that's 5 x 800 = 4,000 tokens. You'd save 6,900 tokens across the system.

Same logic for skills: 54 skills x 5 agents = frontmatter loading 5 times. Archive unused skills, save ~3,500 x 5 = 17,500 tokens across all agents.

## Action Plan

1. **Assign correct agent types**: Web scraper and doc reader as Explore/Haiku (read-only). Analyzer as general-purpose/Sonnet. Report writer as general-purpose/Sonnet. (~60x cost reduction on data-gathering agents)
2. **Implement coordination folder**: Agents write to files, orchestrator reads summaries only. Prevents context overflow.
3. **Slim CLAUDE.md to 800 tokens**: Saves ~6,900 tokens across 5 agents (5 x 1,380 tokens saved).
4. **Archive unused skills**: Saves ~17,500 tokens across 5 agents (5 x 3,500 tokens saved).
5. **No skill assignments to data-gathering agents**: Web scraper and doc reader need zero skills. Analyzer gets only the analysis skill. Report writer gets only the writing skill.
