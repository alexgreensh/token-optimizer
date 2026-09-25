# Token Optimizer for Pi

Install from the repository package directory: `pi install git:github.com/alexgreensh/token-optimizer` is **not yet supported** because this repository root also contains other adapters. Use `pi install ./pi` from a local checkout. `pi update --extensions` updates installed package sources; `pi remove ./pi` removes the local package declaration. Project-local installations require Pi's project trust.

Run `/token-optimizer` for status, `/token-optimizer doctor` for runtime diagnostics, and `/token-optimizer enable` to opt in to local measurement. The extension is off by default. Its isolated data directory is `~/.pi/agent/token-optimizer` (override with `TOKEN_OPTIMIZER_PI_HOME`). `/token-optimizer disable` stops measurement. No model or Pi compaction behavior is changed.

This is an initial package skeleton; deeper metrics and continuity features are under development.
