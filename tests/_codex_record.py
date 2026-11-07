"""Test helper: stand in for the Codex install that wrote some hook entries.

Ownership is the whole entry, (event, matcher, type, command), so a test
records the exact entries of a hooks.json object that carry the given
commands, in the ledger the installer module itself reads.
"""
from __future__ import annotations

import json
from pathlib import Path


def record_codex(installer, hooks_path, data, *commands):
    """Record every entry of ``data`` (a hooks.json object; None reads
    ``hooks_path``) whose command is one of ``commands``."""
    import codex_hook_provenance as chp
    import hook_provenance as hp
    if data is None:
        data = json.loads(Path(hooks_path).read_text(encoding="utf-8"))
    contexts = [c for event, groups in data["hooks"].items() for g in groups
                for c in chp.contexts_in(event, g) if c is not None and c[3] in commands]
    assert {c[3] for c in contexts} == set(commands), "every command must be placed in data"
    ledger = installer._codex_proof(Path(hooks_path)).ledger_path
    assert hp.record_contexts(ledger, hooks_path, contexts, "test")
