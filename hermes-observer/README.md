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
  unsupported schemas, and databases above the injectable 4 GiB default limit.
- Rejects noncanonical path spellings before normalization (`.`, `..`, repeated
  separators, and non-root trailing separators). It records device, inode, mode,
  and (for the database) size identities for the full ancestry, holds read-only
  descriptors for the home and database, and revalidates around open, schema,
  and data reads. Any observed replacement fails closed.
- Uses a WAL-aware SQLite `mode=ro` connection with `query_only` enabled and a
  short busy timeout. It does not use `immutable=1`, so committed live WAL rows
  remain visible.
- Emits aggregate token, cost, activity, date-bucket, and context-availability
  data only. It never emits prompts, messages, tool output, titles, working
  directories, model strings, private paths, or raw session IDs.
- Defines token `total` and daily token totals as input plus output only. Cache
  reads, cache writes, and reasoning remain separately reported counters.
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

Newest-session selection separately bounds numeric and ISO-text timestamp
candidates, normalizes supported forms to UTC in Python, and uses the internal
session ID only as a deterministic tie-breaker. The ID is never emitted.

Python's SQLite API cannot bind a normal WAL-aware connection to an already
opened database descriptor. The identity protocol therefore detects persistent
or overlapping path/object replacement, but an attacker with write access that
replaces and restores every pathname entirely between two checks remains an
residual race. `mode=ro` is retained instead of `/proc` or `immutable=1` so this
works on macOS and keeps committed WAL rows visible.

## Review sabotage proof

The corrected total semantics have an explicit red/green check. Replacing the
input-plus-output expression with `sum(values.values())` and running
`test_total_means_input_plus_output_and_keeps_other_counters_separate` failed
with `total: 329 != 18`; restoring the expression made the same test pass
(`1 passed`).

`manifest.json` hashes the two executable runtime files (`observer.py` and
`doctor.py`). The manifest does not hash itself, avoiding a recursive self-hash
convention.
