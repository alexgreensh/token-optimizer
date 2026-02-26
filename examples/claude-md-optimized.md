# Example: Optimized CLAUDE.md Structure
#
# This example shows how to structure CLAUDE.md for prompt caching.
# Static content FIRST (cacheable), volatile content LAST.
# Target: <800 tokens (~50-60 lines)

# ============================================================
# STATIC (rarely changes, cached by prompt caching)
# ============================================================

## Identity
- Project: My Web App
- Stack: Rails 8, PostgreSQL, Hotwire
- Test: `bin/rails test`

## Key Paths
- App code: `app/`
- Tests: `test/`
- Config: `config/`

## Standards
- Follow Rails conventions
- Use Hotwire over custom JS
- Run tests before committing

## Agent Model Selection
| Task Type | Model |
|-----------|-------|
| File reading, data gathering | haiku |
| Analysis, synthesis, writing | sonnet |
| Architecture, complex debugging | opus |

# ============================================================
# VOLATILE (changes frequently, loaded last, not cached)
# ============================================================

## Current Focus
See `docs/current-sprint.md` for active work.
# Better: reference a separate file instead of inline content.
# This way the cacheable prefix stays stable.
