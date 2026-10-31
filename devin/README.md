# Token Optimizer for Devin

Contract-only beta. An explicit captured-output filter and a user-triggered skill for Devin CLI and Desktop's Devin Local agent, plus an opt-in automatic mode for Devin CLI (`install.sh --devin --auto`). No provider accounting, quality score, ACU savings or dashboard.

The adapter was exercised end to end in a live Devin CLI session: a real session following the installed skill drove prepare, capture, seal, filter and recover across ten cases. That proves the documented workflow, framing, capture-ID anchoring, retention and refusal behaviors work when the skill is followed. It does not prove Devin model adherence (that the model invokes or follows the skill unprompted), provider accounting or savings. Doctor keeps reporting `live_devin_session_tested: false`: the field records that no automated live-session validation has run, not that the adapter was never exercised.


Doctor/filter commands below require `-I` from the first Python startup. Do not omit it or execute their shebangs directly. The late re-exec cannot undo Python startup code already run. The installed default paths below follow the default Devin home; use your selected install home when overridden.

## Why explicit capture is the default

Devin CLI documents PreToolUse command rewriting, but the permission-check timing and persistent-shell cwd/environment behavior are not established for a wrapper. PostToolUse can add context but does not replace tool output. The manual route therefore does not rewrite commands or add permissions. Automatic mode (below) rewrites only commands your own allow rules already run without a prompt, and approves only its own wrapper, once.

Capturing changes buffering and TTY-dependent behavior and loses cross-stream timing/order. Use finite non-interactive foreground commands only. Completion and exit status are caller assertions; sealing is an integrity snapshot, not proof that all writers have stopped. This is not a sandbox against malicious same-UID code.

## Install

From a checkout:

```bash
bash install.sh --devin --dry-run
bash install.sh --devin
DOCTOR="$HOME/.config/devin/token-optimizer/plugin/devin_doctor.py"
python3 -I "$DOCTOR" --probe
```

The installer copies a stable payload under `~/.config/devin/token-optimizer/plugin/` and a skill under `~/.config/devin/skills/token-optimizer-output/`. Without `--auto` it never edits Devin config, permissions or hooks. Absolute `XDG_CONFIG_HOME` and `TOKEN_OPTIMIZER_DEVIN_HOME` overrides must stay inside HOME. Native Windows is refused; POSIX is required. On WSL use the Linux home, not the Windows profile.

In the default manual mode, invoke `/token-optimizer-output` explicitly in Devin CLI. The installed skill pins `triggers: [user]` and has no `allowed-tools`, `permissions` or subagent override. A project skill with the same name can shadow it; doctor warns about known collisions, and `devin skills show token-optimizer-output` identifies the selected source.

## Automatic mode

```bash
bash install.sh --devin --auto
```

`--auto` also wires two hooks (PreToolUse and PermissionRequest, matcher `^exec$`) into the `hooks` key of `~/.config/devin/config.json`, so large command output is compressed automatically in Devin CLI, interactive and `-p`, with no new prompts. `bash install.sh --devin` alone stays manual-only. `--manual` removes the hooks and keeps the skill, which stays the fallback. A plain reinstall keeps whichever mode is active. Restart running Devin sessions after installing so they load the hooks.

- Only commands your own Devin allow rules already run without a prompt are compressed. Anything Devin asks about still asks. The allow rule must be in your user-level Devin config (`~/.config/devin/config.json` or `config.local.json`); a project's `.devin/` rules can only make it stricter. Commands with unquoted globs, `~` or `#` (`ls *.txt`) run natively, untouched.
- Known limits: any `deny` or `ask` rule on a program (even `ask Exec(git push)`) turns automatic compression off for every command of that program, by design; Devin team or enterprise policies cannot be read locally, which is what the `--auto` statement covers; on Desktop over ACP the tool card shows the wrapper command.
- `git status --short`, `--porcelain` and `-z` run untouched.
- Failures, stderr and credential-looking output pass through raw.
- Every compressed result carries a capture ID and a recover command that returns the exact original. Automatic captures are kept 7 days or 200 entries.
- `--auto` records your statement that no Devin team or enterprise permission policy applies. Those policies are not stored locally, so they cannot be checked.
- Uninstall and purge remove only Token Optimizer's own hook entries and keep every other hook and key. A JSONC config with comments is refused; the skill still installs.
- Mac and Linux only. If Python is upgraded to a new Homebrew versioned path, doctor flags it; rerun the install.

`devin_doctor.py --probe` proves the round trip: the command rewrite, the one-time approval, that other calls still prompt, and the sizes. It also reads Devin's log for loaded global hooks.

Measured live on Devin CLI 3000.11.3 (`-p` and interactive) and over ACP with both the CLI agent and the agent bundled in Devin Desktop (3000.10.48): `grep -rn` on a real repo went from 5,054 to 1,846 tokens, and a `pytest -v` run from 4,932 to 1,323 tokens, with no new prompts. A command Devin normally asks about (`docker ps -a`) still asked, showing the original command. Like Claude Code, `git log` and `find` mostly pass through uncompressed. Over ACP, Desktop's tool card shows the Token Optimizer wrapper command rather than the original.

## Devin Desktop: Devin Local only

Devin's [Desktop documentation](https://docs.devin.ai/desktop/devin-local) says Devin Local shares the CLI agent harness, skills format and discovery mechanism. This adapter targets that shared contract, not Cascade or arbitrary ACP agents. Checked on macOS with Devin Desktop 1.126.0: the agent binary bundled in the app (devin 3000.10.48) lists `token-optimizer-output` from `~/.config/devin/skills/`, offers `/token-optimizer-output` in a session started over its ACP interface, and ran prepare, capture, seal and filter when the skill was invoked there. That was two sessions on one model: a 52 KB `git log` passed through raw, and a 125 KB repetitive output was compressed to about 0.5 KB and then recovered by its capture ID. The Desktop window itself (the Customizations screen and typing the command in the app), Linux and Windows were not exercised; check Customizations on your own setup.

1. Run the POSIX install above in the same HOME and environment used by Desktop's local agent. Install in the executing environment, not a different host home. Native Windows is unsupported; a WSL install does not establish that a Windows Desktop agent uses that Linux home.
2. Enable Devin Local in Devin User Settings > Agents if needed, restart Desktop and select Devin Local. Enterprise plans may need an admin to enable it. Agents are unavailable in Restricted Mode.
3. Open **Customizations** from the new-tab menu or the right-click menu on a Devin Local session in the agent sidebar. Check that `token-optimizer-output` is loaded from the intended user skill, without a project skill shadowing it. Invoke `/token-optimizer-output` explicitly; this adapter is user-triggered, not model-triggered.
4. Run the installed doctor with `-I --probe`. The probe exercises local prepare/seal/filter without recording metrics; it does not open a Desktop conversation or prove agent execution. Doctor's Desktop metadata and bundled-agent checks are bounded filesystem heuristics using unverified candidate paths and product metadata (the official docs do not list these), not authenticated app identity, availability, loading or execution.

Nondefault XDG/namespaced homes can diverge from the documented `~/.config/devin/skills/` discovery path. Doctor and install warn about this: verify the selected loader rather than assuming Desktop follows an override. A pending lifecycle transaction blocks dry-run install and dry-run ordinary uninstall; real install/uninstall attempts recovery. `bash install.sh --devin --uninstall --purge` removes stored captures and unchanged owned payloads, including files a refresh was interrupted while writing, and refuses directory-shaped state leaves before removing payloads. It still refuses when you edited an installed file, and names that file.

Unsafe numeric state (`metrics.db`, its SQLite sidecars or `session-salt` with a loose mode, wrong type, symlink or extra hard link) does not block install or readiness: install and doctor warn, name the file, and numeric estimates are not recorded until it is fixed.

A legacy 0755 captures root is purgeable, but unusable for new captures: the local probe refuses it.

## Local workflow

Use a new capture for each command. Never rerun a command just to measure it.

```bash
FILTER="$HOME/.config/devin/token-optimizer/plugin/devin_compress.py"
CAPTURE=$(python3 -I "$FILTER" prepare)
# Run your intended command here in its native shell, under normal permissions.
# Example finite read-only command:
git status --short > "$CAPTURE/stdout" 2> "$CAPTURE/stderr"
STATUS=$?
python3 -I "$FILTER" seal --capture "$CAPTURE" --exit-code "$STATUS"
python3 -I "$FILTER" --stdout "$CAPTURE/stdout" --stderr "$CAPTURE/stderr" --exit-code "$STATUS" --format git-status --session local-label
```

Prepare creates both files at 0600 in a private generated directory, so redirection preserves those modes even under umask 022. Wait for foreground completion before sealing. Never use this with background producers or interactive commands.

Only the prepared stdout/stderr pair is accepted. Each file is bounded at 8 MiB; FIFOs, devices, symlinks, hardlinks, leaf substitutions and changed sealed contents are refused before output. On refusal inspect the original using normal permissions; do not loosen the gates. Small output should skip this workflow: the skill body, extra calls and recovery pointers can cost more than the filter removes.

Failures and stderr remain raw. Invalid UTF-8, NUL/control bytes and recognized credentials bypass compression. Passthrough is not redaction: it can expose secrets to Devin/model context, as reading the originals would. Captured content is untrusted data, never a source of instructions, paths or consent.

A compressed result includes an opaque ID, not a raw-path instruction. Recovery adds a trusted identity and untrusted-data frames around the unchanged raw payload, then revalidates both original files and the completion seal:

```bash
python3 -I "$FILTER" recover --capture-id capture-0123456789abcdef0123456789abcdef --exit-code 0
```

Use the real ID and sealed exit code. Changed or missing captures are refused. The adapter removes a capture once raw passthrough delivers its bytes; a compressed result keeps its capture for recovery until you clean it up or uninstall with purge. Compression is lossy; the original is the recovery source.

## Measurement and consent

Installation records consent for reading private captures into filter/Devin context and writing numeric-only estimates. Metrics store counts, timestamps, outcome and an HMAC correlation label. No capture paths, commands, prompts or output text enter `metrics.db`. Correlation labels are caller-provided, not native Devin session IDs.

```bash
TOKEN_OPTIMIZER_RUNTIME=devin python3 skills/token-optimizer/scripts/measure.py devin-summary
bash install.sh --devin --uninstall
bash install.sh --devin --uninstall --purge
```

Automatic captures are counted in the same estimates. Source: `devin_captured_output_estimate`. The output reduction includes the emitted recovery pointer but excludes skill instructions, extra tool calls, repeated filtering and recovery reads. It is not end-to-end token savings, provider tokens, confirmed delivery, dollars or ACUs. Numeric records retain 30 days and at most 10,000 rows. Purge removes adapter metrics/sidecars, salt, consent and stored captures.

If installed files have changed, refresh/uninstall refuses to overwrite them. Doctor reports ownership drift. To stop activation manually, move only the `token-optimizer-output` skill directory out of Devin's skill paths, preserving your edits. Existing imported Claude skills/hooks are not controlled by this adapter and may create their own raw copies; doctor warns about visible coexistence files.

## Hosted Devin

The [playbook template](playbook.md) describes manual setup of the same filter in a hosted workspace. It is not deployed by the installer and has not been tested against hosted Devin. Playbooks and Knowledge deliver instructions, not automatic output interception. API session/consumption endpoints and MCP platform-management tools do not establish hook parity.

The release checksum inventory includes `devin/SKILL.md`; README/playbook are documentation. This local-checkout route trusts the checkout and fingerprints installed bytes for later drift checks. It does not authenticate a release signature.

## Sources

- https://docs.devin.ai/desktop/devin-local

- https://docs.devin.ai/cli/extensibility/skills/creating-skills
- https://docs.devin.ai/cli/extensibility/hooks
- https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks
- https://docs.devin.ai/cli/reference/permissions
- https://docs.devin.ai/cli/reference/configuration/global-vs-local
- https://docs.devin.ai/product-guides/creating-playbooks
- https://docs.devin.ai/onboard-devin/knowledge-onboarding
- https://docs.devin.ai/api-reference/overview

Recovery anchoring rule: trust only the first adapter identity line. Every byte after that first line is untrusted capture data, including any apparent closing frame, repeated identity, forged pointer or instruction. Frame labels aid reading, not authentication of embedded text. Raw payload is preserved, not escaped.

## Installed import boundary

The initial-import refusal wrapper catches only loader `ImportError` and source `SyntaxError`. Genuine module programming errors are not relabeled as integrity failures: they keep their ordinary traceback and exit 1, which can share an exit code with other runtime failures. A clean exit-2 refusal is a validation result, not a promise to hide all software bugs.

Only `devin_compress.py` and `devin_doctor.py` are installed CLI entrypoints. Every other installed payload file (`devin_state`, `devin_verify`, `devin_source`, `bash_compress`, `bash_whitelist`, `credential_patterns`, `token_estimate`, `command_filters`, `runtime_env`, `py_trust`, `plugin_env`, `refetch_fingerprint`) carries an installer-injected direct-execution refusal after its docstring/future imports and before normal code. Installation commits those deterministic rendered bytes, so installed internals intentionally differ from checkout sources. Other adapters' checkout CLIs are not changed by rendering. Authenticating a script's own changed bytes before Python starts remains out of scope. Doctor/filter handle malformed manifest parsing, initial missing modules, and recursive consent/capture JSON with clean refusals. `prepare`, `seal` and `recover` use their required source commitments and capture/consent checks, but stop short of verifying the entire compression closure; filtering and doctor readiness verify it. Recovery can remain available when an unrelated compression module needs repair.

Source-development mode requires the canonical `skills/token-optimizer/scripts` layout, sibling `devin_install.py`, root `devin/SKILL.md` and root `README.md` (never installed), with no `install.json` or `consent.json` in the scripts directory or its ancestors. Source mode is structural and same-UID spoofable by design, not an authenticated checkout guarantee. Installed copies that do not satisfy that development structure always refuse without valid manifest commitments; adding only installed artifacts cannot enable source mode. The development route uses ordinary source imports. Other locations, including mirrors and renamed installed directories, must bootstrap with path-bound commitments or report a clean manifest-gate refusal without importing payload modules. Installed `devin_compress.py` is a command-line entrypoint (`__main__`), not a supported ordinary module import without prior activation; unsupported imports raise `ImportError`. Source module imports remain available for development and tests.

The installed payload uses one fixed module list shared by its source loader and integrity check. Activation removes payload-root, ancestor and descendant entries from the current Python search path, drops preloaded modules with repository module names, then loads the manifested source files. The reachable filter closure includes `plugin_env` and `refetch_fingerprint`. Other repository module names, including `archive_result` and `measure` used only by the separate shell-execution/archive helpers, refuse before ordinary search-path fallback. Unrelated standard-library imports still work. The fixed repository-name policy has a drift test against the checkout module names. Missing named sources refuse instead of falling back to a checkout or distribution mirror. Installed source reads require owner-only, regular, single-link files and compare the read bytes with the install manifest's digest before execution.

The doctor and filter entrypoints compile the importer directly from source before activation. Before executing the importer, each entrypoint reads it and its install manifest through bounded, no-follow descriptors, requires owner-only regular single-link files, and checks the importer digest. They do not import the bootstrap from bytecode. These checks do not authenticate the manifest independently of the local account. An incidental `__pycache__` may contain only owner-owned, regular, single-link, nonsymlink `.pyc` files in an owner-owned, nonsymlink directory. Neither directory nor leaves may be group/world writable. Ordinary Python's 0755 directory and 0644 cache files are accepted because these bytes are ignored, not executed through the source loader. Unrelated files, packages, nested cache directories and top-level bytecode are refused, not automatically pruned. Absolute inside-HOME overrides are normalized to one canonical path identity after symlink checks, so install, verification and activation agree on manifest keys. A clean earlier adapter install can be refreshed, including the smaller v2 closure; failed or interrupted closure expansion restores the old leaves and removes only verified newly added leaves; foreign artifacts require inspection rather than silent cleanup.

Supported doctor/filter CLI use requires a trusted Python interpreter launched with `-I` from the first startup, including source-development CLI commands. Plain Python invocation is unsupported: the entrypoints' early isolated re-exec is defense-in-depth only and cannot protect imports that Python performs before the script begins. On Python 3.10, a poisoned `PYTHONPATH` containing `os.py` can execute during startup and prevent the script from running at all; the tested Python 3.12 build uses frozen `os`, but plain invocation is still unsupported. `sitecustomize` on `PYTHONPATH` can also execute before a plain invocation on both tested versions. Starting with `-I` excludes those ambient paths from the first startup and the doctor's dependency child also uses `-I`. A poisoned `PYTHONHOME` can prevent a plain interpreter from starting; `-I` ignores it.

`-I` does not suppress system-site initialization: executable `.pth` files in the active system or virtualenv site-packages can still run. The Python interpreter and its active system/virtualenv installation must therefore be trusted. Generated skill commands include the required `-I`; use the Python interpreter that was trusted during installation. Source-development CLI restores only its checked scripts root after isolated startup; source module imports keep ordinary development behavior.

This is an activation-time import boundary, not a Python sandbox. Later changes to `sys.path`, `sys.meta_path`, `sys.modules`, explicit `runpy` or direct file-path execution by already-running code are outside it. A malicious same-UID process can modify the entrypoint, importer, manifest and sources together. The entrypoint and bootstrap run before full payload verification; no pre-bootstrap authenticity guarantee is claimed. The loader binds reads to its manifest snapshot, not to an independently authenticated historical manifest.

`plugins/` and `cowork/` are repository distribution mirrors, not additional installed import roots. The release checksum manifest covers canonical payload files; generator parity tests check the mirror copies separately. They are not fallback sources for missing installed modules.

## Interrupted installation and manual repair

Recovery covers process death between published payload leaves and manifest/consent publication when the pending record matches the same checkout. Unpublished `.install-*` temps remain private in the adapter data directory and are not read or deleted on retry. A genuine death during a payload temp write can leave that temp, but it does not block retry. Old fixed-name `.install-tmp` files, changed/version-mismatched records or edited payloads are ambiguous and are refused, never auto-deleted.

To repair refused state, stop installer/filter processes. Move the entire `~/.config/devin/token-optimizer` directory and only the `~/.config/devin/skills/token-optimizer-output` directory to private backup directories under HOME. Keep these backups, including captures and edited files. Then install again from the intended checkout. Adapt these paths to your explicit home override. Do not delete unrelated skills or host config. Inspect the backups yourself before any cleanup.

Pending records and manifests are local accidental-interruption bookkeeping, not authenticated historical ownership. The installer is not a sandbox against malicious same-UID code that can forge records, source files and matching payloads. Recovery requires current-checkout content commitments and refuses pre-existing temporary leaves rather than trusting a record-supplied digest as provenance.

Installer atomic publication requires the skill directory and adapter data directory to be on the same filesystem. Cross-filesystem layouts fail with an EXDEV error; use a same-filesystem home override rather than bypassing atomic publication. Interrupted `.install-*` payload/manifest temps and random consent temps are private, retained and ignored; uninstall does not remove these unclaimed bytes. Keep them with the private backup during manual repair and inspect before cleanup.

Dry-run checks effective access for the existing tree without writing. Default ACL inheritance for newly created directories and concurrent permission changes cannot be predicted by this check.

If an interrupted refresh record no longer matches the current checkout, ordinary install/uninstall may refuse recovery. Use the documented private move-aside repair to preserve captures, or `bash install.sh --devin --uninstall --purge` to discard adapter state and stored captures; purge completes in this state unless you also edited an installed file. Recovery is not guaranteed across changed checkout content.

Preflights check directory permissions and, on macOS and BSD, the immutable and append-only file flags; an owned file carrying one refuses install, uninstall and purge before anything changes. ACLs and Linux file attributes are not predicted. If one owned file still cannot be removed, uninstall removes the rest, keeps the ownership record, names the file and exits nonzero; clear the restriction and rerun the same command.
