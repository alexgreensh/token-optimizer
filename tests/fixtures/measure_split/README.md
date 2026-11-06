# Measure split proof

`cases.json` inventories the command spellings, literal dispatch branches, output
modes, native adapters, dashboard handlers, Node statusline, and the three hook
manifests. Its expectations must come from the unsplit BASE tree.

The final BASE is **55da0b1179afa98455fa1b1c6e3dd372afa8ec3f** (5.13.64).
The source and case manifests are frozen with `provisional: false`. Accepted
evidence still requires two equal BASE captures and candidate equality before
any extraction lane starts. Candidate code must never overwrite expected
captures. The driver refuses an existing capture
directory and confines disposable payloads to `.scratch-u0/`.

For one bounded case:

```bash
python3 scripts/measure_split_golden.py --source /path/to/unsplit-base \
  --workspace .scratch-u0/golden-path --capture .scratch-u0/base-first \
  --case cli---version
```

Run the same BASE twice at the same workspace, compare those captures, then
capture the candidate at that workspace and compare it with the first BASE.
`--compare EXPECTED ACTUAL` never updates either capture. Full runs belong in
GitHub CI; local checks run one explicitly named test file at a time.

Each case gets a fresh synthetic home and project with spaces and non-ASCII
characters. Inputs include duplicate Claude requests and a sidechain, a midnight
boundary, native session fixtures, unrelated settings, and a fake plugin tree.
Seeded cases add current-schema usage, savings, counted re-reads, a re-expansion
debit, checkpoints, archives, and cache states. Separate cases exercise a legacy
schema, a busy database, settings lock contention, and read-only settings.

The probe freezes wall clocks, SQLite clocks, random IDs, temporary names and
fixture mtimes. Its monotonic clock advances deterministically. Host process,
browser and network boundaries are recorded fakes. Known Python children in the
real hook and daemon chains run through the same probe. HTTP handlers use an
in-memory transport; their raw response and server console output are separate
artifacts. Node runs the actual statusline with a fixed clock and process boundary.
The fake payload includes the source-layout README marker. The Claude version
probe returns a pinned parseable version, so normal enable paths run normally.
Registry metadata for 5.13.64 is synthetic with a fixed, fake SHA-512 digest;
DNS uses documentation-only addresses. Unexpected URL and resolver requests are
blocked before they can reach the host network. Watchdogs run at deterministic
virtual-clock boundaries. SQLite fixture and snapshot handles close explicitly.
The driver compiles each immutable payload once and reuses those code objects
without changing source filenames or runpy globals. A cached/uncached comparison
guards that seam. Cold-import timing and public subprocess smoke bypass it.

Compare stdout, stderr, exit status, user files and HTTP bytes without rewriting
paths or rounding prices. Database comparisons retain schema SQL and sorted table
rows rather than comparing SQLite layout bytes. Five end-to-end planted mutations prove
that a JSON field, dollar amount, statusline character, hook envelope or exit-code
change is detected.

Installed executable source is NOT frozen byte-for-byte: adapters install the
tree's own modules, so a deliberate source fix changes those installed bytes.
The frozen guarantee is source-copy FAITHFULNESS, proven independently of the
captures. Devin's closed payload must re-render from both trees with an
AST-identical released renderer, a differing `devin_source.py` is still
AST-limited to the mandated `_measure` refusal-root addition, and the install
manifest is re-verified commitment by commitment with only the proven modules'
digests masked. The verbatim adapters (antigravity, copilot, cursor, grok,
hermes) must have an AST-identical installer whose BASE-declared module list
predicts exactly the observed differences, two ways, with every differing
installed file hashing to exactly its tree's source bytes. An installer or
renderer edit, an unmapped file, a changed destination, a corrupt copy, or a
missing/extra difference fails closed. User outputs, settings and errors have
no allowance. On POSIX a changed, selected install case must leave verified
artifacts: a silent pass means the installer refused the sandbox interpreter
and the proof is vacuous. Negative controls reject wrong source, hashes,
metadata, installer edits and dollar figures. Not yet mapped (fail closed):
the Codex Windows-only launcher copy and the Hermes `hermes/` payload tree.

`measure_split_ci.py` checks the actual offline npm tarball, a read-only installed
payload, the real CLI/hook launcher, BASE self-equality and candidate equality.
Installed smoke checks run normal JSON settings management through both direct
CLI and hook entrypoints, read back its edits, and check actual pricing and empty
accounting results. Those bounded smokes supplement the CI golden matrix.
`measure_split_performance.py` keeps cold import timing separate from seven warm
samples of each deadline-sensitive path. A median regression above both 10% and
20 ms fails for investigation.

The frozen symbol/statement manifest and lane ownership file live in `scripts/`.
After lane-zero review, extraction lanes inherit the five canaries and edit only their assigned
definitions and fragments. The integrator owns shared source resolution and
logical mutations that span lanes.
Owned retained symbols are listed separately from movable inputs. The bytecode
bootstrap remains in the facade before its early call and compat import. New
provider files obey the global 1,000-line limit (compat retains its stricter 200).
