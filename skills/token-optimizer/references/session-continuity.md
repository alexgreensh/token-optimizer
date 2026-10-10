# Session continuity: cold-resume-lean

Reopen a forgotten/cold session cheaply, no `--resume`, no command. On a fresh
session, when the user naturally asks to continue prior work ("continue the X
work, check what we discussed last session"), the continuity hook reconstructs a
**lean** context for the right **same-project** prior session and injects it.

- **Selection** ("both"): if the user names a topic → keyword-match winner; if
  vague ("where we left off") → most-recent same-project session.
- **Token-free**: reconstruction reads checkpoints + `session_log` only (no LLM,
  no subprocess). The only cost is the fresh session's normal first turn.
- **Same-project = files touched** (path-prefix vs cwd), never a cross-project leak.
- **Savings** are credited as a realized `resume_lean` event (avoided cold-resume
  cache-rewrite minus the lean block), idempotent per target session, shown in the
  Savings view. Realized tier, same as `checkpoint_restore`.
- **Manual fallback**: `python3 $MEASURE_PY resume-lean` lists cold sessions;
  `resume-lean <#|session_id> --print` emits the block for `claude "$(...)"`.
- Ported across Claude Code, Codex, OpenClaw, opencode (checkpoint richness varies
  by platform; the lean block adapts to available fields).
