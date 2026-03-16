---
name: smart-compact
description: Protect session state across OpenClaw compaction events
events:
  - session:compact:before
  - session:compact:after
---

# Smart Compaction Hook

Automatically captures session state before OpenClaw compacts context, and restores it after.

## Events

- **session:compact:before**: Saves the last 20 messages as a markdown checkpoint
- **session:compact:after**: Injects the checkpoint back into context

## Storage

Checkpoints saved to `~/.openclaw/token-optimizer/checkpoints/{sessionId}.md`

Old checkpoints (>7 days) are cleaned up on gateway startup.
