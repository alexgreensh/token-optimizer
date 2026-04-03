---
review_agents:
  - kieran-python-reviewer
  - kieran-typescript-reviewer
  - code-simplicity-reviewer
  - security-sentinel
  - performance-oracle
  - architecture-strategist
plan_review_agents:
  - kieran-python-reviewer
  - kieran-typescript-reviewer
  - code-simplicity-reviewer
---

# Review Context

- This repo mixes Python hook scripts and TypeScript OpenClaw runtime code. Review cross-runtime parity, not just single-file correctness.
- Background hooks must stay lightweight. Prefer one-shot guards, cooldowns, and deterministic extraction over any behavior that reparses large transcripts on every event.
- Checkpoint restore and cleanup paths are security-sensitive because they run automatically. Reject symlinks and keep all resolved paths inside the checkpoint root.
- Token-saving features must not introduce extra context injection or user-visible latency.
