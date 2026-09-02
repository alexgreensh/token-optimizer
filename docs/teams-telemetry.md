# Teams edition: org telemetry (admin guide)

Token Optimizer's Teams edition adds an **admin-enabled** telemetry channel and a
private collector + org dashboard. It is off by default and file-only: nothing
leaves a machine until an org admin places a `fleet.json` there. See `PRIVACY.md`
for the exact field list and the never-sent list.

## 1. Run the collector

The collector lives in the private `token-optimizer-teams` repo (single-file
Python, sqlite, stdlib only):

```bash
python3 to_teams.py add-org "Acme Corp"     # prints ingest token, admin token, hash_key — once
python3 to_teams.py serve --port 8787       # binds 127.0.0.1 by default
```

Non-loopback binds require `--bind 0.0.0.0 --behind-tls` and a TLS-terminating
proxy in front (see the deploy note in that repo). `/healthz` needs no auth;
everything else needs a scoped bearer token.

## 2. Enable machines

On each developer machine (config dir is `~/.claude/token-optimizer/` on Claude
Code; runtime-specific elsewhere):

```bash
python3 fleet_emitter.py --enable https://collector.acme.internal <ingest-token> <hash-key>
python3 fleet_emitter.py --status
```

`--enable` writes `fleet.json` at mode 0600. The token may instead come from a
secret manager via `"token_env": "ACME_FLEET_TOKEN"` in the file; the hash key
likewise via `"hash_key_env"`. A group- or world-readable `fleet.json` is
ignored. **Windows note:** win32 does not enforce POSIX file modes, and
`os.stat` there reports group/world read bits on ordinary files, so the 0600
check fails and `fleet.json` is ignored — emission is fail-closed and
effectively unavailable on Windows until a platform-aware check lands. That is
the accepted behaviour: an unverifiable config never enables telemetry.
Kill switch on one machine: `TO_FLEET_DISABLE=1`.

Inspect what would be sent: `python3 fleet_emitter.py --dry-run` (output is
sensitive — pseudonymous org aggregates; share deliberately).

## 3. Claude Code OTel as a second ingest path

Team/Enterprise admins can point Claude Code's OTel exporter at the collector
instead of (or alongside) the emitter. Put the ingest token in **user-level**
settings, never a committed `.claude/settings.json`:

```bash
export CLAUDE_CODE_ENABLE_OTEL=1
export OTEL_METRICS_EXPORTER=otlphttp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.acme.internal:8787
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <ingest-token>"
```

Only `claude_code.api_request` events are retained; everything else is dropped
before any write. OTel writes never lower an emitter-written value and never
touch savings.

## 4. Rotation and remediation

```bash
python3 to_teams.py rotate-token --org <id> --scope ingest    # old ingest token dies; identities unchanged
python3 to_teams.py rotate-identity-key --org <id>            # history-breaking: all hashes change
python3 to_teams.py delete-session --org <id> --session <uuid>
python3 to_teams.py purge-user --org <id> --user-hash <hash>
```

## 5. Dashboard

```bash
python3 to_teams.py dashboard --org <id>    # writes org-dashboard.html (0600)
```

Views: org overview, per person (subscription-vs-API, hosts seen), per model,
per platform, limit proximity, % saved (with measured share), seat
right-sizing (heuristic).
