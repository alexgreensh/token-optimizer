# Issue #155 — ensure-health re-adds legacy quality-cache hook (partly undoing #139)

## Root cause (confirmed)
Since a299bf7 (#139, v5.11.93) the shipped `UserPromptSubmit` hook is a single
in-process dispatcher whose command references `hooks/userpromptsubmit_runner.py`
and runs `quality-cache` *inside* the runner. The literal substring
`quality-cache` no longer appears in the hook command.

Three predicates in `skills/token-optimizer/scripts/measure.py` detect the
quality-cache hook purely by the substring `"quality-cache"`:
- `measure.py:41604` — UserPromptSubmit self-heal probe (`ensure-health` path).
- `measure.py:39568` — SessionStart auto-restore probe (`has_cache_hook`).
- `measure.py:34283` / `:34298` — `_is_quality_bar_installed` idempotence check
  (settings.json + plugin-cache hooks.json fallback), used by `setup_quality_bar`.

All three return `False` against the consolidated dispatcher, so ensure-health
concludes "hook missing" and `setup_quality_bar` appends a fresh legacy
`python3 '<mp>' quality-cache --quiet` group at `measure.py:35073`. Every
subsequent prompt then runs quality-cache twice (dispatcher + legacy), and the
duplicate is un-removable because the next SessionStart re-adds it.

## Design Decisions
- Add two module-level helpers next to `_is_quality_bar_installed`:
  - `_command_drives_quality_cache(command)` — True if a hook *command string*
    matches either the legacy shape (`quality-cache`) OR the #139 consolidated
    dispatcher (`userpromptsubmit_runner.py`).
  - `_quality_cache_hook_present(groups)` — True if any UserPromptSubmit *group*
    already drives quality-cache (faithful two-marker generalization of the old
    `any("quality-cache" in str(h) ...)` scan).
- Route all three detection sites through the helpers so the consolidated
  dispatcher is recognized as "quality-cache already installed" and ensure-health
  no longer appends the legacy hook. This respects the #139 state on script AND
  plugin installs (the plugin-cache fallback now recognizes the shipped hook too).
- Marker `userpromptsubmit_runner.py` chosen as the dispatcher signature: it is
  the stable, distinctive filename the consolidated command always execs, present
  in both settings.json (script install) and hooks.json (plugin install).

## Deviations
- Left the `--uninstall` path (`measure.py:35016`) matching only `quality-cache`
  on purpose: uninstall must strip the *legacy standalone* hook, never the
  consolidated dispatcher (which is the legitimate #139 hook, not a quality-bar
  add-on). Removing the dispatcher on `setup-quality-bar --uninstall` would break
  every other UserPromptSubmit subcommand.

## Tradeoffs
- Substring/marker detection is inherently heuristic. `userpromptsubmit_runner.py`
  is a strong, stable signal; a hand-rolled hook that runs the runner under a
  different filename would not be recognized, but that is not a shipped shape.

## Mirrors
- Canonical file is `skills/token-optimizer/scripts/measure.py`. The
  `plugins/token-optimizer/` and `cowork/token-optimizer/` copies are generated
  mirrors enforced byte-identical by `scripts/check-mirror-sync.sh` and
  `tests/test_cowork_committed_plugin.py`. Regenerated after the edit.

## Picked-up pre-existing drift (not part of the #155 logic fix)
- Regenerating the cowork mirror with `cowork_install.py --emit-committed` (the
  only supported regen path, required by `test_cowork_committed_plugin.py`) also
  refreshed `cowork/token-optimizer/README.md`. That README was already stale on
  origin/main: #156 updated the root README (BoostGraph scoping) without
  regenerating the cowork mirror, so `test_rebuild_emit_committed_leaves_git_clean`
  already fails on a clean origin/main checkout. Verified: root README mentions
  "BoostGraph" 1x, committed cowork README 0x, before any of my edits. The refresh
  is committed because the mirror must be byte-reproducible from the generator; it
  is unrelated to the hook logic.

## Open Questions
- None blocking. The fix is behavior-preserving for already-correct installs
  (no-op) and stops the duplicate on consolidated-dispatcher installs.
