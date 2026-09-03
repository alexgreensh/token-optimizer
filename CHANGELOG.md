# Changelog

## [Unreleased]

- Add: burn nudge. When the same command fails 3 times in a row with different output, a nudge suggests changing approach instead of re-running. Catches the edit-compile-fail cycle that the existing identical-output streak guard cannot see. Tunable with `TOKEN_OPTIMIZER_FAIL_STREAK_THRESHOLD` (default `3`).
- Add: inline-script repeat nudge. When a command with a heredoc body >= 300 chars has been run 8 times in a session, a nudge suggests saving the script to a file and running that instead, so the body is not re-sent as input tokens every turn. Tunable with `TOKEN_OPTIMIZER_INLINE_SCRIPT_THRESHOLD` (default `8`).
