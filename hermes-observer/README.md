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
  separators, and non-root trailing separators). It records device, inode, and
  mode identities for the full ancestry, retains the first validation baseline,
  holds read-only descriptors for the home and database, and revalidates around
  open, schema, and data reads. Mutable database size is checked independently at
  those boundaries; legitimate growth remains accepted while it stays within the
  configured limit. Any observed replacement fails closed.
- Uses a WAL-aware SQLite `mode=ro` connection with `query_only` enabled and a
  short busy timeout. Before connecting it validates the database's WAL header
  and the `state.db-wal`/`state.db-shm` objects as non-symlink regular files,
  binds existing sidecars to read-only descriptors, and rechecks their identity
  and presence around SQLite operations. A WAL-mode source must have an existing
  `state.db-wal`; if `state.db-shm` is absent, SQLite may create it and the
  observer immediately validates and binds its identity. It does not use
  `immutable=1`, which could silently omit committed rows present only in the
  WAL, or `nolock=1`, which does not reliably open a WAL database.
- Emits aggregate token, cost, activity, date-bucket, and context-availability
  data only. It never emits prompts, messages, tool output, titles, working
  directories, model strings, private paths, or raw session IDs.
- Defines token `total` as exactly the final reported `input + output`, including
  at saturation. Each component is bounded to `10^18`, so token `total` is
  bounded to `2 * 10^18`. Daily token totals apply the same independent input
  and output component bounds and are therefore also bounded to `2 * 10^18`.
  Cache reads, cache writes, and reasoning remain separately reported counters.
- Has no network, subprocess, daemon, hook, installer, updater, browser, trend
  database, configuration-editing, or dynamic-extension behavior.
- Never writes SQL, `state.db`, `state.db-wal`, configuration, analytics,
  bytecode, or any other profile/package file. The one narrow filesystem-write
  exception is SQLite itself creating or updating `state.db-shm` read marks and
  locks as required for a supported same-user WAL-visible `mode=ro` read. Python
  bytecode creation is disabled for the doctor's sibling import. JSON and text
  output, scanned rows, and date buckets are capped.
  Mutation tests seed `config.yaml` and a nested unrelated binary and verify
  their inventory, bytes, modes, sizes, and nanosecond timestamps are unchanged
  across both existing-SHM and SQLite-created-SHM reads. Only the direct-child
  `state.db-shm` is excluded from that comparison.

## Beta limitations

This is reporting and validation only; it does not optimize or modify Hermes.
The observer supports the current Hermes `sessions` aggregate columns and fails
closed on an unsupported schema. Hermes currently stores cumulative session
usage, not reliable live context occupancy, so context occupancy is reported as
unavailable rather than inferred from cumulative tokens. Reports scan at most
10,000 newest sessions and expose at most 30 daily buckets. Cost values are
passed through from Hermes aggregates; the observer does not load pricing data
or estimate missing costs.

Newest-session selection accepts numeric Unix seconds from `-62135596800`
through `253402300799`, or calendar-valid canonical extended UTC text in exactly
`YYYY-MM-DDTHH:MM:SSZ` form (years `0001` through `9999`). SQL candidate filters
and ordering enforce that same grammar before Python normalization, so compact
ISO (`20260104T000000Z`), spaces, offsets, fractional seconds, and invalid dates
are ignored rather than consuming the bounded candidate set. Numeric and text
candidates are separately bounded, then normalized together; the internal
session ID is only a deterministic tie-breaker and is never emitted.

Python's SQLite API cannot bind a normal WAL-aware connection to already opened
read-only database/WAL/SHM descriptors. The identity protocol therefore detects
persistent or overlapping path/object replacement, but an attacker with write
access that replaces and restores every pathname—or creates and removes WAL
state entirely—between two checks remains a residual portable-stdlib race.
`mode=ro` is retained instead of `/proc`, `nolock=1`, or `immutable=1` so this
works on macOS and keeps committed WAL rows visible. A missing `state.db-shm`
is acceptable only when `state.db-wal` already exists; SQLite may create SHM for
coordination. A WAL-mode database with no WAL, and every hostile or non-regular
sidecar, is rejected before a query.

## Review sabotage proof

The corrected total semantics have an explicit red/green check. Replacing the
input-plus-output expression with `sum(values.values())` and running
`test_total_means_input_plus_output_and_keeps_other_counters_separate` failed
with `total: 329 != 18`; restoring the expression made the same test pass
(`1 passed`).

`manifest.json` hashes the two executable runtime files (`observer.py` and
`doctor.py`). The manifest does not hash itself, avoiding a recursive self-hash
convention.
