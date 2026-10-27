"""Shared helper: normalize the Codex marketplace mirror's runtime version-resolver
back to the root's simple launcher guard.

The sync script (sync-codex-marketplace-plugin.sh) applies three intentional
transforms to the Codex mirror's hooks.json: async-strip, SessionEnd-timeout
clamp, and — for launcher commands — a rewrite of the fail-open guard into a
runtime version-resolver (Codex pins ${CLAUDE_PLUGIN_ROOT} to the session's
plugin version, so scanning for the newest dir heals a mid-session refresh).

Tests that assert "the mirror equals the root except for the sanctioned
transforms" use `guard_from_resolver` to undo the resolver rewrite before
comparing, so they still catch any *other* drift.

Since 5.13.51 the root hooks.json ships the same resolver (a Claude Code
session's root can be deleted under it too), so callers normalize BOTH sides.

Keep _RESOLVER / _GUARD byte-identical to the sync script.
"""

_RESOLVER = ('D="${CLAUDE_PLUGIN_ROOT}"; [ -r "$D/hooks/python-launcher.sh" ] || '
             'D="$(ls -d "${D%[/\\\\]*}"/*/ 2>/dev/null | grep -E "/[0-9]+[.][0-9]+[.][0-9]+/$" | sort -V | tail -n1)"; '
             'D="${D%/}"; [ -r "$D/hooks/python-launcher.sh" ] || exit 0; '
             'exec "$b" "$D/hooks/python-launcher.sh" "$D/hooks/run.py"')
_GUARD = ('L="${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh"; [ -r "$L" ] || exit 0; '
          'exec "$b" "$L" "${CLAUDE_PLUGIN_ROOT}/hooks/run.py"')


def guard_from_resolver(command):
    """Return `command` with the Codex resolver rewritten back to the simple guard."""
    if isinstance(command, str):
        return command.replace(_RESOLVER, _GUARD)
    return command
