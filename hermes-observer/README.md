# Token Optimizer Hermes Observer (beta)

This directory contains a **standalone, observer-only beta** for reporting
bounded aggregate usage from one explicitly selected Hermes profile.

## Commands

```bash
python3 hermes-observer/observer.py report --hermes-home /absolute/profile/path --format json
python3 hermes-observer/observer.py report --hermes-home /absolute/profile/path --format text
python3 hermes-observer/doctor.py --hermes-home /absolute/profile/path --format json
```

## Privacy and safety limits

- Reads only the direct-child `state.db` of the supplied absolute profile path.
- Rejects symlinked path components, symlinked/non-regular databases, malformed or
  unsupported schemas, and databases above the bounded size limit.
- Uses a WAL-aware SQLite `mode=ro` connection with `query_only` enabled and a
  short busy timeout. It does not use `immutable=1`, so committed live WAL rows
  remain visible.
- Emits aggregate token, cost, activity, date-bucket, and context-availability
  data only. It never emits prompts, messages, tool output, titles, working
  directories, model strings, private paths, or raw session IDs.
- Has no network, subprocess, daemon, hook, installer, updater, browser, trend
  database, configuration-editing, or dynamic-extension behavior.
- Creates and modifies no files. JSON and text output, scanned rows, and date
  buckets are capped.

## Beta limitations

This is reporting and validation only; it does not optimize or modify Hermes.
The observer supports the current Hermes `sessions` aggregate columns and fails
closed on an unsupported schema. Hermes currently stores cumulative session
usage, not reliable live context occupancy, so context occupancy is reported as
unavailable rather than inferred from cumulative tokens. Reports scan at most
10,000 newest sessions and expose at most 30 daily buckets. Cost values are
passed through from Hermes aggregates; the observer does not load pricing data
or estimate missing costs.

`manifest.json` hashes the two executable runtime files (`observer.py` and
`doctor.py`). The manifest does not hash itself, avoiding a recursive self-hash
convention.
