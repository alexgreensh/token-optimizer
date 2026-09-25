# Token Optimizer for Pi

Install with `pi install git:github.com/alexgreensh/token-optimizer` or test a local checkout with `pi install ./pi`. Update with `pi update --extensions`. Uninstall with `pi remove git:github.com/alexgreensh/token-optimizer` (or `pi remove ./pi` for the local source). The root manifest loads only the Pi extension and does not auto-load the other runtime skills. Project-local installations require Pi's project trust.

Run `/token-optimizer` for status, `/token-optimizer doctor` for runtime diagnostics, and `/token-optimizer enable` to opt in to local measurement. The extension is off by default. Its isolated data directory is `~/.pi/agent/token-optimizer` (override with `TOKEN_OPTIMIZER_PI_HOME`). `/token-optimizer disable` stops measurement. No model or Pi compaction behavior is changed.

Local model-level usage is read from the active Pi branch, and native Pi costs are reported without invented savings. Tool-output archives are separately opt-in with `/token-optimizer archive on`; archived text is redacted, so secret-bearing output cannot be restored verbatim. Archives expire after seven days by default when archive pruning runs (after a new archive). Checkpoints are not yet automatically pruned; remove the isolated data directory to delete all local data. The extension does not replace native compaction.
