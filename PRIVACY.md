# Privacy Policy

**Token Optimizer** is an open-source Claude Code plugin that runs entirely on your local machine.

## Data Collection

Token Optimizer does **not** collect, transmit, or store any user data. Specifically:

- **No telemetry**: No usage data, analytics, or metrics are sent anywhere.
- **No network calls**: The tool makes zero outbound network requests. Everything runs locally.
- **No third-party services**: No external APIs, tracking pixels, or data processors are involved.
- **No accounts required**: There is no sign-up, login, or registration of any kind.

## What the Tool Accesses Locally

To perform its audit, Token Optimizer reads configuration files that already exist on your machine:

- `~/.claude/settings.json` (global Claude Code settings)
- `.claude/settings.json` (project-level settings)
- `~/.claude/CLAUDE.md` and project-level `CLAUDE.md` files
- `~/.claude/MEMORY.md`
- Skill and command directories under `~/.claude/`
- MCP server configurations

All of this data stays on your machine. The generated dashboard, snapshots, and backups are saved as local files in your project or home directory.

## Backups

Before making any changes, Token Optimizer creates local backups of your configuration files. These backups are stored in `~/.claude/_backups/` on your machine and are never transmitted anywhere.

## Open Source

Token Optimizer is licensed under [PolyForm Noncommercial 1.0.0](LICENSE). The full source code is available at [github.com/alexgreensh/token-optimizer](https://github.com/alexgreensh/token-optimizer) and can be audited by anyone.

## Contact

For privacy-related questions, reach out to [Alex Greenshpun](https://linkedin.com/in/alexgreensh) or open an issue on the [GitHub repository](https://github.com/alexgreensh/token-optimizer/issues).
