# Example: Coaching Session for Building a New Project

User chose option (a): Building something new, want it token-efficient from the start.

---

## Coach Output

**Coach**: Your current setup starts every session with ~18,200 tokens of overhead. That's 9.1% of your 200K window, pretty reasonable. The big items: CLAUDE.md at 1,450 tokens, 23 skills at ~2,300 tokens, and 4 MCP servers at ~480 tokens.

What are you building?

**User**: A multi-step skill for doing code reviews. It should read files, run tests, and generate a review summary.

**Coach**: Good scope. For a code review skill, the architecture is:

1. **SKILL.md** (~80 lines, ~1,200 tokens on invoke): Orchestrator only. Phase 0 resolves paths, Phase 1 collects code context, Phase 2 analyzes, Phase 3 generates summary. Keep the body focused on control flow.

2. **references/review-criteria.md**: Your review standards, patterns to check for, severity levels. Zero tokens until the skill reads it. This is where the heavy domain knowledge goes.

3. **references/output-templates.md**: Example review outputs. Also zero cost until read.

Your frontmatter budget: description under 80 chars. Something like "Code review with test verification and structured summary." That's 56 chars, perfect.

The skill adds ~100 tokens to your startup overhead (frontmatter in the menu). Full cost of ~1,200 tokens only when someone invokes /code-review. The reference files cost zero unless the skill explicitly reads them during execution.

**User**: Should I use subagents for the test running?

**Coach**: Yes, for one reason: context isolation. When the skill reads code files for review, those stay in context. Running tests can produce verbose output. A subagent for test execution keeps that output in its own 200K window and reports back just pass/fail + relevant failures.

Use an Explore agent (Haiku, read-only) if you just need to read test results. Use a general-purpose agent if you need to actually run the tests via Bash. Either way, don't assign skills to the test agent unless it specifically needs them.

## Action Plan

1. **Create skill skeleton**: SKILL.md + references/ directory. Budget: ~100 tokens startup, ~1,200 on invoke. (~0 additional startup cost vs no skill)
2. **Keep description under 80 chars**: "Code review with test verification and structured summary" (56 chars)
3. **Use subagent for test execution**: Isolates verbose test output from review context. Use general-purpose agent with Bash access, no skill assignments.
4. **Put review criteria in references/**: Zero cost until the skill reads them. Don't inline domain knowledge in SKILL.md body.
